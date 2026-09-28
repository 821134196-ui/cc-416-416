"""重复扫描测试:已交付住户/餐盒/路线的重复扫描必须被识别并忽略。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services as svc
from app.db import transaction
from tests.world import World


class TestDuplicateScans(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn
        self.bid, self.rid = self.w.batch_and_route(["d_pump", "d_rice", "d_pork"])
        with transaction(self.c):
            svc.hand_over_route(self.c, self.rid, "stf_chef")

    def tearDown(self):
        self.w.close()

    def _deliver(self, resident="r1"):
        box = self.w.box_for(resident)
        with transaction(self.c):
            r = svc.scan(self.c, self.rid, "RES:" + self.w.resident_code(resident),
                         "stf_run1")
            svc.scan(self.c, self.rid, "BOX:" + self.w.active_token(box), "stf_run1",
                     active_stop_id=r["stop_id"])
        return box

    def test_duplicate_resident_scan_after_delivery(self):
        self._deliver("r1")
        with transaction(self.c):
            res = svc.scan(self.c, self.rid, "RES:" + self.w.resident_code("r1"),
                           "stf_run1")
        self.assertFalse(res["accepted"])
        self.assertEqual(res["stage"], "duplicate")
        # 不应产生第二条送达记录
        n = self.c.execute(
            "SELECT COUNT(*) c FROM deliveries WHERE resident_id='r1'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_duplicate_box_scan_after_delivery(self):
        box = self._deliver("r1")
        sid = self.w.stop_for(self.rid, "r1")
        with transaction(self.c):
            res = svc.scan(self.c, self.rid, "BOX:" + self.w.active_token(box),
                           "stf_run1", active_stop_id=sid)
        self.assertEqual(res["stage"], "duplicate")
        self.assertFalse(res["accepted"])

    def test_resident_rescan_before_box_returns_to_box_step(self):
        # 已扫住户但未扫餐盒时,再次扫住户 → duplicate,提示去扫餐盒,不重置会话
        with transaction(self.c):
            r = svc.scan(self.c, self.rid, "RES:" + self.w.resident_code("r2"),
                         "stf_run1")
        self.assertEqual(r["stage"], "resident_ok")
        with transaction(self.c):
            r2 = svc.scan(self.c, self.rid, "RES:" + self.w.resident_code("r2"),
                          "stf_run1")
        self.assertEqual(r2["stage"], "duplicate")
        # 仍可用同一 stop_id 完成餐盒扫描
        box = self.w.box_for("r2")
        with transaction(self.c):
            ok = svc.scan(self.c, self.rid, "BOX:" + self.w.active_token(box),
                          "stf_run1", active_stop_id=r["stop_id"])
        self.assertEqual(ok["stage"], "delivered")

    def test_scan_after_route_completed_rejected(self):
        for res in ("r1", "r2", "r3"):
            self._deliver(res)
        self.assertEqual(self.c.execute(
            "SELECT status FROM routes WHERE id=?", (self.rid,)).fetchone()["status"],
            "completed")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan(self.c, self.rid, "RES:" + self.w.resident_code("r1"), "stf_run1")
        self.assertIn("已完成", str(cm.exception))

    def test_duplicate_scans_logged_as_rejected_events(self):
        self._deliver("r1")
        before = self.c.execute(
            "SELECT COUNT(*) c FROM scan_events WHERE accepted=0").fetchone()["c"]
        with transaction(self.c):
            svc.scan(self.c, self.rid, "RES:" + self.w.resident_code("r1"), "stf_run1")
        after = self.c.execute(
            "SELECT COUNT(*) c FROM scan_events WHERE accepted=0").fetchone()["c"]
        self.assertGreater(after, before)


if __name__ == "__main__":
    unittest.main()
