"""字段权限与记录不可变测试:角色-动作矩阵、双人确认、送达记录只追加。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services as svc
from app.db import transaction
from tests.world import World


class TestFieldPermissions(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn

    def tearDown(self):
        self.w.close()

    def test_role_permission_matrix(self):
        # (动作, 允许角色, 拒绝角色)
        cases = [
            ("dish.edit_nutrition", ["stf_nutr"], ["stf_mgr", "stf_chef", "stf_run1"]),
            ("dish.review", ["stf_nutr"], ["stf_chef", "stf_run1"]),
            ("menu.review", ["stf_nutr"], ["stf_chef", "stf_run1", "stf_mgr"]),
            ("batch.produce", ["stf_chef"], ["stf_nutr", "stf_run1"]),
            ("route.handover", ["stf_chef"], ["stf_run1", "stf_nutr"]),
            ("route.deliver", ["stf_run1", "stf_run2"], ["stf_chef", "stf_nutr", "stf_mgr"]),
            ("family.confirm", ["stf_mgr", "stf_nutr"], ["stf_chef", "stf_run1"]),
            ("profile.request", ["stf_mgr", "stf_nutr"], ["stf_chef", "stf_run1"]),
        ]
        for perm, allowed, denied in cases:
            for sid in allowed:
                self.assertIn(svc.get_staff(self.c, sid)["role"],
                              svc.PERMISSIONS[perm], f"{sid} 应允许 {perm}")
            for sid in denied:
                self.assertNotIn(svc.get_staff(self.c, sid)["role"],
                                 svc.PERMISSIONS[perm], f"{sid} 不应允许 {perm}")

    def test_courier_cannot_review_dish(self):
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.review_dish(self.c, "d_draft", "stf_run1", True)

    def test_dietitian_review_changes_status(self):
        with transaction(self.c):
            svc.review_dish(self.c, "d_draft", "stf_nutr", True, "可上架")
        self.assertEqual(self.c.execute(
            "SELECT review_status FROM dishes WHERE id='d_draft'").fetchone()[0],
            "approved")

    def test_direct_review_status_field_edit_blocked(self):
        with self.assertRaises(svc.DomainError) as cm:
            with transaction(self.c):
                svc.update_dish_field(self.c, "d_rice", "review_status",
                                      "approved", "stf_nutr")
        self.assertIn("审核状态必须通过", str(cm.exception))

    def test_kitchen_cannot_edit_nutrition_fields(self):
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.update_dish_field(self.c, "d_rice", "kcal", 999, "stf_chef")

    def test_dietitian_can_edit_kcal(self):
        with transaction(self.c):
            svc.update_dish_field(self.c, "d_rice", "kcal", 321, "stf_nutr")
        self.assertEqual(self.c.execute(
            "SELECT kcal FROM dishes WHERE id='d_rice'").fetchone()[0], 321)

    def test_menu_cannot_publish_without_review(self):
        with transaction(self.c):
            mid = svc.create_menu(self.c, "2026-09-28", "dinner", "stf_chef")
            svc.add_menu_item(self.c, mid, "d_rice")
        # 未审核菜品不能加进菜单
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.add_menu_item(self.c, mid, "d_draft")
        with transaction(self.c):
            svc.submit_menu(self.c, mid)
        # 草稿状态不可重复提交
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.submit_menu(self.c, mid)
        # 营养师审核通过发布
        with transaction(self.c):
            svc.review_menu(self.c, mid, "stf_nutr", True)
        self.assertEqual(self.c.execute("SELECT status FROM menus WHERE id=?",
                                        (mid,)).fetchone()[0], "published")
        # 已发布不能再审
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.review_menu(self.c, mid, "stf_nutr", False)


class TestTwoPersonConfirmation(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn

    def tearDown(self):
        self.w.close()

    def _request(self, requester="stf_mgr"):
        payload = {"avoidances": "香菜", "chewing_level": "soft",
                   "effective_date": "2026-09-28",
                   "allergens": [{"name": "花生", "severity": "severe"}]}
        with transaction(self.c):
            return svc.submit_profile_change(self.c, "r2", payload, requester)

    def test_change_not_applied_until_confirmed(self):
        self._request()
        # 生效档案未变
        row = self.c.execute(
            "SELECT chewing_level,version FROM diet_profiles WHERE resident_id='r2'").fetchone()
        self.assertEqual(row[0], "soft")  # 原本就是 soft
        # 增加一个可观察变化:鸡蛋过敏尚不存在
        self.assertIsNone(self.c.execute(
            "SELECT 1 FROM allergens WHERE resident_id='r2' AND name='花生'").fetchone())

    def test_same_person_confirmation_rejected(self):
        cid = self._request("stf_mgr")
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.confirm_profile_change(self.c, cid, "stf_mgr", True)

    def test_different_person_confirms_and_applies(self):
        cid = self._request("stf_mgr")
        with transaction(self.c):
            svc.confirm_profile_change(self.c, cid, "stf_run1", True)
        self.assertEqual(self.c.execute(
            "SELECT status FROM profile_changes WHERE id=?", (cid,)).fetchone()[0],
            "confirmed")
        self.assertEqual(self.c.execute(
            "SELECT severity FROM allergens WHERE resident_id='r2' AND name='花生'"
        ).fetchone()[0], "severe")

    def test_double_confirm_rejected(self):
        cid = self._request("stf_mgr")
        with transaction(self.c):
            svc.confirm_profile_change(self.c, cid, "stf_run1", True)
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.confirm_profile_change(self.c, cid, "stf_nutr", True)

    def test_rejection_keeps_profile_unchanged(self):
        cid = self._request("stf_mgr")
        with transaction(self.c):
            svc.confirm_profile_change(self.c, cid, "stf_nutr", False, "信息待核")
        self.assertIsNone(self.c.execute(
            "SELECT 1 FROM allergens WHERE resident_id='r2' AND name='花生'").fetchone())


class TestDeliveryAppendOnly(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn
        self.bid, self.rid = self.w.batch_and_route(["d_pump", "d_rice", "d_pork"])
        with transaction(self.c):
            svc.hand_over_route(self.c, self.rid, "stf_chef")
        box = self.w.box_for("r1")
        with transaction(self.c):
            r = svc.scan(self.c, self.rid, "RES:" + self.w.resident_code("r1"),
                         "stf_run1")
            svc.scan(self.c, self.rid, "BOX:" + self.w.active_token(box),
                     "stf_run1", active_stop_id=r["stop_id"])
        self.delivery_id = self.c.execute(
            "SELECT id FROM deliveries WHERE resident_id='r1'").fetchone()["id"]

    def tearDown(self):
        self.w.close()

    def test_notes_append_only(self):
        with transaction(self.c):
            svc.add_delivery_note(self.c, self.delivery_id, "stf_run1", "护理员代收")
        with transaction(self.c):
            svc.add_delivery_note(self.c, self.delivery_id, "stf_mgr", "14:00 已进食")
        detail = svc.delivery_detail(self.c, self.delivery_id)
        self.assertEqual([n["note"] for n in detail["notes"]],
                         ["护理员代收", "14:00 已进食"])
        self.assertEqual(len(detail["notes"]), 2)

    def test_empty_note_rejected(self):
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.add_delivery_note(self.c, self.delivery_id, "stf_run1", "  ")

    def test_core_timestamps_immutable_via_api(self):
        # 领域层没有提供任何修改 deliveries 主记录的函数;
        # 追加说明不会改变送达时间戳。
        before = self.c.execute(
            "SELECT delivered_at,courier_id FROM deliveries WHERE id=?",
            (self.delivery_id,)).fetchone()
        with transaction(self.c):
            svc.add_delivery_note(self.c, self.delivery_id, "stf_nutr", "追加一条")
        after = self.c.execute(
            "SELECT delivered_at,courier_id FROM deliveries WHERE id=?",
            (self.delivery_id,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))


class TestTemperatureAndFamily(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.c = self.w.conn
        self.bid, self.rid = self.w.batch_and_route(["d_pump", "d_rice", "d_pork"])
        with transaction(self.c):
            svc.hand_over_route(self.c, self.rid, "stf_chef")

    def tearDown(self):
        self.w.close()

    def test_low_temperature_quarantines_box(self):
        sid = self.w.stop_for(self.rid, "r1")
        with transaction(self.c):
            res = svc.record_temperature(self.c, sid, "stf_run1", 52.5)
        self.assertFalse(res["ok"])
        self.assertEqual(self.c.execute(
            "SELECT status FROM boxes WHERE id=?", (self.w.box_for("r1"),)).fetchone()[0],
            "quarantined")
        inc = self.c.execute(
            "SELECT kind,temp_c FROM incidents WHERE kind='temperature'").fetchone()
        self.assertEqual(inc["temp_c"], 52.5)

    def test_good_temperature_passes(self):
        sid = self.w.stop_for(self.rid, "r2")
        with transaction(self.c):
            res = svc.record_temperature(self.c, sid, "stf_run1", 68.0)
        self.assertTrue(res["ok"])
        self.assertEqual(self.c.execute(
            "SELECT COUNT(*) c FROM incidents WHERE kind='temperature'").fetchone()["c"], 0)

    def test_family_adjust_requires_confirmation_role(self):
        with transaction(self.c):
            fid = svc.create_family_adjustment(
                self.c, "r1", "2026-09-28", "lunch", "late_serve", "家属要求延后", "stf_mgr")
        with self.assertRaises(svc.DomainError):
            with transaction(self.c):
                svc.confirm_family_adjustment(self.c, fid, "stf_run1", True)
        with transaction(self.c):
            svc.confirm_family_adjustment(self.c, fid, "stf_mgr", True)
        self.assertEqual(self.c.execute(
            "SELECT status FROM family_adjustments WHERE id=?", (fid,)).fetchone()[0],
            "confirmed")
        # 确认留痕为独立的 family_adjust 事件
        self.assertEqual(self.c.execute(
            "SELECT COUNT(*) c FROM incidents WHERE kind='family_adjust'").fetchone()["c"], 1)


if __name__ == "__main__":
    unittest.main()
