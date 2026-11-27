"""同日跨境班次上的有界路径枚举与多目标选路。

价格为整数人民币，时间为本地 HH:MM。多目标语义对应大创“研究二”建模：
偏好进入权重 w（Time/Cost/TransferWait），预算与最迟到达是硬约束，
输出为 Pareto 前沿上的差异化方案（默认最多 4 个，对齐可研验收口径）。
工程上不引入需训练的 GNN/PPO：OD 对上枚举有界，Pareto+加权标量化
在毫秒级给出同样语义且可解释、可复算的结果。
"""
import csv
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
DEFAULT_DATA = Path(__file__).with_name("fixtures")

PREFERENCE_WEIGHTS = {  # (w_time, w_cost, w_wait)，与显式权重同一套打分口径
    "earliest": (1.0, 0.0, 0.0),
    "cheapest": (0.0, 1.0, 0.0),
    "fewest_transfers": (0.5, 0.0, 0.0),  # 换乘次数另作排序主键
}


def minutes(clock: str) -> int:
    if not CLOCK.fullmatch(clock):
        raise ValueError(f"无效时间 {clock!r}，请用 24 小时制 HH:MM")
    h, m = map(int, clock.split(":"))
    return h * 60 + m


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    origin: str = ""
    destination: str = ""
    depart_after: str = "00:00"
    arrive_by: str = "23:59"
    max_fare: int | None = Field(None, ge=0, description="总预算上限，整数人民币；空值表示不限")
    max_transfers: int = Field(2, ge=0, le=2)
    preference: Literal["earliest", "cheapest", "fewest_transfers"] = "earliest"
    weight_time: float | None = Field(None, ge=0, description="总时长权重；给出任一权重即进入加权模式")
    weight_cost: float | None = Field(None, ge=0, description="总费用权重")
    weight_wait: float | None = Field(None, ge=0, description="中转等待权重")
    max_routes: int = Field(4, ge=1, le=6, description="最多返回的差异化方案数")

    @field_validator("depart_after", "arrive_by")
    @classmethod
    def valid_time(cls, value: str) -> str:
        minutes(value)
        return value

    def weights(self):
        """显式权重优先；否则用 preference 预设。返回 (w_time, w_cost, w_wait)。"""
        if any(w is not None for w in (self.weight_time, self.weight_cost, self.weight_wait)):
            return (self.weight_time or 0.0, self.weight_cost or 0.0, self.weight_wait or 0.0)
        return PREFERENCE_WEIGHTS[self.preference]


@dataclass(frozen=True)
class Leg:
    id: str
    origin: str
    destination: str
    depart: str
    arrive: str
    fare: int
    mode: str


