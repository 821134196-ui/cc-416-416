"""匿名演示数据：一条命令生成菜单、配餐与异常处置样例。

运行：python3 -m seed [--reset]
所有姓名与健康信息均为虚构的匿名演示数据。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (  # noqa: E402
    audit, connect, init_db, jdumps, jloads, now,
)
from datetime import date, datetime, timedelta  # noqa: E402

TODAY = date.today().isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------
# 基础数据
# ---------------------------------------------------------------------

STAFF = [
    ("S01", "管理员·院长", "manager", "1111"),
    ("S02", "营养师·甘", "nutritionist", "2222"),
    ("S03", "护理员·梅", "nurse", "3333"),
    ("S04", "厨师长·灶", "kitchen", "4444"),
    ("S05", "送餐员·甲", "courier", "5555"),
    ("S06", "送餐员·乙", "courier", "6666"),
]

# id, code, floor, room, alias, allergies, avoidances, chewing, special, status
RESIDENTS = [
    ("R001", "A001", "1F", "101", "住户·青松",
     [{"allergen": "花生", "level": "severe", "note": "微量即可诱发严重反应"}],
     ["羊肉"], "normal", "", "active"),
    ("R002", "A002", "1F", "102", "住户·翠竹",
     [{"allergen": "虾", "level": "severe", "note": "甲壳类过敏"},
      {"allergen": "蟹", "level": "severe", "note": ""}],
     [], "soft", "低盐", "active"),
    ("R003", "A003", "1F", "103", "住户·幽兰",
     [{"allergen": "牛奶", "level": "warning", "note": "乳蛋白不耐受"}],
     ["辛辣"], "pureed", "", "active"),
    ("R004", "A004", "2F", "201", "住户·寒梅",
     [{"allergen": "鸡蛋", "level": "severe", "note": "含蛋糕点均不可"}],
     [], "minced", "糖尿病餐", "active"),
    ("R005", "A005", "2F", "202", "住户·白鹤",
     [], ["内脏", "油腻"], "normal", "低盐", "active"),
    ("R006", "A006", "2F", "203", "住户·银杏",
     [{"allergen": "大豆", "level": "warning", "note": "豆制品胀气"}],
     [], "soft", "", "active"),
    ("R007", "A007", "3F", "301", "住户·秋水",
     [{"allergen": "芒果", "level": "severe", "note": "果皮接触即红肿"},
      {"allergen": "花生", "level": "warning", "note": ""}],
     ["生冷"], "normal", "", "active"),
    ("R008", "A008", "3F", "302", "住户·暮雪",
     [{"allergen": "麸质", "level": "warning", "note": "改用米饭餐"}],
     [], "soft", "", "active"),
]

# id, name, category, allergens, texture, diet_tags, kcal, review_status
DISHES = [
    ("D01", "软米饭", "staple", [], "soft", ["低盐", "糖尿病适餐"], 220, "approved"),
    ("D02", "杂粮馒头", "staple", ["麸质"], "normal", [], 200, "pending"),
    ("D03", "清蒸鲈鱼", "meat", ["鱼"], "soft", ["低盐"], 180, "approved"),
    ("D04", "胡萝卜炖鸡丝", "meat", [], "minced", ["低盐", "糖尿病适餐"], 210, "approved"),
    ("D05", "冬瓜虾仁", "meat", ["虾"], "soft", [], 150, "approved"),
    ("D06", "番茄炒蛋", "egg", ["鸡蛋"], "soft", [], 190, "approved"),
    ("D07", "麻婆豆腐", "soy", ["大豆"], "soft", ["辛辣"], 170, "approved"),
    ("D08", "清炒时蔬", "veg", [], "soft", ["低盐", "糖尿病适餐"], 90, "approved"),
    ("D09", "南瓜泥", "veg", [], "pureed", ["低盐", "糖尿病适餐"], 110, "approved"),
    ("D10", "玉米排骨汤", "soup", [], "soft", ["低盐"], 130, "approved"),
    ("D11", "花生糊", "soup", ["花生"], "normal", [], 160, "approved"),
    ("D12", "牛奶燕麦羹", "soup", ["牛奶"], "soft", [], 180, "approved"),
    ("D13", "香蕉段", "fruit", [], "soft", ["低盐", "糖尿病适餐"], 80, "approved"),
    ("D14", "芒果布丁", "fruit", ["芒果"], "normal", [], 140, "approved"),
    ("D15", "土豆泥", "veg", [], "pureed", ["低盐", "糖尿病适餐"], 120, "approved"),
    ("D16", "白灼青菜", "veg", [], "normal", ["低盐"], 70, "approved"),
    ("D19", "营养米糊", "staple", [], "pureed", ["低盐", "糖尿病适餐"], 180, "approved"),
    ("D20", "肉末稠粥", "staple", [], "minced", ["低盐", "糖尿病适餐"], 200, "approved"),
    # 未审核：替换餐不可选，菜单不可包含
    ("D17", "试制·栗子烧鸡", "meat", [], "minced", [], 240, "pending"),
    # 审核不通过
    ("D18", "咸肉咸菜煲", "meat", [], "normal", ["高盐"], 290, "rejected"),
]


def seed(reset: bool = True) -> str:
    db_file = os.environ.get(
        "MEAL_DB",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mealcare.db"),
    )
    if reset and os.path.exists(db_file):
        os.remove(db_file)
    conn = init_db(db_file)
    cur = conn.cursor()

    # 员工 ----------------------------------------------------------------
    for sid, name, role, code in STAFF:
        cur.execute("INSERT INTO staff(id,name,role,passcode) VALUES(?,?,?,?)",
                    (sid, name, role, code))

    # 住户 ----------------------------------------------------------------
    for rid, code, floor, room, alias, allerg, avoid, chew, diet, status in RESIDENTS:
        cur.execute(
            """INSERT INTO residents(id,code,room,floor,alias,allergies,avoidances,
                   chewing,special_diet,effective_date,card_status,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, code, room, floor, alias, jdumps(allerg), jdumps(avoid),
             chew, diet, TODAY, status, "S03", now(), now()),
        )

    # 菜品 ----------------------------------------------------------------
    for did, name, cat, allerg, tex, tags, kcal, rstatus in DISHES:
        reviewed = "S02" if rstatus in ("approved", "rejected") else None
        cur.execute(
            """INSERT INTO dishes(id,name,category,allergens,texture,diet_tags,energy_kcal,
                   review_status,reviewed_by,reviewed_at,review_note,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, name, cat, jdumps(allerg), tex, jdumps(tags), kcal, rstatus,
             reviewed, now() if reviewed else None,
             "高盐菜品未通过" if rstatus == "rejected" else ("已审核" if reviewed else ""), now()),
        )

    # 菜单：今日午餐（待营养审核 -> 演示审核流）；今日晚餐（已发布）---------
    lunch_items = ["D01", "D20", "D19", "D04", "D08", "D09", "D10", "D13"]
    cur.execute(
        """INSERT INTO menus(id,serve_date,meal_session,status,created_by,created_at,review_note)
           VALUES(?,?,?,?,?,?,?)""",
        ("M-LUNCH", TODAY, "lunch", "pending", "S04", now(),
         "请重点核对花生、虾与糖尿病餐适配"),
    )
    for i, d in enumerate(lunch_items):
        cur.execute("INSERT INTO menu_items(menu_id,dish_id,seq) VALUES(?,?,?)",
                    ("M-LUNCH", d, i))

    dinner_items = ["D01", "D19", "D20", "D03", "D04", "D16", "D08", "D15", "D10", "D13"]
    cur.execute(
        """INSERT INTO menus(id,serve_date,meal_session,status,created_by,created_at,
               reviewed_by,reviewed_at,review_note)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("M-DINNER", TODAY, "dinner", "published", "S04", now(), "S02", now(),
         "已核对：无花生/虾/蟹/芒果，含软质与匀浆选项"),
    )
    for i, d in enumerate(dinner_items):
        cur.execute("INSERT INTO menu_items(menu_id,dish_id,seq) VALUES(?,?,?)",
                    ("M-DINNER", d, i))

    # 厨房批次（晚餐，已备好）---------------------------------------------
    bid = f"B{TODAY.replace('-','')}-D"
    cur.execute(
        """INSERT INTO batches(id,serve_date,meal_session,menu_id,status,created_by,
               created_at,prepared_at,note) VALUES(?,?,?,?,?,?,?,?,?)""",
        (bid, TODAY, "dinner", "M-DINNER", "prepared", "S04", now(), now(),
         "晚餐批次：8 份住户餐 + 1 份备用替换餐"),
    )

    # 为每位住户配餐：调用与生产相同的规则引擎自动选安全菜 ---------------
    # 严重过敏/慎食/忌口/质地/特殊餐任一不符即跳过该菜；无安全主食会抛错。
    import services as svc
    menu_rows = conn.execute(
        "SELECT * FROM dishes WHERE id IN (%s) ORDER BY id"
        % ",".join("?" * len(dinner_items)), dinner_items).fetchall()
    plans = {}
    for _r in conn.execute("SELECT * FROM residents ORDER BY id").fetchall():
        plans[_r["id"]] = svc.auto_plan_for_resident(conn, svc.resident_view(_r), menu_rows)
    box_codes = {}
    seq = 1
    for rid in [f"R{n:03d}" for n in range(1, 9)]:
        dishes = plans[rid]
        code = f"{TODAY.replace('-','')}-D-{seq:04d}"
        box_codes[rid] = code
        cur.execute(
            """INSERT INTO meal_boxes(id,label_code,batch_id,resident_id,dishes,
                   is_substitution,sub_reason,status,temp_c,temp_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"BX-{code}", code, bid, rid, jdumps(dishes), 0, "", "labeled",
             62.5 if seq != 5 else 54.1, now(), now(), now()),
        )
        seq += 1

    # 备用替换餐（尚未指派住户）
    sub_code = f"{TODAY.replace('-','')}-D-{seq:04d}"
    cur.execute(
        """INSERT INTO meal_boxes(id,label_code,batch_id,resident_id,dishes,
               is_substitution,sub_reason,status,temp_c,temp_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"BX-{sub_code}", sub_code, bid, None, jdumps(["D01", "D04", "D08", "D13"]),
         1, "备用：拒餐/家属调整时替换（全部菜品已审核）", "labeled",
         63.0, now(), now(), now()),
    )

    # 一张已作废标签：演示"旧标签不能继续使用"
    old_code = f"{TODAY.replace('-','')}-D-0000"
    cur.execute(
        """INSERT INTO meal_boxes(id,label_code,batch_id,resident_id,dishes,
               is_substitution,sub_reason,status,void_reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (f"BX-{old_code}", old_code, bid, "R003", jdumps(["D01", "D07"]), 1,
         "误配含大豆菜，已换用新标签", "void", "旧标签作废：重打标签 0003",
         now(), now()),
    )

    # 路线 1（1F+2F 部分）在途，送餐员甲 -> 交接给乙 ----------------------
    route1 = f"RT{TODAY.replace('-','')}-D1"
    cur.execute(
        """INSERT INTO routes(id,serve_date,meal_session,name,courier_id,status,
               handover_from,handover_to,handover_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (route1, TODAY, "dinner", "1F/2F 东翼线", "S05", "in_transit",
         "S05", "S06", now(), now()),
    )
    stops1 = ["R001", "R002", "R003", "R004"]
    for i, rid in enumerate(stops1, 1):
        cur.execute(
            """INSERT INTO route_stops(id,route_id,seq,resident_id,meal_box_id,status)
               VALUES(?,?,?,?,?,?)""",
            (f"ST-{route1}-{i}", route1, i, rid, f"BX-{box_codes[rid]}",
             "delivered" if i == 1 else "pending"),
        )
        if i == 1:
            cur.execute(
                """UPDATE route_stops SET resident_scan_at=?, box_scan_at=?, delivered_at=?
                   WHERE id=?""", (now(), now(), now(), f"ST-{route1}-{i}"))
            cur.execute(
                """INSERT INTO deliveries(id,stop_id,meal_box_id,resident_id,result,
                       delivered_by,delivered_at) VALUES(?,?,?,?,?,?,?)""",
                (f"DL-{route1}-1", f"ST-{route1}-1", f"BX-{box_codes['R001']}",
                 "R001", "delivered", "S05", now()),
            )

    # 路线 2（2F+3F）planned
    route2 = f"RT{TODAY.replace('-','')}-D2"
    cur.execute(
        """INSERT INTO routes(id,serve_date,meal_session,name,courier_id,status,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (route2, TODAY, "dinner", "2F/3F 西翼线", "S06", "planned", now()),
    )
    stops2 = ["R005", "R006", "R007", "R008"]
    for i, rid in enumerate(stops2, 1):
        cur.execute(
            """INSERT INTO route_stops(id,route_id,seq,resident_id,meal_box_id,status)
               VALUES(?,?,?,?,?,?)""",
            (f"ST-{route2}-{i}", route2, i, rid, f"BX-{box_codes[rid]}", "pending"),
        )

    # 异常事件样例 --------------------------------------------------------
    cur.execute(
        """INSERT INTO meal_events(id,type,serve_date,meal_session,resident_id,
               meal_box_id,route_id,detail,status,recorded_by,created_at,
               handled_by,handled_at,resolution)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_id("EV-"), "wrong_meal", TODAY, "lunch", "R002", None, route1,
         "午餐曾错放含虾餐盒，送达前比对发现并拦截", "handled", "S03", now(),
         "S02", now(), "立即撤回厨房换餐，未送达，已重做安全标签"),
    )
    cur.execute(
        """INSERT INTO meal_events(id,type,serve_date,meal_session,resident_id,
               meal_box_id,route_id,detail,status,recorded_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (_id("EV-"), "missed", TODAY, "lunch", "R008", None, route2,
         "午餐路线遗漏 302 室，发现时已过供餐时段", "open", "S05", now()),
    )
    cur.execute(
        """INSERT INTO meal_events(id,type,serve_date,meal_session,resident_id,
               meal_box_id,route_id,detail,temp_c,status,recorded_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_id("EV-"), "temp_fail", TODAY, "dinner", "R005",
         f"BX-{box_codes['R005']}", route2,
         "出餐测温仅 54.1℃，低于 60℃ 中心温度要求", 54.1, "open", "S04", now()),
    )
    cur.execute(
        """INSERT INTO meal_events(id,type,serve_date,meal_session,resident_id,
               meal_box_id,route_id,detail,status,recorded_by,created_at,
               handled_by,handled_at,resolution)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_id("EV-"), "refused", TODAY, "lunch", "R003", None, route1,
         "住户表示午餐无胃口拒餐，护理员已登记并提供营养补充", "handled", "S03",
         now(), "S03", now(), "登记拒餐，晚餐安排匀浆小份并随访"),
    )
    # 家属临时调整（待家属确认页处理）
    cur.execute(
        """INSERT INTO family_adjustments(id,resident_id,family_name,contact_code,
               serve_date,meal_session,change_type,detail,suggested_dish_id,
               status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (_id("FA-"), "R007", "家属·A007", "FAM-7742", TODAY, "dinner", "hold",
         "家属来电：住户今晚外出就诊，请暂停晚餐并准备便携点心", None,
         "requested", now()),
    )
    cur.execute(
        """INSERT INTO family_adjustments(id,resident_id,family_name,contact_code,
               serve_date,meal_session,change_type,detail,suggested_dish_id,
               status,created_at,handled_by,handled_at,handle_note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_id("FA-"), "R004", "家属·A004", "FAM-4408", TODAY, "lunch", "swap",
         "午餐要求把含蛋甜点换成香蕉段，已完成", "D13",
         "fulfilled", now(), "S03", now(), "已替换为 D13 香蕉段，旧标签作废重打"),
    )

    # 提醒（进程内任务消费；出发提醒安排在启动后不久触发，便于演示）
    fire_soon = (datetime.now() + timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
    fire_later = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """INSERT INTO reminders(id,serve_date,meal_session,route_id,fire_at,message,
               status,created_at) VALUES(?,?,?,?,?,?,?,?)""",
        (_id("RM-"), TODAY, "dinner", route2, fire_soon,
         "西翼线即将出发：请核对餐盒温度，重点确认 302 室（今日午餐曾漏餐）",
         "pending", now()),
    )
    cur.execute(
        """INSERT INTO reminders(id,serve_date,meal_session,route_id,fire_at,message,
               status,created_at,fired_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (_id("RM-"), TODAY, "dinner", route1, fire_later,
         "东翼线已在交接给送餐员·乙，请确认交接完成", "fired", now(), now()),
    )

    audit(conn, "S01", "seed.demo_data", "system", "", {"date": TODAY})
    conn.commit()
    conn.close()
    return db_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成匿名演示数据")
    parser.add_argument("--keep", action="store_true", help="保留现有数据（默认重建）")
    args = parser.parse_args()
    path = seed(reset=not args.keep)
    print(f"演示数据已生成：{path}")
    print(f"供餐日期：{TODAY}；员工口令 1111/2222/3333/4444/5555/6666")
