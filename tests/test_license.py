import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module
from database import DomainError, LicenseConflict, RadioDB


class LicenseNarrowingTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.pid = self.db.add_program("周末剧场", "live", 60, "2026-01-01", "2026-12-31", None, 0, ["华东", "华南"])
        self.other = self.db.add_program("备用节目", "music", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def current_license(self):
        return self.db.get_program(self.pid)

    def test_conflict_after_new_end_blocks_save(self):
        slot = self.db.schedule_slot("2026-12-20", "20:00", self.pid, "华东")
        with self.assertRaises(LicenseConflict) as ctx:
            self.db.update_program_license(self.pid, "2026-01-01", "2026-09-30", ["华东", "华南"])
        conflict = ctx.exception.conflicts[0]
        self.assertEqual(slot, conflict["slot_id"])
        self.assertEqual("2026-12-20", conflict["air_date"])
        self.assertIn("晚于新授权结束日期", conflict["reason"])
        program = self.current_license()
        self.assertEqual("2026-12-31", program["end_date"])

    def test_conflict_before_new_start_blocks_save(self):
        slot = self.db.schedule_slot("2026-01-10", "20:00", self.pid, "华东")
        with self.assertRaises(LicenseConflict) as ctx:
            self.db.update_program_license(self.pid, "2026-06-01", "2026-12-31", ["华东", "华南"])
        self.assertEqual(slot, ctx.exception.conflicts[0]["slot_id"])
        self.assertIn("早于新授权开始日期", ctx.exception.conflicts[0]["reason"])
        self.assertEqual("2026-01-01", self.current_license()["start_date"])

    def test_conflict_removed_region_blocks_save(self):
        slot = self.db.schedule_slot("2026-07-01", "20:00", self.pid, "华南")
        with self.assertRaises(LicenseConflict) as ctx:
            self.db.update_program_license(self.pid, "2026-01-01", "2026-12-31", ["华东"])
        self.assertEqual(slot, ctx.exception.conflicts[0]["slot_id"])
        self.assertIn("华南", ctx.exception.conflicts[0]["reason"])
        self.assertEqual(["华东", "华南"], self.current_license()["regions"])

    def test_slot_can_combine_date_and_region_reasons(self):
        self.db.schedule_slot("2026-12-20", "20:00", self.pid, "华南")
        with self.assertRaises(LicenseConflict) as ctx:
            self.db.update_program_license(self.pid, "2026-01-01", "2026-09-30", ["华东"])
        self.assertEqual(1, len(ctx.exception.conflicts))
        reason = ctx.exception.conflicts[0]["reason"]
        self.assertIn("结束日期", reason)
        self.assertIn("华南", reason)

    def test_planned_slot_with_playout_is_kept_and_does_not_block(self):
        slot = self.db.schedule_slot("2026-12-20", "20:00", self.pid, "华东")
        self.db.record_playout(slot, "20:00", 60)
        updated = self.db.update_program_license(self.pid, "2026-01-01", "2026-09-30", ["华东"])
        self.assertEqual("2026-09-30", updated["end_date"])
        row = self.db.get_slot(slot)
        self.assertEqual("planned", row["status"])
        self.assertEqual(self.pid, row["program_id"])
        logs = self.db.conn.execute("SELECT COUNT(*) FROM playout_logs WHERE slot_id=?", (slot,)).fetchone()[0]
        self.assertEqual(1, logs)

    def test_unplayed_replaced_slot_blocks_but_played_one_does_not(self):
        unplayed = self.db.schedule_slot("2026-08-01", "20:00", self.pid, "华南")
        played = self.db.schedule_slot("2026-12-20", "21:00", self.pid, "华东")
        # live replacement previously swapped these slots; current content is self.pid
        with self.db.transaction():
            self.db.conn.execute(
                "UPDATE slots SET status='replaced', replaced_from=? WHERE id IN (?,?)",
                (self.other, unplayed, played),
            )
        self.db.record_playout(played, "21:00", 60)
        with self.assertRaises(LicenseConflict) as ctx:
            self.db.update_program_license(self.pid, "2026-01-01", "2026-09-30", ["华东"])
        self.assertEqual([unplayed], [c["slot_id"] for c in ctx.exception.conflicts])
        updated = self.db.update_program_license(self.pid, "2026-01-01", "2026-12-31", ["华东", "华南"])
        self.assertEqual("2026-12-31", updated["end_date"])
        self.assertEqual("replaced", self.db.get_slot(unplayed)["status"])
        self.assertEqual("replaced", self.db.get_slot(played)["status"])

    def test_cancelled_slot_is_ignored(self):
        slot = self.db.schedule_slot("2026-12-20", "20:00", self.pid, "华东")
        with self.db.transaction():
            self.db.conn.execute("UPDATE slots SET status='cancelled' WHERE id=?", (slot,))
        updated = self.db.update_program_license(self.pid, "2026-01-01", "2026-09-30", ["华东"])
        self.assertEqual("2026-09-30", updated["end_date"])

    def test_compliant_narrowing_updates_window_and_regions(self):
        keep = self.db.schedule_slot("2026-05-01", "20:00", self.pid, "华东")
        updated = self.db.update_program_license(self.pid, "2026-03-01", "2026-09-30", ["华东"])
        self.assertEqual("2026-03-01", updated["start_date"])
        self.assertEqual("2026-09-30", updated["end_date"])
        self.assertEqual(["华东"], updated["regions"])
        self.assertEqual(self.pid, self.db.get_slot(keep)["program_id"])

    def test_conflict_list_is_ordered_and_complete(self):
        late = self.db.schedule_slot("2026-12-20", "20:00", self.pid, "华东")
        early = self.db.schedule_slot("2026-01-05", "20:00", self.pid, "华东")
        with self.assertRaises(LicenseConflict) as ctx:
            self.db.update_program_license(self.pid, "2026-03-01", "2026-09-30", ["华东"])
        ids = [c["slot_id"] for c in ctx.exception.conflicts]
        self.assertEqual([early, late], ids)
        for conflict in ctx.exception.conflicts:
            self.assertEqual({"slot_id", "air_date", "start_time", "region", "reason"}, set(conflict))

    def test_invalid_inputs(self):
        with self.assertRaisesRegex(DomainError, "YYYY-MM-DD"):
            self.db.update_program_license(self.pid, "bad", "2026-12-31", ["华东"])
        with self.assertRaisesRegex(DomainError, "早于"):
            self.db.update_program_license(self.pid, "2026-12-31", "2026-01-01", ["华东"])
        with self.assertRaisesRegex(DomainError, "地区不能为空"):
            self.db.update_program_license(self.pid, "2026-01-01", "2026-12-31", ["", "  "])
        with self.assertRaisesRegex(DomainError, "不能重复"):
            self.db.update_program_license(self.pid, "2026-01-01", "2026-12-31", ["华东", "华东"])
        with self.assertRaisesRegex(DomainError, "节目不存在"):
            self.db.update_program_license(9999, "2026-01-01", "2026-12-31", ["华东"])


class LicenseApiTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self._orig_db = app_module.Handler.db
        app_module.Handler.db = self.db
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pid = self.db.add_program("午夜访谈", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.db.schedule_slot("2026-12-20", "23:00", self.pid, "华东")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app_module.Handler.db = self._orig_db
        self.db.close()
        os.unlink(self.path)

    def post(self, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/programs/{self.pid}/license",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_conflict_returns_409_with_per_slot_list_and_no_save(self):
        status, body = self.post({"start_date": "2026-01-01", "end_date": "2026-09-30", "regions": ["华东"]})
        self.assertEqual(409, status)
        self.assertFalse(body["ok"])
        self.assertEqual(1, len(body["conflicts"]))
        conflict = body["conflicts"][0]
        self.assertEqual("2026-12-20", conflict["air_date"])
        self.assertTrue(conflict["slot_id"])
        self.assertIn("晚于新授权结束日期", conflict["reason"])
        self.assertEqual("2026-12-31", self.db.get_program(self.pid)["end_date"])

    def test_compliant_returns_updated_program(self):
        status, body = self.post({"start_date": "2026-01-01", "end_date": "2026-12-31", "regions": ["华东", "华北"]})
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(["华东", "华北"], body["program"]["regions"])


if __name__ == "__main__":
    unittest.main()
