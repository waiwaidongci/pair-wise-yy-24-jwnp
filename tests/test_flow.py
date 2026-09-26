import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB


class RadioSchedulingFlowTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p1 = self.db.add_program("早间新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.p2 = self.db.add_program("品牌广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_complete_replace_playout_and_reconcile_flow(self):
        first = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        second = self.db.schedule_slot("2026-09-28", "10:00", self.p2, "华东")
        self.assertEqual("planned", self.db.get_slot(first)["status"])
        replaced = self.db.replace_slot(first, self.p2)
        self.assertEqual("replaced", replaced["status"])
        self.assertEqual(self.p2, replaced["program_id"])
        self.db.record_playout(first, "09:00", 5, self.p1, "临时切回旧内容")
        self.db.record_playout(second, "10:00", 5, self.p2)
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(row["slot_id"], row["kind"]) for row in exceptions}
        self.assertIn((first, "wrong_program"), kinds)

    def test_rejects_overlap_and_unauthorized_region(self):
        self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.schedule_slot("2026-09-28", "09:15", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "未授权"):
            self.db.schedule_slot("2026-09-28", "11:00", self.p1, "华北")

    def _program(self, program_id):
        return next(p for p in self.db.snapshot()["programs"] if p["id"] == program_id)

    def test_license_update_applies_and_keeps_aired_slots(self):
        inside = self.db.schedule_slot("2026-03-01", "09:00", self.p1, "华东")
        aired = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        self.db.record_playout(aired, "09:00", 30, self.p1)
        cancelled = self.db.schedule_slot("2026-10-05", "09:00", self.p1, "华东")
        self.db.conn.execute("UPDATE slots SET status='cancelled' WHERE id=?", (cancelled,))
        self.db.conn.commit()
        conflicts = self.db.update_program_license(self.p1, "2026-01-01", "2026-06-30", ["华东", "华北"])
        self.assertEqual([], conflicts)
        program = self._program(self.p1)
        self.assertEqual("2026-06-30", program["end_date"])
        self.assertEqual(["华东", "华北"], program["regions"])
        self.assertEqual("planned", self.db.get_slot(inside)["status"])
        self.assertEqual("planned", self.db.get_slot(aired)["status"])
        self.assertEqual(1, len(self.db.conn.execute(
            "SELECT * FROM playout_logs WHERE slot_id=?", (aired,)).fetchall()))

    def test_license_update_blocked_by_slot_outside_window(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        conflicts = self.db.update_program_license(self.p1, "2026-01-01", "2026-06-30", ["华东"])
        self.assertEqual(1, len(conflicts))
        self.assertEqual(slot, conflicts[0]["slot_id"])
        self.assertEqual("2026-09-28", conflicts[0]["air_date"])
        self.assertIn("授权窗口", conflicts[0]["reason"])
        program = self._program(self.p1)
        self.assertEqual("2026-12-31", program["end_date"])
        self.assertEqual(["华东"], program["regions"])

    def test_license_update_checks_replaced_slots_and_regions(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        self.db.replace_slot(slot, self.p2)
        conflicts = self.db.update_program_license(self.p2, "2026-01-01", "2026-12-31", ["华北"])
        self.assertEqual(1, len(conflicts))
        self.assertEqual(slot, conflicts[0]["slot_id"])
        self.assertIn("华东", conflicts[0]["reason"])
        self.assertEqual(["华东"], self._program(self.p2)["regions"])


if __name__ == "__main__":
    unittest.main()
