"""过敏拦截测试：严重过敏、慎食级别、忌口、咀嚼质地、特殊餐、替换餐与菜单审核门禁。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_helper import fresh_db  # noqa: E402

import services as svc  # noqa: E402
from db import jloads  # noqa: E402


class AllergyInterceptionTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_severe_allergy_blocks_dish(self):
        # R001 花生严重过敏；D11 花生糊必须拦截
        r = svc.resident(self.conn, "R001")
        with self.assertRaises(svc.DomainError) as cm:
            svc.assert_safe_dishes(self.conn, ["D11"], r)
        self.assertEqual(cm.exception.code, "allergy_block")
        self.assertIn("花生", str(cm.exception))

    def test_severe_shrimp_blocks_for_resident(self):
        # R002 虾蟹严重过敏；D05 冬瓜虾仁必须拦截
        r = svc.resident(self.conn, "R002")
        with self.assertRaises(svc.DomainError) as cm:
            svc.assert_safe_dishes(self.conn, ["D05"], r)
        self.assertIn("虾", str(cm.exception))

    def test_warning_level_blocks_by_default_but_relaxable(self):
        # R003 牛奶 warning；D12 同时存在质地问题（匀浆 vs 软食，硬拦截）。
        # 这里把视图质地放宽为 soft，单独验证 warning 的默认拦截/可放行语义。
        r = svc.resident_view(svc.resident(self.conn, "R003"))
        r["chewing"] = "soft"
        r["chewing_name"] = "软食"
        with self.assertRaises(svc.DomainError):
            svc.assert_safe_dishes(self.conn, ["D12"], r)
        svc.assert_safe_dishes(self.conn, ["D12"], r, allow_warning=True)  # 不抛异常

    def test_avoidance_tag_blocks(self):
        # R003 忌口"辛辣"；D07 麻婆豆腐带辛辣标签
        r = svc.resident(self.conn, "R003")
        with self.assertRaises(svc.DomainError) as cm:
            svc.assert_safe_dishes(self.conn, ["D07"], r)
        self.assertIn("辛辣", str(cm.exception))

    def test_texture_mismatch_blocks(self):
        # R003 需要匀浆；普通质地的 D16 白灼青菜不符
        r = svc.resident(self.conn, "R003")
        with self.assertRaises(svc.DomainError) as cm:
            svc.assert_safe_dishes(self.conn, ["D16"], r)
        self.assertIn("质地", str(cm.exception))
        # 更细软的 D09 南瓜泥（匀浆）通过
        svc.assert_safe_dishes(self.conn, ["D09"], r)

    def test_minced_resident_rejects_normal_and_accepts_softer(self):
        r = svc.resident(self.conn, "R004")  # 碎末
        with self.assertRaises(svc.DomainError):
            svc.assert_safe_dishes(self.conn, ["D16"], r)  # normal 不符
        svc.assert_safe_dishes(self.conn, ["D04"], r)       # minced 鸡丝通过

    def test_diabetic_warning(self):
        # R004 糖尿病餐；无糖尿病适餐标签的 D10 应给出 warning（默认拦截）
        r = svc.resident(self.conn, "R004")
        with self.assertRaises(svc.DomainError) as cm:
            svc.assert_safe_dishes(self.conn, ["D10"], r)
        self.assertIn("糖尿病", str(cm.exception))

    def test_egg_allergy_dessert_blocked_safe_dish_ok(self):
        # R004 鸡蛋严重过敏且需碎末/糖尿病：D06 番茄炒蛋拦截；
        # D04 胡萝卜炖鸡丝（碎末、糖尿病适餐、无蛋）安全
        r = svc.resident(self.conn, "R004")
        with self.assertRaises(svc.DomainError):
            svc.assert_safe_dishes(self.conn, ["D06"], r)
        svc.assert_safe_dishes(self.conn, ["D04"], r)

    def test_mango_peanut_double_allergy(self):
        # R007 芒果 severe + 花生 warning
        r = svc.resident(self.conn, "R007")
        with self.assertRaises(svc.DomainError) as cm:
            svc.assert_safe_dishes(self.conn, ["D14"], r)  # 芒果布丁
        self.assertIn("芒果", str(cm.exception))
        with self.assertRaises(svc.DomainError):
            svc.assert_safe_dishes(self.conn, ["D11"], r)  # 花生糊

    def test_substitution_with_allergen_blocked(self):
        box = self.conn.execute(
            "SELECT id FROM meal_boxes WHERE resident_id='R001' AND status!='void' LIMIT 1"
        ).fetchone()["id"]
        with self.assertRaises(svc.DomainError) as cm:
            svc.substitute_box(self.conn, "S04", box, ["D11"], "测试花生")
        self.assertEqual(cm.exception.code, "allergy_block")
        # 被拦截后原餐盒仍为正常状态
        self.assertNotEqual(svc.box(self.conn, box)["status"], "void")

    def test_substitution_requires_approved_dish(self):
        box = self.conn.execute(
            "SELECT id FROM meal_boxes WHERE resident_id='R001' AND status!='void' LIMIT 1"
        ).fetchone()["id"]
        with self.assertRaises(svc.DomainError) as cm:
            svc.substitute_box(self.conn, "S04", box, ["D17"], "未审核菜")
        self.assertEqual(cm.exception.code, "not_reviewed")

    def test_batch_generation_requires_published_menu(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.generate_batch(self.conn, "S04", "M-LUNCH")
        self.assertEqual(cm.exception.code, "not_reviewed")

    def test_published_menu_blocks_later_unapproved_item(self):
        # 已发布晚餐菜单不能再加入任何菜（即便已审核）
        with self.assertRaises(svc.DomainError) as cm:
            svc.add_menu_item(self.conn, "S04", "M-DINNER", "D13")
        self.assertEqual(cm.exception.code, "state")

    def test_safety_report_flags_affected_residents(self):
        report = svc.menu_safety_report(self.conn, "M-DINNER")
        # 晚餐含 D03 鱼；没有鱼过敏住户，但报告结构应可解析
        self.assertIn("allergens", report)
        self.assertIn("affected", report)
        aliases = {a["resident"] for a in report["affected"]}
        # 无人对鱼严重过敏，但若有质地问题（R003 匀浆 vs 软食）应出现
        r3 = svc.resident_view(svc.resident(self.conn, "R003"))
        # D03 清蒸鲈鱼 soft，R003 需 pureed -> 质地不符，应被报告
        self.assertIn(r3["alias"], aliases)

    def test_batch_auto_plan_is_allergy_safe(self):
        # 审核并发布午餐菜单后生成批次，所有餐盒必须通过该住户的安全检查
        svc.review_menu(self.conn, "S02", "M-LUNCH", True, "审核")
        svc.generate_batch(self.conn, "S04", "M-LUNCH")
        boxes = self.conn.execute(
            """SELECT mb.* FROM meal_boxes mb JOIN batches b ON b.id=mb.batch_id
               WHERE b.menu_id='M-LUNCH' AND mb.status!='void'""").fetchall()
        self.assertGreaterEqual(len(boxes), 8)
        for b in boxes:
            rv = svc.resident_view(svc.resident(self.conn, b["resident_id"]))
            # 逐盒校验：不允许任何严重过敏/质地问题（warning 也不允许）
            svc.assert_safe_dishes(self.conn, jloads(b["dishes"], []), rv)


if __name__ == "__main__":
    unittest.main()
