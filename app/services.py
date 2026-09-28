"""核心领域服务：过敏拦截、菜单审核、批次标签、替换餐、路线交接、
扫描交付、异常隔离、家属调整、双人确认与字段权限。

所有写操作均在传入的 sqlite 连接上执行并提交。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date

from db import audit, jdumps, jloads, now
from qradapter import LocalScanner, ScanError

scanner = LocalScanner()


class DomainError(Exception):
    """业务规则冲突（过敏、未审核、旧标签等）。"""

    def __init__(self, message: str, code: str = "domain", extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


# ---------------------------------------------------------------------
# 角色与字段权限
# ---------------------------------------------------------------------

ROLE_NAMES = {
    "manager": "管理员",
    "nutritionist": "营养师",
    "nurse": "护理员",
    "kitchen": "厨房",
    "courier": "送餐员",
}

# 各角色可执行的动作域
PERMISSIONS = {
    "manager": {"*"},
    "nutritionist": {
        "dish.review", "menu.review", "menu.create", "dish.create",
        "profile.view", "resident.view", "kitchen.view", "route.view",
        "event.view", "event.handle", "family.view",
    },
    "nurse": {
        "resident.view", "profile.view", "profile.propose", "profile.confirm",
        "family.view", "family.handle",
        "route.view", "delivery.note", "event.view", "event.handle",
        "delivery.refuse",
    },
    "kitchen": {
        "menu.create", "dish.create", "batch.create", "batch.prepare",
        "box.substitute", "box.void", "temp.record", "label.print",
        "resident.view", "profile.view", "menu.review.view", "route.view",
        "event.view", "family.view",
    },
    "courier": {
        "route.view", "route.start", "route.handover", "route.complete",
        "scan.resident", "scan.box", "scan.route", "delivery.confirm",
        "delivery.refuse", "delivery.note", "box.quarantine",
        "resident.view", "profile.view", "event.view", "event.record",
        "family.view",
    },
}

# 住户档案字段的编辑权限：只有护理员/管理员可发起，且必须第二名员工确认
PROFILE_EDITABLE = {"allergies", "avoidances", "chewing", "special_diet",
                    "effective_date", "room", "floor"}
ALLERGY_FIELDS = {"allergies"}  # 高敏字段：确认页固定高对比提示


def can(role: str, action: str) -> bool:
    perms = PERMISSIONS.get(role, set())
    return "*" in perms or action in perms


def require(role: str, action: str):
    if not can(role, action):
        raise DomainError(
            f"{ROLE_NAMES.get(role, role)}无权执行「{action}」", code="forbidden")


def staff(conn, staff_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM staff WHERE id=? AND active=1", (staff_id,)).fetchone()


def require_staff(conn, staff_id: str) -> sqlite3.Row:
    row = staff(conn, staff_id)
    if not row:
        raise DomainError("员工不存在或已停用", code="auth")
    return row


# ---------------------------------------------------------------------
# 质地（咀嚼要求）：等级越高越细软
# ---------------------------------------------------------------------

TEXTURE_RANK = {"normal": 0, "soft": 1, "minced": 2, "pureed": 3}
TEXTURE_NAMES = {"normal": "普通", "soft": "软食", "minced": "碎末", "pureed": "匀浆"}
ALLERGY_LEVEL = {"severe": "严重", "warning": "慎食"}


# ---------------------------------------------------------------------
# 住户档案
# ---------------------------------------------------------------------

def resident(conn, resident_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM residents WHERE id=?", (resident_id,)).fetchone()


def resident_by_code(conn, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM residents WHERE code=? ", (code,)).fetchone()


def list_residents(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM residents ORDER BY floor, room").fetchall()


def resident_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "code": row["code"], "alias": row["alias"],
        "floor": row["floor"], "room": row["room"],
        "allergies": jloads(row["allergies"], []),
        "avoidances": jloads(row["avoidances"], []),
        "chewing": row["chewing"], "chewing_name": TEXTURE_NAMES.get(row["chewing"], row["chewing"]),
        "special_diet": row["special_diet"],
        "effective_date": row["effective_date"],
        "card_status": row["card_status"],
    }


def severe_allergens(res: dict) -> list[str]:
    return [a["allergen"] for a in res["allergies"] if a.get("level") == "severe"]


def propose_profile_change(conn, actor_id: str, resident_id: str,
                           patch: dict, reason: str) -> str:
    """发起档案修改：不直接生效，必须由第二名员工确认。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "profile.propose")
    row = resident(conn, resident_id)
    if not row:
        raise DomainError("住户不存在", code="not_found")
    bad = set(patch) - PROFILE_EDITABLE
    if bad:
        raise DomainError(f"不可修改的字段：{','.join(sorted(bad))}", code="field_permission")
    if not reason.strip():
        raise DomainError("档案修改必须填写原因", code="validation")
    # 规范化并做基础校验
    clean = _clean_profile_patch(patch)
    change_id = f"PC-{uuid.uuid4().hex[:12]}"
    conn.execute(
        """INSERT INTO profile_changes(id,resident_id,patch,reason,status,
               created_by,created_at) VALUES(?,?,?,?,?,?,?)""",
        (change_id, resident_id, jdumps(clean), reason, "pending", actor_id, now()),
    )
    conn.execute("UPDATE residents SET card_status='needs_confirm', updated_at=? WHERE id=?",
                 (now(), resident_id))
    audit(conn, actor_id, "profile.propose", "resident", resident_id,
          {"patch": clean, "reason": reason, "change_id": change_id})
    conn.commit()
    return change_id


