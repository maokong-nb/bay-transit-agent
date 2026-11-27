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
PROMPT = """你是一个湾区模拟班次查询助手。当前日期 {today}。所有班次与票价均为演示数据。
理解中文出行需求并用 plan_transit 工具检查。当用户只改变预算、时间或偏好时，沿用本会话其余条件。
预算、最迟到达和最多换乘是硬约束。不要把“尽量便宜”解释成预算。不要擅自改变限制。
同城多站时转述工具的澄清选项。遇到范围外、跨日、退改签或订票需求，告知此原型只支持同日模拟选路。
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
HELP = "演示输入示例：广州南到香港西九龙，08:00出发，预算100元，最早到达。追问可写“预算改为80元”。"


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
                message += " " + " / ".join(example["service_ids"]) + "，" + "、".join(differences) + "。"
        return message
    route = result["routes"][0]
    legs = route["legs"]
    details = [f"{leg['id']} {leg['from']} {leg['depart']} → {leg['to']} {leg['arrive']}"
               for leg in legs]
    return (f"模拟班次：{'；'.join(details)}。合计 {route['fare_cny']} 元，"
            f"换乘 {route['transfers']} 次，{route['arrive']} 到达。")


class TransitRuleLLM(MockLLM):
    """规则解析只负责免 Key 演示。工具返回前不生成路线。"""
    def chat(self, messages, tools):
        if messages[-1]["role"] == "tool":
            content = messages[-1]["content"]
            try:
                result = json.loads(content)
            except (TypeError, ValueError):
                return LLMResponse("工具未能生成路线：" + content[:200])
            return LLMResponse(describe_result(result))
        slots = {}
        last_reply = ""
        for i, message in enumerate(messages):
            if message["role"] == "assistant":
                last_reply = message.get("content", "")
            elif message["role"] == "user":
                text = message["content"].strip()
                clarification = re.fullmatch(r"请明确(出发站|目的站)，可选：(.+)", last_reply)
                if clarification and text in clarification[2].split("、"):
                    field = "origin" if clarification[1] == "出发站" else "destination"
                    slots[field] = text
                else:
                    try:
                        slots = apply_turn(slots, text)
                    except ValueError as error:
                        if i == len(messages) - 1:
                            return LLMResponse(str(error))
                last_reply = ""
        self._n += 1
        return LLMResponse("", [{"id": f"transit-{self._n}", "name": "plan_transit",
                                 "arguments": {"request": slots}}])


def make_transit_agent():
    llm = make_llm()
    if isinstance(llm, MockLLM):
        llm = TransitRuleLLM()
    folder = os.getenv("TRANSIT_DATA_DIR")
    env = {"TRANSIT_DATA_DIR": str(Path(folder).resolve())} if folder else None
    server = StdioServerParameters(command=sys.executable, args=["-m", "transit.server"],
                                   cwd=str(ROOT), env=env)
    return Agent(servers={"transit": server}, system_prompt=PROMPT, llm=llm)
