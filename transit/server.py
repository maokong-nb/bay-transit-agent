"""MCP 服务；以 python -m transit.server 从仓库根目录启动。"""
from mcp.server.fastmcp import FastMCP

from transit.planner import PlanRequest, Timetable

mcp = FastMCP("bay-transit")
timetable = Timetable.configured()


@mcp.tool()
def list_transit_stations() -> dict:
    """列出当前演示班次表支持的出发站与目的站。"""
    return {"stations": timetable.names()}


@mcp.tool()
def plan_transit(request: PlanRequest) -> dict:
    """查同日模拟班次。预算为总价上限，arrive_by 是最迟到站时间，换乘上限为 0–2 次。
    返回可行路线、逐段证据或澄清/无解原因。preference 只用于可行路线排序。"""
    return timetable.plan(request)


if __name__ == "__main__":
    mcp.run()