def _clean_profile_patch(patch: dict) -> dict:
    clean = {}
    if "allergies" in patch:
        items = patch["allergies"]
        if not isinstance(items, list):
            raise DomainError("过敏项格式错误", code="validation")
        norm = []
        for it in items:
            if not isinstance(it, dict) or not it.get("allergen"):
                raise DomainError("过敏项必须包含过敏原名称", code="validation")
            level = it.get("level", "warning")
            if level not in ALLERGY_LEVEL:
                raise DomainError("过敏等级必须是 severe/warning", code="validation")
            norm.append({"allergen": str(it["allergen"]).strip(),
                         "level": level, "note": str(it.get("note", ""))})
        clean["allergies"] = norm
    if "avoidances" in patch:
        items = patch["avoidances"]
        if not isinstance(items, list) or not all(isinstance(x, str) and x.strip() for x in items):
            raise DomainError("忌口必须是字符串列表", code="validation")
        clean["avoidances"] = [x.strip() for x in items]
    if "chewing" in patch:
        if patch["chewing"] not in TEXTURE_RANK:
            raise DomainError("咀嚼要求不合法", code="validation")
        clean["chewing"] = patch["chewing"]
    for k in ("special_diet", "effective_date", "room", "floor"):
        if k in patch:
            clean[k] = str(patch[k]).strip()
    if "effective_date" in clean:
        try:
            date.fromisoformat(clean["effective_date"])
        except ValueError:
            raise DomainError("生效日期格式应为 YYYY-MM-DD", code="validation")
    return clean


def pending_changes(conn, resident_id: str | None = None):
    sql = """SELECT pc.*, r.alias AS resident_alias, s.name AS created_by_name
             FROM profile_changes pc
             JOIN residents r ON r.id=pc.resident_id
             JOIN staff s ON s.id=pc.created_by
             WHERE pc.status='pending'"""
    args = []
    if resident_id:
        sql += " AND pc.resident_id=?"
        args.append(resident_id)
    sql += " ORDER BY pc.created_at"
    return conn.execute(sql, args).fetchall()


def confirm_profile_change(conn, change_id: str, confirmer_id: str,
                           accept: bool, note: str = "") -> sqlite3.Row:
    """第二名员工确认/驳回。确认人不得是发起人本人。"""
    confirmer = require_staff(conn, confirmer_id)
    require(confirmer["role"], "profile.confirm")
    chg = conn.execute("SELECT * FROM profile_changes WHERE id=?", (change_id,)).fetchone()
    if not chg:
        raise DomainError("修改单不存在", code="not_found")
    if chg["status"] != "pending":
        raise DomainError("该修改单已处理，不能重复确认", code="state")
    if chg["created_by"] == confirmer_id:
        raise DomainError("档案修改必须由第二名员工确认，不能本人确认", code="second_eye")

    if accept:
        patch = jloads(chg["patch"], {})
        sets, args = [], []
        for k, v in patch.items():
            sets.append(f"{k}=?")
            args.append(jdumps(v) if isinstance(v, (list, dict)) else v)
        sets.append("card_status='active'")
        sets.append("updated_at=?")
        args.append(now())
        args.append(chg["resident_id"])
        conn.execute(f"UPDATE residents SET {','.join(sets)} WHERE id=?", args)
        new_status = "confirmed"
    else:
        new_status = "rejected"
        conn.execute("UPDATE residents SET card_status='active', updated_at=? WHERE id=?",
                     (now(), chg["resident_id"]))
    conn.execute(
        """UPDATE profile_changes SET status=?, confirmed_by=?, confirmed_at=?, review_note=?
           WHERE id=?""",
        (new_status, confirmer_id, now(), note, change_id),
    )
    audit(conn, confirmer_id, f"profile.{new_status}", "profile_change", change_id,
          {"note": note})
    conn.commit()
    return conn.execute("SELECT * FROM profile_changes WHERE id=?", (change_id,)).fetchone()


# ---------------------------------------------------------------------
# 过敏 / 忌口 / 质地 拦截
# ---------------------------------------------------------------------

def dish_allergens(row: sqlite3.Row) -> list[str]:
    return list(jloads(row["allergens"], []))


def check_dish_for_resident(dish: sqlite3.Row, res: dict) -> dict:
    """返回 {blocked:[...], warnings:[...], texture_ok:bool}。"""
    blocked, warnings = [], []
    allergens = set(dish_allergens(dish))
    for a in res["allergies"]:
        if a["allergen"] in allergens:
            target = blocked if a["level"] == "severe" else warnings
            target.append(f"{a['allergen']}过敏（{ALLERGY_LEVEL[a['level']]}）")
    for av in res["avoidances"]:
        if av in allergens:
            blocked.append(f"忌口：{av}")
    # 忌口也可能是饮食偏好词，命中菜品名（如“辛辣”作为标签）
    tags = set(jloads(dish["diet_tags"], []))
    for av in res["avoidances"]:
        if av in tags and f"忌口：{av}" not in blocked:
            blocked.append(f"忌口标签：{av}")
    texture_ok = TEXTURE_RANK[dish["texture"]] >= TEXTURE_RANK[res["chewing"]]
    if not texture_ok:
        blocked.append(f"质地不符：菜品{TEXTURE_NAMES[dish['texture']]}/住户需"
                       f"{res['chewing_name']}")
    if res.get("special_diet"):
        need = res["special_diet"]
        if need == "糖尿病餐" and "糖尿病适餐" not in tags:
            warnings.append("未标注糖尿病适餐")
        if need == "低盐" and "低盐" not in tags:
            warnings.append("未标注低盐")
    return {"blocked": blocked, "warnings": warnings, "texture_ok": texture_ok}


def assert_safe_dishes(conn, dish_ids: list[str], res, *, allow_warning=False):
    """拦截核心：任何严重过敏/忌口/质地不符都拒绝；warning 默认也拦截。

    res 可以是 resident 行或 resident_view() 字典。
    """
    if isinstance(res, sqlite3.Row):
        rv = resident_view(res)
    elif isinstance(res, dict) and "chewing_name" in res:
        rv = res
    else:
        rv = resident_view(_res_row(conn, res))
    problems = []
    for did in dish_ids:
        d = conn.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone()
        if not d:
            raise DomainError(f"菜品 {did} 不存在", code="not_found")
        chk = check_dish_for_resident(d, rv)
        problems.extend(f"{d['name']}：{b}" for b in chk["blocked"])
        if not allow_warning:
            problems.extend(f"{d['name']}：{w}" for w in chk["warnings"])
    if problems:
        raise DomainError("过敏/饮食限制拦截：" + "；".join(problems),
                          code="allergy_block", extra={"problems": problems})


