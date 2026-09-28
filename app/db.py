"""SQLite 持久化：连接、建表与基础工具。

所有时间统一存本地时间文本 (YYYY-MM-DD HH:MM:SS)，日期存 YYYY-MM-DD。
列表/字典字段存 JSON 文本。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.environ.get(
    "MEAL_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mealcare.db")
)

_lock = threading.RLock()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    target = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
-- 员工 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff(
    id        TEXT PRIMARY KEY,        -- S01 ...
    name      TEXT NOT NULL,           -- 匿名称呼：营养师·甘、送餐员·甲
    role      TEXT NOT NULL,           -- manager/nutritionist/nurse/kitchen/courier
    passcode  TEXT NOT NULL DEFAULT '',-- 演示用简单口令
    active    INTEGER NOT NULL DEFAULT 1
);

-- 住户饮食档案（当前已确认版本）-------------------------------------
CREATE TABLE IF NOT EXISTS residents(
    id             TEXT PRIMARY KEY,   -- R001
    code           TEXT NOT NULL UNIQUE, -- 腕带码 A001（二维码载荷 RES:A001）
    room           TEXT NOT NULL,
    floor          TEXT NOT NULL,
    alias          TEXT NOT NULL,      -- 匿名称呼
    allergies      TEXT NOT NULL DEFAULT '[]',  -- [{allergen,level,note}] level: avoid/warning/severe
    avoidances     TEXT NOT NULL DEFAULT '[]',  -- 忌口 ["羊肉"]
    chewing        TEXT NOT NULL DEFAULT 'normal', -- normal/soft/minced/pureed
    special_diet   TEXT NOT NULL DEFAULT '',    -- 低盐/糖尿病餐等
    effective_date TEXT NOT NULL,
    card_status    TEXT NOT NULL DEFAULT 'active', -- active/needs_confirm
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 档案修改：必须由第二名员工确认 ------------------------------------
CREATE TABLE IF NOT EXISTS profile_changes(
    id           TEXT PRIMARY KEY,
    resident_id  TEXT NOT NULL REFERENCES residents(id),
    patch        TEXT NOT NULL,        -- JSON: {field: newvalue}
    reason       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending', -- pending/confirmed/rejected
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    review_note  TEXT NOT NULL DEFAULT ''
);

-- 菜品库 -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS dishes(
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    category  TEXT NOT NULL,           -- staple/meat/veg/egg/soy/soup/fruit
    allergens TEXT NOT NULL DEFAULT '[]',  -- ["花生","虾"]
    texture   TEXT NOT NULL DEFAULT 'normal', -- normal/soft/minced/pureed
    diet_tags TEXT NOT NULL DEFAULT '[]',    -- ["低盐","糖尿病适餐"]
    energy_kcal INTEGER NOT NULL DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
    reviewed_by TEXT, reviewed_at TEXT, review_note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- 菜单 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS menus(
    id           TEXT PRIMARY KEY,
    serve_date   TEXT NOT NULL,
    meal_session TEXT NOT NULL,        -- lunch/dinner
    status       TEXT NOT NULL DEFAULT 'draft', -- draft/pending/approved/published/rejected
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    reviewed_by  TEXT, reviewed_at TEXT, review_note TEXT DEFAULT '',
    UNIQUE(serve_date, meal_session)
);
CREATE TABLE IF NOT EXISTS menu_items(
    menu_id TEXT NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    dish_id TEXT NOT NULL REFERENCES dishes(id),
    seq     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(menu_id, dish_id)
);

-- 厨房批次 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS batches(
    id           TEXT PRIMARY KEY,     -- B20260928-L
    serve_date   TEXT NOT NULL,
    meal_session TEXT NOT NULL,
    menu_id      TEXT REFERENCES menus(id),
    status       TEXT NOT NULL DEFAULT 'planned', -- planned/prepared/closed
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    prepared_at  TEXT,
    note         TEXT NOT NULL DEFAULT ''
);

-- 餐盒 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meal_boxes(
    id            TEXT PRIMARY KEY,
    label_code    TEXT NOT NULL UNIQUE,  -- 20260928-L-0007（标签唯一）
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    resident_id   TEXT REFERENCES residents(id), -- 备用餐可为 NULL
    dishes        TEXT NOT NULL DEFAULT '[]',    -- 盒内菜品 id
    is_substitution INTEGER NOT NULL DEFAULT 0,
    sub_reason    TEXT NOT NULL DEFAULT '',
    replaces_box_id TEXT REFERENCES meal_boxes(id),
    status        TEXT NOT NULL DEFAULT 'labeled',
                  -- labeled/delivering/delivered/refused/quarantined/void
    void_reason   TEXT NOT NULL DEFAULT '',
    temp_c        REAL,
    temp_at       TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_box_batch ON meal_boxes(batch_id);
CREATE INDEX IF NOT EXISTS idx_box_resident ON meal_boxes(resident_id);

-- 送餐路线 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS routes(
    id           TEXT PRIMARY KEY,     -- RT20260928-L1
    serve_date   TEXT NOT NULL,
    meal_session TEXT NOT NULL,
    name         TEXT NOT NULL,
    courier_id   TEXT NOT NULL REFERENCES staff(id),
    status       TEXT NOT NULL DEFAULT 'planned', -- planned/in_transit/handed_over/completed
    handover_from TEXT, handover_to TEXT, handover_at TEXT,
    created_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS route_stops(
    id           TEXT PRIMARY KEY,
    route_id     TEXT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    resident_id  TEXT NOT NULL REFERENCES residents(id),
    meal_box_id  TEXT REFERENCES meal_boxes(id),
    status       TEXT NOT NULL DEFAULT 'pending',
                 -- pending/resident_scanned/box_scanned/delivered/refused/exception
    resident_scan_at TEXT,
    box_scan_at      TEXT,
    delivered_at     TEXT,
    UNIQUE(route_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_stops_route ON route_stops(route_id);

-- 扫描流水（重复扫描留痕）-------------------------------------------
CREATE TABLE IF NOT EXISTS scans(
    id          TEXT PRIMARY KEY,
    stop_id     TEXT REFERENCES route_stops(id),
    courier_id  TEXT,
    kind        TEXT NOT NULL,         -- RES / MEAL / ROUTE
    code        TEXT NOT NULL,
    result      TEXT NOT NULL,         -- ok/duplicate/mismatch/wrong_stop/void/unknown
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 交付记录（只允许追加说明）-----------------------------------------
CREATE TABLE IF NOT EXISTS deliveries(
    id           TEXT PRIMARY KEY,
    stop_id      TEXT NOT NULL REFERENCES route_stops(id),
    meal_box_id  TEXT NOT NULL REFERENCES meal_boxes(id),
    resident_id  TEXT NOT NULL REFERENCES residents(id),
    result       TEXT NOT NULL,        -- delivered/refused
    delivered_by TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_notes(
    id          TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES deliveries(id),
    note        TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 异常事件：错餐/漏餐/拒餐/温度不合格/家属调整/隔离 ----------------
CREATE TABLE IF NOT EXISTS meal_events(
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
                 -- wrong_meal/missed/refused/temp_fail/family_adjust/quarantine
    serve_date   TEXT NOT NULL,
    meal_session TEXT NOT NULL,
    resident_id  TEXT REFERENCES residents(id),
    meal_box_id  TEXT REFERENCES meal_boxes(id),
    route_id     TEXT REFERENCES routes(id),
    detail       TEXT NOT NULL DEFAULT '',
    temp_c       REAL,
    status       TEXT NOT NULL DEFAULT 'open', -- open/handled
    recorded_by  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    handled_by   TEXT, handled_at TEXT, resolution TEXT NOT NULL DEFAULT ''
);

-- 家属临时调整 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS family_adjustments(
    id           TEXT PRIMARY KEY,
    resident_id  TEXT NOT NULL REFERENCES residents(id),
    family_name  TEXT NOT NULL,        -- 匿名：家属·A001
    contact_code TEXT NOT NULL DEFAULT '', -- 脱敏联系凭证
    serve_date   TEXT NOT NULL,
    meal_session TEXT NOT NULL,
    change_type  TEXT NOT NULL,        -- hold/swap/extra
    detail       TEXT NOT NULL,
    suggested_dish_id TEXT REFERENCES dishes(id),
    status       TEXT NOT NULL DEFAULT 'requested',
                 -- requested/confirmed/rejected/fulfilled
    created_at   TEXT NOT NULL,
    handled_by   TEXT, handled_at TEXT, handle_note TEXT NOT NULL DEFAULT ''
);

-- 送餐提醒（进程内任务消费）-----------------------------------------
CREATE TABLE IF NOT EXISTS reminders(
    id          TEXT PRIMARY KEY,
    serve_date  TEXT NOT NULL,
    meal_session TEXT NOT NULL,
    route_id    TEXT REFERENCES routes(id),
    fire_at     TEXT NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending/fired/acked
    created_at  TEXT NOT NULL,
    fired_at    TEXT
);

-- 通用审计留痕（追加）-----------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL DEFAULT '',
    entity_id  TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def init_db(path: str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------

def jloads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return default


def jdumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def audit(conn, actor: str, action: str, entity: str = "", entity_id: str = "", payload=None):
    with _lock:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity,entity_id,payload,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity, entity_id, jdumps(payload or {}), now()),
        )
        conn.commit()
