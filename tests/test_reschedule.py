import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


OP = Actor("dispatcher", "port_controller")


def plan(vessel, berth, eta, operation, risk="low", dangerous=False, draft=8.0, captain="Captain", berth_depth=15.0):
    return {
        "vessel": vessel,
        "captain_name": captain,
        "berth": berth,
        "vessel_length_m": 100,
        "berth_length_m": 200,
        "draft_m": draft,
        "berth_depth_m": berth_depth,
        "eta_hour": eta,
        "operation_hours": operation,
        "risk_level": risk,
        "dangerous_goods": dangerous,
        "dangerous_class": "class-3" if dangerous else "",
    }


class RescheduleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, reference, data):
        return self.service.create(OP, reference, data)

    def berth_vessel(self, record):
        record = self.service.act(OP, record["id"], record["version"], "confirm", {"pilot_id": "P-00"})
        return self.service.act(OP, record["id"], record["version"], "berth", {"actual_draft_m": 8.0})

    def test_close_moves_unstarted_to_waiting_and_keeps_berthed_working(self):
        berthed = self.berth_vessel(self.create("VOY-1", plan("ShipA", "B1", 4, 10)))
        waiting = self.create("VOY-2", plan("ShipB", "B2", 6, 4))
        result = self.service.close_port(OP, "台风预警", 8)
        self.assertEqual(result["moved_to_waiting"], [waiting["id"]])
        self.assertEqual(result["berthed_continue"], [berthed["id"]])
        self.assertEqual(self.service.get_record(OP, waiting["id"])["state"], "waiting")
        self.assertEqual(self.service.get_record(OP, berthed["id"])["state"], "berthed")
        queue = self.service.waiting_queue(OP)
        self.assertEqual(len(queue), 1)
        self.assertIn("台风预警", queue[0]["queue_reason"])
        occupancy = self.service.berth_occupancy(OP)
        self.assertTrue(any(o["record_id"] == berthed["id"] and o["source"] == "berth" for o in occupancy))
        self.assertEqual(self.service.port_status(OP)["status"], "closed")

    def test_reopen_orders_dangerous_first_then_fcfs_with_exclusive_slots(self):
        low = self.create("VOY-1", plan("LowEta5", "B1", 5, 2, risk="low"))
        dangerous = self.create("VOY-2", plan("DangerEta9", "B2", 9, 3, risk="high", dangerous=True))
        medium = self.create("VOY-3", plan("MedEta3", "B3", 3, 4, risk="medium"))
        self.service.close_port(OP, "大风", 8)
        result = self.service.reopen_port(OP, 10, 15.0, 2, ["P-1", "P-2", "P-3"])
        order = [item["record_id"] for item in result["scheduled"]]
        self.assertEqual(order, [dangerous["id"], medium["id"], low["id"]])
        slots = [(item["channel_slot"]["start_hour"], item["channel_slot"]["end_hour"]) for item in result["scheduled"]]
        self.assertEqual(slots, [(10, 12), (12, 14), (14, 16)])
        for item in result["scheduled"]:
            self.assertEqual(item["channel_slot"]["end_hour"], item["berth_window"]["start_hour"])
        stored = self.service.channel_slots(OP)
        self.assertEqual([s["sequence"] for s in stored], [1, 2, 3])
        self.assertEqual(self.service.waiting_queue(OP), [])
        self.assertEqual(self.service.get_record(OP, low["id"])["state"], "scheduled")
        self.assertEqual(self.service.port_status(OP)["status"], "open")

    def test_deep_draft_vessel_is_postponed_without_consuming_slot(self):
        deep = self.create("VOY-1", plan("Deep", "B1", 5, 2, draft=14.8, berth_depth=16.0))
        normal = self.create("VOY-2", plan("Normal", "B2", 6, 2))
        self.service.close_port(OP, "军演", 8)
        result = self.service.reopen_port(OP, 10, 15.0, 2, ["P-1", "P-2"])
        self.assertEqual([item["record_id"] for item in result["scheduled"]], [normal["id"]])
        self.assertEqual(result["scheduled"][0]["channel_slot"], {"start_hour": 10, "end_hour": 12})
        self.assertEqual(len(result["postponed"]), 1)
        self.assertEqual(result["postponed"][0]["record_id"], deep["id"])
        self.assertIn("吃水", result["postponed"][0]["reason"])
        record = self.service.get_record(OP, deep["id"])
        self.assertEqual(record["state"], "waiting")
        self.assertIn("吃水", record["payload"]["wait_reason"])

    def test_missing_pilot_is_postponed(self):
        first = self.create("VOY-1", plan("First", "B1", 5, 2, dangerous=True))
        second = self.create("VOY-2", plan("Second", "B2", 6, 2))
        self.service.close_port(OP, "大雾", 8)
        result = self.service.reopen_port(OP, 10, 15.0, 2, ["P-1"])
        self.assertEqual([item["record_id"] for item in result["scheduled"]], [first["id"]])
        self.assertEqual(result["scheduled"][0]["pilot_id"], "P-1")
        self.assertEqual(result["postponed"][0]["record_id"], second["id"])
        self.assertIn("引航员", result["postponed"][0]["reason"])
        queue = self.service.waiting_queue(OP)
        self.assertEqual(len(queue), 1)
        self.assertIn("引航员", queue[0]["queue_reason"])

    def test_berth_occupancy_pushes_slot_later(self):
        berthed = self.berth_vessel(self.create("VOY-1", plan("InPort", "B1", 8, 12)))
        follower = self.create("VOY-2", plan("Follower", "B1", 6, 2))
        self.service.close_port(OP, "台风", 9)
        result = self.service.reopen_port(OP, 10, 15.0, 2, ["P-1"])
        self.assertEqual(len(result["scheduled"]), 1)
        entry = result["scheduled"][0]
        self.assertEqual(entry["record_id"], follower["id"])
        self.assertEqual(entry["channel_slot"], {"start_hour": 18, "end_hour": 20})
        self.assertEqual(entry["berth_window"], {"start_hour": 20, "end_hour": 22})
        occupancy = [o for o in self.service.berth_occupancy(OP) if o["berth"] == "B1"]
        self.assertEqual(len(occupancy), 2)

    def test_close_and_reopen_guards(self):
        with self.assertRaises(Conflict):
            self.service.reopen_port(OP, 10, 15.0, 2, [])
        self.service.close_port(OP, "台风", 8)
        with self.assertRaises(Conflict):
            self.service.close_port(OP, "再次封航", 9)

    def test_create_while_closed_goes_straight_to_waiting(self):
        self.service.close_port(OP, "台风", 8)
        record = self.create("VOY-9", plan("Late", "B1", 12, 3))
        self.assertEqual(record["state"], "waiting")
        self.assertIn("封航", record["payload"]["wait_reason"])
        queue = self.service.waiting_queue(OP)
        self.assertEqual([item["record_id"] for item in queue], [record["id"]])


if __name__ == "__main__":
    unittest.main()