def _res_row(conn, res) -> sqlite3.Row:
    if isinstance(res, sqlite3.Row):
        return res
    row = resident(conn, res if isinstance(res, str) else res.get("id"))
    if not row:
        raise DomainError("住户不存在", code="not_found")
    return row


# ---------------------------------------------------------------------
# 菜品审核
# ---------------------------------------------------------------------

def review_dish(conn, actor_id: str, dish_id: str, approve: bool, note: str = ""):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "dish.review")
    d = conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()
    if not d:
        raise DomainError("菜品不存在", code="not_found")
    new_status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE dishes SET review_status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
        (new_status, actor_id, now(), note, dish_id),
    )
    audit(conn, actor_id, f"dish.{new_status}", "dish", dish_id, {"note": note})
    conn.commit()
    return new_status


# ---------------------------------------------------------------------
# 菜单与营养审核
# ---------------------------------------------------------------------

def menu(conn, menu_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM menus WHERE id=?", (menu_id,)).fetchone()


def menu_dishes(conn, menu_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.* FROM menu_items mi JOIN dishes d ON d.id=mi.dish_id
           WHERE mi.menu_id=? ORDER BY mi.seq""", (menu_id,)).fetchall()


def add_menu_item(conn, actor_id: str, menu_id: str, dish_id: str):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "menu.create")
    m = menu(conn, menu_id)
    if not m:
        raise DomainError("菜单不存在", code="not_found")
    if m["status"] in ("approved", "published"):
        raise DomainError("菜单已审核发布，不能再增改菜品（请新建菜单）", code="state")
    d = conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()
    if not d:
        raise DomainError("菜品不存在", code="not_found")
    if d["review_status"] != "approved":
        raise DomainError(f"《{d['name']}》尚未通过营养审核，不能加入菜单",
                          code="not_reviewed")
    exists = conn.execute("SELECT 1 FROM menu_items WHERE menu_id=? AND dish_id=?",
                          (menu_id, dish_id)).fetchone()
    if exists:
        raise DomainError("菜品已在菜单中", code="duplicate")
    seq = conn.execute("SELECT COALESCE(MAX(seq)+1,0) FROM menu_items WHERE menu_id=?",
                       (menu_id,)).fetchone()[0]
    conn.execute("INSERT INTO menu_items(menu_id,dish_id,seq) VALUES(?,?,?)",
                 (menu_id, dish_id, seq))
    audit(conn, actor_id, "menu.add_item", "menu", menu_id, {"dish": dish_id})
    conn.commit()


def submit_menu(conn, actor_id: str, menu_id: str):
    actor = require_staff(conn, actor_id)
    m = menu(conn, menu_id)
    if not m:
        raise DomainError("菜单不存在", code="not_found")
    if m["status"] != "draft":
        raise DomainError("只有草稿菜单可提交审核", code="state")
    items = menu_dishes(conn, menu_id)
    if not items:
        raise DomainError("菜单为空，不能提交审核", code="validation")
    unapproved = [d["name"] for d in items if d["review_status"] != "approved"]
    if unapproved:
        raise DomainError("存在未审核菜品：" + "、".join(unapproved), code="not_reviewed")
    conn.execute("UPDATE menus SET status='pending' WHERE id=?", (menu_id,))
    audit(conn, actor_id, "menu.submit", "menu", menu_id)
    conn.commit()


def menu_safety_report(conn, menu_id: str) -> dict:
    """营养审核辅助：列出菜单含有的过敏原，以及受影响住户。"""
    items = menu_dishes(conn, menu_id)
    allergen_map: dict[str, list[str]] = {}
    for d in items:
        for a in dish_allergens(d):
            allergen_map.setdefault(a, []).append(d["name"])
    affected = []
    for r in list_residents(conn):
        rv = resident_view(r)
        hits = []
        for d in items:
            chk = check_dish_for_resident(d, rv)
            if chk["blocked"] or chk["warnings"]:
                hits.append({"dish": d["name"],
                             "issues": chk["blocked"] + chk["warnings"]})
        if hits:
            affected.append({"resident": rv["alias"], "room": rv["room"],
                             "severe": severe_allergens(rv), "hits": hits})
    return {"allergens": {a: sorted(set(names)) for a, names in allergen_map.items()},
            "affected": affected}


def review_menu(conn, actor_id: str, menu_id: str, approve: bool, note: str = "",
                publish: bool = True):
    """菜单发布前必须经过营养审核。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "menu.review")
    m = menu(conn, menu_id)
    if not m:
        raise DomainError("菜单不存在", code="not_found")
    if m["status"] not in ("pending", "rejected"):
        raise DomainError(f"菜单状态为 {m['status']}，不能审核", code="state")
    items = menu_dishes(conn, menu_id)
    unapproved = [d["name"] for d in items if d["review_status"] != "approved"]
    if approve and unapproved:
        raise DomainError("存在未审核菜品：" + "、".join(unapproved), code="not_reviewed")
    new_status = "published" if approve and publish else ("approved" if approve else "rejected")
    conn.execute(
        """UPDATE menus SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?""",
        (new_status, actor_id, now(), note, menu_id),
    )
    audit(conn, actor_id, f"menu.{new_status}", "menu", menu_id, {"note": note})
    conn.commit()
    return new_status


# ---------------------------------------------------------------------
# 厨房批次与餐盒标签
# ---------------------------------------------------------------------

def list_batches(conn, serve_date: str | None = None):
    sql = "SELECT * FROM batches"
    args = []
    if serve_date:
        sql += " WHERE serve_date=?"
        args.append(serve_date)
    sql += " ORDER BY created_at DESC"
    return conn.execute(sql, args).fetchall()


def box(conn, box_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM meal_boxes WHERE id=?", (box_id,)).fetchone()


def box_by_label(conn, label_code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM meal_boxes WHERE label_code=?", (label_code,)).fetchone()


def batch_boxes(conn, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM meal_boxes WHERE batch_id=? ORDER BY label_code",
                        (batch_id,)).fetchall()


def _next_label(conn, serve_date: str, session: str) -> str:
    prefix = f"{serve_date.replace('-', '')}-{session[0].upper()}-"
    row = conn.execute(
        "SELECT label_code FROM meal_boxes WHERE label_code LIKE ? ORDER BY label_code DESC LIMIT 1",
        (prefix + "%",)).fetchone()
    nxt = 1
    if row:
        nxt = int(row["label_code"].rsplit("-", 1)[1]) + 1
    return f"{prefix}{nxt:04d}"


def auto_plan_for_resident(conn, rv: dict, menu_items: list[sqlite3.Row]) -> list[str]:
    """从已发布菜单中为住户挑选安全餐：每个类别选一道安全菜。

    同类有多道安全菜时，优先选质地最接近住户所需的（不过度软烂化）。
    """
    cats = ["staple", "meat", "veg", "soup", "fruit", "egg", "soy"]
    need = TEXTURE_RANK[rv["chewing"]]
    chosen = []
    for cat in cats:
        safe = []
        for d in menu_items:
            if d["category"] != cat:
                continue
            chk = check_dish_for_resident(d, rv)
            if not chk["blocked"] and not chk["warnings"]:
                safe.append(d)
        if not safe:
            continue
        # 质地差最小（菜品等级 - 住户需求），即刚好满足且不额外软烂
        safe.sort(key=lambda d: TEXTURE_RANK[d["texture"]] - need)
        chosen.append(safe[0]["id"])
    if not any(
            conn.execute("SELECT 1 FROM dishes WHERE id=? AND category='staple'", (did,)).fetchone()
            for did in chosen):
        raise DomainError(f"{rv['alias']}（{rv['room']}）在本菜单中没有安全主食，需替换餐",
                          code="no_safe_plan", extra={"resident": rv})
    return chosen


def generate_batch(conn, actor_id: str, menu_id: str) -> str:
    """按已发布菜单生成批次与每位住户的餐盒标签。标签全局唯一。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "batch.create")
    m = menu(conn, menu_id)
    if not m:
        raise DomainError("菜单不存在", code="not_found")
    if m["status"] != "published":
        raise DomainError("菜单未通过营养审核发布，不能生成批次", code="not_reviewed")
    exists = conn.execute("SELECT 1 FROM batches WHERE menu_id=?", (menu_id,)).fetchone()
    if exists:
        raise DomainError("该菜单已生成过批次", code="duplicate")

    batch_id = f"B{m['serve_date'].replace('-', '')}-{m['meal_session'][0].upper()}"
    conn.execute(
        """INSERT INTO batches(id,serve_date,meal_session,menu_id,status,created_by,
               created_at,note) VALUES(?,?,?,?,?,?,?,?)""",
        (batch_id, m["serve_date"], m["meal_session"], menu_id, "planned", actor_id, now(),
         "按已审核菜单自动配餐"),
    )
    items = menu_dishes(conn, menu_id)
    failures = []
    for r in list_residents(conn):
        rv = resident_view(r)
        try:
            dish_ids = auto_plan_for_resident(conn, rv, items)
        except DomainError as e:
            failures.append(str(e))
            continue
        label = _next_label(conn, m["serve_date"], m["meal_session"])
        conn.execute(
            """INSERT INTO meal_boxes(id,label_code,batch_id,resident_id,dishes,
                   status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
            (f"BX-{label}", label, batch_id, rv["id"], jdumps(dish_ids),
             "labeled", now(), now()),
        )
    audit(conn, actor_id, "batch.generate", "batch", batch_id,
          {"menu": menu_id, "failures": failures})
    conn.commit()
    if failures:
        raise DomainError("批次已生成，但部分住户无安全配餐：" + "；".join(failures),
                          code="partial", extra={"batch_id": batch_id})
    return batch_id


def prepare_batch(conn, actor_id: str, batch_id: str):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "batch.prepare")
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        raise DomainError("批次不存在", code="not_found")
    conn.execute("UPDATE batches SET status='prepared', prepared_at=? WHERE id=?",
                 (now(), batch_id))
    audit(conn, actor_id, "batch.prepare", "batch", batch_id)
    conn.commit()


def record_temperature(conn, actor_id: str, box_id: str, temp_c: float):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "temp.record")
    b = box(conn, box_id)
    if not b:
        raise DomainError("餐盒不存在", code="not_found")
    conn.execute("UPDATE meal_boxes SET temp_c=?, temp_at=?, updated_at=? WHERE id=?",
                 (temp_c, now(), now(), box_id))
    if temp_c < 60.0:
        ev = f"EV-{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO meal_events(id,type,serve_date,meal_session,resident_id,
                   meal_box_id,detail,temp_c,status,recorded_by,created_at)
               SELECT ?, 'temp_fail', b.serve_date, b.meal_session, b.resident_id, b.id,
                      ?, ?, 'open', ?, ? FROM batches b WHERE b.id=?""",
            (ev, f"中心温度 {temp_c}℃ 低于 60℃，需复检或隔离", temp_c, actor_id, now(),
             b["batch_id"]),
        )
        audit(conn, actor_id, "event.temp_fail", "meal_box", box_id, {"temp": temp_c})
    conn.commit()
    return temp_c >= 60.0


# ---------------------------------------------------------------------
# 作废 / 替换餐（只能选已审核菜品；旧标签立即失效）
# ---------------------------------------------------------------------

def void_box(conn, actor_id: str, box_id: str, reason: str):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "box.void")
    _void_box_core(conn, actor_id, box_id, reason)


def _void_box_core(conn, actor_id: str, box_id: str, reason: str):
    """作废核心（不做角色检查）：家属确认等已授权流程内部复用。"""
    b = box(conn, box_id)
    if not b:
        raise DomainError("餐盒不存在", code="not_found")
    if b["status"] == "void":
        raise DomainError("标签已作废", code="state")
    if b["status"] == "delivered":
        raise DomainError("已送达餐盒不能作废", code="state")
    conn.execute(
        "UPDATE meal_boxes SET status='void', void_reason=?, updated_at=? WHERE id=?",
        (reason, now(), box_id))
    audit(conn, actor_id, "box.void", "meal_box", box_id, {"reason": reason})
    conn.commit()


def substitute_box(conn, actor_id: str, old_box_id: str, new_dish_ids: list[str],
                   reason: str, resident_id: str | None = None) -> str:
    """生成替换餐：旧标签作废，新标签唯一，所有新菜品必须已审核且通过住户拦截。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "box.substitute")
    return _substitute_core(conn, actor_id, old_box_id, new_dish_ids, reason, resident_id)


