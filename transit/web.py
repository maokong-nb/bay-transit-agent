"""从仓库根目录运行 python -m transit.web，在 127.0.0.1:8001 查看演示。"""
from asyncio import Lock
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from weakref import WeakValueDictionary

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from transit.chat import TransitRuleLLM, make_transit_agent
from transit.planner import PlanRequest, Timetable


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


@app.get("/", response_class=HTMLResponse)
async def home():
    return Path(__file__).with_name("demo.html").read_text(encoding="utf-8")


@app.get("/health")
async def health(request: Request):
    agent = request.app.state.agent
    mode = "rule-demo" if isinstance(agent.llm, TransitRuleLLM) else "llm"
    return {"mode": mode, "tools": [t["function"]["name"] for t in agent.tools]}


@app.post("/plan")
async def plan(query: PlanRequest, request: Request):
    return request.app.state.timetable.plan(query)


@app.post("/chat")
async def chat(query: ChatRequest, request: Request):
    session_id = query.session_id or uuid4().hex
    lock = request.app.state.session_locks.setdefault(session_id, Lock())
    async with lock:
        result = await request.app.state.agent.chat(query.message, "transit:" + session_id)
    return {"session_id": session_id, **result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
