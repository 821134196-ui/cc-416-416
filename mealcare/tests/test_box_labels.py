"""餐盒标签唯一性测试:全局唯一、替换换签、旧签作废不可继续使用。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services as svc
from app.db import transaction
from tests.world import World


class TestBoxLabels(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn

    def tearDown(self):
        self.w.close()

    def test_every_box_gets_unique_active_label(self):
        bid, rid = self.w.batch_and_route(["d_pump", "d_rice", "d_pork"])
        tokens = [r["token"] for r in self.c.execute(
            "SELECT token FROM box_labels WHERE status='active'")]
        self.assertEqual(len(tokens), len(set(tokens)), "标签号必须全局唯一")
        # 数据库层面也有 UNIQUE 约束
        self.assertGreaterEqual(len(tokens), 3)

    def test_label_token_unique_constraint(self):
        bid, rid = self.w.batch_and_route(["d_pump"])
        tok = self.c.execute("SELECT token FROM box_labels LIMIT 1").fetchone()["token"]
        with self.assertRaises(Exception):
            with transaction(self.c):
                self.c.execute(
                    "INSERT INTO box_labels(id,token,box_id,version,status,created_at)"
                    " VALUES('dup',?,(SELECT id FROM boxes LIMIT 1),99,'active',?)",
                    (tok, "2026-09-28T10:00:00"))

    def test_substitution_voids_old_and_issues_new(self):
        bid, rid = self.w.batch_and_route(["d_rice", "d_pump"])
        box = self.w.box_for("r3")
        old = svc.active_label(self.c, box)
        self.assertEqual(old["version"], 1)
        with transaction(self.c):
            new = svc.substitute_meal(self.c, box, "d_pump", "stf_chef", "替换测试")
        # 旧签作废
        old_status = self.c.execute("SELECT status,voided_at FROM box_labels WHERE id=?",
                                    (old["id"],)).fetchone()
        self.assertEqual(old_status["status"], "void")
        self.assertIsNotNone(old_status["voided_at"])
        # 新签生效且号不同
        self.assertNotEqual(old["token"], new["token"])
        active = svc.active_label(self.c, box)
        self.assertEqual(active["token"], new["token"])
        self.assertEqual(active["version"], 2)
        # 同一餐盒只有一张有效签
        n = self.c.execute(
            "SELECT COUNT(*) c FROM box_labels WHERE box_id=? AND status='active'",
            (box,)).fetchone()["c"]
        self.assertEqual(n, 1)
        # 替换留痕
        self.assertEqual(self.c.execute(
            "SELECT COUNT(*) c FROM substitutions WHERE box_id=?", (box,)).fetchone()["c"], 1)

    def test_only_approved_dish_can_replace(self):
        bid, rid = self.w.batch_and_route(["d_pump", "d_rice"])
        box = self.w.box_for("r1")
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.substitute_meal(self.c, box, "d_draft", "stf_chef", "x")

    def test_delivered_box_cannot_be_substituted(self):
        bid, rid = self.w.batch_and_route(["d_pump", "d_rice"])
        with transaction(self.c):
            svc.hand_over_route(self.c, rid, "stf_chef")
        box = self.w.box_for("r1")
        sid = self.w.stop_for(rid, "r1")
        with transaction(self.c):
            r = svc.scan(self.c, rid, "RES:" + self.w.resident_code("r1"), "stf_run1")
            svc.scan(self.c, rid, "BOX:" + self.w.active_token(box), "stf_run1",
                     active_stop_id=r["stop_id"])
        with self.assertRaises(svc.DomainError) as cm:
            with transaction(self.c):
                svc.substitute_meal(self.c, box, "d_pork", "stf_chef", "x")
        self.assertIn("已送达", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