def _substitute_core(conn, actor_id: str, old_box_id: str, new_dish_ids: list[str],
                     reason: str, resident_id: str | None = None) -> str:
    """替换核心（不做角色检查）：家属确认流程内部复用，规则（已审核菜/过敏拦截/
    旧标签作废/标签唯一/站点换绑）完全一致。"""
    old = box(conn, old_box_id)
    if not old:
        raise DomainError("原餐盒不存在", code="not_found")
    if old["status"] == "void":
        raise DomainError("原餐盒标签已是作废状态", code="state")
    rid = resident_id or old["resident_id"]
    if not rid:
        raise DomainError("备用餐替换时必须指定住户", code="validation")
    r = resident(conn, rid)
    if not r:
        raise DomainError("住户不存在", code="not_found")
    rv = resident_view(r)

    if not new_dish_ids:
        raise DomainError("替换餐至少包含一道菜", code="validation")
    for did in new_dish_ids:
        d = conn.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone()
        if not d:
            raise DomainError(f"菜品 {did} 不存在", code="not_found")
        if d["review_status"] != "approved":
            raise DomainError(f"替换餐只能选已审核菜品：《{d['name']}》尚未审核",
                              code="not_reviewed")
    assert_safe_dishes(conn, new_dish_ids, rv)  # 过敏/忌口/质地拦截

    b = conn.execute("SELECT * FROM batches WHERE id=?", (old["batch_id"],)).fetchone()
    label = _next_label(conn, b["serve_date"], b["meal_session"])
    new_id = f"BX-{label}"
    conn.execute(
        """INSERT INTO meal_boxes(id,label_code,batch_id,resident_id,dishes,
               is_substitution,sub_reason,replaces_box_id,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (new_id, label, old["batch_id"], rid, jdumps(new_dish_ids), 1, reason,
         old_box_id, "labeled", now(), now()),
    )
    conn.execute(
        "UPDATE meal_boxes SET status='void', void_reason=?, updated_at=? WHERE id=?",
        (f"替换为新标签 {label}：{reason}", now(), old_box_id))
    # 同步路线站点指向新餐盒
    conn.execute("UPDATE route_stops SET meal_box_id=?, status='pending', "
                 "resident_scan_at=NULL, box_scan_at=NULL, delivered_at=NULL "
                 "WHERE meal_box_id=?", (new_id, old_box_id))
    audit(conn, actor_id, "box.substitute", "meal_box", new_id,
          {"old": old_box_id, "dishes": new_dish_ids, "reason": reason})
    conn.commit()
    return new_id


# ---------------------------------------------------------------------
# 路线与交接
# ---------------------------------------------------------------------

def list_routes(conn, serve_date: str | None = None):
    sql = "SELECT * FROM routes"
    args = []
    if serve_date:
        sql += " WHERE serve_date=?"
        args.append(serve_date)
    sql += " ORDER BY created_at"
    return conn.execute(sql, args).fetchall()


def route(conn, route_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()


def route_stops(conn, route_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT rs.*, r.alias, r.room, r.code AS resident_code, r.allergies,
                  r.avoidances, r.chewing, mb.label_code, mb.status AS box_status,
                  mb.is_substitution
           FROM route_stops rs
           JOIN residents r ON r.id=rs.resident_id
           LEFT JOIN meal_boxes mb ON mb.id=rs.meal_box_id
           WHERE rs.route_id=? ORDER BY rs.seq""", (route_id,)).fetchall()


