"""路线交接与双码交付测试：交接码、扫描顺序、重复扫描、错配隔离、拒餐、完成门禁。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_helper import fresh_db  # noqa: E402

import services as svc  # noqa: E402
import qradapter  # noqa: E402

R1 = "RT20260928-D1"   # 种子：在途，送餐员甲(S05)，1 号站已交付
R2 = "RT20260928-D2"   # 种子：planned，送餐员乙(S06)


def stop(conn, route_id, seq):
    return conn.execute("SELECT * FROM route_stops WHERE route_id=? AND seq=?",
                        (route_id, seq)).fetchone()


class RouteHandoverTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_handover_requires_route_qr(self):
        # 路线码与当前路线不符 -> mismatch
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_route_code(self.conn, "S05", R1, qradapter.build_payload("ROUTE", "RT-OTHER"))
        self.assertEqual(cm.exception.code, "mismatch")
        # 非 ROUTE 载荷 -> wrong_kind
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_route_code(self.conn, "S05", R1, "RES:A001")
        self.assertEqual(cm.exception.code, "wrong_kind")
        # 正确路线码
        self.assertTrue(svc.scan_route_code(self.conn, "S05", R1,
                                            qradapter.build_payload("ROUTE", R1))["ok"])

    def test_handover_changes_courier(self):
        svc.scan_route_code(self.conn, "S05", R1, qradapter.build_payload("ROUTE", R1))
        svc.handover_route(self.conn, "S05", R1, "S06")
        rt = svc.route(self.conn, R1)
        self.assertEqual(rt["courier_id"], "S06")
        self.assertEqual(rt["handover_from"], "S05")
        self.assertEqual(rt["handover_to"], "S06")
        self.assertEqual(rt["status"], "in_transit")

    def test_handover_to_self_rejected(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.handover_route(self.conn, "S05", R1, "S05")
        self.assertEqual(cm.exception.code, "validation")

    def test_handover_to_non_courier_rejected(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.handover_route(self.conn, "S05", R1, "S02")  # 营养师不是送餐员
        self.assertEqual(cm.exception.code, "validation")

    def test_planned_route_start(self):
        svc.start_route(self.conn, "S06", R2)
        self.assertEqual(svc.route(self.conn, R2)["status"], "in_transit")

    def test_cannot_scan_before_route_started(self):
        s = stop(self.conn, R2, 1)
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S06", s["id"], "RES:A005")
        self.assertEqual(cm.exception.code, "state")


class ScanDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _stop2(self):
        return stop(self.conn, R1, 2)  # R002 / A002 / 盒 0002

    def test_box_before_resident_wrong_order(self):
        s = self._stop2()
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0002")
        self.assertEqual(cm.exception.code, "wrong_order")

    def test_full_two_scan_delivery(self):
        s = self._stop2()
        r1 = svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        self.assertEqual(r1["stage"], "resident")
        r2 = svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0002")
        self.assertEqual(r2["stage"], "done")
        s2 = stop(self.conn, R1, 2)
        self.assertEqual(s2["status"], "delivered")
        self.assertEqual(svc.box(self.conn, s2["meal_box_id"])["status"], "delivered")

    def test_duplicate_resident_scan_is_logged_not_error(self):
        s = self._stop2()
        svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        r = svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")  # 重扫
        self.assertTrue(r.get("duplicate"))
        cnt = self.conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE stop_id=? AND result='duplicate'",
            (s["id"],)).fetchone()["c"]
        self.assertGreaterEqual(cnt, 1)

    def test_duplicate_scan_after_delivery_rejected_but_logged(self):
        s = self._stop2()
        svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0002")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0002")
        self.assertEqual(cm.exception.code, "duplicate")

    def test_wrong_resident_code_blocks_and_marks_exception(self):
        # 站点2 应扫 A002，错扫 A001
        s = self._stop2()
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A001")
        self.assertEqual(cm.exception.code, "mismatch")
        self.assertEqual(stop(self.conn, R1, 2)["status"], "exception")
        ev = self.conn.execute(
            "SELECT * FROM meal_events WHERE type='wrong_meal' AND status='open' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertIsNotNone(ev)

    def test_wrong_box_is_quarantined(self):
        # 站点2 完成住户确认后，错扫别人的餐盒（盒 0005 属于 R005）
        s = self._stop2()
        svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0005")
        self.assertEqual(cm.exception.code, "mismatch")
        wrong = svc.box_by_label(self.conn, "20260928-D-0005")
        self.assertEqual(wrong["status"], "quarantined")
        self.assertEqual(stop(self.conn, R1, 2)["status"], "exception")

    def test_void_label_rejected(self):
        s = self._stop2()
        svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0000")
        self.assertEqual(cm.exception.code, "void_label")

    def test_quarantined_box_cannot_be_delivered(self):
        # 先隔离盒 0005，再尝试在任何站点交付它 -> quarantined
        svc.quarantine_box(self.conn, "S05",
                           svc.box_by_label(self.conn, "20260928-D-0005")["id"],
                           "测试隔离")
        s = self._stop2()
        svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0005")
        self.assertEqual(cm.exception.code, "quarantined")

    def test_unknown_label_rejected(self):
        s = self._stop2()
        svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:NOT-EXIST")
        self.assertEqual(cm.exception.code, "unknown")

    def test_bad_qr_payload_rejected(self):
        s = self._stop2()
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", s["id"], "garbage")
        self.assertEqual(cm.exception.code, "bad_qr")

    def test_refuse_stop_records_event(self):
        s = self._stop2()
        svc.refuse_stop(self.conn, "S05", s["id"], "住户无胃口")
        self.assertEqual(stop(self.conn, R1, 2)["status"], "refused")
        ev = self.conn.execute(
            "SELECT COUNT(*) c FROM meal_events WHERE type='refused'").fetchone()["c"]
        self.assertGreaterEqual(ev, 1)

    def test_complete_route_blocked_with_pending_stops(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.complete_route(self.conn, "S05", R1)
        self.assertEqual(cm.exception.code, "state")

    def test_complete_route_after_all_stops_handled(self):
        # 站点 2、3、4 全部拒餐（1 已交付），随后可完成路线
        for seq in (2, 3, 4):
            s = stop(self.conn, R1, seq)
            svc.refuse_stop(self.conn, "S05", s["id"], "测试")
        svc.complete_route(self.conn, "S05", R1)
        self.assertEqual(svc.route(self.conn, R1)["status"], "completed")

    def test_delivery_notes_append_only(self):
        s = self._stop2()
        svc.scan_at_stop(self.conn, "S05", s["id"], "RES:A002")
        svc.scan_at_stop(self.conn, "S05", s["id"], "MEAL:20260928-D-0002")
        d = self.conn.execute("SELECT * FROM deliveries WHERE stop_id=?",
                              (s["id"],)).fetchone()
        svc.append_delivery_note(self.conn, "S03", d["id"], "家属已知悉")
        svc.append_delivery_note(self.conn, "S05", d["id"], "补充：老人反馈良好")
        notes = svc.delivery_notes(self.conn, d["id"])
        self.assertEqual(len(notes), 2)
        with self.assertRaises(svc.DomainError):
            svc.append_delivery_note(self.conn, "S03", d["id"], "  ")
        # 原始交付结果字段不可被追加接口改动
        self.assertEqual(
            self.conn.execute("SELECT result FROM deliveries WHERE id=?",
                              (d["id"],)).fetchone()["result"], "delivered")


if __name__ == "__main__":
    unittest.main()
