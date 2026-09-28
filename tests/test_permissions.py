"""字段权限与双人确认测试：角色动作权限、档案字段白名单、第二人确认、交付只追加。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_helper import fresh_db  # noqa: E402

import services as svc  # noqa: E402


class RolePermissionTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_role_matrix(self):
        # 送餐员
        self.assertTrue(svc.can("courier", "scan.box"))
        self.assertTrue(svc.can("courier", "box.quarantine"))
        self.assertFalse(svc.can("courier", "profile.propose"))
        self.assertFalse(svc.can("courier", "menu.review"))
        self.assertFalse(svc.can("courier", "batch.create"))
        # 护理员
        self.assertTrue(svc.can("nurse", "profile.propose"))
        self.assertTrue(svc.can("nurse", "family.handle"))
        self.assertFalse(svc.can("nurse", "menu.review"))
        self.assertFalse(svc.can("nurse", "batch.create"))
        # 厨房
        self.assertTrue(svc.can("kitchen", "batch.create"))
        self.assertTrue(svc.can("kitchen", "box.substitute"))
        self.assertFalse(svc.can("kitchen", "menu.review"))
        self.assertFalse(svc.can("kitchen", "profile.propose"))
        # 营养师
        self.assertTrue(svc.can("nutritionist", "menu.review"))
        self.assertTrue(svc.can("nutritionist", "dish.review"))
        self.assertFalse(svc.can("nutritionist", "batch.create"))
        # 管理员全权
        self.assertTrue(svc.can("manager", "anything"))

    def test_courier_cannot_propose_profile_change(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.propose_profile_change(self.conn, "S05", "R005",
                                       {"chewing": "soft"}, "测试")
        self.assertEqual(cm.exception.code, "forbidden")

    def test_kitchen_cannot_review_menu(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.review_menu(self.conn, "S04", "M-LUNCH", True)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_nutritionist_cannot_create_batch(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.generate_batch(self.conn, "S02", "M-DINNER")
        self.assertEqual(cm.exception.code, "forbidden")


class ProfileTwoPersonTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_propose_then_self_confirm_blocked(self):
        cid = svc.propose_profile_change(self.conn, "S03", "R005",
                                         {"chewing": "soft"}, "医嘱")
        with self.assertRaises(svc.DomainError) as cm:
            svc.confirm_profile_change(self.conn, cid, "S03", True)
        self.assertEqual(cm.exception.code, "second_eye")
        # 未生效
        self.assertEqual(svc.resident(self.conn, "R005")["chewing"], "normal")
        self.assertEqual(svc.resident(self.conn, "R005")["card_status"], "needs_confirm")

    def test_second_person_confirm_applies_patch(self):
        cid = svc.propose_profile_change(self.conn, "S03", "R005",
                                         {"chewing": "soft",
                                          "avoidances": ["内脏", "油腻", "海鲜"]},
                                         "复查调整")
        out = svc.confirm_profile_change(self.conn, cid, "S01", True, "已核对")
        self.assertEqual(out["status"], "confirmed")
        r = svc.resident(self.conn, "R005")
        self.assertEqual(r["chewing"], "soft")
        self.assertIn("海鲜", svc.resident_view(r)["avoidances"])
        self.assertEqual(r["card_status"], "active")

    def test_reject_keeps_old_values(self):
        cid = svc.propose_profile_change(self.conn, "S03", "R005",
                                         {"chewing": "pureed"}, "误报")
        svc.confirm_profile_change(self.conn, cid, "S01", False, "无需调整")
        self.assertEqual(svc.resident(self.conn, "R005")["chewing"], "normal")

    def test_double_confirm_rejected(self):
        cid = svc.propose_profile_change(self.conn, "S03", "R005",
                                         {"chewing": "soft"}, "x")
        svc.confirm_profile_change(self.conn, cid, "S01", True)
        # 另一名有确认权限的护理员再次确认 -> 状态已处理
        with self.assertRaises(svc.DomainError) as cm:
            svc.confirm_profile_change(self.conn, cid, "S03", True)
        self.assertEqual(cm.exception.code, "state")

    def test_reason_required(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.propose_profile_change(self.conn, "S03", "R005",
                                       {"chewing": "soft"}, "  ")
        self.assertEqual(cm.exception.code, "validation")

    def test_field_whitelist(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.propose_profile_change(self.conn, "S03", "R005",
                                       {"id": "R999", "alias": "hacker"}, "x")
        self.assertEqual(cm.exception.code, "field_permission")

    def test_invalid_allergy_level_rejected(self):
        with self.assertRaises(svc.DomainError) as cm:
            svc.propose_profile_change(self.conn, "S03", "R005",
                                       {"allergies": [{"allergen": "鱼",
                                                       "level": "catastrophic"}]},
                                       "x")
        self.assertEqual(cm.exception.code, "validation")

    def test_allergy_change_takes_effect_and_blocks_dishes(self):
        # 给原本无过敏的 R005 增加虾严重过敏，确认后 D05 必须被拦截
        cid = svc.propose_profile_change(
            self.conn, "S03", "R005",
            {"allergies": [{"allergen": "虾", "level": "severe", "note": "新发"}]},
            "复查新发过敏")
        svc.confirm_profile_change(self.conn, cid, "S01", True)
        with self.assertRaises(svc.DomainError) as cm:
            svc.assert_safe_dishes(self.conn, ["D05"],
                                   svc.resident(self.conn, "R005"))
        self.assertEqual(cm.exception.code, "allergy_block")

    def test_courier_cannot_confirm_profile_change(self):
        cid = svc.propose_profile_change(self.conn, "S03", "R005",
                                         {"chewing": "soft"}, "x")
        with self.assertRaises(svc.DomainError) as cm:
            svc.confirm_profile_change(self.conn, cid, "S05", True)
        self.assertEqual(cm.exception.code, "forbidden")


class FamilyFlowTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _pending_hold(self):
        return self.conn.execute(
            "SELECT id FROM family_adjustments WHERE resident_id='R007' "
            "AND status='requested'").fetchone()["id"]

    def test_fulfill_before_confirm_rejected(self):
        fa = self._pending_hold()
        with self.assertRaises(svc.DomainError) as cm:
            svc.fulfill_family_hold(self.conn, "S03", fa, "未确认就执行")
        self.assertEqual(cm.exception.code, "state")

    def test_courier_cannot_handle_family(self):
        fa = self._pending_hold()
        with self.assertRaises(svc.DomainError) as cm:
            svc.handle_family_adjustment(self.conn, "S05", fa, True)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_hold_flow_confirms_then_voids_box(self):
        fa = self._pending_hold()
        svc.handle_family_adjustment(self.conn, "S03", fa, True, "电话复核")
        svc.fulfill_family_hold(self.conn, "S03", fa, "外出就诊")
        self.assertEqual(
            self.conn.execute("SELECT status FROM family_adjustments WHERE id=?",
                              (fa,)).fetchone()["status"], "fulfilled")
        box = self.conn.execute(
            """SELECT mb.status FROM meal_boxes mb JOIN batches b ON b.id=mb.batch_id
               WHERE mb.resident_id='R007' AND b.meal_session='dinner'
               ORDER BY mb.created_at DESC LIMIT 1""").fetchone()
        self.assertEqual(box["status"], "void")

    def test_swap_flow_runs_substitution_rules(self):
        # 新建一条 requested 的换餐（R002：虾过敏），确认后换菜必须过敏拦截
        import uuid
        from seed import TODAY
        fa = "FA-" + uuid.uuid4().hex[:10]
        self.conn.execute(
            """INSERT INTO family_adjustments(id,resident_id,family_name,contact_code,
                   serve_date,meal_session,change_type,detail,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (fa, "R002", "家属·A002", "FAM-X", TODAY, "dinner", "swap",
             "要求加一份虾", "requested", svc.now()))
        self.conn.commit()
        svc.handle_family_adjustment(self.conn, "S03", fa, True)
        with self.assertRaises(svc.DomainError) as cm:
            svc.fulfill_family_swap(self.conn, "S03", fa, ["D05"])  # 虾菜
        self.assertEqual(cm.exception.code, "allergy_block")
        # 换成安全菜成功（全部已审核、无虾、符合软质/低盐），旧标签作废
        new_id = svc.fulfill_family_swap(self.conn, "S03", fa,
                                         ["D01", "D04", "D08"])
        self.assertTrue(svc.box(self.conn, new_id)["is_substitution"])


if __name__ == "__main__":
    unittest.main()
