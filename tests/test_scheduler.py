"""复航重排算法测试。"""
import unittest

from src.scheduler import build_plan, fmt_hour


def vessel(record_id, vessel, berth, eta, draft=10.0, risk="low", dangerous=False, duration=4.0, dangerous_class=""):
    return {
        "id": record_id,
        "reference": "VOY-%s" % record_id,
        "state": "waiting",
        "payload": {
            "vessel": vessel,
            "berth": berth,
            "eta_hour": eta,
            "draft_m": draft,
            "berth_depth_m": 12.0,
            "operation_duration_hours": duration,
            "operation_end_hour": eta + duration,
            "etd_hour": eta + int(duration),
            "risk_level": risk,
            "dangerous_goods": dangerous,
            "dangerous_class": dangerous_class,
            "waiting": {"reason": "channel_closed", "reason_text": "封航", "since_hour": 8},
        },
    }


def berthed(record_id, vessel, berth, start, end):
    return {
        "id": record_id,
        "reference": "VOY-%s" % record_id,
        "state": "berthed",
        "payload": {
            "vessel": vessel,
            "berth": berth,
            "eta_hour": start,
            "etd_hour": end,
            "operation_end_hour": end,
        },
    }


PARAMS = {
    "reopen_hour": 12.0,
    "transit_hours": 1.0,
    "channel_depth_m": 12.0,
    "pilots": [{"pilot_id": "P-01", "available_from": 12.0}],
}


class SchedulerTest(unittest.TestCase):
    def test_dangerous_first_then_eta(self):
        plan = build_plan(
            [
                vessel(1, "A", "B1", eta=6),
                vessel(2, "B", "B2", eta=5, risk="high", dangerous=True, dangerous_class="3"),
                vessel(3, "C", "B3", eta=4),
            ],
            [],
            PARAMS,
        )
        self.assertEqual([item["record_id"] for item in plan["queue"]], [2, 3, 1])
        slots = {item["record_id"]: item for item in plan["slots"]}
        self.assertEqual(slots[2]["start_hour"], 12.0)
        self.assertEqual(slots[3]["start_hour"], 13.0)
        self.assertEqual(slots[1]["start_hour"], 14.0)
        for item in plan["slots"]:
            self.assertEqual(item["status"], "scheduled")

    def test_channel_is_exclusive(self):
        plan = build_plan([vessel(1, "A", "B1", 9), vessel(2, "B", "B2", 9)], [], PARAMS)
        starts = sorted(item["start_hour"] for item in plan["slots"])
        self.assertEqual(starts, [12.0, 13.0])

    def test_eta_in_future_wait_for_arrival(self):
        plan = build_plan([vessel(1, "A", "B1", eta=15)], [], PARAMS)
        slot = plan["slots"][0]
        self.assertEqual(slot["start_hour"], 15.0)

    def test_draft_insufficient_postponed_with_reason(self):
        plan = build_plan([vessel(1, "A", "B1", eta=9, draft=11.8)], [], PARAMS)
        slot = plan["slots"][0]
        self.assertEqual(slot["status"], "postponed")
        self.assertEqual(slot["reason"], "draft_insufficient")
        self.assertIn("吃水", slot["detail"])
        # 顺延船不占用航道，光标不应推进。
        self.assertEqual(plan["queue"][0]["reason"], "channel_closed")

    def test_no_pilot_postponed(self):
        params = dict(PARAMS, pilots=[])
        plan = build_plan([vessel(1, "A", "B1", eta=9, duration=2.0)], [], params)
        slot = plan["slots"][0]
        self.assertEqual(slot["status"], "postponed")
        self.assertEqual(slot["reason"], "no_pilot")

    def test_pilot_availability_delays_slot(self):
        params = dict(PARAMS, pilots=[{"pilot_id": "P-01", "available_from": 14.0}])
        plan = build_plan([vessel(1, "A", "B1", eta=9)], [], params)
        slot = plan["slots"][0]
        self.assertEqual(slot["start_hour"], 14.0)
        self.assertIn("引航员", slot["detail"])

    def test_berth_occupied_delays_slot(self):
        plan = build_plan(
            [vessel(1, "A", "B1", eta=9, duration=2.0)],
            [berthed(9, "ON", "B1", start=8, end=14.0)],
            PARAMS,
        )
        slot = plan["slots"][0]
        # 航道时段结束时（进场）泊位必须已释放。
        self.assertEqual(slot["end_hour"], 14.0)
        self.assertEqual(slot["start_hour"], 13.0)
        self.assertIn("泊位", slot["detail"])
        ongoing = [item for item in plan["occupancy"] if item["source"] == "ongoing"]
        self.assertEqual(len(ongoing), 1)

    def test_finished_berth_is_released(self):
        plan = build_plan(
            [vessel(1, "A", "B1", eta=9)],
            [berthed(9, "ON", "B1", start=4, end=10.0)],
            PARAMS,
        )
        self.assertEqual([item for item in plan["occupancy"] if item["source"] == "ongoing"], [])
        self.assertEqual(plan["slots"][0]["start_hour"], 12.0)

    def test_single_pilot_serializes_vessels(self):
        plan = build_plan(
            [vessel(1, "A", "B1", eta=9), vessel(2, "B", "B2", eta=9)],
            [],
            PARAMS,
        )
        pilots = [item["pilot_id"] for item in plan["slots"]]
        self.assertEqual(pilots, ["P-01", "P-01"])

    def test_fmt_hour_next_day(self):
        self.assertEqual(fmt_hour(26.5), "第2天02:30")
        self.assertEqual(fmt_hour(8.0), "08:00")
