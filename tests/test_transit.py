"""业务验收使用公开班次快照，HTTP/对话验收使用真实的本地 MCP 子进程。"""
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
from transit.simulate import SimulateRequest, simulate_route
from transit.web import app


class TimetableAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = Timetable(DEFAULT_DATA)

    def query(self, **changes):
        spec = {"origin": "广州南", "destination": "香港西九龙", "depart_after": "08:00"}
        return self.board.plan(PlanRequest(**{**spec, **changes}))

    def test_budget_160_returns_cheapest_via_shenzhen_north(self):
        route = self.query(max_fare=160)["routes"][0]
        self.assertEqual([s["id"] for s in route["legs"]], ["G6201", "G5803"])
        self.assertEqual((route["fare_cny"], route["arrive"]), (150, "09:19"))
        self.assertEqual(route["connections"], [{"station": "深圳北", "wait_min": 19, "required_min": 12}])

    def test_budget_140_no_route_with_verifiable_delta(self):
        result = self.query(max_fare=140)
        self.assertEqual((result["status"], result["routes"]), ("no_route", []))
        conflicts = {tuple(c["service_ids"]): c["over"] for c in result["conflicts"]}
        self.assertEqual(conflicts[("G6201", "G5803")], {"extra_cny": 10})
        self.assertEqual(conflicts[("G6501",)], {"extra_cny": 75})

    def test_equal_budget_and_arrival_deadline_are_allowed(self):
        self.assertEqual(self.query(max_fare=150, arrive_by="09:19")["status"], "ok")
        self.assertEqual(self.query(max_fare=150, arrive_by="09:18")["status"], "no_route")

    def test_preferences_rank_feasible_routes(self):
        early = self.query()["routes"][0]
        cheapest = self.query(preference="cheapest")["routes"][0]
        direct = self.query(preference="fewest_transfers")["routes"][0]
        self.assertEqual((early["arrive"], early["fare_cny"]), ("09:05", 215))
        self.assertEqual(cheapest["fare_cny"], 150)
        self.assertEqual(direct["transfers"], 0)
        self.assertLessEqual(self.query(max_fare=160, preference="fewest_transfers")["routes"][0]["fare_cny"], 160)

    def test_pareto_front_and_labels(self):
        result = self.query()
        self.assertEqual(result["feasible_count"], 10)
        self.assertEqual(result["pareto_count"], 3)
        self.assertEqual(len(result["routes"]), 4)
        first, second = result["routes"][:2]
        self.assertIn("最快", first["labels"])
        self.assertIn("最少换乘", first["labels"])
        self.assertIn("最便宜", second["labels"])
        self.assertTrue(all(r["pareto"] for r in result["routes"][:3]))
        self.assertEqual(len(self.query(max_routes=2)["routes"]), 2)

    def test_explicit_weights_override_preference(self):
        by_cost = self.query(weight_cost=1.0, preference="earliest")
        self.assertEqual(by_cost["objective"]["source"], "explicit")
        self.assertEqual(by_cost["routes"][0]["fare_cny"], 150)
        by_time = self.query(weight_time=1.0, weight_cost=0.0, weight_wait=0.0)
        self.assertEqual((by_time["routes"][0]["arrive"], by_time["routes"][0]["fare_cny"]), ("09:05", 215))

    def test_transfer_minima_remove_impossible_connections(self):
        paths = self.board.itineraries("GZS", "HKW", 0)
        ids = {tuple(leg.id for leg in p) for p in paths}
        self.assertIn(("G6201", "G5803"), ids)
        self.assertIn(("G6203", "G5805"), ids)
        self.assertNotIn(("G6201", "G5801"), ids)
        self.assertNotIn(("G6203", "G5803"), ids)

    def test_direct_only_filters_transfer_routes(self):
        self.assertTrue(all(r["transfers"] == 0 for r in self.query(max_transfers=0)["routes"]))
        self.assertEqual(self.query(max_transfers=0, max_fare=160)["status"], "no_route")

    def test_conflicts_expose_verifiable_deltas(self):
        result = self.query(max_fare=140, arrive_by="09:00")
        conflicts = {tuple(c["service_ids"]): c["over"] for c in result["conflicts"]}
        self.assertEqual(conflicts[("G6501",)], {"extra_cny": 75, "late_min": 5})
        self.assertEqual(conflicts[("G6201", "G5803")], {"extra_cny": 10, "late_min": 19})

    def test_macau_route_uses_intercity_and_border_walk(self):
        result = self.board.plan(PlanRequest(origin="广州南", destination="澳门",
                                             depart_after="08:00", max_fare=100))
        route = result["routes"][0]
        self.assertEqual([s["id"] for s in route["legs"]], ["C7601", "W1001"])
        self.assertEqual((route["fare_cny"], route["arrive"]), (70, "10:30"))
        tight = self.board.plan(PlanRequest(origin="广州南", destination="澳门",
                                            depart_after="08:00", max_fare=60))
        self.assertEqual(tight["status"], "no_route")
        self.assertEqual(tight["conflicts"][0]["over"], {"extra_cny": 10})

    def test_departure_after_last_service(self):
        self.assertEqual(self.query(depart_after="23:00")["conflicts"], [])

    def test_ambiguous_city_lists_its_stations(self):
        r = self.query(origin="深圳")
        self.assertEqual((r["status"], r["field"]), ("clarify", "origin"))
        self.assertEqual(set(r["options"]), {"深圳北", "福田", "蛇口邮轮母港", "深圳湾口岸"})

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
            for wait, expected in [(19, "ok"), (20, "no_route")]:
                (folder / "stations.csv").write_text(text.replace("深圳北,深圳,12", f"深圳北,深圳,{wait}"), encoding="utf-8")
                with patch.dict(os.environ, {"TRANSIT_DATA_DIR": temp}):
                    q = PlanRequest(origin="GZS", destination="HKW", max_fare=160, arrive_by="09:19")
                    self.assertEqual(Timetable.configured().plan(q)["status"], expected)

    def test_import_rejects_unknown_station(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            shutil.copytree(DEFAULT_DATA, folder, dirs_exist_ok=True)
            with (folder / "services.csv").open("a", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(["X1", "GZS", "UNKNOWN", "11:00", "11:30", 10, "测试"])
            with self.assertRaisesRegex(ValueError, "X1.*未知站点"):
                Timetable(folder)


class SimulationAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = Timetable(DEFAULT_DATA)

    def simulate(self, ids, **changes):
        return simulate_route(self.board, SimulateRequest(service_ids=ids, **changes))

    def test_zero_delay_never_misses(self):
        result = self.simulate(["G6201", "G5803"], delay_factor=0)
        self.assertEqual(result["connection_success_rate"], 1.0)
        self.assertEqual(result["expected_arrive"], "09:19")
        self.assertEqual(result["riskiest_connection"]["station"], "深圳北")

    def test_heavy_delay_lowers_success_and_shifts_arrival(self):
        calm = self.simulate(["G6201", "G5803"], delay_factor=0.5)
        heavy = self.simulate(["G6201", "G5803"], delay_factor=3.0)
        self.assertEqual(calm["connection_success_rate"], 1.0)
        self.assertLess(heavy["connection_success_rate"], calm["connection_success_rate"])
        self.assertGreater(heavy["expected_arrive"], calm["expected_arrive"])

    def test_on_time_rate_bounded_by_success_rate(self):
        result = self.simulate(["G6201", "G5803"], arrive_by="09:19", delay_factor=2.0)
        self.assertLessEqual(result["on_time_rate"], result["connection_success_rate"])

    def test_direct_route_has_no_connection_risk(self):
        result = self.simulate(["G6501"], delay_factor=3.0)
        self.assertEqual(result["connection_success_rate"], 1.0)
        self.assertNotIn("riskiest_connection", result)

    def test_invalid_input_is_reported(self):
        self.assertEqual(self.simulate(["G6201", "G5813"])["status"], "error")
        self.assertEqual(self.simulate(["NOPE"])["status"], "error")


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
        self.assertEqual(set(data["tools"]),
                         {"list_transit_stations", "plan_transit", "simulate_transit_route"})

    def test_stations_endpoint_exposes_coordinates(self):
        stations = self.client.get("/stations").json()["stations"]
        self.assertEqual(len(stations), 11)
        gz = next(s for s in stations if s["id"] == "GZS")
        self.assertAlmostEqual(gz["lat"], 22.9876, places=3)

    def test_structured_api_rejects_invalid_input(self):
        for payload in ({"depart_after": "25:10"}, {"max_fare": -1},
                        {"max_transfers": 3}, {"nonexistent": True}, {"weight_time": -0.5}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/plan", json=payload).status_code, 422)

    def test_simulate_endpoint(self):
        ok = self.client.post("/simulate", json={"service_ids": ["G6201", "G5803"], "delay_factor": 0})
        self.assertEqual(ok.json()["connection_success_rate"], 1.0)
        bad = self.client.post("/simulate", json={"service_ids": ["G6201", "G5803"], "trials": 10})
        self.assertEqual(bad.status_code, 422)
        unknown = self.client.post("/simulate", json={"service_ids": ["X1"]})
        self.assertEqual(unknown.json()["status"], "error")

    def test_structured_api_and_text_agree(self):
        route = self.client.post("/plan", json={"origin": "GZS", "destination": "HKW",
                                                "max_fare": 160}).json()["routes"][0]
        result = self.chat("广州南到香港西九龙，最多160元，最早到达")
        self.assertEqual([t["tool"] for t in result["tool_calls"]], ["plan_transit"])
        self.assertEqual(result["tool_calls"][0]["status"], "ok")
        self.assertIn(f"{route['fare_cny']} 元", result["reply"])
        self.assertIn(route["arrive"], result["reply"])

    def test_followup_keeps_route_and_reports_actual_conflicts(self):
        first = self.chat("广州南到香港西九龙，08:00出发，预算160元，最早到达")
        self.assertIn("G5803", first["reply"])
        cheaper = self.chat("预算改为140元", first["session_id"])
        self.assertIn("超预算 10 元", cheaper["reply"])
        conflict = self.chat("09:00前到", first["session_id"])
        self.assertIn("超预算 75 元", conflict["reply"])
        self.assertIn("晚到 5 分钟", conflict["reply"])

    def test_new_session_does_not_inherit_route(self):
        self.chat("广州南到香港西九龙，预算160元")
        fresh = self.chat("预算改为140元")
        self.assertIn("明确出发站", fresh["reply"])
        self.assertNotIn("G5803", fresh["reply"])

    def test_station_clarification_in_same_session(self):
        result = self.chat("深圳到香港西九龙，最早到达")
        self.assertIn("深圳北", result["reply"])
        resolved = self.chat("深圳北", result["session_id"])
        self.assertIn("G5801", resolved["reply"])
        self.assertEqual(resolved["tool_calls"][0]["args"]["request"]["origin"], "深圳北")

    def test_soft_preference_leaves_budget_unset(self):
        resp = self.chat("广州南到香港西九龙，便宜一点")
        self.assertNotIn("max_fare", resp["tool_calls"][0]["args"]["request"])
        self.assertIn("120 元", resp["reply"])  # 不限出发时段时，深圳湾跨境巴士组合最便宜

    def test_unsupported_rule_input_does_not_invent_an_itinerary(self):
        r = self.chat("请帮我直接订票")
        self.assertEqual(r["tool_calls"], [])
        self.assertIn("示例", r["reply"])


if __name__ == "__main__":
    unittest.main()
