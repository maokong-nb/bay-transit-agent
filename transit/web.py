"""从仓库根目录运行 python -m transit.web，在 127.0.0.1:8001 查看演示。"""
from asyncio import Lock
from contextlib import asynccontextmanager
import json
from pathlib import Path
import time
from uuid import uuid4
from weakref import WeakValueDictionary

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from transit.chat import TransitRuleLLM, make_transit_agent
from transit.planner import PlanRequest, Timetable
from transit.simulate import SimulateRequest, simulate_route, stress_sweep


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.timetable = Timetable.configured()
    app.state.session_locks = WeakValueDictionary()
    agent = make_transit_agent()
    try:
        await agent.connect()
        app.state.agent = agent
        yield
    finally:
        await agent.close()


app = FastAPI(title="湾区联程规划", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class StressRequest(BaseModel):
    service_ids: list[str] = Field(min_length=1, max_length=3)
    arrive_by: str | None = None
    extra_transfer_buffer_min: dict[str, int] = Field(default_factory=dict)


@app.get("/", response_class=HTMLResponse)
async def home():
    return Path(__file__).with_name("demo.html").read_text(encoding="utf-8")


@app.get("/health")
async def health(request: Request):
    agent = request.app.state.agent
    mode = "rule-demo" if isinstance(agent.llm, TransitRuleLLM) else "llm"
    return {"mode": mode, "tools": [t["function"]["name"] for t in agent.tools]}


@app.get("/stations")
async def stations(request: Request):
    return {"stations": request.app.state.timetable.station_points()}


@app.post("/plan")
async def plan(query: PlanRequest, request: Request):
    t0 = time.perf_counter()
    res = request.app.state.timetable.plan(query)
    if isinstance(res, dict):
        res["solver_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return res


@app.post("/simulate")
async def simulate(query: SimulateRequest, request: Request):
    return simulate_route(request.app.state.timetable, query)


@app.post("/stress-test")
async def stress_test(query: StressRequest, request: Request):
    return stress_sweep(
        request.app.state.timetable,
        query.service_ids,
        arrive_by=query.arrive_by,
        extra_transfer_buffer_min=query.extra_transfer_buffer_min,
    )


@app.get("/eval")
async def evaluate_system(request: Request):
    board = request.app.state.timetable
    cases = [
        ("广深港正常联程 (¥160)", PlanRequest(origin="广州南", destination="香港西九龙", depart_after="08:00", max_fare=160), "ok"),
        ("同城多站自动澄清 (深圳→香港)", PlanRequest(origin="深圳", destination="香港西九龙", depart_after="08:00"), "clarify"),
        ("超严预算冲突解释 (¥30)", PlanRequest(origin="广州南", destination="香港西九龙", depart_after="08:00", max_fare=30), "no_route"),
        ("跨境珠澳联程 (¥100)", PlanRequest(origin="广州南", destination="澳门", depart_after="08:00", max_fare=100), "ok"),
        ("禁用跨境巴士熔断重算", PlanRequest(origin="广州南", destination="香港西九龙", depart_after="08:00", blocked_modes=["跨境巴士"]), "ok"),
    ]
    results, latencies = [], []
    for name, req, expected in cases:
        t0 = time.perf_counter()
        res = board.plan(req)
        latencies.append(round((time.perf_counter() - t0) * 1000, 2))
        passed = res.get("status") == expected
        results.append({"name": name, "expected": expected, "actual": res.get("status"), "passed": passed,
                        "feasible": res.get("feasible_count", 0), "pareto": res.get("pareto_count", 0)})
    passed_count = sum(1 for r in results if r["passed"])
    return {
        "total": len(results),
        "passed": passed_count,
        "pass_rate": round(passed_count / len(results), 4),
        "avg_solver_ms": round(sum(latencies) / max(1, len(latencies)), 2),
        "cases": results,
    }


@app.post("/chat")
async def chat(query: ChatRequest, request: Request):
    t0 = time.perf_counter()
    session_id = query.session_id or uuid4().hex
    lock = request.app.state.session_locks.setdefault(session_id, Lock())
    async with lock:
        result = await request.app.state.agent.chat(query.message, "transit:" + session_id)
    plan_payload = None
    plan_args = None
    for tc in reversed(result.get("tool_calls", [])):
        if tc.get("tool") == "plan_transit" and tc.get("status") == "ok":
            req_dict = (tc.get("args") or {}).get("request")
            if isinstance(req_dict, dict):
                plan_args = req_dict
                try:
                    plan_payload = request.app.state.timetable.plan(PlanRequest(**req_dict))
                except Exception:
                    plan_payload = None
                break
    chat_ms = round((time.perf_counter() - t0) * 1000, 2)
    ans_len = len(str(result.get("answer", "")))
    telemetry = {
        "chat_ms": chat_ms,
        "tool_calls": len(result.get("tool_calls", [])),
        "context_tokens_est": max(120, (len(query.message) + ans_len) // 2 + len(result.get("tool_calls", [])) * 140),
    }
    return {"session_id": session_id, "plan_result": plan_payload, "plan_args": plan_args, "telemetry": telemetry, **result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