def start_route(conn, actor_id: str, route_id: str):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "route.start")
    rt = route(conn, route_id)
    if not rt:
        raise DomainError("路线不存在", code="not_found")
    if rt["status"] not in ("planned", "handed_over"):
        raise DomainError("路线已出发或已完成", code="state")
    new_status = "in_transit"
    conn.execute("UPDATE routes SET status=? WHERE id=?", (new_status, route_id))
    audit(conn, actor_id, "route.start", "route", route_id)
    conn.commit()


def handover_route(conn, actor_id: str, route_id: str, to_courier_id: str):
    """送餐员之间交接：扫码 ROUTE:<id> 后完成。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "route.handover")
    to = require_staff(conn, to_courier_id)
    if to["role"] != "courier":
        raise DomainError("接收人必须是送餐员", code="validation")
    rt = route(conn, route_id)
    if not rt:
        raise DomainError("路线不存在", code="not_found")
    if actor_id == to_courier_id:
        raise DomainError("交接双方不能是同一人", code="validation")
    if rt["status"] not in ("in_transit", "planned"):
        raise DomainError("当前路线状态不能交接", code="state")
    conn.execute(
        """UPDATE routes SET status='in_transit', courier_id=?, handover_from=?,
               handover_to=?, handover_at=? WHERE id=?""",
        (to_courier_id, actor_id, to_courier_id, now(), route_id),
    )
    audit(conn, actor_id, "route.handover", "route", route_id, {"to": to_courier_id})
    conn.commit()


def complete_route(conn, actor_id: str, route_id: str):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "route.complete")
    rt = route(conn, route_id)
    if not rt:
        raise DomainError("路线不存在", code="not_found")
    pending = conn.execute(
        "SELECT COUNT(*) FROM route_stops WHERE route_id=? AND status='pending'",
        (route_id,)).fetchone()[0]
    if pending:
        raise DomainError(f"还有 {pending} 个站点未完成，不能结束路线", code="state")
    conn.execute("UPDATE routes SET status='completed', completed_at=? WHERE id=?",
                 (now(), route_id))
    audit(conn, actor_id, "route.complete", "route", route_id)
    conn.commit()


# ---------------------------------------------------------------------
# 扫描与交付（一屏核对：先住户码后餐盒码）
# ---------------------------------------------------------------------

def _log_scan(conn, stop_id, courier_id, kind, code, result, detail=""):
    conn.execute(
        """INSERT INTO scans(id,stop_id,courier_id,kind,code,result,detail,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (f"SC-{uuid.uuid4().hex[:12]}", stop_id, courier_id, kind, code, result,
         detail, now()),
    )


