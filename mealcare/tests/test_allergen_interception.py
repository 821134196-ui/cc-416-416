"""过敏拦截测试:配餐自动绕开过敏菜、咀嚼不符拦截、替换餐过敏复核。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services as svc
from app.db import transaction
from tests.world import World


class TestAllergenInterception(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn

    def tearDown(self):
        self.w.close()

    def test_check_dish_flags_known_allergen(self):
        r = svc.get_resident(self.c, "r1")  # 花生(速发)、虾(严重)
        chicken = dict(self.c.execute("SELECT * FROM dishes WHERE id='d_chicken'").fetchone())
        reasons = svc.check_dish_for_resident(self.c, r, chicken)
        self.assertTrue(any("过敏拦截" in x and "花生" in x for x in reasons))

    def test_severe_allergy_blocks_shrimp_for_r1(self):
        r = svc.get_resident(self.c, "r1")
        shrimp = dict(self.c.execute("SELECT * FROM dishes WHERE id='d_shrimp'").fetchone())
        self.assertTrue(any("虾" in x for x in svc.check_dish_for_resident(self.c, r, shrimp)))

    def test_texture_requirement_blocks(self):
        # r3 需糊状:软食/碎餐都不满足
        r = svc.get_resident(self.c, "r3")
        for did in ("d_chicken", "d_pork"):
            d = dict(self.c.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone())
            self.assertTrue(any("咀嚼要求不符" in x
                                for x in svc.check_dish_for_resident(self.c, r, d)),
                            f"{did} 应被咀嚼要求拦截")
        pump = dict(self.c.execute("SELECT * FROM dishes WHERE id='d_pump'").fetchone())
        self.assertEqual(svc.check_dish_for_resident(self.c, r, pump), [])

    def test_assign_dish_avoids_allergen_in_order(self):
        r = svc.get_resident(self.c, "r1")  # 花生+虾过敏,普通质地
        dishes = svc.menu_dishes if False else [
            dict(x) for x in self.c.execute(
                "SELECT * FROM dishes WHERE id IN ('d_chicken','d_shrimp','d_pork','d_rice')")]
        # 前两道分别含花生/虾被拦截 → 选到无过敏的肉末冬瓜
        chosen = svc.assign_dish(self.c, r, dishes)
        self.assertEqual(chosen["id"], "d_pork")

    def test_assign_dish_raises_when_nothing_edible(self):
        r = svc.get_resident(self.c, "r3")  # 糊状 + 大豆严重过敏
        only_bad = [dict(x) for x in self.c.execute(
            "SELECT * FROM dishes WHERE id IN ('d_chicken','d_pork','d_rice')")]
        with self.assertRaises(svc.DomainError) as cm:
            svc.assign_dish(self.c, r, only_bad)
        self.assertIn("无可用菜品", str(cm.exception))

    def test_produced_boxes_never_contain_resident_allergen(self):
        """端到端:批次配餐后,任何餐盒菜品都不得含该住户过敏原。"""
        bid, _ = self.w.batch_and_route(
            ["d_chicken", "d_shrimp", "d_pork", "d_egg", "d_pump", "d_rice"])
        bad = self.c.execute("""
            SELECT r.alias FROM boxes b
            JOIN residents r ON r.id=b.resident_id
            JOIN dishes d ON d.id=b.dish_id
            JOIN allergens a ON a.resident_id=r.id
            WHERE b.batch_id=?
              AND (';'||REPLACE(d.contains_allergens,'，',';')||';') LIKE '%'||a.name||'%'
        """, (bid,)).fetchall()
        self.assertEqual([dict(x) for x in bad], [], "存在过敏配餐!")

    def test_substitution_with_allergen_dish_is_blocked(self):
        bid, rid = self.w.batch_and_route(["d_pump", "d_rice", "d_chicken"])
        # r1(花生过敏)的盒子无论分到什么,尝试换成含花生的 d_chicken 必须被拦截
        box = self.w.box_for("r1")
        with self.assertRaises(svc.DomainError) as cm:
            with transaction(self.c):
                svc.substitute_meal(self.c, box, "d_chicken", "stf_chef", "测试")
        self.assertIn("过敏拦截", str(cm.exception))

    def test_substitution_with_unreviewed_dish_is_blocked(self):
        bid, rid = self.w.batch_and_route(["d_pump", "d_rice"])
        box = self.w.box_for("r3")
        with self.assertRaises(svc.DomainError) as cm:
            with transaction(self.c):
                svc.substitute_meal(self.c, box, "d_draft", "stf_chef", "测试")
        self.assertIn("未通过营养审核", str(cm.exception))

    def test_safe_substitution_succeeds_and_updates_snapshot(self):
        bid, rid = self.w.batch_and_route(["d_rice", "d_pump"])
        box = self.w.box_for("r3")  # 糊状
        with transaction(self.c):
            label = svc.substitute_meal(self.c, box, "d_pump", "stf_chef",
                                        "温度低,换发热的南瓜糊")
        self.assertEqual(label["version"], 2)
        # 盒上菜品确为南瓜糊
        self.assertEqual(self.c.execute(
            "SELECT d.id FROM boxes b JOIN dishes d ON d.id=b.dish_id WHERE b.id=?",
            (box,)).fetchone()["id"], "d_pump")


if __name__ == "__main__":
    unittest.main()
