"""Agent 主循环：连接多个 MCP 服务 → 注册工具 → LLM 多步工具调用 → 记忆与日志。"""
import json
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from host.llm import make_llm
from host.logging_utils import log_tool_call
from host.memory import SessionMemory

ROOT = Path(__file__).resolve().parent.parent

try:  # 可选：从 .env 读取 LLM_API_KEY 等配置
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

SERVERS = {
    "transit": ROOT / "transit" / "server.py",
}

SYSTEM_PROMPT = """你是一个可以使用工具的智能助手，当前日期 {today}。

行为要求：
1. 需要实时信息或执行操作时，必须调用工具，不要凭记忆编造。
2. 用户一句话包含多个需求时，按依赖顺序依次调用工具，后面的工具可以利用前面工具的结果。
3. 工具返回"错误"或"未找到"时，如实转告用户并给出修正建议，不要假装成功。
4. 用简洁的中文回答，关键信息要保留。
"""

MAX_TOOL_ROUNDS = 8


class Agent:
    def __init__(self, *, servers=None, system_prompt=SYSTEM_PROMPT, llm=None):
        self.llm = make_llm() if llm is None else llm
        self.system_prompt = system_prompt
        self.servers = servers if servers is not None else {
            name: StdioServerParameters(command=sys.executable, args=[str(path)])
            for name, path in SERVERS.items()
        }
        self.sessions = {}    # server 名 -> ClientSession
        self.tools = []       # OpenAI function-calling 格式
        self._tool_map = {}   # 工具名 -> server 名
        self._stack = None

    async def connect(self):
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for name, params in self.servers.items():
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            for t in listed.tools:
                self._tool_map[t.name] = name
                self.tools.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema,
                    },
                })
            self.sessions[name] = session

    async def chat(self, user_text: str, session_id: str = "default") -> dict:
        memory = SessionMemory(session_id)
        try:
            memory.add({"role": "user", "content": user_text})
            system = self.system_prompt.format(today=datetime.now().strftime("%Y-%m-%d"))
            messages = [{"role": "system", "content": system}] + memory.history()

            calls_made, prompt_tokens, completion_tokens = [], 0, 0
            for _ in range(MAX_TOOL_ROUNDS):
                resp = self.llm.chat(messages, self.tools)
                prompt_tokens += resp.prompt_tokens
                completion_tokens += resp.completion_tokens

                if not resp.tool_calls:
                    memory.add({"role": "assistant", "content": resp.content})
                    return {
                        "reply": resp.content,
                        "tool_calls": calls_made,
                        "usage": {"prompt": prompt_tokens, "completion": completion_tokens},
                    }

                messages.append({
                    "role": "assistant",
                    "content": resp.content,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"],
                                      "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                        for tc in resp.tool_calls
                    ],
                })
                for tc in resp.tool_calls:
                    server = self._tool_map.get(tc["name"])
                    t0 = time.time()
                    try:
                        if server is None:
                            raise ValueError(f"未注册的工具：{tc['name']}")
                        result = await self.sessions[server].call_tool(tc["name"], tc["arguments"])
                        text = "".join(b.text for b in result.content if hasattr(b, "text"))
                        status = "error" if result.isError else "ok"
                    except Exception as e:  # 工具异常不中断对话，反馈给 LLM 让它修正或转告
                        text, status = f"工具调用失败：{e}", "error"
                    log_tool_call(session_id, tc["name"], tc["arguments"], text,
                                  (time.time() - t0) * 1000, status)
                    calls_made.append({"tool": tc["name"], "args": tc["arguments"], "status": status})
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "name": tc["name"], "content": text})

            return {"reply": "（达到最大工具调用轮数，请把问题拆小一点再试）",
                    "tool_calls": calls_made,
                    "usage": {"prompt": prompt_tokens, "completion": completion_tokens}}
        finally:
            # 显式释放 SQLite 连接：协程帧可能活在引用环里，等 GC 回收会长时间占用文件
            memory.close()

    async def close(self):
        if self._stack:
            await self._stack.aclose()