def _record_event(conn, etype, res_id, box_id, route_id, detail, actor_id, temp=None):
    ev = f"EV-{uuid.uuid4().hex[:12]}"
    rt = route(conn, route_id) if route_id else None
    conn.execute(
        """INSERT INTO meal_events(id,type,serve_date,meal_session,resident_id,
               meal_box_id,route_id,detail,temp_c,status,recorded_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ev, etype, rt["serve_date"] if rt else date.today().isoformat(),
         rt["meal_session"] if rt else "", res_id, box_id, route_id, detail, temp,
         "open", actor_id, now()),
    )
    return ev


def scan_route_code(conn, actor_id: str, route_id: str, raw: str) -> dict:
    """处理路线交接码。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "scan.route")
    try:
        kind, code = scanner.scan(raw)
    except ScanError as e:
        raise DomainError(str(e), code="bad_qr")
    if kind != "ROUTE":
        raise DomainError("该二维码不是路线交接码", code="wrong_kind")
    rt = route(conn, route_id)
    if not rt:
        raise DomainError("路线不存在", code="not_found")
    # 交接码载荷即路线 id
    if code != route_id:
        _log_scan(conn, None, actor_id, kind, code, "mismatch",
                  f"路线码 {code} 与当前路线 {route_id} 不符")
        conn.commit()
        raise DomainError("路线码与当前路线不符", code="mismatch")
    _log_scan(conn, None, actor_id, kind, code, "ok", "路线交接码确认")
    conn.commit()
    return {"ok": True, "route": route_id}


def scan_at_stop(conn, actor_id: str, stop_id: str, raw: str) -> dict:
    """送餐端核对：扫描住户码/餐盒码。

    - 必须先扫住户、再扫餐盒；
    - 重复扫描留痕并返回 duplicate（不改变状态）；
    - 错配/作废/隔离标签一律拒绝并记录事件；
    - 两次都通过后自动完成交付。
    """
    actor = require_staff(conn, actor_id)
    stop = conn.execute(
        """SELECT rs.*, r.alias, r.room, r.code AS resident_code, r.allergies, r.avoidances,
                  r.chewing, r.special_diet, mb.label_code, mb.status AS box_status,
                  mb.id AS box_pk
           FROM route_stops rs
           JOIN residents r ON r.id=rs.resident_id
           LEFT JOIN meal_boxes mb ON mb.id=rs.meal_box_id
           WHERE rs.id=?""", (stop_id,)).fetchone()
    if not stop:
        raise DomainError("站点不存在", code="not_found")
    rt = route(conn, stop["route_id"])
    if rt["status"] not in ("in_transit",):
        raise DomainError("路线不在配送中，请先出发或完成交接", code="state")

    try:
        kind, code = scanner.scan(raw)
    except ScanError as e:
        raise DomainError(str(e), code="bad_qr")

    if stop["status"] in ("delivered", "refused", "exception"):
        _log_scan(conn, stop_id, actor_id, kind, code, "duplicate",
                  f"站点已是 {stop['status']} 状态，扫描仅留痕")
        conn.commit()
        raise DomainError(f"该站点已{('交付' if stop['status']=='delivered' else '处理')}，"
                          f"重复扫描已记录", code="duplicate")

    if kind == "RES":
        return _scan_resident(conn, actor, stop, code)
    if kind == "MEAL":
        return _scan_box(conn, actor, stop, code)
    _log_scan(conn, stop_id, actor_id, kind, code, "unknown", "非住户/餐盒码")
    conn.commit()
    raise DomainError("请扫描住户腕带码或餐盒标签码", code="wrong_kind")


def _scan_resident(conn, actor, stop, code) -> dict:
    actor_id = actor["id"]
    if code != stop["resident_code"]:
        other = resident_by_code(conn, code)
        detail = (f"住户码不符：当前站点 {stop['room']} {stop['alias']}"
                  f"（{stop['resident_code']}），扫到 {code}"
                  + (f" {other['alias']}" if other else ""))
        _log_scan(conn, stop["id"], actor_id, "RES", code, "mismatch", detail)
        conn.execute("UPDATE route_stops SET status='exception' WHERE id=?", (stop["id"],))
        _record_event(conn, "wrong_meal", stop["resident_id"], stop["box_pk"],
                      stop["route_id"], detail + "；已暂停交付等待核对", actor_id)
        audit(conn, actor_id, "scan.mismatch_resident", "route_stop", stop["id"])
        conn.commit()
        raise DomainError(detail, code="mismatch")
    if stop["resident_scan_at"]:
        _log_scan(conn, stop["id"], actor_id, "RES", code, "duplicate", "住户码重复扫描")
        conn.commit()
        return {"ok": True, "duplicate": True, "stage": "resident",
                "message": "住户身份已确认（重复扫描已记录）"}
    conn.execute("UPDATE route_stops SET status='resident_scanned', resident_scan_at=? WHERE id=?",
                 (now(), stop["id"]))
    _log_scan(conn, stop["id"], actor_id, "RES", code, "ok", "住户身份确认")
    audit(conn, actor_id, "scan.resident", "route_stop", stop["id"])
    conn.commit()
    return {"ok": True, "stage": "resident", "message": "住户身份确认，请扫描餐盒标签"}


