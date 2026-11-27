"""业务验收使用公开合成班次，HTTP/对话验收使用真实的本地 MCP 子进程。"""
import csv
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import host.logging_utils as tool_logs
import host.memory as memory
from transit.planner import DEFAULT_DATA, PlanRequest, Timetable
from transit.web import app


class TimetableAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = Timetable(DEFAULT_DATA)

    def query(self, **changes):
        spec = {"origin": "广州南", "destination": "香港西九龙", "depart_after": "08:00"}
        return self.board.plan(PlanRequest(**{**spec, **changes}))

    def test_budget_100_returns_earliest_feasible(self):
        route = self.query(max_fare=100)["routes"][0]
        self.assertEqual([s["id"] for s in route["legs"]], ["R12", "R23"])
        self.assertEqual((route["fare_cny"], route["arrive"]), (97, "10:01"))
        self.assertEqual(route["connections"], [{"station": "深圳北", "wait_min": 18, "required_min": 12}])

    def test_lower_budget_changes_transfer_station(self):
        route = self.query(max_fare=80)["routes"][0]
        self.assertEqual([s["id"] for s in route["legs"]], ["B11", "B13"])
        self.assertEqual((route["fare_cny"], route["arrive"]), (68, "10:50"))

    def test_equal_budget_and_arrival_deadline_are_allowed(self):
        self.assertEqual(self.query(max_fare=97, arrive_by="10:01")["status"], "ok")
        self.assertEqual(self.query(max_fare=97, arrive_by="10:00")["status"], "no_route")

    def test_preferences_rank_feasible_routes(self):
        early = self.query()["routes"][0]
        cheapest = self.query(preference="cheapest")["routes"][0]
        self.assertEqual((early["arrive"], cheapest["fare_cny"]), ("09:03", 68))
        self.assertLessEqual(self.query(max_fare=80, preference="fewest_transfers")["routes"][0]["fare_cny"], 80)

    def test_transfer_minima_remove_impossible_connections(self):
        paths = self.board.itineraries("GZS", "HKW", 0)
        ids = {tuple(leg.id for leg in p) for p in paths}
        self.assertNotIn(("R11", "R21"), ids)
        self.assertNotIn(("B11", "B12"), ids)
        self.assertIn(("R11", "L01", "L02"), ids)

    def test_direct_only_filters_two_leg_routes(self):
        self.assertTrue(all(r["transfers"] == 0 for r in self.query(max_transfers=0)["routes"]))
        self.assertEqual(self.query(max_transfers=0, max_fare=100)["status"], "no_route")

    def test_conflicts_expose_verifiable_deltas(self):
        result = self.query(max_fare=80, arrive_by="10:00")
        conflicts = {tuple(c["service_ids"]): c["over"] for c in result["conflicts"]}
        self.assertEqual((result["status"], result["routes"]), ("no_route", []))
        self.assertEqual(conflicts[("B11", "B13")], {"late_min": 50})
        self.assertEqual(conflicts[("R11", "L01", "L02")], {"extra_cny": 43})

    def test_departure_after_last_service(self):
        self.assertEqual(self.query(depart_after="23:00")["conflicts"], [])

    def test_ambiguous_city_lists_its_stations(self):
        r = self.query(origin="深圳")
        self.assertEqual((r["status"], r["field"]), ("clarify", "origin"))
        self.assertEqual(set(r["options"]), {"福田", "深圳北", "深圳湾口岸"})

    def test_station_codes_and_suffix(self):
        self.assertEqual(self.query(origin="gzs", destination="香港西九龙站"), self.query())

    def test_unknown_station_and_reversed_time_are_handled(self):
        self.assertEqual(self.query(destination="上海")["field"], "destination")
        self.assertEqual(self.query(arrive_by="07:00")["status"], "clarify")

    def test_custom_data_affects_connection_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            shutil.copy(DEFAULT_DATA / "services.csv", folder)
            text = (DEFAULT_DATA / "stations.csv").read_text(encoding="utf-8")
            for wait, expected in [(18, "ok"), (19, "no_route")]:
                (folder / "stations.csv").write_text(text.replace("深圳北,深圳,12", f"深圳北,深圳,{wait}"), encoding="utf-8")
                with patch.dict(os.environ, {"TRANSIT_DATA_DIR": temp}):
                    q = PlanRequest(origin="GZS", destination="HKW", max_fare=97, arrive_by="10:01")
                    self.assertEqual(Timetable.configured().plan(q)["status"], expected)

    def test_import_rejects_unknown_station(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            shutil.copytree(DEFAULT_DATA, folder, dirs_exist_ok=True)
            with (folder / "services.csv").open("a", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(["X1", "GZS", "UNKNOWN", "11:00", "11:30", 10, "测试"])
            with self.assertRaisesRegex(ValueError, "X1.*未知站点"):
                Timetable(folder)


class HttpAndMcpAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {"MOCK_LLM": "1", "TRANSIT_DATA_DIR": str(DEFAULT_DATA)}))
        stack.enter_context(patch.object(memory, "DB_PATH", temp / "memory.db"))
        stack.enter_context(patch.object(tool_logs, "LOG_PATH", temp / "calls.jsonl"))
        cls.client = stack.enter_context(TestClient(app))
        # 先关闭 SQLite 连接，再清理解压目录（Windows 文件占用会阻止 TemporaryDirectory 删除）
        stack.callback(memory.close_all)

    def chat(self, text, session=None):
        res = self.client.post("/chat", json={"message": text, "session_id": session})
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_home_and_tool_discovery(self):
        self.assertIn("湾区联程规划", self.client.get("/").text)
        data = self.client.get("/health").json()
        self.assertEqual(data["mode"], "rule-demo")
        self.assertEqual(set(data["tools"]), {"list_transit_stations", "plan_transit"})

    def test_structured_api_rejects_invalid_input(self):
        for payload in ({"depart_after": "25:10"}, {"max_fare": -1},
                        {"max_transfers": 3}, {"nonexistent": True}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/plan", json=payload).status_code, 422)

    def test_structured_api_and_text_agree(self):
        route = self.client.post("/plan", json={"origin": "GZS", "destination": "HKW",
                                                "max_fare": 100}).json()["routes"][0]
        result = self.chat("广州南到香港西九龙，最多100元，最早到达")
        self.assertEqual([t["tool"] for t in result["tool_calls"]], ["plan_transit"])
        self.assertEqual(result["tool_calls"][0]["status"], "ok")
        self.assertIn(f"{route['fare_cny']} 元", result["reply"])
        self.assertIn(route["arrive"], result["reply"])

    def test_followup_keeps_route_and_reports_actual_conflicts(self):
        first = self.chat("广州南到香港西九龙，08:00出发，预算100元，最早到达")
        self.assertIn("R12", first["reply"])
        cheaper = self.chat("预算改为80元", first["session_id"])
        self.assertIn("68 元", cheaper["reply"])
        self.assertIn("B13", cheaper["reply"])
        conflict = self.chat("10:00前到", first["session_id"])
        self.assertIn("晚到 50 分钟", conflict["reply"])
        self.assertIn("超预算 43 元", conflict["reply"])

    def test_new_session_does_not_inherit_route(self):
        self.chat("广州南到香港西九龙，预算100元")
        fresh = self.chat("预算改为80元")
        self.assertIn("明确出发站", fresh["reply"])
        self.assertNotIn("B13", fresh["reply"])

    def test_station_clarification_in_same_session(self):
        result = self.chat("深圳到香港西九龙，最早到达")
        self.assertIn("深圳北", result["reply"])
        resolved = self.chat("深圳北", result["session_id"])
        self.assertIn("R21", resolved["reply"])
        self.assertEqual(resolved["tool_calls"][0]["args"]["request"]["origin"], "深圳北")

    def test_soft_preference_leaves_budget_unset(self):
        resp = self.chat("广州南到香港西九龙，便宜一点")
        self.assertNotIn("max_fare", resp["tool_calls"][0]["args"]["request"])
        self.assertIn("68 元", resp["reply"])

    def test_unsupported_rule_input_does_not_invent_an_itinerary(self):
        r = self.chat("请帮我直接订票")
        self.assertEqual(r["tool_calls"], [])
        self.assertIn("示例", r["reply"])


if __name__ == "__main__":
    unittest.main()
