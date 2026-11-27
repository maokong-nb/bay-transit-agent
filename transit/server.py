"""MCP 服务；以 python -m transit.server 从仓库根目录启动。"""
from mcp.server.fastmcp import FastMCP

from transit.planner import PlanRequest, Timetable
from transit.simulate import SimulateRequest, simulate_route

mcp = FastMCP("bay-transit")
timetable = Timetable.configured()


@mcp.tool()
def list_transit_stations() -> dict:
    """列出当前班次快照支持的出发站与目的站（含城市与坐标）。"""
    return {"stations": timetable.station_points()}


@mcp.tool()
def plan_transit(request: PlanRequest) -> dict:
    """查同日跨境班次。预算为总价上限，arrive_by 是最迟到站时间，换乘上限 0–2 次。
    时间/费用/中转等待可用 weight_* 加权；默认按 preference 排序。
    返回 Pareto 前沿上的差异化方案（默认最多 4 个）、逐段证据或澄清/无解原因。"""
    return timetable.plan(request)


@mcp.tool()
def simulate_transit_route(request: SimulateRequest) -> dict:
    """对 plan_transit 返回的班次 ID 序列做蒙特卡洛接驳可靠性仿真：
    按交通方式注入随机延误，输出接驳成功率、期望/90 分位到达时间与最薄弱环节。"""
    return simulate_route(timetable, request)


if __name__ == "__main__":
    mcp.run()