def _scan_box(conn, actor, stop, code) -> dict:
    actor_id = actor["id"]
    if not stop["resident_scan_at"]:
        _log_scan(conn, stop["id"], actor_id, "MEAL", code, "wrong_order",
                  "未先扫描住户码")
        conn.commit()
        raise DomainError("请先扫描住户腕带码，再扫描餐盒标签", code="wrong_order")

    scanned = box_by_label(conn, code)
    if not scanned:
        _log_scan(conn, stop["id"], actor_id, "MEAL", code, "unknown", "标签不存在")
        conn.commit()
        raise DomainError("系统中找不到该餐盒标签", code="unknown")
    if scanned["status"] == "void":
        _log_scan(conn, stop["id"], actor_id, "MEAL", code, "void",
                  f"旧标签已作废：{scanned['void_reason']}")
        conn.commit()
        raise DomainError(f"旧标签已作废，不能继续使用（{scanned['void_reason']}）",
                          code="void_label")
    if scanned["status"] == "quarantined":
        _log_scan(conn, stop["id"], actor_id, "MEAL", code, "void", "异常隔离餐盒")
        conn.commit()
        raise DomainError("该餐盒已隔离，禁止交付", code="quarantined")
    if scanned["id"] != stop["box_pk"]:
        detail = (f"餐盒错配：{stop['alias']} 的餐盒应为 {stop['label_code']}，"
                  f"扫到 {code}"
                  + (f"（登记住户 {resident(conn, scanned['resident_id'])['alias']}）"
                     if scanned["resident_id"] else "（备用餐）"))
        _log_scan(conn, stop["id"], actor_id, "MEAL", code, "mismatch", detail)
        conn.execute("UPDATE meal_boxes SET status='quarantined', updated_at=? WHERE id=?",
                     (now(), scanned["id"]))
        conn.execute("UPDATE route_stops SET status='exception' WHERE id=?", (stop["id"],))
        _record_event(conn, "wrong_meal", stop["resident_id"], scanned["id"],
                      stop["route_id"], detail + "；错餐盒已立即隔离", actor_id)
        audit(conn, actor_id, "scan.mismatch_box", "meal_box", scanned["id"])
        conn.commit()
        raise DomainError(detail + "；错餐盒已立即隔离", code="mismatch")

    if stop["box_scan_at"]:
        _log_scan(conn, stop["id"], actor_id, "MEAL", code, "duplicate", "餐盒码重复扫描")
        conn.commit()
        return {"ok": True, "duplicate": True, "stage": "box",
                "message": "餐盒已核对（重复扫描已记录）"}

    # 双码齐全 → 完成交付
    conn.execute(
        """UPDATE route_stops SET status='delivered', box_scan_at=?, delivered_at=?
           WHERE id=?""", (now(), now(), stop["id"]))
    conn.execute("UPDATE meal_boxes SET status='delivered', updated_at=? WHERE id=?",
                 (now(), scanned["id"]))
    did = f"DL-{uuid.uuid4().hex[:12]}"
    conn.execute(
        """INSERT INTO deliveries(id,stop_id,meal_box_id,resident_id,result,
               delivered_by,delivered_at) VALUES(?,?,?,?,?,?,?)""",
        (did, stop["id"], scanned["id"], stop["resident_id"], "delivered",
         actor_id, now()),
    )
    _log_scan(conn, stop["id"], actor_id, "MEAL", code, "ok", "双码核对完成，交付")
    audit(conn, actor_id, "delivery.confirm", "delivery", did,
          {"box": scanned["id"]})
    conn.commit()
    return {"ok": True, "stage": "done", "delivery_id": did,
            "message": "核对完成，已交付"}


def refuse_stop(conn, actor_id: str, stop_id: str, reason: str) -> str:
    actor = require_staff(conn, actor_id)
    require(actor["role"], "delivery.refuse")
    stop = conn.execute("SELECT * FROM route_stops WHERE id=?", (stop_id,)).fetchone()
    if not stop:
        raise DomainError("站点不存在", code="not_found")
    if stop["status"] == "delivered":
        raise DomainError("已交付站点不能登记拒餐", code="state")
    conn.execute(
        "UPDATE route_stops SET status='refused', delivered_at=? WHERE id=?",
        (now(), stop_id))
    if stop["meal_box_id"]:
        conn.execute("UPDATE meal_boxes SET status='refused', updated_at=? WHERE id=?",
                     (now(), stop["meal_box_id"]))
    did = f"DL-{uuid.uuid4().hex[:12]}"
    conn.execute(
        """INSERT INTO deliveries(id,stop_id,meal_box_id,resident_id,result,
               delivered_by,delivered_at) VALUES(?,?,?,?,?,?,?)""",
        (did, stop_id, stop["meal_box_id"], stop["resident_id"], "refused",
         actor_id, now()),
    )
    _record_event(conn, "refused", stop["resident_id"], stop["meal_box_id"],
                  stop["route_id"], f"住户拒餐：{reason}", actor_id)
    audit(conn, actor_id, "delivery.refuse", "route_stop", stop_id, {"reason": reason})
    conn.commit()
    return did


def quarantine_box(conn, actor_id: str, box_id: str, reason: str,
                   event_type: str = "quarantine") -> str:
    """异常餐盒立即隔离。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "box.quarantine")
    b = box(conn, box_id)
    if not b:
        raise DomainError("餐盒不存在", code="not_found")
    if b["status"] == "delivered":
        raise DomainError("已交付餐盒不能隔离", code="state")
    conn.execute("UPDATE meal_boxes SET status='quarantined', updated_at=? WHERE id=?",
                 (now(), box_id))
    ev = _record_event(conn, event_type, b["resident_id"], box_id, None,
                       f"餐盒隔离：{reason}", actor_id)
    audit(conn, actor_id, "box.quarantine", "meal_box", box_id,
          {"reason": reason, "event": ev})
    conn.commit()
    return ev


# ---------------------------------------------------------------------
# 已送达记录：只允许追加说明
# ---------------------------------------------------------------------

def append_delivery_note(conn, actor_id: str, delivery_id: str, note: str) -> str:
    actor = require_staff(conn, actor_id)
    require(actor["role"], "delivery.note")
    d = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
    if not d:
        raise DomainError("交付记录不存在", code="not_found")
    if not note.strip():
        raise DomainError("说明内容不能为空", code="validation")
    nid = f"DN-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO delivery_notes(id,delivery_id,note,created_by,created_at) VALUES(?,?,?,?,?)",
        (nid, delivery_id, note.strip(), actor_id, now()),
    )
    audit(conn, actor_id, "delivery.note_append", "delivery", delivery_id, {"note": note})
    conn.commit()
    return nid


def delivery_notes(conn, delivery_id: str):
    return conn.execute(
        """SELECT dn.*, s.name AS created_by_name FROM delivery_notes dn
           JOIN staff s ON s.id=dn.created_by
           WHERE delivery_id=? ORDER BY created_at""", (delivery_id,)).fetchall()


# ---------------------------------------------------------------------
# 家属临时调整
# ---------------------------------------------------------------------

def list_family_adjustments(conn, status: str | None = None):
    sql = """SELECT fa.*, r.alias AS resident_alias, r.room,
                    d.name AS suggested_dish_name
             FROM family_adjustments fa
             JOIN residents r ON r.id=fa.resident_id
             LEFT JOIN dishes d ON d.id=fa.suggested_dish_id"""
    args = []
    if status:
        sql += " WHERE fa.status=?"
        args.append(status)
    sql += " ORDER BY fa.created_at DESC"
    return conn.execute(sql, args).fetchall()


def family_adjustment(conn, fa_id: str):
    return conn.execute(
        """SELECT fa.*, r.alias AS resident_alias, r.room FROM family_adjustments fa
           JOIN residents r ON r.id=fa.resident_id WHERE fa.id=?""", (fa_id,)).fetchone()


def handle_family_adjustment(conn, actor_id: str, fa_id: str, approve: bool,
                             note: str = ""):
    """护理员与家属电话复核后确认/驳回（家属确认页面）。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "family.handle")
    fa = family_adjustment(conn, fa_id)
    if not fa:
        raise DomainError("家属调整不存在", code="not_found")
    if fa["status"] != "requested":
        raise DomainError("该调整已处理", code="state")
    new_status = "confirmed" if approve else "rejected"
    conn.execute(
        "UPDATE family_adjustments SET status=?, handled_by=?, handled_at=?, handle_note=? WHERE id=?",
        (new_status, actor_id, now(), note, fa_id),
    )
    audit(conn, actor_id, f"family.{new_status}", "family_adjustment", fa_id,
          {"note": note})
    conn.commit()
    return new_status