def rows(path: Path, required: set[str]):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{path.name} 缺列：{sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            if any(not (row[key] or "").strip() for key in required):
                raise ValueError(f"{path.name}:{reader.line_num} 有空字段")
            yield {key: row[key].strip() for key in required}


class Timetable:
    def __init__(self, directory: Path):
        self.stations = {}
        self.from_station = defaultdict(list)
        self.by_id = {}
        for row in rows(directory / "stations.csv",
                        {"id", "name", "city", "min_transfer_min", "lat", "lng"}):
            key = row["id"]
            if key in self.stations or int(row["min_transfer_min"]) < 0:
                raise ValueError(f"站点重复或换乘时长无效：{key}")
            self.stations[key] = row
        for row in rows(directory / "services.csv",
                        {"id", "origin", "destination", "depart", "arrive", "fare_cny", "mode"}):
            leg = Leg(row["id"], row["origin"], row["destination"], row["depart"],
                      row["arrive"], int(row["fare_cny"]), row["mode"])
            if leg.origin not in self.stations or leg.destination not in self.stations:
                raise ValueError(f"{leg.id} 引用了未知站点")
            if leg.id in self.by_id or leg.fare < 0 or minutes(leg.depart) >= minutes(leg.arrive):
                raise ValueError(f"班次重复、票价无效或非同日正向行程：{leg.id}")
            self.by_id[leg.id] = leg
            self.from_station[leg.origin].append(leg)
        if not self.stations or not self.by_id:
            raise ValueError("演示需要至少一个站点和一条班次")

    @classmethod
    def configured(cls):
        custom = os.getenv("TRANSIT_DATA_DIR")
        return cls(Path(custom) if custom else DEFAULT_DATA)

    def names(self):
        return [s["name"] for s in self.stations.values()]

    def station_points(self):
        return [{"id": sid, "name": s["name"], "city": s["city"],
                 "lat": float(s["lat"]), "lng": float(s["lng"])}
                for sid, s in self.stations.items()]

    def resolve(self, value: str):
        exact = [sid for sid, s in self.stations.items()
                 if value.upper() == sid or value.removesuffix("站") == s["name"]]
        if exact:
            return exact
        return [sid for sid, s in self.stations.items() if value == s["city"]]

    def itineraries(self, origin: str, destination: str, earliest: int):
        def walk(station, ready, path, visited):
            for leg in self.from_station[station]:
                if minutes(leg.depart) < ready or leg.destination in visited:
                    continue
                extended = (*path, leg)
                if leg.destination == destination:
                    yield extended
                elif len(extended) < 3:
                    wait = int(self.stations[leg.destination]["min_transfer_min"])
                    yield from walk(leg.destination, minutes(leg.arrive) + wait, extended,
                                    visited | {leg.destination})
        yield from walk(origin, earliest, (), {origin})

    def describe(self, path):
        legs, waits = [], []
        for i, leg in enumerate(path):
            start = self.stations[leg.origin]
            legs.append({"id": leg.id, "from": start["name"], "to": self.stations[leg.destination]["name"],
                         "depart": leg.depart, "arrive": leg.arrive, "fare_cny": leg.fare, "mode": leg.mode})
            if i:
                waits.append({"station": start["name"], "wait_min": minutes(leg.depart) - minutes(path[i-1].arrive),
                              "required_min": int(start["min_transfer_min"])})
        return {"depart": path[0].depart, "arrive": path[-1].arrive,
                "total_time_min": minutes(path[-1].arrive) - minutes(path[0].depart),
                "wait_total_min": sum(w["wait_min"] for w in waits),
                "fare_cny": sum(l.fare for l in path),
                "transfers": len(path)-1, "legs": legs, "connections": waits}

    @staticmethod
    def pareto_front(routes):
        """四维最小化（到达时刻、费用、换乘、中转等待）的 Pareto 前沿。"""
        keys = [(minutes(r["arrive"]), r["fare_cny"], r["transfers"], r["wait_total_min"]) for r in routes]
        front = []
        for i, a in enumerate(keys):
            dominated = any(i != j and all(x <= y for x, y in zip(b, a)) and b != a
                            for j, b in enumerate(keys))
            if not dominated:
                front.append(routes[i])
        return front

    def plan(self, q: PlanRequest):
        station_ids = []
        for field, value in (("origin", q.origin), ("destination", q.destination)):
            hits = self.resolve(value)
            if len(hits) != 1:
                options = [self.stations[s]["name"] for s in hits] if hits else self.names()
                name = "出发站" if field == "origin" else "目的站"
                return {"status": "clarify", "field": field, "options": options,
                        "message": f"请明确{name}，可选：" + "、".join(options)}
            station_ids.append(hits[0])
        origin, destination = station_ids
        if origin == destination or q.depart_after > q.arrive_by:
            return {"status": "clarify", "message": "请确认起终点不同，且到达期限不早于同日出发时间。"}
        feasible, rejected = [], []
        for path in self.itineraries(origin, destination, minutes(q.depart_after)):
            route = self.describe(path)
            delta = {}
            if q.max_fare is not None and route["fare_cny"] > q.max_fare:
                delta["extra_cny"] = route["fare_cny"] - q.max_fare
            if route["arrive"] > q.arrive_by:
                delta["late_min"] = minutes(route["arrive"]) - minutes(q.arrive_by)
            if route["transfers"] > q.max_transfers:
                delta["extra_transfers"] = route["transfers"] - q.max_transfers
            if delta:
                rejected.append({"service_ids": [l.id for l in path], "over": delta})
            else:
                feasible.append(route)
        if not feasible:
            return {"status": "no_route", "routes": [],
                    "message": "符合接驳条件的班次均超过所给限制。" if rejected else "在该出发时段及班次快照中无可达路径。",
                    "conflicts": rejected}
        w_time, w_cost, w_wait = q.weights()
        explicit = any(w is not None for w in (q.weight_time, q.weight_cost, q.weight_wait))

        def score(route):
            weighted = (w_time * route["total_time_min"] + w_cost * route["fare_cny"]
                        + w_wait * route["wait_total_min"])
            if explicit:
                return (weighted, minutes(route["arrive"]), route["fare_cny"],
                        tuple(l["id"] for l in route["legs"]))
            if q.preference == "cheapest":
                primary = (route["fare_cny"], minutes(route["arrive"]), route["transfers"])
            elif q.preference == "fewest_transfers":
                primary = (route["transfers"], minutes(route["arrive"]), route["fare_cny"])
            else:
                primary = (minutes(route["arrive"]), route["fare_cny"], route["transfers"])
            return (primary, weighted, tuple(l["id"] for l in route["legs"]))

        # 先 Pareto 前沿、再按目标分补齐，保证返回集合差异化且不漏非支配解
        front = self.pareto_front(feasible)
        ordered = sorted(front, key=score)
        if len(ordered) < q.max_routes:
            rest = sorted((r for r in feasible if r not in front), key=score)
            ordered.extend(rest[:q.max_routes - len(ordered)])
        selected = ordered[:q.max_routes]

        best = {"最快": min(minutes(r["arrive"]) for r in selected),
                "最便宜": min(r["fare_cny"] for r in selected),
                "最少换乘": min(r["transfers"] for r in selected),
                "最少等待": min(r["wait_total_min"] for r in selected)}
        for route in selected:
            route["labels"] = [name for name, limit in best.items()
                               if {"最快": minutes(route["arrive"]), "最便宜": route["fare_cny"],
                                   "最少换乘": route["transfers"],
                                   "最少等待": route["wait_total_min"]}[name] == limit]
            route["pareto"] = route in front
            route["score"] = round(w_time * route["total_time_min"] + w_cost * route["fare_cny"]
                                   + w_wait * route["wait_total_min"], 2)
        return {"status": "ok", "feasible_count": len(feasible), "pareto_count": len(front),
                "objective": {"weight_time": w_time, "weight_cost": w_cost, "weight_wait": w_wait,
                              "source": "explicit" if explicit else q.preference},
                "routes": selected}
