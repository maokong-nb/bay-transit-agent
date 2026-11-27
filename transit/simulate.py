"""蒙特卡洛接驳可靠性仿真：给每段班次注入方式相关的随机延误，评估联程路线稳健性。

延误模型：每段班次的实际出发 = 计划出发 + max(0, N(μ, σ²))，μ/σ 按交通方式给定
（高铁准点率高、路面巴士受路况影响大），并乘上全局延误强度 delay_factor。
一次试验中，前一段实际到达 + 站点最小换乘时间 ≤ 后一段实际出发，视为接驳成功；
中途误接即失败（不重签后续班次）。结果为固定随机种子的可复现统计量。
"""
import random
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from transit.planner import Timetable, minutes

CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

# (μ, σ)，单位分钟；量级参考各方式公开准点率口径，可在 DATA 文档中调整
DELAY_PROFILES = {
    "高铁": (1.0, 2.0),
    "城际": (2.0, 4.0),
    "地铁接驳": (1.0, 3.0),
    "口岸步行": (2.0, 5.0),
    "口岸接驳": (3.0, 6.0),
    "口岸巴士": (4.0, 8.0),
    "穿梭巴士": (3.0, 6.0),
    "跨境巴士": (6.0, 10.0),
    "客船": (4.0, 8.0),
}
DEFAULT_PROFILE = (3.0, 6.0)


def clock(total_minutes: float) -> str:
    total = int(round(total_minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_ids: list[str] = Field(min_length=1, max_length=3, description="plan_transit 返回的班次 ID 序列")
    arrive_by: str | None = Field(None, description="可选；给出时额外统计准点到达率")
    trials: int = Field(1000, ge=100, le=20000)
    delay_factor: float = Field(1.0, ge=0.0, le=5.0, description="延误强度倍率：0 为完全准点，2 为延误翻倍")
    seed: int = 42

    @field_validator("arrive_by")
    @classmethod
    def valid_time(cls, value):
        if value is not None and not CLOCK.fullmatch(value):
            raise ValueError(f"无效时间 {value!r}，请用 24 小时制 HH:MM")
        return value


def simulate_route(board: Timetable, q: SimulateRequest) -> dict:
    legs = []
    for sid in q.service_ids:
        leg = board.by_id.get(sid)
        if leg is None:
            return {"status": "error", "message": f"未知班次 {sid}，请先调用 plan_transit 获取班次 ID。"}
        legs.append(leg)
    for prev, nxt in zip(legs, legs[1:]):
        if prev.destination != nxt.origin:
            return {"status": "error", "message": f"{prev.id} 与 {nxt.id} 首尾不接，无法构成一条联程路线。"}

    rng = random.Random(q.seed)
    caught = [0] * (len(legs) - 1)
    success = 0
    on_time = 0
    arrivals = []
    deadline = minutes(q.arrive_by) if q.arrive_by else None

    for _ in range(q.trials):
        delays = [max(0.0, rng.gauss(*(p for p in DELAY_PROFILES.get(leg.mode, DEFAULT_PROFILE))))
                  for leg in legs]
        delays = [d * q.delay_factor for d in delays]
        actual_arrive = [minutes(leg.arrive) + d for leg, d in zip(legs, delays)]
        actual_depart = [minutes(leg.depart) + d for leg, d in zip(legs, delays)]
        ok = True
        for i in range(len(legs) - 1):
            buffer = int(board.stations[legs[i].destination]["min_transfer_min"])
            if actual_arrive[i] + buffer <= actual_depart[i + 1]:
                caught[i] += 1
            else:
                ok = False
                break
        if ok:
            success += 1
            if deadline is None or actual_arrive[-1] <= deadline:
                on_time += 1
        arrivals.append(actual_arrive[-1])

    arrivals.sort()
    result = {
        "status": "ok",
        "service_ids": q.service_ids,
        "trials": q.trials,
        "delay_factor": q.delay_factor,
        "connection_success_rate": round(success / q.trials, 4),
        "expected_arrive": clock(sum(arrivals) / len(arrivals)),
        "p90_arrive": clock(arrivals[int(0.9 * (len(arrivals) - 1))]),
    }
    if deadline is not None:
        result["on_time_rate"] = round(on_time / q.trials, 4)
        result["arrive_by"] = q.arrive_by
    if caught:
        rates = [(caught[i] / q.trials, legs[i].destination, legs[i].id, legs[i + 1].id)
                 for i in range(len(caught))]
        worst = min(rates)
        result["riskiest_connection"] = {
            "station": board.stations[worst[1]]["name"],
            "from": worst[2], "to": worst[3],
            "catch_rate": round(worst[0], 4),
        }
    return result