def fulfill_family_hold(conn, actor_id: str, fa_id: str, reason: str):
    """确认后的暂停供餐：对应餐盒作废，站点标记异常。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "family.handle")
    fa = family_adjustment(conn, fa_id)
    if not fa or fa["status"] != "confirmed":
        raise DomainError("家属调整未确认，不能执行", code="state")
    row = conn.execute(
        """SELECT mb.id FROM meal_boxes mb JOIN batches b ON b.id=mb.batch_id
           WHERE mb.resident_id=? AND b.serve_date=? AND b.meal_session=?
                 AND mb.status NOT IN ('void','delivered')
           ORDER BY mb.created_at DESC LIMIT 1""",
        (fa["resident_id"], fa["serve_date"], fa["meal_session"])).fetchone()
    if row:
        _void_box_core(conn, actor_id, row["id"], f"家属暂停供餐：{reason}")
    conn.execute("UPDATE family_adjustments SET status='fulfilled' WHERE id=?", (fa_id,))
    _record_event(conn, "family_adjust", fa["resident_id"],
                  row["id"] if row else None, None,
                  f"家属临时调整（暂停供餐）：{reason}", actor_id)
    audit(conn, actor_id, "family.fulfill_hold", "family_adjustment", fa_id)
    conn.commit()


def fulfill_family_swap(conn, actor_id: str, fa_id: str, new_dish_ids: list[str]):
    """确认后的换餐：走替换餐流程（已审核菜品 + 过敏拦截 + 旧标签作废）。"""
    actor = require_staff(conn, actor_id)
    require(actor["role"], "family.handle")
    fa = family_adjustment(conn, fa_id)
    if not fa or fa["status"] != "confirmed":
        raise DomainError("家属调整未确认，不能执行", code="state")
    row = conn.execute(
        """SELECT mb.id FROM meal_boxes mb JOIN batches b ON b.id=mb.batch_id
           WHERE mb.resident_id=? AND b.serve_date=? AND b.meal_session=?
                 AND mb.status NOT IN ('void','delivered')
           ORDER BY mb.created_at DESC LIMIT 1""",
        (fa["resident_id"], fa["serve_date"], fa["meal_session"])).fetchone()
    if not row:
        raise DomainError("没有可替换的在制餐盒", code="state")
    new_id = _substitute_core(conn, actor_id, row["id"], new_dish_ids,
                              f"家属临时换餐：{fa['detail']}")
    conn.execute("UPDATE family_adjustments SET status='fulfilled' WHERE id=?", (fa_id,))
    _record_event(conn, "family_adjust", fa["resident_id"], new_id, None,
                  f"家属临时调整（换餐）：{fa['detail']}", actor_id)
    audit(conn, actor_id, "family.fulfill_swap", "family_adjustment", fa_id,
                  {"new_box": new_id})
    conn.commit()
    return new_id


# ---------------------------------------------------------------------
# 异常事件
# ---------------------------------------------------------------------

EVENT_NAMES = {
    "wrong_meal": "错餐", "missed": "漏餐", "refused": "拒餐",
    "temp_fail": "温度不合格", "family_adjust": "家属临时调整",
    "quarantine": "异常隔离",
}


def list_events(conn, status: str | None = None):
    sql = """SELECT e.*, r.alias AS resident_alias, s.name AS recorded_by_name,
                    h.name AS handled_by_name, mb.label_code
             FROM meal_events e
             LEFT JOIN residents r ON r.id=e.resident_id
             LEFT JOIN staff s ON s.id=e.recorded_by
             LEFT JOIN staff h ON h.id=e.handled_by
             LEFT JOIN meal_boxes mb ON mb.id=e.meal_box_id"""
    args = []
    if status:
        sql += " WHERE e.status=?"
        args.append(status)
    sql += " ORDER BY e.created_at DESC"
    return conn.execute(sql, args).fetchall()


def handle_event(conn, actor_id: str, event_id: str, resolution: str):
    actor = require_staff(conn, actor_id)
    require(actor["role"], "event.handle")
    e = conn.execute("SELECT * FROM meal_events WHERE id=?", (event_id,)).fetchone()
    if not e:
        raise DomainError("事件不存在", code="not_found")
    if e["status"] != "open":
        raise DomainError("事件已处置", code="state")
    conn.execute(
        "UPDATE meal_events SET status='handled', handled_by=?, handled_at=?, resolution=? WHERE id=?",
        (actor_id, now(), resolution, event_id),
    )
    audit(conn, actor_id, "event.handle", "meal_event", event_id,
          {"resolution": resolution})
    conn.commit()
