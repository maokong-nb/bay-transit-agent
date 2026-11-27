"""同一天合成班次上的有界路径枚举。价格为整数人民币，时间为本地 HH:MM。"""
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

    @field_validator("depart_after", "arrive_by")
    @classmethod
    def valid_time(cls, value: str) -> str:
        minutes(value)
        return value


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
        for row in rows(directory / "stations.csv", {"id", "name", "city", "min_transfer_min"}):
            key = row["id"]
            if key in self.stations or int(row["min_transfer_min"]) < 0:
                raise ValueError(f"站点重复或换乘时长无效：{key}")
            self.stations[key] = row
        service_ids = set()
        for row in rows(directory / "services.csv",
                        {"id", "origin", "destination", "depart", "arrive", "fare_cny", "mode"}):
            leg = Leg(row["id"], row["origin"], row["destination"], row["depart"],
                      row["arrive"], int(row["fare_cny"]), row["mode"])
            if leg.origin not in self.stations or leg.destination not in self.stations:
                raise ValueError(f"{leg.id} 引用了未知站点")
            if leg.id in service_ids or leg.fare < 0 or minutes(leg.depart) >= minutes(leg.arrive):
                raise ValueError(f"班次重复、票价无效或非同日正向行程：{leg.id}")
            service_ids.add(leg.id)
            self.from_station[leg.origin].append(leg)
        if not self.stations or not service_ids:
            raise ValueError("演示需要至少一个站点和一条班次")

    @classmethod
    def configured(cls):
        custom = os.getenv("TRANSIT_DATA_DIR")
        return cls(Path(custom) if custom else DEFAULT_DATA)

    def names(self):
        return [s["name"] for s in self.stations.values()]

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
        return {"depart": path[0].depart, "arrive": path[-1].arrive, "fare_cny": sum(l.fare for l in path),
                "transfers": len(path)-1, "legs": legs, "connections": waits}

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
                    "message": "符合接驳条件的班次均超过所给限制。" if rejected else "在该出发时段及样例班次中无可达路径。",
                    "conflicts": rejected}
        fields = {"earliest": ("arrive", "fare_cny", "transfers"),
                  "cheapest": ("fare_cny", "arrive", "transfers"),
                  "fewest_transfers": ("transfers", "arrive", "fare_cny")}[q.preference]
        feasible.sort(key=lambda r: (tuple(r[f] for f in fields), tuple(l["id"] for l in r["legs"])))
        return {"status": "ok", "feasible_count": len(feasible), "routes": feasible[:3]}
