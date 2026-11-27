"""联程 Agent 复用宿主的工具发现、调用循环、会话记忆和日志。"""
import json
import os
import re
import sys
from pathlib import Path

from mcp import StdioServerParameters

from host.agent import Agent
from host.llm import LLMResponse, MockLLM, make_llm

ROOT = Path(__file__).resolve().parents[1]
PROMPT = """你是一个湾区跨境班次查询助手。当前日期 {today}。班次与票价来自按官方公布信息整理的非实时快照。
理解中文出行需求并用 plan_transit 工具检查。当用户只改变预算、时间或偏好时，沿用本会话其余条件。
预算、最迟到达和最多换乘是硬约束。不要把“尽量便宜”解释成预算。不要擅自改变限制。
时间、费用、中转等待的侧重可用 weight_time / weight_cost / weight_wait 表达。
用户关心“稳不稳、会不会赶不上”时，用 simulate_transit_route 对候选班次序列做接驳可靠性仿真。
同城多站时转述工具的澄清选项。遇到范围外、跨日、退改签或订票需求，告知此原型只支持同日快照选路。
用工具返回的真实班次 ID、时间和金额复述第一条可行路线。无解时列出冲突，不要把超限路线推荐成可行。"""

# 免 Key 演示使用显式语法。完整对话模型通过相同的 MCP 工具工作。
CLOCK = r"((?:[01]?\d|2[0-3]):[0-5]\d)"
SLOTS = [
    (r"预算(?:改为|改成|不超过|最多)?\s*(\d+)\s*元?", "max_fare", int),
    (r"(?:最多|不超过)\s*(\d+)\s*元", "max_fare", int),
    (r"(?:最多|至多)换乘\s*([0-2])\s*次", "max_transfers", int),
    (CLOCK + r"(?:以后|后)?出发", "depart_after", lambda x: x.zfill(5)),
    (CLOCK + r"(?:前|之前)(?:到达|到)", "arrive_by", lambda x: x.zfill(5)),
]
PREFERENCES = {"最早到达": "earliest", "最便宜": "cheapest", "优先便宜": "cheapest", "便宜一点": "cheapest",
               "最少换乘": "fewest_transfers"}
HELP = "演示输入示例：广州南到香港西九龙，08:00出发，预算160元，最早到达。追问可写“预算改为140元”。"


def apply_turn(previous: dict, text: str) -> dict:
    slots = previous.copy()
    clauses = [p.strip() for p in re.split(r"[，,；;。！？\n]", text) if p.strip()]
    if not clauses:
        raise ValueError(HELP)
    # 新行程从空条件开始；同一句的约束可以写在起终点的前面或后面。
    route_pattern = r"(?:从)?([\u4e00-\u9fffA-Za-z]{1,14}?)(?:到|去)([\u4e00-\u9fffA-Za-z]{1,14})"
    routes = [re.fullmatch(route_pattern, p) for p in clauses if p not in PREFERENCES]
    routes = [r for r in routes if r]
    if len(routes) > 1:
        raise ValueError("请一次给出一组起终点。")
    if routes:
        slots = dict(zip(("origin", "destination"), routes[0].groups()))
    for p in clauses:
        if p in PREFERENCES:
            slots["preference"] = PREFERENCES[p]
        elif p == "直达":
            slots["max_transfers"] = 0
        elif re.fullmatch(route_pattern, p):
            continue
        else:
            for pattern, field, convert in SLOTS:
                match = re.fullmatch(pattern, p)
                if match:
                    slots[field] = convert(match[1])
                    break
            else:
                raise ValueError(HELP)
    return slots


def describe_result(result: dict) -> str:
    if result["status"] != "ok":
        message = result["message"]
        conflicts = result.get("conflicts", [])
        if conflicts:
            labels = {"extra_cny": ("超预算", "元"), "late_min": ("晚到", "分钟"),
                      "extra_transfers": ("超出换乘上限", "次")}
            examples = []
            for field in labels:
                single = [c for c in conflicts if set(c["over"]) == {field}]
                if single:
                    examples.append(min(single, key=lambda c: c["over"][field]))
            for example in examples or [min(conflicts, key=lambda c: len(c["over"]))]:
                differences = [f"{labels[k][0]} {n} {labels[k][1]}" for k, n in example["over"].items()]
                message += " " + " / ".join(example["service_ids"]) + "，" + "。".join(differences) + "。".join([]) if False else " "
        return message
    routes = result["routes"]
    route = routes[0]
    legs = route["legs"]
    details = [f"{leg['id']} {leg['from']} {leg['depart']} → {leg['to']} {leg['arrive']}"
               for leg in legs]
    tag = f"（{'、'.join(route['labels'])}）" if route.get("labels") else ""
    head = f"共 {result['feasible_count']} 个可行方案，返回 {len(routes)} 个差异化方案。推荐{tag}："
    others = "；其余：" + "。".join(
        f"{'+'.join(l['id'] for l in r['legs'])} {r['fare_cny']}元 {r['arrive']}到"
        for r in routes[1:]) if len(routes) > 1 else ""
    return (head + f"{'；'.join(details)}。合计 {route['fare_cny']} 元，"
            f"换乘 {route['transfers']} 次，中转等待 {route['wait_total_min']} 分钟，{route['arrive']} 到达" + others + "。")
