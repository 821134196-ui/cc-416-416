"""测试夹具:在临时文件上构建最小可控数据集(不复用演示种子)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db, now_iso, transaction
from app import services as svc

TODAY = "2026-09-28"


class World:
    """构造一个微型机构:4 名员工 + 若干住户/菜品。"""

    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(self.path)
        self.conn = connect(self.path)
        self._build()

    def _build(self):
        c = self.conn
        with transaction(c):
            for sid, name, role in [
                    ("stf_mgr", "主管", "manager"),
                    ("stf_nutr", "营养师", "dietitian"),
                    ("stf_chef", "厨工", "kitchen"),
                    ("stf_run1", "送餐甲", "courier"),
                    ("stf_run2", "送餐乙", "courier")]:
                c.execute("INSERT INTO staff(id,code,name,role) VALUES(?,?,?,?)",
                          (sid, sid.upper(), name, role))
            self._resident("r1", "R-001", "住户甲", "01", "东区",
                           avoid="香菜", chew="normal",
                           allergens=[("花生", "anaphylaxis"), ("虾", "severe")])
            self._resident("r2", "R-002", "住户乙", "02", "东区",
                           avoid="辛辣", chew="soft",
                           allergens=[("鸡蛋", "moderate")])
            self._resident("r3", "R-003", "住户丙", "03", "西区",
                           avoid="", chew="pureed", allergens=[("大豆", "severe")])
            # 菜品(全部已审核,除特别标注)
            self._dish("d_chicken", "花生烧鸡", "花生;大豆", "soft")
            self._dish("d_shrimp", "白灼虾", "虾", "normal")
            self._dish("d_pork", "肉末冬瓜", "", "minced")
            self._dish("d_pump", "南瓜糊", "", "pureed")
            self._dish("d_egg", "蛋羹", "鸡蛋", "soft")
            self._dish("d_rice", "软米饭", "", "soft")
            self._dish("d_draft", "待审芒果布丁", "芒果", "soft", status="draft")

    def _resident(self, rid, code, alias, bed, zone, avoid, chew, allergens):
        c = self.conn
        c.execute("INSERT INTO residents(id,code,alias,bed_no,care_zone) VALUES(?,?,?,?,?)",
                  (rid, code, alias, bed, zone))
        c.execute("INSERT INTO diet_profiles(id,resident_id,avoidances,chewing_level,"
                  "effective_date,version,updated_by,updated_at) VALUES(?,?,?,?,?,1,?,?)",
                  ("dp_" + rid, rid, avoid, chew, TODAY, "stf_mgr", now_iso()))
        for name, sev in allergens:
            c.execute("INSERT INTO allergens(id,resident_id,name,severity) VALUES(?,?,?,?)",
                    ("al_" + rid + "_" + name, rid, name, sev))

    def _dish(self, did, name, contains, texture, status="approved"):
        c = self.conn
        c.execute("INSERT INTO dishes(id,code,name,category,contains_allergens,texture,kcal,"
                  "nutrition_note,review_status,reviewed_by,reviewed_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (did, did.upper(), name, "主菜", contains, texture, 200, "均衡",
                   status, "stf_nutr" if status != "draft" else None,
                   now_iso() if status != "draft" else None))

    # ---- 业务快捷方式 ----
    def published_menu(self, dish_ids, meal_type="lunch"):
        c = self.conn
        with transaction(c):
            n = c.execute("SELECT COUNT(*) c FROM menus").fetchone()["c"]
            mid = f"mn_{meal_type}_{n}"
            c.execute("INSERT INTO menus(id,meal_date,meal_type,status,created_by,submitted_at,"
                      "reviewed_by,reviewed_at) VALUES(?,?,?, 'published',?,?,?,?)",
                      (mid, TODAY, meal_type, "stf_chef", now_iso(),
                       "stf_nutr", now_iso()))
            for i, d in enumerate(dish_ids):
                c.execute("INSERT INTO menu_items(id,menu_id,dish_id) VALUES(?,?,?)",
                          (f"mi_{mid}_{i}", mid, d))
        return mid

    def batch_and_route(self, dish_ids, courier="stf_run1"):
        c = self.conn
        mid = self.published_menu(dish_ids)
        with transaction(c):
            bid = svc.produce_batch(c, mid, "stf_chef")
            rid = svc.create_route_for_batch(c, bid, courier)
        return bid, rid

    def stop_for(self, route_id, resident_id):
        return self.conn.execute(
            "SELECT id FROM route_stops WHERE route_id=? AND resident_id=?",
            (route_id, resident_id)).fetchone()["id"]

    def box_for(self, resident_id):
        return self.conn.execute(
            "SELECT id FROM boxes WHERE resident_id=? ORDER BY created_at DESC LIMIT 1",
            (resident_id,)).fetchone()["id"]

    def active_token(self, box_id):
        return self.conn.execute(
            "SELECT token FROM box_labels WHERE box_id=? AND status='active' "
            "ORDER BY version DESC", (box_id,)).fetchone()["token"]

    def resident_code(self, resident_id):
        return self.conn.execute("SELECT code FROM residents WHERE id=?",
                                 (resident_id,)).fetchone()["code"]

    def close(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass
