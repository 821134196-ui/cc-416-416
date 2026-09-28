"""领域服务:过敏拦截、营养审核、批次配餐、标签生命周期、
路线交接与扫描核对、异常隔离、双人确认、字段权限。

所有写操作都在单个事务内完成,并写入 audit_log。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from . import qr
from .db import now_iso


class DomainError(Exception):
    """业务规则冲突(权限、过敏拦截、状态机非法跳转等)。"""


# ---------------------------------------------------------------------------
# 常量与权限
# ---------------------------------------------------------------------------

TEXTURE_RANK = {"normal": 0, "soft": 1, "minced": 2, "pureed": 3}
TEXTURE_CN = {"normal": "普通", "soft": "软食", "minced": "碎餐", "pureed": "糊状"}
SEVERITY_CN = {"mild": "轻度", "moderate": "中度", "severe": "严重",
               "anaphylaxis": "速发(可致命)"}
SEVERITY_RANK = {"mild": 1, "moderate": 2, "severe": 3, "anaphylaxis": 4}

HOT_MEAL_MIN_C = 60.0  # 热餐中心温度下限

# 字段级权限:角色 -> 可执行动作集合
PERMISSIONS = {
    "dish.edit_nutrition": {"dietitian"},
    "dish.review": {"dietitian"},
    "menu.review": {"dietitian"},
    "batch.produce": {"kitchen"},
    "route.handover": {"kitchen"},
    "route.deliver": {"courier"},
    "incident.record": {"courier", "kitchen", "manager"},
    "incident.resolve": {"kitchen", "manager"},
    "family.confirm": {"manager", "dietitian"},
    "profile.request": {"manager", "dietitian"},
    "profile.confirm": {"manager", "dietitian", "kitchen", "courier"},
}
# 菜品各字段归属权限
DISH_FIELD_PERMISSION = {
    "name": "dish.edit_nutrition",
    "category": "dish.edit_nutrition",
    "contains_allergens": "dish.edit_nutrition",
    "texture": "dish.edit_nutrition",
    "kcal": "dish.edit_nutrition",
    "nutrition_note": "dish.edit_nutrition",
    "review_status": "dish.review",
}


def gid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def split_items(s: str) -> list[str]:
    for sep in (",", "，", "；"):
        s = (s or "").replace(sep, ";")
    return [x.strip() for x in s.split(";") if x.strip()]


def audit(conn, staff_id, action, entity, entity_id, detail=""):
    conn.execute(
        "INSERT INTO audit_log(at,staff_id,action,entity,entity_id,detail) VALUES(?,?,?,?,?,?)",
        (now_iso(), staff_id, action, entity, entity_id, detail))


def get_staff(conn, staff_id: str) -> dict:
    row = conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
    if not row:
        raise DomainError(f"员工不存在: {staff_id}")
    return dict(row)


def require_permission(conn, staff_id: str, perm: str):
    staff = get_staff(conn, staff_id)
    if staff["role"] not in PERMISSIONS.get(perm, set()):
        raise DomainError(f"字段权限拒绝:{staff['role']} 无权执行 {perm}")
    return staff


# ---------------------------------------------------------------------------
# 住户与饮食档案
# ---------------------------------------------------------------------------

def get_resident(conn, resident_id: str) -> dict:
    row = conn.execute("SELECT * FROM residents WHERE id=?", (resident_id,)).fetchone()
    if not row:
        raise DomainError("住户不存在")
    d = dict(row)
    prof = conn.execute("SELECT * FROM diet_profiles WHERE resident_id=?",
                        (resident_id,)).fetchone()
    d["profile"] = dict(prof) if prof else None
    rows = conn.execute(
        "SELECT * FROM allergens WHERE resident_id=? ORDER BY "
        "CASE severity WHEN 'anaphylaxis' THEN 0 WHEN 'severe' THEN 1 "
        "WHEN 'moderate' THEN 2 ELSE 3 END, name", (resident_id,)).fetchall()
    d["allergens"] = [dict(r) for r in rows]
    d["allergen_names"] = {r["name"] for r in d["allergens"]}
    return d


def list_residents(conn, active_only=True) -> list[dict]:
    sql = "SELECT * FROM residents" + (" WHERE active=1" if active_only else "") + " ORDER BY care_zone, bed_no"
    return [get_resident(conn, r["id"]) for r in conn.execute(sql)]


def allergen_snapshot(conn, resident_id: str) -> str:
    rows = conn.execute(
        "SELECT name,severity FROM allergens WHERE resident_id=? "
        "ORDER BY CASE severity WHEN 'anaphylaxis' THEN 0 WHEN 'severe' THEN 1 "
        "WHEN 'moderate' THEN 2 ELSE 3 END", (resident_id,)).fetchall()
    return ";".join(f"{r['name']}({SEVERITY_CN[r['severity']]})" for r in rows)


def _validate_payload(payload: dict):
    chew = payload.get("chewing_level", "normal")
    if chew not in TEXTURE_RANK:
        raise DomainError(f"非法咀嚼要求: {chew}")
    if not payload.get("effective_date"):
        raise DomainError("生效日期必填")
    seen = set()
    for a in payload.get("allergens", []):
        if a["severity"] not in SEVERITY_RANK:
            raise DomainError(f"非法过敏等级: {a['severity']}")
        if a["name"] in seen:
            raise DomainError(f"过敏项重复: {a['name']}")
        seen.add(a["name"])


def submit_profile_change(conn, resident_id: str, payload: dict, requester_id: str) -> str:
    """档案修改必须走双人确认:任何角色都不能直接改生效档案。"""
    require_permission(conn, requester_id, "profile.request")
    get_resident(conn, resident_id)
    _validate_payload(payload)
    change_id = gid("pc")
    conn.execute(
        "INSERT INTO profile_changes(id,resident_id,payload_json,requested_by,requested_at,status)"
        " VALUES(?,?,?,?,?,'pending')",
        (change_id, resident_id, json.dumps(payload, ensure_ascii=False),
         requester_id, now_iso()))
    audit(conn, requester_id, "profile_change_request", "resident", resident_id,
          f"待第二人确认:忌口={payload.get('avoidances')}")
    return change_id


def confirm_profile_change(conn, change_id: str, confirmer_id: str,
                           approve: bool, note: str = "") -> None:
    """第二名员工确认;确认人不得与申请人相同。"""
    require_permission(conn, confirmer_id, "profile.confirm")
    row = conn.execute("SELECT * FROM profile_changes WHERE id=?", (change_id,)).fetchone()
    if not row:
        raise DomainError("变更请求不存在")
    if row["status"] != "pending":
        raise DomainError(f"变更请求已处理:{row['status']},不可重复确认")
    if row["requested_by"] == confirmer_id:
        raise DomainError("双人确认失败:确认人必须是另一名员工")

    if not approve:
        conn.execute(
            "UPDATE profile_changes SET status='rejected',confirmed_by=?,confirmed_at=?,"
            "review_note=? WHERE id=?",
            (confirmer_id, now_iso(), note, change_id))
        audit(conn, confirmer_id, "profile_change_reject", "resident",
              row["resident_id"], note)
        return

    payload = json.loads(row["payload_json"])
    rid = row["resident_id"]
    existing = conn.execute("SELECT id,version FROM diet_profiles WHERE resident_id=?",
                            (rid,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE diet_profiles SET avoidances=?,chewing_level=?,effective_date=?,"
            "version=version+1,updated_by=?,updated_at=? WHERE resident_id=?",
            (payload.get("avoidances", ""), payload["chewing_level"],
             payload["effective_date"], confirmer_id, now_iso(), rid))
    else:
        conn.execute(
            "INSERT INTO diet_profiles(id,resident_id,avoidances,chewing_level,"
            "effective_date,version,updated_by,updated_at) VALUES(?,?,?,?,?,1,?,?)",
            (gid("dp"), rid, payload.get("avoidances", ""), payload["chewing_level"],
             payload["effective_date"], confirmer_id, now_iso()))
    conn.execute("DELETE FROM allergens WHERE resident_id=?", (rid,))
    for a in payload.get("allergens", []):
        conn.execute(
            "INSERT INTO allergens(id,resident_id,name,severity) VALUES(?,?,?,?)",
            (gid("al"), rid, a["name"], a["severity"]))
    conn.execute(
        "UPDATE profile_changes SET status='confirmed',confirmed_by=?,confirmed_at=?,"
        "review_note=? WHERE id=?",
        (confirmer_id, now_iso(), note, change_id))
    audit(conn, confirmer_id, "profile_change_confirm", "resident", rid,
          f"双人确认通过 {row['requested_by']}→{confirmer_id}")


def pending_profile_changes(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT pc.*, r.alias FROM profile_changes pc JOIN residents r ON r.id=pc.resident_id"
        " WHERE pc.status='pending' ORDER BY pc.requested_at").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 菜品与菜单审核
# ---------------------------------------------------------------------------

def update_dish_field(conn, dish_id: str, field: str, value, staff_id: str):
    """字段权限:营养字段仅营养师可改;审核状态只能走专用审核动作。"""
    if field not in DISH_FIELD_PERMISSION:
        raise DomainError(f"未知菜品字段: {field}")
    if field == "review_status":
        raise DomainError("审核状态必须通过营养审核动作修改,不能直接编辑")
    require_permission(conn, staff_id, DISH_FIELD_PERMISSION[field])
    if not conn.execute("SELECT 1 FROM dishes WHERE id=?", (dish_id,)).fetchone():
        raise DomainError("菜品不存在")
    if field == "kcal":
        value = int(value)
    conn.execute(f"UPDATE dishes SET {field}=? WHERE id=?", (value, dish_id))
    audit(conn, staff_id, "dish_field_update", "dish", dish_id, f"{field}={value}")


def review_dish(conn, dish_id: str, dietitian_id: str, approve: bool, note: str = ""):
    require_permission(conn, dietitian_id, "dish.review")
    row = conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()
    if not row:
        raise DomainError("菜品不存在")
    status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE dishes SET review_status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
        (status, dietitian_id, now_iso(), note, dish_id))
    audit(conn, dietitian_id, "dish_review", "dish", dish_id, status)


def create_menu(conn, meal_date: str, meal_type: str, creator_id: str) -> str:
    get_staff(conn, creator_id)
    mid = gid("mn")
    conn.execute(
        "INSERT INTO menus(id,meal_date,meal_type,status,created_by) VALUES(?,?,?, 'draft',?)",
        (mid, meal_date, meal_type, creator_id))
    audit(conn, creator_id, "menu_create", "menu", mid, f"{meal_date} {meal_type}")
    return mid


def add_menu_item(conn, menu_id: str, dish_id: str):
    menu = conn.execute("SELECT * FROM menus WHERE id=?", (menu_id,)).fetchone()
    if not menu:
        raise DomainError("菜单不存在")
    if menu["status"] not in ("draft", "rejected"):
        raise DomainError("菜单已提交审核,不能再加菜")
    dish = conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()
    if not dish:
        raise DomainError("菜品不存在")
    if dish["review_status"] != "approved":
        raise DomainError(f"菜品《{dish['name']}》尚未通过营养审核,不能进入菜单")
    conn.execute("INSERT OR IGNORE INTO menu_items(id,menu_id,dish_id) VALUES(?,?,?)",
                 (gid("mi"), menu_id, dish_id))


def submit_menu(conn, menu_id: str):
    menu = conn.execute("SELECT * FROM menus WHERE id=?", (menu_id,)).fetchone()
    if not menu:
        raise DomainError("菜单不存在")
    if menu["status"] not in ("draft", "rejected"):
        raise DomainError("仅草稿/驳回状态可提交审核")
    n = conn.execute("SELECT COUNT(*) c FROM menu_items WHERE menu_id=?", (menu_id,)).fetchone()["c"]
    if n == 0:
        raise DomainError("空菜单不能提交审核")
    conn.execute("UPDATE menus SET status='pending_review',submitted_at=? WHERE id=?",
                 (now_iso(), menu_id))


def review_menu(conn, menu_id: str, dietitian_id: str, approve: bool, note: str = ""):
    """菜单发布前必须经过营养审核。通过即发布。"""
    require_permission(conn, dietitian_id, "menu.review")
    menu = conn.execute("SELECT * FROM menus WHERE id=?", (menu_id,)).fetchone()
    if not menu:
        raise DomainError("菜单不存在")
    if menu["status"] != "pending_review":
        raise DomainError("只有待审核菜单可以审核")
    # 发布前再次校验:全部菜品必须仍是已审核状态
    rows = conn.execute(
        "SELECT d.name, d.review_status FROM menu_items mi JOIN dishes d ON d.id=mi.dish_id"
        " WHERE mi.menu_id=?", (menu_id,)).fetchall()
    bad = [r["name"] for r in rows if r["review_status"] != "approved"]
    if approve and bad:
        raise DomainError("存在未过审菜品,不能发布:" + "、".join(bad))
    status = "published" if approve else "rejected"
    conn.execute(
        "UPDATE menus SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
        (status, dietitian_id, now_iso(), note, menu_id))
    audit(conn, dietitian_id, "menu_review", "menu", menu_id, status)


# ---------------------------------------------------------------------------
# 过敏拦截与配餐
# ---------------------------------------------------------------------------

def check_dish_for_resident(conn, resident: dict, dish: dict) -> list[str]:
    """返回拦截原因列表;空列表表示可食用。"""
    reasons = []
    dish_allergens = set(split_items(dish["contains_allergens"]))
    hit = dish_allergens & resident["allergen_names"]
    if hit:
        sev = max((SEVERITY_RANK[a["severity"]] for a in resident["allergens"]
                   if a["name"] in hit), default=0)
        names = "、".join(sorted(hit))
        reasons.append(f"过敏拦截:菜品含 {names}(住户最高过敏等级 {sev} 级)")
    need = TEXTURE_RANK[resident["profile"]["chewing_level"]] if resident.get("profile") else 0
    have = TEXTURE_RANK[dish["texture"]]
    if have < need:
        reasons.append(
            f"咀嚼要求不符:住户需{TEXTURE_CN[resident['profile']['chewing_level']]},"
            f"菜品为{TEXTURE_CN[dish['texture']]}")
    for av in split_items(resident["profile"]["avoidances"] if resident.get("profile") else ""):
        if av in dish["name"] or av in split_items(dish["contains_allergens"]):
            reasons.append(f"忌口拦截:住户忌口「{av}」")
    return reasons


def menu_dishes(conn, menu_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT d.* FROM menu_items mi JOIN dishes d ON d.id=mi.dish_id "
        "WHERE mi.menu_id=? ORDER BY mi.rowid", (menu_id,)).fetchall()
    return [dict(r) for r in rows]


def approved_dishes(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM dishes WHERE review_status='approved' AND active=1 ORDER BY code")]


def assign_dish(conn, resident: dict, dishes: list[dict]) -> dict:
    """从菜品中选出第一份可食用菜品;否则抛出带原因的异常。"""
    for dish in dishes:
        if not check_dish_for_resident(conn, resident, dish):
            return dish
    reasons = []
    for d in dishes:
        reasons.append(f"《{d['name']}》:" + ";".join(check_dish_for_resident(conn, resident, d)))
    raise DomainError("无可用菜品: " + " | ".join(reasons))


def _issue_label(conn, box_id: str, version: int, reason: str) -> dict:
    """生成全局唯一标签号。旧标签必须先作废。"""
    for _ in range(5):
        token = "L-" + uuid.uuid4().hex[:10].upper()
        if not conn.execute("SELECT 1 FROM box_labels WHERE token=?", (token,)).fetchone():
            break
    else:
        raise DomainError("标签号生成冲突")
    lid = gid("bl")
    conn.execute(
        "INSERT INTO box_labels(id,token,box_id,version,status,reason,created_at)"
        " VALUES(?,?,?,?, 'active',?,?)",
        (lid, token, box_id, version, reason, now_iso()))
    return {"id": lid, "token": token, "version": version}


def produce_batch(conn, menu_id: str, kitchen_id: str,
                  resident_ids: list[str] | None = None,
                  station: str = "中央厨房") -> str:
    """厨房按已发布菜单批次生成餐盒与唯一标签。"""
    require_permission(conn, kitchen_id, "batch.produce")
    menu = conn.execute("SELECT * FROM menus WHERE id=?", (menu_id,)).fetchone()
    if not menu:
        raise DomainError("菜单不存在")
    if menu["status"] != "published":
        raise DomainError("只能按已发布菜单生产批次")
    dishes = menu_dishes(conn, menu_id)
    if not dishes:
        raise DomainError("菜单内没有菜品")

    base = "BT-" + menu["meal_date"].replace("-", "") + "-" + menu["meal_type"][0].upper()
    existing = conn.execute(
        "SELECT COUNT(*) c FROM batches WHERE code LIKE ?", (base + "-%",)).fetchone()["c"]
    code = f"{base}-{existing + 1:02d}"
    bid = gid("bt")
    conn.execute(
        "INSERT INTO batches(id,code,menu_id,station,produced_by,produced_at,status)"
        " VALUES(?,?,?,?,?,?,'produced')",
        (bid, code, menu_id, station, kitchen_id, now_iso()))

    if resident_ids is None:
        resident_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM residents WHERE active=1 ORDER BY care_zone,bed_no")]
    # 同一天的餐盒序号全局递增(跨餐次),保证餐盒编码唯一
    n_today = conn.execute(
        "SELECT COUNT(*) c FROM boxes b JOIN batches bt ON bt.id=b.batch_id "
        "JOIN menus m ON m.id=bt.menu_id WHERE m.meal_date=?",
        (menu["meal_date"],)).fetchone()["c"]
    seq = n_today
    for rid in resident_ids:
        resident = get_resident(conn, rid)
        if not resident.get("profile"):
            raise DomainError(f"住户 {resident['alias']} 缺少饮食档案,不能配餐")
        dish = assign_dish(conn, resident, dishes)
        seq += 1
        box_code = f"BX-{menu['meal_date'].replace('-','')}-{seq:03d}"
        box_id = gid("bx")
        conn.execute(
            "INSERT INTO boxes(id,code,batch_id,resident_id,dish_id,texture,"
            "allergen_snapshot,special_note,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,'packed',?)",
            (box_id, box_code, bid, rid, dish["id"], dish["texture"],
             allergen_snapshot(conn, rid),
             TEXTURE_CN[resident["profile"]["chewing_level"]], now_iso()))
        _issue_label(conn, box_id, 1, "首次贴签")
    audit(conn, kitchen_id, "batch_produce", "batch", bid, f"{code} 配餐 {len(resident_ids)} 份")
    return bid


def active_label(conn, box_id: str):
    row = conn.execute(
        "SELECT * FROM box_labels WHERE box_id=? AND status='active' ORDER BY version DESC",
        (box_id,)).fetchone()
    return dict(row) if row else None


def substitute_meal(conn, box_id: str, new_dish_id: str, staff_id: str, reason: str) -> dict:
    """替换餐只能选已审核菜品;必须重新过过敏拦截;旧标签立即作废并换发新签。"""
    get_staff(conn, staff_id)
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box:
        raise DomainError("餐盒不存在")
    if box["status"] in ("delivered", "refused"):
        raise DomainError("已送达/已拒餐的餐盒不能替换")
    dish = conn.execute("SELECT * FROM dishes WHERE id=?", (new_dish_id,)).fetchone()
    if not dish:
        raise DomainError("菜品不存在")
    if dish["review_status"] != "approved":
        raise DomainError(f"替换餐《{dish['name']}》未通过营养审核,禁止选用")
    resident = get_resident(conn, box["resident_id"])
    blocked = check_dish_for_resident(conn, resident, dict(dish))
    if blocked:
        raise DomainError("替换被拦截:" + ";".join(blocked))

    old = active_label(conn, box_id)
    old_dish = box["dish_id"]
    conn.execute(
        "UPDATE box_labels SET status='void',voided_at=? WHERE id=?",
        (now_iso(), old["id"]))
    conn.execute(
        "INSERT INTO substitutions(id,box_id,old_dish_id,new_dish_id,reason,staff_id,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (gid("sb"), box_id, old_dish, new_dish_id, reason, staff_id, now_iso()))
    conn.execute(
        "UPDATE boxes SET dish_id=?,texture=?,allergen_snapshot=? WHERE id=?",
        (new_dish_id, dish["texture"], allergen_snapshot(conn, box["resident_id"]), box_id))
    label = _issue_label(conn, box_id, old["version"] + 1, f"替换:{reason}")
    if box["status"] == "quarantined":
        conn.execute("UPDATE boxes SET status='in_transit' WHERE id=?", (box_id,))
    # 因温度不合格被跳过、且尚未扫住户码的停靠点恢复为待送,凭新标签继续核对
    conn.execute(
        "UPDATE route_stops SET status='pending' WHERE box_id=? AND status='skipped'"
        " AND resident_scan_at IS NULL", (box_id,))
    audit(conn, staff_id, "meal_substitute", "box", box_id,
          f"旧签 {old['token']} 作废 → 新签 {label['token']}")
    return label


# ---------------------------------------------------------------------------
# 路线、交接、扫描核对
# ---------------------------------------------------------------------------

def create_route_for_batch(conn, batch_id: str, courier_id: str) -> str:
    get_staff(conn, courier_id)
    batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise DomainError("批次不存在")
    menu = conn.execute("SELECT * FROM menus WHERE id=?", (batch["menu_id"],)).fetchone()
    code = "RT-" + batch["code"][3:]
    rid = gid("rt")
    conn.execute(
        "INSERT INTO routes(id,code,meal_date,meal_type,courier_id,status)"
        " VALUES(?,?,?,?,?, 'planned')",
        (rid, code, menu["meal_date"], menu["meal_type"], courier_id))
    rows = conn.execute(
        "SELECT b.id box_id, b.resident_id FROM boxes b JOIN residents r ON r.id=b.resident_id"
        " WHERE b.batch_id=? ORDER BY r.care_zone DESC, r.bed_no", (batch_id,)).fetchall()
    for i, row in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO route_stops(id,route_id,seq,resident_id,box_id,status)"
            " VALUES(?,?,?,?,?, 'pending')",
            (gid("rs"), rid, i, row["resident_id"], row["box_id"]))
    return rid


def hand_over_route(conn, route_id: str, kitchen_id: str) -> None:
    """厨房 → 送餐员交接;交接后餐盒在途,才允许扫描交付。"""
    require_permission(conn, kitchen_id, "route.handover")
    route = conn.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    if not route:
        raise DomainError("路线不存在")
    if route["status"] != "planned":
        raise DomainError(f"路线当前状态 {route['status']},不能重复交接")
    conn.execute(
        "UPDATE routes SET status='handed_over',handed_by=?,handed_at=? WHERE id=?",
        (kitchen_id, now_iso(), route_id))
    conn.execute(
        "UPDATE boxes SET status='in_transit' WHERE id IN"
        " (SELECT box_id FROM route_stops WHERE route_id=?) AND status='packed'",
        (route_id,))
    audit(conn, kitchen_id, "route_handover", "route", route_id,
          f"交给 {route['courier_id']}")


def _log_scan(conn, stop_id, box_id, kind, payload, accepted, reason, staff_id):
    conn.execute(
        "INSERT INTO scan_events(id,stop_id,box_id,kind,payload,accepted,reason,staff_id,at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (gid("se"), stop_id, box_id, kind, payload, 1 if accepted else 0,
         reason, staff_id, now_iso()))


def _reject(conn, message: str):
    """拒绝一次扫描:先把已产生的留痕/隔离落库,再抛出业务异常。

    扫描事件与异常隔离属于"发生即确定"的事实,不应因调用方回滚而消失。
    """
    conn.commit()
    raise DomainError(message)


def _maybe_complete_route(conn, route_id: str):
    row = conn.execute(
        "SELECT SUM(status IN ('delivered','refused','skipped')) done, COUNT(*) total"
        " FROM route_stops WHERE route_id=?", (route_id,)).fetchone()
    if row["done"] == row["total"]:
        conn.execute(
            "UPDATE routes SET status='completed',completed_at=? WHERE id=?",
            (now_iso(), route_id))


def scan(conn, route_id: str, payload: str, staff_id: str,
         active_stop_id: str | None = None) -> dict:
    """送餐端一屏核对的唯一入口。返回 {accepted, stage, message, stop_id?}。

    流程:先扫住户码建立"当前停靠点"(stop_id),再扫餐盒新标签;
    active_stop_id 由页面在住户扫描成功后持有并随餐盒扫描回传。
    """
    require_permission(conn, staff_id, "route.deliver")
    route = conn.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    if not route:
        raise DomainError("路线不存在")
    if route["status"] == "planned":
        raise DomainError("路线尚未由厨房交接,不能开始核对")
    if route["status"] == "completed":
        raise DomainError("路线已完成;重复扫描被拒绝")

    kind, key = qr.parse_scan(payload)

    # ---- 扫住户码 ----
    if kind == "resident":
        stop = conn.execute(
            "SELECT rs.*, r.alias, r.code rcode FROM route_stops rs "
            "JOIN residents r ON r.id=rs.resident_id "
            "WHERE rs.route_id=? AND r.code=?", (route_id, key)).fetchone()
        if not stop:
            _log_scan(conn, None, None, "resident", payload, False,
                      "非本路线住户", staff_id)
            _reject(conn, "扫描失败:该住户不在当前送餐路线上")
        if stop["status"] == "delivered":
            _log_scan(conn, stop["id"], stop["box_id"], "resident", payload, False,
                      "重复扫描", staff_id)
            return {"accepted": False, "stage": "duplicate",
                    "message": f"{stop['alias']} 已送达,请勿重复扫描"}
        if stop["resident_scan_at"]:
            _log_scan(conn, stop["id"], stop["box_id"], "resident", payload, False,
                      "重复扫描", staff_id)
            return {"accepted": False, "stage": "duplicate",
                    "message": f"{stop['alias']} 已扫过,请扫餐盒标签"}
        conn.execute(
            "UPDATE route_stops SET resident_scan_at=?,status='ready' WHERE id=?",
            (now_iso(), stop["id"]))
        _log_scan(conn, stop["id"], stop["box_id"], "resident", payload, True,
                  "住户已核对", staff_id)
        return {"accepted": True, "stage": "resident_ok",
                "message": f"住户核对通过:{stop['alias']},请扫描餐盒新标签",
                "stop_id": stop["id"]}

    # ---- 扫餐盒标签(标签号,旧签作废后扫到即拒) ----
    label = conn.execute("SELECT * FROM box_labels WHERE token=?", (key,)).fetchone()
    if not label:
        # 兼容直接扫餐盒编码:只有在仍存在有效标签时放行
        box_by_code = conn.execute("SELECT * FROM boxes WHERE code=?", (key,)).fetchone()
        if box_by_code:
            label = conn.execute(
                "SELECT * FROM box_labels WHERE box_id=? AND status='active' ORDER BY version DESC",
                (box_by_code["id"],)).fetchone()
        if not label:
            _log_scan(conn, None, None, "box", payload, False, "未知/作废标签", staff_id)
            _reject(conn, "扫描失败:标签不存在或已作废,旧标签不能继续使用")
    if label["status"] == "void":
        _log_scan(conn, None, label["box_id"], "box", payload, False,
                  "旧标签已作废", staff_id)
        _reject(conn, "扫到旧标签(已作废),请使用替换后的新标签")

    box = conn.execute("SELECT * FROM boxes WHERE id=?", (label["box_id"],)).fetchone()
    on_route = conn.execute(
        "SELECT 1 FROM route_stops WHERE route_id=? AND box_id=?",
        (route_id, box["id"])).fetchone()
    if not on_route:
        _log_scan(conn, None, box["id"], "box", payload, False, "非本路线餐盒", staff_id)
        _reject(conn, "扫描失败:该餐盒不在当前路线上")

    # 必须先扫住户,确定当前停靠点
    if not active_stop_id:
        _log_scan(conn, None, box["id"], "box", payload, False, "先扫住户", staff_id)
        _reject(conn, "请先扫描住户码,再扫餐盒标签")
    stop = conn.execute(
        "SELECT rs.*, r.alias FROM route_stops rs JOIN residents r ON r.id=rs.resident_id"
        " WHERE rs.id=? AND rs.route_id=?", (active_stop_id, route_id)).fetchone()
    if not stop:
        raise DomainError("当前核对会话失效,请重新扫描住户码")

    # 错餐:餐盒与当前住户不一致 → 立即隔离
    if box["id"] != stop["box_id"] or box["resident_id"] != stop["resident_id"]:
        _quarantine(conn, box["id"], "wrong_meal",
                    f"错餐:在 {stop['alias']} 处扫到他人餐盒 {box['code']},立即隔离",
                    staff_id, critical=True)
        _log_scan(conn, stop["id"], box["id"], "box", payload, False,
                  "错餐隔离", staff_id)
        _reject(conn, "错餐!该餐盒与当前住户不符,已立即隔离")

    if stop["status"] == "delivered":
        _log_scan(conn, stop["id"], box["id"], "box", payload, False,
                  "重复扫描", staff_id)
        return {"accepted": False, "stage": "duplicate",
                "message": "该餐盒已交付,重复扫描已忽略"}
    if box["status"] == "quarantined":
        _log_scan(conn, stop["id"], box["id"], "box", payload, False,
                  "异常隔离", staff_id)
        _reject(conn, "餐盒处于隔离状态,不能交付")
    if not stop["resident_scan_at"]:
        _log_scan(conn, stop["id"], box["id"], "box", payload, False,
                  "先扫住户", staff_id)
        _reject(conn, "请先扫描住户码,再扫餐盒标签")

    # —— 双码齐全,完成交付 ——
    ts = now_iso()
    conn.execute(
        "UPDATE route_stops SET box_scan_at=?,status='delivered' WHERE id=?",
        (ts, stop["id"]))
    conn.execute("UPDATE boxes SET status='delivered',delivered_at=? WHERE id=?",
                 (ts, box["id"]))
    conn.execute(
        "INSERT INTO deliveries(id,stop_id,box_id,resident_id,courier_id,"
        "resident_scan_at,box_scan_at,delivered_at,result)"
        " VALUES(?,?,?,?,?,?,?,?,'delivered')",
        (gid("dv"), stop["id"], box["id"], stop["resident_id"], staff_id,
         stop["resident_scan_at"], ts, ts))
    _log_scan(conn, stop["id"], box["id"], "box", payload, True, "交付完成", staff_id)
    _maybe_complete_route(conn, route_id)
    audit(conn, staff_id, "delivery_complete", "box", box["id"], stop["alias"])
    return {"accepted": True, "stage": "delivered",
            "message": f"交付完成:{stop['alias']}", "stop_id": stop["id"]}


# ---------------------------------------------------------------------------
# 异常:拒餐 / 温度 / 漏餐 / 隔离 / 家属调整
# ---------------------------------------------------------------------------

def _quarantine(conn, box_id, kind, detail, staff_id, temp=None, critical=False):
    conn.execute("UPDATE boxes SET status='quarantined' WHERE id=?", (box_id,))
    box = conn.execute("SELECT resident_id FROM boxes WHERE id=?", (box_id,)).fetchone()
    iid = gid("in")
    conn.execute(
        "INSERT INTO incidents(id,kind,box_id,resident_id,detail,temp_c,severity,"
        "recorded_by,at,status) VALUES(?,?,?,?,?,?,?,?,?, 'quarantined')",
        (iid, kind, box_id, box["resident_id"], detail, temp,
         "critical" if critical else "major", staff_id, now_iso()))
    audit(conn, staff_id, "incident_quarantine", "box", box_id, detail)
    return iid


def record_temperature(conn, stop_id: str, staff_id: str, temp_c: float) -> dict:
    require_permission(conn, staff_id, "incident.record")
    stop = conn.execute("SELECT * FROM route_stops WHERE id=?", (stop_id,)).fetchone()
    if not stop:
        raise DomainError("停靠点不存在")
    if stop["status"] == "delivered":
        raise DomainError("该站已完成交付,不能再登记温度")
    if temp_c >= HOT_MEAL_MIN_C:
        return {"ok": True, "message": f"中心温度 {temp_c}℃ 合格"}
    iid = _quarantine(conn, stop["box_id"], "temperature",
                      f"热餐中心温度 {temp_c}℃ 低于 {HOT_MEAL_MIN_C:g}℃,隔离待替换",
                      staff_id, temp=temp_c)
    conn.execute("UPDATE route_stops SET status='skipped' WHERE id=?", (stop_id,))
    return {"ok": False, "incident_id": iid,
            "message": f"温度不合格 {temp_c}℃,餐盒已隔离,等待厨房替换"}


def mark_refused(conn, stop_id: str, staff_id: str, reason: str):
    require_permission(conn, staff_id, "incident.record")
    stop = conn.execute("SELECT * FROM route_stops WHERE id=?", (stop_id,)).fetchone()
    if not stop:
        raise DomainError("停靠点不存在")
    if stop["status"] == "delivered":
        raise DomainError("已送达不能登记拒餐")
    if stop["status"] == "refused":
        raise DomainError("该站已登记拒餐,不能重复登记")
    conn.execute("UPDATE route_stops SET status='refused' WHERE id=?", (stop_id,))
    conn.execute("UPDATE boxes SET status='refused' WHERE id=?", (stop["box_id"],))
    iid = gid("in")
    conn.execute(
        "INSERT INTO incidents(id,kind,box_id,resident_id,detail,severity,"
        "recorded_by,at,status) VALUES(?,?,?,?,?, 'minor',?,?, 'open')",
        (iid, "refused", stop["box_id"], stop["resident_id"],
             f"住户拒餐:{reason}", staff_id, now_iso()))
    audit(conn, staff_id, "meal_refused", "box", stop["box_id"], reason)
    _maybe_complete_route(conn, stop["route_id"])
    return iid


def record_missed(conn, resident_id: str, meal_date: str, meal_type: str,
                  staff_id: str, detail: str = "送餐时住户不在,餐次漏送"):
    require_permission(conn, staff_id, "incident.record")
    iid = gid("in")
    conn.execute(
        "INSERT INTO incidents(id,kind,resident_id,detail,severity,recorded_by,at,status)"
        " VALUES(?,?,?,?,'major',?,?, 'open')",
        (iid, "missed", resident_id, f"{meal_date} {meal_type}:{detail}",
         staff_id, now_iso()))
    audit(conn, staff_id, "meal_missed", "resident", resident_id, detail)
    return iid


def resolve_incident(conn, incident_id: str, staff_id: str, resolution: str):
    """异常处置。"""
    require_permission(conn, staff_id, "incident.resolve")
    row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if not row:
        raise DomainError("异常事件不存在")
    if row["status"] == "resolved":
        raise DomainError("异常已处置,不能重复处理")
    conn.execute(
        "UPDATE incidents SET status='resolved',resolution=?,resolved_by=?,resolved_at=?"
        " WHERE id=?", (resolution, staff_id, now_iso(), incident_id))
    audit(conn, staff_id, "incident_resolve", "incident", incident_id, resolution)


def list_incidents(conn, open_only=False):
    sql = ("SELECT i.*, r.alias, b.code box_code FROM incidents i "
           "LEFT JOIN residents r ON r.id=i.resident_id "
           "LEFT JOIN boxes b ON b.id=i.box_id "
           + ("WHERE i.status!='resolved' " if open_only else "")
           + "ORDER BY i.at DESC")
    return [dict(r) for r in conn.execute(sql)]


# ---- 家属临时调整:单独建单、单独确认、单独留痕 ----

def create_family_adjustment(conn, resident_id: str, meal_date: str, meal_type: str,
                             action: str, detail: str, staff_id: str) -> str:
    get_staff(conn, staff_id)
    get_resident(conn, resident_id)
    fid = gid("fa")
    conn.execute(
        "INSERT INTO family_adjustments(id,resident_id,meal_date,meal_type,action,"
        "contact,detail,status,created_at) VALUES(?,?,?,?,?, '家属(匿名)',?, 'pending',?)",
        (fid, resident_id, meal_date, meal_type, action, detail, now_iso()))
    audit(conn, staff_id, "family_adjust_request", "resident", resident_id,
          f"{action}:{detail}")
    return fid


def confirm_family_adjustment(conn, adjust_id: str, staff_id: str,
                              approve: bool, note: str = ""):
    """家属临时调整须员工确认后才执行;确认同时生成独立的 family_adjust 异常记录。"""
    require_permission(conn, staff_id, "family.confirm")
    row = conn.execute("SELECT * FROM family_adjustments WHERE id=?", (adjust_id,)).fetchone()
    if not row:
        raise DomainError("家属调整不存在")
    if row["status"] != "pending":
        raise DomainError("该家属调整已处理")
    if not approve:
        conn.execute(
            "UPDATE family_adjustments SET status='rejected',confirmed_by=?,confirmed_at=?"
            " WHERE id=?", (staff_id, now_iso(), adjust_id))
        return
    iid = gid("in")
    conn.execute(
        "INSERT INTO incidents(id,kind,resident_id,detail,severity,recorded_by,at,status)"
        " VALUES(?,?,?,?, 'minor',?,?, 'resolved')",
        (iid, "family_adjust", row["resident_id"],
         f"家属临时调整 {row['action']}:{row['detail']}({note})",
         staff_id, now_iso()))
    conn.execute(
        "UPDATE family_adjustments SET status='confirmed',confirmed_by=?,confirmed_at=?,"
        "incident_id=? WHERE id=?", (staff_id, now_iso(), iid, adjust_id))
    audit(conn, staff_id, "family_adjust_confirm", "resident", row["resident_id"],
          row["action"])


# ---------------------------------------------------------------------------
# 已送达记录:只允许追加说明
# ---------------------------------------------------------------------------

def add_delivery_note(conn, delivery_id: str, staff_id: str, note: str):
    get_staff(conn, staff_id)
    d = conn.execute("SELECT 1 FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
    if not d:
        raise DomainError("送达记录不存在")
    if not note.strip():
        raise DomainError("说明内容不能为空")
    conn.execute(
        "INSERT INTO delivery_notes(id,delivery_id,note,staff_id,at) VALUES(?,?,?,?,?)",
        (gid("dn"), delivery_id, note.strip(), staff_id, now_iso()))
    audit(conn, staff_id, "delivery_note_append", "delivery", delivery_id, note)


def delivery_detail(conn, delivery_id: str) -> dict:
    d = conn.execute(
        "SELECT dv.*, r.alias, b.code box_code, s.name staff_name FROM deliveries dv "
        "JOIN residents r ON r.id=dv.resident_id JOIN boxes b ON b.id=dv.box_id "
        "JOIN staff s ON s.id=dv.courier_id WHERE dv.id=?", (delivery_id,)).fetchone()
    if not d:
        raise DomainError("送达记录不存在")
    d = dict(d)
    d["notes"] = [dict(r) for r in conn.execute(
        "SELECT dn.*, s.name staff_name FROM delivery_notes dn JOIN staff s ON s.id=dn.staff_id"
        " WHERE delivery_id=? ORDER BY at", (delivery_id,))]
    return d


# ---------------------------------------------------------------------------
# 查询辅助(供页面使用)
# ---------------------------------------------------------------------------

def route_detail(conn, route_id: str) -> dict:
    route = conn.execute(
        "SELECT rt.*, s.name courier_name FROM routes rt LEFT JOIN staff s ON s.id=rt.courier_id"
        " WHERE rt.id=?", (route_id,)).fetchone()
    if not route:
        raise DomainError("路线不存在")
    d = dict(route)
    stops = conn.execute(
        "SELECT rs.*, r.alias, r.bed_no, r.care_zone, b.code box_code, "
        "d.name dish_name, b.allergen_snapshot, b.texture, "
        "bl.token label_token, bl.status label_status, bl.version label_version "
        "FROM route_stops rs JOIN residents r ON r.id=rs.resident_id "
        "JOIN boxes b ON b.id=rs.box_id JOIN dishes d ON d.id=b.dish_id "
        "JOIN box_labels bl ON bl.box_id=b.id AND bl.status='active' "
        "WHERE rs.route_id=? ORDER BY rs.seq", (route_id,)).fetchall()
    d["stops"] = [dict(r) for r in stops]
    return d


def batch_detail(conn, batch_id: str) -> dict:
    b = conn.execute(
        "SELECT bt.*, m.meal_date, m.meal_type FROM batches bt "
        "JOIN menus m ON m.id=bt.menu_id WHERE bt.id=?", (batch_id,)).fetchone()
    if not b:
        raise DomainError("批次不存在")
    d = dict(b)
    d["boxes"] = [dict(r) for r in conn.execute(
        "SELECT b.*, r.alias, r.bed_no, r.care_zone, dish.name dish_name, "
        "bl.token label_token, bl.version label_version, bl.status label_status "
        "FROM boxes b JOIN residents r ON r.id=b.resident_id "
        "JOIN dishes dish ON dish.id=b.dish_id "
        "LEFT JOIN box_labels bl ON bl.id=(SELECT id FROM box_labels WHERE box_id=b.id "
        "ORDER BY version DESC LIMIT 1) WHERE b.batch_id=? ORDER BY r.care_zone,r.bed_no",
        (batch_id,))]
    return d


def list_routes(conn):
    return [dict(r) for r in conn.execute(
        "SELECT rt.*, s.name courier_name, "
        "(SELECT COUNT(*) FROM route_stops WHERE route_id=rt.id) total, "
        "(SELECT COUNT(*) FROM route_stops WHERE route_id=rt.id AND status='delivered') done "
        "FROM routes rt LEFT JOIN staff s ON s.id=rt.courier_id ORDER BY rt.meal_date,rt.code")]


def list_batches(conn):
    return [dict(r) for r in conn.execute(
        "SELECT bt.*, m.meal_date, m.meal_type, "
        "(SELECT COUNT(*) FROM boxes WHERE batch_id=bt.id) box_count "
        "FROM batches bt JOIN menus m ON m.id=bt.menu_id ORDER BY bt.produced_at DESC")]


def list_menus(conn):
    return [dict(r) for r in conn.execute(
        "SELECT mn.*, s.name creator_name, "
        "(SELECT COUNT(*) FROM menu_items WHERE menu_id=mn.id) item_count "
        "FROM menus mn LEFT JOIN staff s ON s.id=mn.created_by ORDER BY mn.meal_date,mn.meal_type")]


def menu_detail(conn, menu_id: str) -> dict:
    m = conn.execute("SELECT * FROM menus WHERE id=?", (menu_id,)).fetchone()
    if not m:
        raise DomainError("菜单不存在")
    d = dict(m)
    d["items"] = [dict(r) for r in conn.execute(
        "SELECT d.* FROM menu_items mi JOIN dishes d ON d.id=mi.dish_id WHERE mi.menu_id=?",
        (menu_id,))]
    return d
