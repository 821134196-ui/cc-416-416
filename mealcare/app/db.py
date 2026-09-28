"""SQLite 持久层:饮食档案、过敏项、菜单审核、批次、餐盒标签、路线与异常。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 员工(演示账号)。角色:
--   manager    护理主管  dietitian 营养师  kitchen 厨房  courier 送餐员
CREATE TABLE staff(
    id        TEXT PRIMARY KEY,
    code      TEXT NOT NULL UNIQUE,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL CHECK(role IN ('manager','dietitian','kitchen','courier'))
);

-- 住户:全部为匿名演示数据,只保留编号 / 床位 / 照护区与演示化名
CREATE TABLE residents(
    id         TEXT PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,          -- 二维码载荷 RES:<code>
    alias      TEXT NOT NULL,                 -- 演示化名,如 住户甲-12床
    bed_no     TEXT NOT NULL,
    care_zone  TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1
);

-- 饮食档案(当前生效版本)
CREATE TABLE diet_profiles(
    id             TEXT PRIMARY KEY,
    resident_id    TEXT NOT NULL UNIQUE REFERENCES residents(id),
    avoidances     TEXT NOT NULL DEFAULT '',  -- 忌口,分号分隔,如 香菜;内脏
    chewing_level  TEXT NOT NULL DEFAULT 'normal'
                   CHECK(chewing_level IN ('normal','soft','minced','pureed')),
    effective_date TEXT NOT NULL,             -- 生效日期 YYYY-MM-DD
    version        INTEGER NOT NULL DEFAULT 1,
    updated_by     TEXT REFERENCES staff(id),
    updated_at     TEXT NOT NULL
);

-- 过敏项:等级 none/mild/moderate/severe/anaphylaxis
CREATE TABLE allergens(
    id          TEXT PRIMARY KEY,
    resident_id TEXT NOT NULL REFERENCES residents(id),
    name        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK(severity IN ('mild','moderate','severe','anaphylaxis')),
    UNIQUE(resident_id, name)
);

-- 档案变更请求(双人确认:申请人与确认人必须为两名不同员工)
CREATE TABLE profile_changes(
    id            TEXT PRIMARY KEY,
    resident_id   TEXT NOT NULL REFERENCES residents(id),
    payload_json  TEXT NOT NULL,   -- 目标档案与过敏项快照
    requested_by  TEXT NOT NULL REFERENCES staff(id),
    requested_at  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','confirmed','rejected')),
    confirmed_by  TEXT REFERENCES staff(id),
    confirmed_at  TEXT,
    review_note   TEXT
);

-- 菜品。营养审核通过后方可入菜单或作为替换餐
CREATE TABLE dishes(
    id               TEXT PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT '主菜',
    contains_allergens TEXT NOT NULL DEFAULT '', -- 分号分隔,如 花生;虾
    texture          TEXT NOT NULL DEFAULT 'normal', -- normal/soft/minced/pureed
    kcal             INTEGER NOT NULL DEFAULT 0,
    nutrition_note   TEXT NOT NULL DEFAULT '',
    review_status    TEXT NOT NULL DEFAULT 'draft'
                     CHECK(review_status IN ('draft','approved','rejected')),
    reviewed_by      TEXT REFERENCES staff(id),
    reviewed_at      TEXT,
    review_note      TEXT DEFAULT '',
    active           INTEGER NOT NULL DEFAULT 1
);

-- 菜单:草稿 -> 待审 -> 已审(可发布) / 驳回
CREATE TABLE menus(
    id           TEXT PRIMARY KEY,
    meal_date    TEXT NOT NULL,
    meal_type    TEXT NOT NULL CHECK(meal_type IN ('lunch','dinner')),
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK(status IN ('draft','pending_review','approved','rejected','published')),
    created_by   TEXT REFERENCES staff(id),
    submitted_at TEXT,
    reviewed_by  TEXT REFERENCES staff(id),
    reviewed_at  TEXT,
    review_note  TEXT DEFAULT '',
    UNIQUE(meal_date, meal_type)
);

CREATE TABLE menu_items(
    id       TEXT PRIMARY KEY,
    menu_id  TEXT NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    dish_id  TEXT NOT NULL REFERENCES dishes(id),
    UNIQUE(menu_id, dish_id)
);

-- 厨房批次:按已发布菜单生成餐盒
CREATE TABLE batches(
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    menu_id     TEXT NOT NULL REFERENCES menus(id),
    station     TEXT NOT NULL DEFAULT '中央厨房',
    produced_by TEXT REFERENCES staff(id),
    produced_at TEXT,
    status      TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','produced','closed'))
);

-- 餐盒。状态:
--   packed 已贴签备餐 | in_transit 交接在途 | delivered 已送达
--   refused 拒餐 | quarantined 异常隔离 | substituted 已替换 | returned 回收
CREATE TABLE boxes(
    id               TEXT PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,     -- 二维码载荷 BOX:<code>
    batch_id         TEXT NOT NULL REFERENCES batches(id),
    resident_id      TEXT NOT NULL REFERENCES residents(id),
    dish_id          TEXT NOT NULL REFERENCES dishes(id),
    texture          TEXT NOT NULL,
    allergen_snapshot TEXT NOT NULL DEFAULT '',-- 装餐时住户过敏项快照,用于标签
    special_note     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'packed',
    created_at       TEXT NOT NULL,
    delivered_at     TEXT,
    UNIQUE(batch_id, resident_id)
);

-- 餐盒标签:每次出餐 / 替换生成新标签,旧标签立即作废,标签号全局唯一
CREATE TABLE box_labels(
    id         TEXT PRIMARY KEY,
    token      TEXT NOT NULL UNIQUE,
    box_id     TEXT NOT NULL REFERENCES boxes(id),
    version    INTEGER NOT NULL DEFAULT 1,
    status     TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
    reason     TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    voided_at  TEXT
);

-- 替换记录:新菜品必须已通过营养审核且不过敏
CREATE TABLE substitutions(
    id           TEXT PRIMARY KEY,
    box_id       TEXT NOT NULL REFERENCES boxes(id),
    old_dish_id  TEXT NOT NULL REFERENCES dishes(id),
    new_dish_id  TEXT NOT NULL REFERENCES dishes(id),
    reason       TEXT NOT NULL,
    staff_id     TEXT NOT NULL REFERENCES staff(id),
    created_at   TEXT NOT NULL
);

-- 送餐路线
CREATE TABLE routes(
    id             TEXT PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    meal_date      TEXT NOT NULL,
    meal_type      TEXT NOT NULL CHECK(meal_type IN ('lunch','dinner')),
    courier_id     TEXT REFERENCES staff(id),
    status         TEXT NOT NULL DEFAULT 'planned'
                   CHECK(status IN ('planned','handed_over','completed')),
    handed_by      TEXT REFERENCES staff(id),
    handed_at      TEXT,
    completed_at   TEXT
);

CREATE TABLE route_stops(
    id          TEXT PRIMARY KEY,
    route_id    TEXT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    resident_id TEXT NOT NULL REFERENCES residents(id),
    box_id      TEXT REFERENCES boxes(id),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','ready','delivered','refused','skipped')),
    resident_scan_at TEXT,
    box_scan_at      TEXT,
    UNIQUE(route_id, seq)
);

-- 扫描事件(重复扫描据此判定)
CREATE TABLE scan_events(
    id          TEXT PRIMARY KEY,
    stop_id     TEXT REFERENCES route_stops(id),
    box_id      TEXT REFERENCES boxes(id),
    kind        TEXT NOT NULL CHECK(kind IN ('resident','box')),
    payload     TEXT NOT NULL,
    accepted    INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    staff_id    TEXT REFERENCES staff(id),
    at          TEXT NOT NULL
);

-- 送达记录:完成后只允许追加说明(delivery_notes),不允许改主记录
CREATE TABLE deliveries(
    id               TEXT PRIMARY KEY,
    stop_id          TEXT NOT NULL UNIQUE REFERENCES route_stops(id),
    box_id           TEXT NOT NULL REFERENCES boxes(id),
    resident_id      TEXT NOT NULL REFERENCES residents(id),
    courier_id       TEXT NOT NULL REFERENCES staff(id),
    resident_scan_at TEXT NOT NULL,
    box_scan_at      TEXT NOT NULL,
    delivered_at     TEXT NOT NULL,
    result           TEXT NOT NULL DEFAULT 'delivered'
                     CHECK(result IN ('delivered','refused'))
);

CREATE TABLE delivery_notes(
    id          TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES deliveries(id),
    note        TEXT NOT NULL,
    staff_id    TEXT NOT NULL REFERENCES staff(id),
    at          TEXT NOT NULL
);

-- 异常事件:错餐 / 漏餐 / 拒餐 / 温度不合格 / 家属临时调整
CREATE TABLE incidents(
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL
                CHECK(kind IN ('wrong_meal','missed','refused','temperature','family_adjust')),
    box_id      TEXT REFERENCES boxes(id),
    resident_id TEXT REFERENCES residents(id),
    detail      TEXT NOT NULL DEFAULT '',
    temp_c      REAL,
    severity    TEXT NOT NULL DEFAULT 'major'
                CHECK(severity IN ('minor','major','critical')),
    recorded_by TEXT REFERENCES staff(id),
    at          TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open','quarantined','resolved')),
    resolution  TEXT DEFAULT '',
    resolved_by TEXT REFERENCES staff(id),
    resolved_at TEXT
);

-- 家属临时调整(匿名家属联系人),需员工在"家属确认"页确认后执行
CREATE TABLE family_adjustments(
    id           TEXT PRIMARY KEY,
    resident_id  TEXT NOT NULL REFERENCES residents(id),
    meal_date    TEXT NOT NULL,
    meal_type    TEXT NOT NULL CHECK(meal_type IN ('lunch','dinner')),
    action       TEXT NOT NULL CHECK(action IN ('cancel','replace_soft','extra_portion','late_serve')),
    contact      TEXT NOT NULL DEFAULT '家属(匿名)',
    detail       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','confirmed','rejected','done')),
    confirmed_by TEXT REFERENCES staff(id),
    confirmed_at TEXT,
    created_at   TEXT NOT NULL,
    incident_id  TEXT REFERENCES incidents(id)
);

-- 进程内送餐提醒(工作线程轮询触发)
CREATE TABLE reminders(
    id         TEXT PRIMARY KEY,
    route_id   TEXT REFERENCES routes(id),
    title      TEXT NOT NULL,
    fire_at    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','fired')),
    created_at TEXT NOT NULL
);

-- 全局审计日志
CREATE TABLE audit_log(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    staff_id  TEXT,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT,
    detail    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_boxes_status ON boxes(status);
CREATE INDEX idx_stops_route  ON route_stops(route_id);
CREATE INDEX idx_inc_status   ON incidents(status);
CREATE INDEX idx_reminders    ON reminders(status, fire_at);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
