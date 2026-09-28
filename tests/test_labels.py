"""餐盒标签唯一性测试：编号唯一、替换发新标签、旧标签立即作废且不可用。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_helper import fresh_db  # noqa: E402

import services as svc  # noqa: E402
from db import jloads  # noqa: E402


def any_active_box(conn, resident_id):
    return conn.execute(
        "SELECT * FROM meal_boxes WHERE resident_id=? AND status!='void' "
        "ORDER BY created_at DESC LIMIT 1", (resident_id,)).fetchone()


class LabelUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_label_codes_unique_in_seed(self):
        dup = self.conn.execute(
            "SELECT label_code FROM meal_boxes GROUP BY label_code HAVING COUNT(*)>1"
        ).fetchall()
        self.assertEqual(dup, [])

    def test_substitution_creates_new_unique_label_and_voids_old(self):
        old = any_active_box(self.conn, "R001")
        new_id = svc.substitute_box(self.conn, "S04", old["id"],
                                    ["D01", "D04", "D16", "D10", "D13"], "测试替换")
        new = svc.box(self.conn, new_id)
        old2 = svc.box(self.conn, old["id"])
        self.assertNotEqual(new["label_code"], old["label_code"])
        self.assertEqual(old2["status"], "void")
        self.assertTrue(new["is_substitution"])
        self.assertEqual(new["replaces_box_id"], old["id"])
        # 旧标签不能继续用于交付（种子路线1已处于配送中）
        stop = self.conn.execute(
            "SELECT id, route_id FROM route_stops WHERE meal_box_id=? ORDER BY seq LIMIT 1",
            (new_id,)).fetchone()
        svc.scan_at_stop(self.conn, "S05", stop["id"], "RES:A001")
        with self.assertRaises(svc.DomainError) as cm:
            svc.scan_at_stop(self.conn, "S05", stop["id"],
                             f"MEAL:{old['label_code']}")
        self.assertEqual(cm.exception.code, "void_label")

    def test_double_void_rejected(self):
        old = any_active_box(self.conn, "R002")
        svc.void_box(self.conn, "S04", old["id"], "人为作废")
        with self.assertRaises(svc.DomainError) as cm:
            svc.void_box(self.conn, "S04", old["id"], "再次作废")
        self.assertEqual(cm.exception.code, "state")

    def test_generated_batch_labels_all_unique(self):
        svc.review_menu(self.conn, "S02", "M-LUNCH", True, "ok")
        bid = svc.generate_batch(self.conn, "S04", "M-LUNCH")
        codes = [r["label_code"] for r in svc.batch_boxes(self.conn, bid)]
        self.assertEqual(len(codes), len(set(codes)))
        # 与晚餐批次也不冲突（同日不同餐次前缀不同，全局仍唯一）
        allcodes = [r["label_code"] for r in self.conn.execute(
            "SELECT label_code FROM meal_boxes").fetchall()]
        self.assertEqual(len(allcodes), len(set(allcodes)))

    def test_substitution_relinks_route_stop(self):
        old = any_active_box(self.conn, "R001")
        new_id = svc.substitute_box(self.conn, "S04", old["id"],
                                    ["D01", "D04", "D16", "D13"], "换绑测试")
        stop = self.conn.execute(
            "SELECT * FROM route_stops WHERE resident_id='R001'").fetchone()
        self.assertEqual(stop["meal_box_id"], new_id)
        self.assertIsNone(stop["resident_scan_at"])  # 换绑后扫描状态重置

    def test_seed_void_label_exists(self):
        # 种子自带一张作废标签
        row = self.conn.execute(
            "SELECT * FROM meal_boxes WHERE label_code LIKE '%-0000'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "void")
        self.assertIn("作废", row["void_reason"])


if __name__ == "__main__":
    unittest.main()
