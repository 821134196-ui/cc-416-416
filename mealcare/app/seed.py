"""演示数据生成:匿名住户、菜品、已审核菜单、批次配餐、路线交接,
以及错餐 / 温度不合格 / 拒餐 / 漏餐 / 家属调整 / 替换餐等异常处置样例。

重复执行会重置数据库。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import services as svc
from .db import connect, init_db, now_iso, transaction

MEAL_DATE = date.today().isoformat()


STAFF = [
    ("stf_nutr", "E-NUTR", "营养师·宁", "dietitian"),
    ("stf_chef", "E-CHEF", "厨工长·齐", "kitchen"),
    ("stf_run1", "E-RUN1", "送餐员·晓行", "courier"),
    ("stf_run2", "E-RUN2", "送餐员·远志", "courier"),
    ("stf_mgr",  "E-MGR",  "护理主管·安", "manager"),
]

# (id, code, 化名, 床位, 照护区, 忌口, 咀嚼等级, [(过敏项, 等级)...])
RESIDENTS = [
    ("res_01", "R-101", "住户甲·12床", "12", "东区",
     "香菜;动物内脏", "normal", [("花生", "anaphylaxis"), ("虾", "severe")]),
    ("res_02", "R-102", "住户乙·13床", "13", "东区",
     "辛辣", "soft", [("鸡蛋", "moderate"), ("芒果", "mild")]),
    ("res_03", "R-201", "住户丙·21床", "21", "西区",
     "牛奶", "minced", [("乳制品", "severe")]),
    ("res_04", "R-202", "住户丁·22床", "22", "西区",
     "", "pureed", [("大豆", "moderate"), ("坚果", "severe")]),
    ("res_05", "R-301", "住户戊·31床", "31", "南区",
     "海鲜;香菜", "normal", [("虾", "anaphylaxis"), ("蟹", "severe"), ("鱼", "moderate")]),
    ("res_06", "R-302", "住户己·32床", "32", "南区",
     "", "normal", []),
]

# (id, code, 名称, 类别, 含过敏原, 质地, kcal, 营养说明, 审核状态)
DISHES = [
    ("dsh_rice",  "D-01", "软米饭", "主食", "", "soft", 230, "低钠,易咀嚼", "approved"),
    ("dsh_pump",  "D-02", "南瓜糊", "主食", "", "pureed", 160, "高纤维,适合糊状餐", "approved"),
    ("dsh_chicken", "D-03", "香菇滑鸡(含花生)", "主菜", "花生;大豆", "soft", 310, "蛋白充足", "approved"),
    ("dsh_fish",  "D-04", "清蒸鲈鱼", "主菜", "鱼", "soft", 205, "优质蛋白,少刺", "approved"),
    ("dsh_shrimp", "D-05", "白灼基围虾", "主菜", "虾", "normal", 150, "高蛋白", "approved"),
    ("dsh_tofu",  "D-06", "小葱拌豆腐", "配菜", "大豆", "soft", 120, "含钙", "approved"),
    ("dsh_egg",   "D-07", "水蒸蛋羹", "配菜", "鸡蛋", "soft", 140, "优质蛋白,软嫩", "approved"),
    ("dsh_greens","D-08", "清炒时蔬", "配菜", "", "soft", 70, "高纤维", "approved"),
    ("dsh_pork",  "D-09", "肉末烧冬瓜", "主菜", "", "minced", 260, "铁与蛋白", "approved"),
    ("dsh_milk",  "D-10", "牛奶燕麦羹", "汤品", "乳制品", "soft", 210, "高钙", "approved"),
    ("dsh_mango", "D-11", "芒果布丁", "甜品", "芒果;乳制品", "soft", 180, "新品,待营养审核", "draft"),
    ("dsh_crab",  "D-12", "蟹粉狮子头", "主菜", "蟹;鱼;鸡蛋", "soft", 290, "高脂,审核驳回样例", "rejected"),
]


def seed(db_path: str, interactive: bool = False) -> dict:
    """初始化并填充样例。返回常用实体 id,供命令行/页面演示。"""
    init_db(db_path)
    conn = connect(db_path)
    ids = {}
    with transaction(conn):
        for sid, code, name, role in STAFF:
            conn.execute(
                "INSERT INTO staff(id,code,name,role) VALUES(?,?,?,?)",
                (sid, code, name, role))

        for rid, rcode, alias, bed, zone, avoid, chew, allergens in RESIDENTS:
            conn.execute(
                "INSERT INTO residents(id,code,alias,bed_no,care_zone) VALUES(?,?,?,?,?)",
                (rid, rcode, alias, bed, zone))
            conn.execute(
                "INSERT INTO diet_profiles(id,resident_id,avoidances,chewing_level,"
                "effective_date,version,updated_by,updated_at) VALUES(?,?,?,?,?,1,?,?)",
                ("dp_" + rid[-3:], rid, avoid, chew, MEAL_DATE, "stf_mgr", now_iso()))
            for name, sev in allergens:
                conn.execute(
                    "INSERT INTO allergens(id,resident_id,name,severity) VALUES(?,?,?,?)",
                    ("al_" + rid[-3:] + "_" + name, rid, name, sev))

        # 一条待双人确认的档案修改:住户己新增大豆过敏 + 改软食
        pending = {
            "avoidances": "辛辣",
            "chewing_level": "soft",
            "effective_date": MEAL_DATE,
            "allergens": [{"name": "大豆", "severity": "severe"}],
        }
        conn.execute(
            "INSERT INTO profile_changes(id,resident_id,payload_json,requested_by,requested_at,status)"
            " VALUES('pc_demo_pending',?,?,?,?,'pending')",
            ("res_06", json.dumps(pending, ensure_ascii=False), "stf_mgr", now_iso()))

        for did, code, name, cat, contains, texture, kcal, note, status in DISHES:
            conn.execute(
                "INSERT INTO dishes(id,code,name,category,contains_allergens,texture,kcal,"
                "nutrition_note,review_status,reviewed_by,reviewed_at,review_note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, code, name, cat, contains, texture, kcal, note, status,
                 "stf_nutr" if status != "draft" else None,
                 now_iso() if status != "draft" else None,
                 "营养审核通过" if status == "approved" else
                 ("驳回:蟹粉高脂且含多种高致敏原,不适合老年餐" if status == "rejected" else "")))

        # ---- 午餐菜单:已审核发布 ----
        lunch = "mn_lunch"
        conn.execute(
            "INSERT INTO menus(id,meal_date,meal_type,status,created_by,submitted_at,"
            "reviewed_by,reviewed_at,review_note) VALUES(?,?, 'lunch','published',?,?,?,?,?)",
            (lunch, MEAL_DATE, "stf_chef", now_iso(), "stf_nutr", now_iso(),
             "营养均衡,准予发布"))
        # 主菜在前主食垫底:自动配餐优先取可食用的高蛋白菜品,
        # 过敏/质地拦截后沿菜单顺序回退,形成可观察的拦截链
        lunch_items = ["dsh_chicken", "dsh_pork", "dsh_fish", "dsh_shrimp",
                       "dsh_tofu", "dsh_egg", "dsh_milk", "dsh_greens",
                       "dsh_rice", "dsh_pump"]
        for i, did in enumerate(lunch_items):
            conn.execute("INSERT INTO menu_items(id,menu_id,dish_id) VALUES(?,?,?)",
                         (f"mi_l{i:02d}", lunch, did))

        # ---- 晚餐菜单:草稿,内含一道待审菜用于演示拦截 ----
        dinner = "mn_dinner"
        conn.execute(
            "INSERT INTO menus(id,meal_date,meal_type,status,created_by) "
            "VALUES(?,?,'dinner','draft',?)", (dinner, MEAL_DATE, "stf_chef"))
        for i, did in enumerate(["dsh_rice", "dsh_fish", "dsh_greens"]):
            conn.execute("INSERT INTO menu_items(id,menu_id,dish_id) VALUES(?,?,?)",
                         (f"mi_d{i:02d}", dinner, did))

        # ---- 厨房批次(过敏拦截在此发生:自动为每位住户选可食用菜) ----
        batch = svc.produce_batch(conn, lunch, "stf_chef", station="中央厨房一线")
        ids["batch"] = batch

        # ---- 路线与交接 ----
        route = svc.create_route_for_batch(conn, batch, "stf_run1")
        ids["route"] = route
        svc.hand_over_route(conn, route, "stf_chef")

        # 送餐提醒(进程内调度,2 分钟后与 15 分钟后各一次)
        fire1 = (datetime.now() + timedelta(minutes=2)).isoformat(timespec="seconds")
        fire2 = (datetime.now() + timedelta(minutes=15)).isoformat(timespec="seconds")
        for rid, title, ts in [("rm_1", "请核对东区住户已出餐(过敏高风险)", fire1),
                               ("rm_2", "南区餐盒即将超时,请完成最后三站", fire2)]:
            conn.execute(
                "INSERT INTO reminders(id,route_id,title,fire_at,status,created_at)"
                " VALUES(?,?,?,?,'pending',?)", (rid, route, title, ts, now_iso()))

    # ---- 下面使用业务函数演示异常处置(各自独立事务) ----

    def res_code(resident_id: str) -> str:
        return conn.execute("SELECT code FROM residents WHERE id=?",
                            (resident_id,)).fetchone()["code"]

    def stop_id(resident_id: str) -> str:
        return conn.execute(
            "SELECT id FROM route_stops WHERE route_id=? AND resident_id=?",
            (route, resident_id)).fetchone()["id"]

    rd = svc.route_detail(conn, route)
    by_res = {s["resident_id"]: s for s in rd["stops"]}

    # 1) 正常交付前两站(东区 12、13 床):先住户码、再餐盒新签
    with transaction(conn):
        for rid in ("res_01", "res_02"):
            s = by_res[rid]
            r = svc.scan(conn, route, f"RES:{res_code(rid)}", "stf_run1")
            svc.scan(conn, route, f"BOX:{s['label_token']}", "stf_run1",
                     active_stop_id=r["stop_id"])
    ids["delivered_residents"] = ["res_01", "res_02"]

    # 2) 温度不合格:21床餐盒测温 54℃ → 隔离(等待替换)
    with transaction(conn):
        s03 = by_res["res_03"]
        temp = svc.record_temperature(conn, s03["id"], "stf_run1", 54.0)
        ids["temperature_incident"] = temp["incident_id"]
        # 厨房用已审核的南瓜糊替换(糊状、不含大豆/乳制品)→ 旧签作废、新签生效
        label = svc.substitute_meal(
            conn, s03["box_id"], "dsh_pump", "stf_chef",
            "温度不合格,换发热餐")
        ids["new_label"] = label["token"]

    # 3) 错餐演示:在 31床(虾类速发过敏)处扫到 22床的餐盒 → 立即隔离
    with transaction(conn):
        s22 = by_res["res_04"]
        svc.scan(conn, route, f"RES:{res_code('res_05')}", "stf_run1")
        try:
            svc.scan(conn, route, f"BOX:{s22['label_token']}", "stf_run1",
                     active_stop_id=stop_id("res_05"))
        except svc.DomainError:
            pass  # 预期:错餐已隔离

    # 4) 拒餐:32床今日胃口不佳拒餐
    with transaction(conn):
        s32 = by_res["res_06"]
        ids["refused_incident"] = svc.mark_refused(
            conn, s32["id"], "stf_run1", "住户表示无食欲,要求午休后再用餐")

    # 5) 漏餐(不依赖餐盒):记录一例昨日晚餐漏餐
    with transaction(conn):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        ids["missed_incident"] = svc.record_missed(
            conn, "res_02", yesterday, "dinner", "stf_run1",
            "住户做检查未在楼层,晚餐漏送,已补加餐")

    # 6) 家属临时调整:12床家属来电要求晚餐加半份主食 → 待确认
    with transaction(conn):
        ids["family_adjust"] = svc.create_family_adjustment(
            conn, "res_01", MEAL_DATE, "lunch", "extra_portion",
            "家属来电:住户今日胃口好转,要求晚餐加半份主食", "stf_mgr")

    # 7) 已送达记录追加说明(只允许 append)
    with transaction(conn):
        dv = conn.execute(
            "SELECT id FROM deliveries WHERE resident_id='res_01'").fetchone()
        svc.add_delivery_note(conn, dv["id"], "stf_run1",
                              "送达时住户正在活动厅,由护理员协助取用")

    conn.close()
    return ids
