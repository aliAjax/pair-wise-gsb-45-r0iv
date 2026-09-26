"""封航与复航重排端到端流程测试。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CONTROLLER = Actor("dispatcher", "port_controller")


def make_data(reference, vessel, berth="B1", eta=6, risk="low", dangerous=False, draft=10.0, duration=4.0):
    return {
        "vessel": vessel,
        "captain": "Capt.%s" % vessel,
        "berth": berth,
        "vessel_length_m": 180,
        "berth_length_m": 220,
        "draft_m": draft,
        "berth_depth_m": 12.0,
        "eta_hour": eta,
        "operation_duration_hours": duration,
        "risk_level": risk,
        "dangerous_goods": dangerous,
        "dangerous_class": "3" if dangerous else "",
    }


class ReopenFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, **kwargs):
        return self.service.create(CONTROLLER, reference, make_data(reference, **kwargs))

    def test_close_moves_unstarted_to_waiting_and_keeps_berthed(self):
        self._create("V-1", vessel="A", eta=5)
        berthed = self._create("V-2", vessel="B", berth="B2", eta=4)
        berthed = self.service.act(CONTROLLER, berthed["id"], berthed["version"], "confirm", {"pilot_id": "P-01"})
        berthed = self.service.act(CONTROLLER, berthed["id"], berthed["version"], "berth", {"actual_draft_m": 10.1})

        result = self.service.close_channel(CONTROLLER, {"close_hour": 8, "reason": "台风"})
        self.assertEqual(result["waiting_count"], 1)
        waiting = [item for item in self.service.list_records(CONTROLLER, state="waiting")]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["payload"]["waiting"]["reason"], "channel_closed")
        self.assertEqual(waiting[0]["payload"]["waiting"]["reason_text"], "台风")
        # 泊位作业继续。
        self.assertEqual(self.service.get_record(CONTROLLER, berthed["id"])["state"], "berthed")

        status = self.service.channel_status(CONTROLLER)
        self.assertEqual(status["status"], "closed")
        with self.assertRaises(Conflict):
            self.service.close_channel(CONTROLLER, {"close_hour": 9})

    def test_reopen_persists_three_artifacts_and_reasons(self):
        dangerous = self._create("V-1", vessel="DG", berth="B1", eta=7, risk="high", dangerous=True)
        normal = self._create("V-2", vessel="NM", berth="B2", eta=5)

        self.service.close_channel(CONTROLLER, {"close_hour": 8})
        result = self.service.reopen_channel(
            CONTROLLER,
            {
                "reopen_hour": 12,
                "channel_depth_m": 12.0,
                "channel_transit_hours": 1,
                "pilots": [{"pilot_id": "P-01", "available_from": 14}],
            },
        )
        batch_id = result["batch_id"]
        plan = result["plan"]
        # 危险品排在最前。
        self.assertEqual(plan["queue"][0]["vessel"], "DG")
        # 候泊顺序、航道时段、泊位占用分别落库。
        loaded = self.service.get_plan(CONTROLLER, batch_id)
        self.assertEqual([item["vessel"] for item in loaded["queue"]], ["DG", "NM"])
        self.assertEqual(len(loaded["slots"]), 2)
        self.assertGreaterEqual(len(loaded["occupancy"]), 2)
        self.assertEqual(self.service.latest_plan(CONTROLLER)["batch_id"], batch_id)

        # 记录上写清每艘船的等待/进场信息；名单引航员14点才到位，首船顺延。
        dg_record = self.service.get_record(CONTROLLER, dangerous["id"])
        self.assertEqual(dg_record["payload"]["reschedule"]["status"], "scheduled")
        self.assertEqual(dg_record["payload"]["reschedule"]["pilot_id"], "P-01")
        self.assertEqual(dg_record["payload"]["reschedule"]["channel_start_hour"], 14.0)
        self.assertIn("引航员", dg_record["payload"]["reschedule"]["delay_detail"])

        # 审计时间线包含封航与复航重排事件。
        actions = [item["action"] for item in self.service.timeline(CONTROLLER, dangerous["id"])]
        self.assertIn("close", actions)
        self.assertIn("replan", actions)

    def test_reopen_requires_closed_channel(self):
        with self.assertRaises(Conflict):
            self.service.reopen_channel(CONTROLLER, {"reopen_hour": 12, "channel_depth_m": 12, "channel_transit_hours": 1})

    def test_reopen_before_close_hour_rejected(self):
        self._create("V-1", vessel="A")
        self.service.close_channel(CONTROLLER, {"close_hour": 10})
        with self.assertRaises(ValidationError):
            self.service.reopen_channel(CONTROLLER, {"reopen_hour": 9, "channel_depth_m": 12, "channel_transit_hours": 1})

    def test_waiting_vessel_can_berth_after_plan(self):
        record = self._create("V-1", vessel="A", eta=6)
        self.service.close_channel(CONTROLLER, {"close_hour": 8})
        self.service.reopen_channel(CONTROLLER, {"reopen_hour": 12, "channel_depth_m": 12, "channel_transit_hours": 1})
        record = self.service.get_record(CONTROLLER, record["id"])
        record = self.service.act(CONTROLLER, record["id"], record["version"], "berth", {"actual_draft_m": 10.4})
        self.assertEqual(record["state"], "berthed")

    def test_operation_duration_is_stored(self):
        record = self._create("V-1", vessel="A", eta=6, duration=3.5)
        payload = record["payload"]
        self.assertEqual(payload["operation_duration_hours"], 3.5)
        self.assertEqual(payload["operation_end_hour"], 9.5)
