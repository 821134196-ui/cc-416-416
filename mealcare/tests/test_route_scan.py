"""路线交接与扫描核对流程测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services as svc
from app.db import transaction
from tests.world import World


class TestRouteHandoverAndScan(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn

    def tearDown(self):
        self.w.close()

    def _setup(self, dishes=("d_pump", "d_rice", "d_pork")):
        bid, rid = self.w.batch_and_route(list(dishes))
        return bid, rid

    def test_scan_before_handover_rejected(self):
        bid, rid = self._setup()
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan(self.c, rid, "RES:" + self.w.resident_code("r1"), "stf_run1")
        self.assertIn("尚未由厨房交接", str(cm.exception))

    def test_handover_permission(self):
        bid, rid = self._setup()
        # 送餐员无权交接
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.hand_over_route(self.c, rid, "stf_run1")
        # 厨房可以交接
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        self.assertEqual(self.c.execute(
            "SELECT status FROM routes WHERE id=?", (rid,)).fetchone()["status"],
            "handed_over")
        # 重复交接被拒
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.hand_over_route(self.c, rid, "stf_chef")

    def test_courier_permission_for_scan(self):
        bid, rid = self._setup()
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        # 营养师不是送餐员
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan(self.c, rid, "RES:" + self.w.resident_code("r1"), "stf_nutr")
        self.assertIn("route.deliver", str(cm.exception))

    def test_scan_box_before_resident_rejected(self):
        bid, rid = self._setup()
        box = self.w.box_for("r1")
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan(self.c, rid, "BOX:" + self.w.active_token(box), "stf_run1")
        self.assertIn("先扫描住户", str(cm.exception))

    def test_resident_not_on_route_rejected(self):
        # 只给 r1 配餐的批次/路线
        c = self.c
        mid = self.w.published_menu(["d_rice"], meal_type="dinner")
        with transaction(c):
            bid = svc.produce_batch(c, mid, "stf_chef", resident_ids=["r1"])
            rid = svc.create_route_for_batch(c, bid, "stf_run1")
            svc.hand_over_route(c, rid, "stf_chef")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan(c, rid, "RES:R-002", "stf_run1")
        self.assertIn("不在当前送餐路线", str(cm.exception))

    def test_happy_path_delivers_with_two_scans(self, dishes=("d_pump", "d_rice", "d_pork")):
        bid, rid = self._setup()
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        for rid_name in ("r1", "r2", "r3"):
            box = self.w.box_for(rid_name)
            with transaction(self.c):
                rr = svc.scan(self.c, rid, "RES:" + self.w.resident_code(rid_name),
                              "stf_run1")
                self.assertEqual(rr["stage"], "resident_ok")
                result = svc.scan(self.c, rid, "BOX:" + self.w.active_token(box),
                                  "stf_run1", active_stop_id=rr["stop_id"])
            self.assertEqual(result["stage"], "delivered")
        # 路线全部完成
        self.assertEqual(self.c.execute(
            "SELECT status FROM routes WHERE id=?", (rid,)).fetchone()["status"],
            "completed")
        # 每位住户一条送达记录,双扫码时间戳齐全
        n_del = self.c.execute("SELECT COUNT(*) c FROM deliveries").fetchone()["c"]
        self.assertEqual(n_del, 3)
        bad_ts = self.c.execute(
            "SELECT COUNT(*) c FROM deliveries WHERE resident_scan_at IS NULL "
            "OR box_scan_at IS NULL").fetchone()["c"]
        self.assertEqual(bad_ts, 0)

    def test_wrong_box_to_wrong_resident_quarantines(self):
        """在 r1 处扫到 r2 的餐盒 → 错餐立即隔离,且不能交付。"""
        bid, rid = self._setup()
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        box_r2 = self.w.box_for("r2")
        token_r2 = self.w.active_token(box_r2)
        with transaction(self.c):
            rr = svc.scan(self.c, rid, "RES:" + self.w.resident_code("r1"), "stf_run1")
            sid = rr["stop_id"]
        with self.assertRaises(svc.DomainError) as cm:
            with transaction(self.c):
                svc.scan(self.c, rid, "BOX:" + token_r2, "stf_run1", active_stop_id=sid)
        self.assertIn("错餐", str(cm.exception))
        # r2 的盒子被隔离
        self.assertEqual(self.c.execute("SELECT status FROM boxes WHERE id=?",
                                        (box_r2,)).fetchone()["status"], "quarantined")
        inc = self.c.execute(
            "SELECT kind,severity,status FROM incidents WHERE box_id=?",
            (box_r2,)).fetchone()
        self.assertEqual(inc["kind"], "wrong_meal")
        self.assertEqual(inc["severity"], "critical")
        self.assertEqual(inc["status"], "quarantined")

    def test_box_not_on_route_rejected_without_quarantine(self):
        # 主路线(午餐)含 r1/r2/r3;另建一条只含 r3 的晚餐路线;
        # 在第二路线扫 r1 的餐盒 → 拒绝但不生成错餐隔离事件
        self._setup()
        c = self.c
        mid = self.w.published_menu(["d_pump"], meal_type="dinner")
        with transaction(c):
            bid2 = svc.produce_batch(c, mid, "stf_chef", resident_ids=["r3"])
            rt2 = svc.create_route_for_batch(c, bid2, "stf_run2")
            svc.hand_over_route(c, rt2, "stf_chef")
        with transaction(c):
            svc.scan(c, rt2, "RES:R-003", "stf_run2")
        # r1 的盒(在午餐路线)不在 rt2
        box_r1 = self.w.box_for("r1")
        sid3 = self.w.stop_for(rt2, "r3")
        n_before = c.execute("SELECT COUNT(*) c FROM incidents").fetchone()["c"]
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan(c, rt2, "BOX:" + self.w.active_token(box_r1), "stf_run2",
                     active_stop_id=sid3)
        self.assertIn("不在当前路线", str(cm.exception))
        self.assertEqual(c.execute("SELECT COUNT(*) c FROM incidents").fetchone()["c"], n_before)

    def test_old_label_rejected(self):
        bid, rid = self._setup()
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        box = self.w.box_for("r3")
        old = self.c.execute(
            "SELECT token FROM box_labels WHERE box_id=? ORDER BY version",
            (box,)).fetchone()["token"]
        with transaction(self.c):
            svc.substitute_meal(self.c, box, "d_pump", "stf_chef", "温度不合格")
        with transaction(self.c):
            rr = svc.scan(self.c, rid, "RES:" + self.w.resident_code("r3"), "stf_run1")
            with self.assertRaises(svc.DomainError) as cm:
                svc.scan(self.c, rid, "BOX:" + old, "stf_run1",
                         active_stop_id=rr["stop_id"])
        self.assertIn("旧标签", str(cm.exception))

    def test_unknown_code_raises(self):
        bid, rid = self._setup()
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        with self.assertRaises(ValueError):
            svc.scan(self.c, rid, "HTTPS://evil.example", "stf_run1")


if __name__ == "__main__":
    unittest.main()
