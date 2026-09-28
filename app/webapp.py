"""养老机构膳食过敏与送餐核对平台 —— 本地 Web 应用（纯标准库）。

页面：
  /residents   住户饮食卡（限制置顶 + 固定高对比过敏提示）
  /menus       菜单与营养审核
  /kitchen     厨房批次与餐盒标签（大字 + 图标）
  /routes      送餐路线与一屏扫码核对
  /exceptions  异常餐盒与事件处置
  /family      家属临时调整确认

配套：
  /qr?payload=   本地二维码适配器（SVG）
  /api/scan      扫码核对（真实扫码枪/摄像头解码文本同样适用）
  /api/reminders 进程内送餐提醒
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import audit, connect, init_db, jloads, now  # noqa: E402
import services as svc  # noqa: E402
import qradapter  # noqa: E402
from seed import TODAY, seed  # noqa: E402

DB_FILE = os.environ.get(
    "MEAL_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mealcare.db"),
)
PORT = int(os.environ.get("MEAL_PORT", "8026"))

# ---------------------------------------------------------------------
# 会话（进程内；演示环境足够）
# ---------------------------------------------------------------------

SESSIONS: dict[str, dict] = {}
_sess_lock = threading.Lock()
REMINDER_FEED: list[dict] = []  # 进程内提醒任务的输出


def _esc(v) -> str:
    import html
    return html.escape("" if v is None else str(v), quote=True)


def _role_badge(role: str) -> str:
    return svc.ROLE_NAMES.get(role, role)


# ---------------------------------------------------------------------
# 进程内送餐提醒任务
# ---------------------------------------------------------------------

class ReminderWorker(threading.Thread):
    daemon = True

    def __init__(self, conn_factory, interval: float = 5.0):
        super().__init__(name="reminder-worker")
        self.conn_factory = conn_factory
        self.interval = interval
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                conn = self.conn_factory()
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE status='pending' AND fire_at<=?",
                    (now(),)).fetchall()
                for r in rows:
                    conn.execute(
                        "UPDATE reminders SET status='fired', fired_at=? WHERE id=?",
                        (now(), r["id"]))
                    REMINDER_FEED.append({"id": r["id"], "message": r["message"],
                                          "route_id": r["route_id"], "at": now()})
                    audit(conn, "system", "reminder.fire", "route",
                          r["route_id"] or "", {"message": r["message"]})
                conn.commit()
                conn.close()
            except Exception as exc:  # 提醒线程不能挂掉
                print(f"[reminder] {exc}", file=sys.stderr)


# ---------------------------------------------------------------------
# 页面骨架与样式
# ---------------------------------------------------------------------

CSS = """
:root{
  --warm:#FDF8F1; --warm2:#F7EFE3; --card:#FFFDF9;
  --orange:#D94A38; --orange-d:#B53728; --orange-l:#FBE3DE;
  --teal:#1F8F83; --teal-d:#176E65; --teal-l:#DDF0ED;
  --ink:#3A322D; --muted:#7C7068; --line:#E7DCCF;
  --warn-bg:#2B1410; --warn-fg:#FFFFFF; --warn-bar:#FFD23F;
}
*{box-sizing:border-box}
body{margin:0;font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  background:var(--warm);color:var(--ink);font-size:15px}
a{color:var(--teal-d);text-decoration:none}
a:hover{text-decoration:underline}
header.top{background:var(--orange);color:#fff;padding:10px 18px;display:flex;
  align-items:center;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:30}
header.top .brand{font-size:18px;font-weight:700}
header.top nav{display:flex;gap:4px;flex-wrap:wrap}
header.top nav a{color:#fff;padding:6px 12px;border-radius:18px;font-size:14px}
header.top nav a.active{background:rgba(255,255,255,.22);font-weight:700}
header.top nav a:hover{background:rgba(255,255,255,.15);text-decoration:none}
header.top .who{margin-left:auto;font-size:13px;display:flex;align-items:center;gap:10px}
.wrap{max-width:1180px;margin:18px auto;padding:0 16px 60px}
.grid{display:grid;gap:14px}
.cards{grid-template-columns:repeat(auto-fill,minmax(310px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:14px 16px;box-shadow:0 1px 3px rgba(120,80,40,.06)}
.card h3{margin:0 0 6px;font-size:17px}
.muted{color:var(--muted)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.tag{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12.5px;
  margin:2px 4px 2px 0;border:1px solid transparent;white-space:nowrap}
.tag.severe{background:var(--orange);color:#fff;border-color:var(--orange-d)}
.tag.warning{background:var(--orange-l);color:var(--orange-d);border-color:var(--orange)}
.tag.teal{background:var(--teal-l);color:var(--teal-d);border-color:var(--teal)}
.tag.gray{background:#EFEAE3;color:#6B5F55;border-color:var(--line)}
.tag.diet{background:#FFF3D6;color:#8A5A00;border-color:#E6C269}
/* 固定位置、高对比过敏提示 */
.allergy-bar{position:sticky;top:56px;z-index:20;background:var(--warn-bg);
  color:var(--warn-fg);border-bottom:4px solid var(--warn-bar);
  padding:9px 16px;margin:-14px -16px 12px;border-radius:14px 14px 0 0;
  display:flex;gap:10px;align-items:center;font-weight:700}
.allergy-bar .ico{flex:0 0 auto}
.allergy-bar .items span{display:inline-block;background:var(--warn-bar);color:#2B1410;
  border-radius:6px;padding:1px 8px;margin:2px 6px 2px 0;font-weight:800}
.btn{display:inline-block;border:0;border-radius:9px;padding:8px 14px;font-size:14px;
  cursor:pointer;background:var(--teal);color:#fff;font-weight:600}
.btn:hover{background:var(--teal-d)}
.btn.orange{background:var(--orange)} .btn.orange:hover{background:var(--orange-d)}
.btn.ghost{background:#fff;color:var(--teal-d);border:1px solid var(--teal)}
.btn.gray{background:#EFEAE3;color:#5C5148}
.btn.danger{background:#fff;color:var(--orange-d);border:1px solid var(--orange)}
.btn.sm{padding:5px 10px;font-size:13px}
.btn:disabled{opacity:.5;cursor:not-allowed}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;
  overflow:hidden}
th,td{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:var(--warm2);font-size:13px}
tr:last-child td{border-bottom:0}
.pill{font-size:12px;padding:2px 9px;border-radius:999px;font-weight:700}
.pill.draft{background:#EFEAE3;color:#6B5F55}
.pill.pending{background:#FFF3D6;color:#8A5A00}
.pill.published,.pill.approved,.pill.delivered,.pill.prepared,.pill.confirmed,
.pill.fulfilled,.pill.handled,.pill.in_transit{background:var(--teal-l);color:var(--teal-d)}
.pill.rejected,.pill.void,.pill.refused,.pill.exception,.pill.quarantined{background:var(--orange-l);color:var(--orange-d)}
.pill.labeled,.pill.planned,.pill.resident_scanned{background:#E7F0FA;color:#2A5C86}
.banner{padding:10px 14px;border-radius:10px;margin:10px 0;font-weight:600}
.banner.err{background:var(--orange-l);border:1px solid var(--orange);color:var(--orange-d)}
.banner.ok{background:var(--teal-l);border:1px solid var(--teal);color:var(--teal-d)}
.banner.info{background:#EDF2FA;border:1px solid #B9CDE4;color:#2A5C86}
input,select,textarea{font:inherit;padding:7px 10px;border:1px solid var(--line);
  border-radius:8px;background:#fff;color:var(--ink)}
label.fld{display:block;margin:8px 0;font-size:13.5px}
label.fld span{display:block;margin-bottom:3px;color:var(--muted);font-weight:600}
/* 餐盒标签：大字 + 图标 */
.label{width:340px;border:3px solid var(--ink);border-radius:12px;padding:12px 14px;
  background:#fff;page-break-after:always}
.label .l-top{display:flex;justify-content:space-between;align-items:flex-start}
.label .l-code{font-size:27px;font-weight:800;letter-spacing:.5px}
.label .l-res{font-size:21px;font-weight:800;margin:6px 0 2px}
.label .l-room{font-size:16px;font-weight:700;color:var(--teal-d)}
.label .l-icons{font-size:30px;margin:4px 0}
.label .l-warn{background:var(--warn-bg);color:var(--warn-fg);border:2px solid var(--warn-bar);
  border-radius:8px;padding:6px 9px;font-weight:800;margin-top:6px}
.label.voided{filter:grayscale(1);opacity:.7;position:relative}
.label.voided::after{content:"作废 DO NOT USE";position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;font-size:34px;font-weight:900;color:var(--orange-d);
  transform:rotate(-16deg);border:6px solid var(--orange-d);border-radius:12px}
.label .sub{background:var(--orange-l);color:var(--orange-d);font-weight:800;border-radius:6px;
  padding:1px 8px;font-size:13px}
/* 送餐端：一屏核对 */
.scanboard{display:grid;grid-template-columns:1.15fr .85fr;gap:14px}
.scan-main{background:var(--card);border:2px solid var(--teal);border-radius:16px;padding:16px}
.scan-step{font-size:22px;font-weight:800;margin:8px 0}
.bigbtn{display:block;width:100%;text-align:center;font-size:18px;font-weight:800;
  padding:15px;border-radius:12px;margin:8px 0;border:2px dashed transparent}
.bigbtn.res{background:#2A5C86;color:#fff}
.bigbtn.box{background:var(--teal);color:#fff}
.bigbtn:disabled{background:#CFCAC2}
.flow{display:flex;gap:6px;margin:6px 0 12px}
.flow .f{flex:1;text-align:center;padding:7px;border-radius:8px;background:#EFEAE3;
  font-weight:700;font-size:13px}
.flow .f.on{background:var(--teal);color:#fff}
.flow .f.done{background:var(--teal-l);color:var(--teal-d)}
.toast{position:fixed;right:18px;bottom:18px;max-width:360px;z-index:60}
.toast .t{background:var(--warn-bg);color:#fff;border-left:6px solid var(--warn-bar);
  border-radius:10px;padding:11px 14px;margin-top:8px;box-shadow:0 6px 18px rgba(0,0,0,.25);
  font-weight:700}
@media(max-width:860px){.scanboard{grid-template-columns:1fr}.allergy-bar{top:96px}}
"""

NAV = [
    ("/residents", "住户饮食卡"), ("/menus", "菜单审核"),
    ("/kitchen", "厨房批次"), ("/routes", "送餐路线"),
    ("/exceptions", "异常餐盒"), ("/family", "家属确认"),
]

SVG = {
    "warn": '<svg width="20" height="20" viewBox="0 0 24 24" fill="#FFD23F"><path d="M12 2L22 21H2z"/><path d="M11 9h2v7h-2z" fill="#2B1410"/><circle cx="12" cy="18.2" r="1.3" fill="#2B1410"/></svg>',
    "temp": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#176E65" stroke-width="2"><path d="M14 4a2 2 0 1 0-4 0v10.5a4 4 0 1 0 4 0z"/><path d="M12 9v6"/></svg>',
    "ban": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#D94A38" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>',
    "truck": '<svg width="22" height="22" viewBox="0 0 24 24" fill="#176E65"><path d="M1 4h14v12H1z"/><path d="M15 8h4l3 4v4h-7z" fill="#176E65"/><circle cx="6" cy="18" r="2.4" fill="#FDF8F1"/><circle cx="18" cy="18" r="2.4" fill="#FDF8F1"/></svg>',
    "bowl": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#176E65" stroke-width="2"><path d="M3 11h18a9 9 0 0 1-18 0z"/><path d="M8 8c0-1 1-1 1-2M12 8c0-1 1-1 1-2"/></svg>',
    "qr": '<svg width="20" height="20" viewBox="0 0 24 24" fill="#263238"><path d="M3 3h8v8H3z M13 3h8v8h-8z M3 13h8v8H3z" fill="#263238"/><path d="M13 13h3v3h-3z M19 13h2v2h-2z M13 19h3v2h-3z M18 18h3v3h-3z" fill="#263238"/></svg>',
}


def shell(title: str, body: str, staff_row, active: str = "", extra_head: str = "") -> bytes:
    nav = "".join(
        f'<a href="{u}" class="{"active" if u == active else ""}">{n}</a>'
        for u, n in NAV)
    who = ""
    if staff_row:
        who = (f'<div class="who"><span>{_esc(staff_row["name"])}·'
               f'{_role_badge(staff_row["role"])}</span>'
               f'<a class="btn sm ghost" href="/logout">退出</a></div>')
    html = f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · 膳食核对平台</title><style>{CSS}</style>{extra_head}</head>
<body>
<header class="top">
  <div class="brand">🍲 膳食过敏与送餐核对平台</div>
  <nav>{nav}</nav>{who}
</header>
<div class="wrap">{body}</div>
<div class="toast" id="toast"></div>
<script>
async function pollReminders(){{
  try{{
    const r = await fetch('/api/reminders');
    const data = await r.json();
    const box = document.getElementById('toast');
    data.items.forEach(it=>{{
      if(sessionStorage.getItem('rm_'+it.id)) return;
      sessionStorage.setItem('rm_'+it.id,'1');
      const d=document.createElement('div'); d.className='t';
      d.textContent='🔔 送餐提醒：'+it.message; box.appendChild(d);
      setTimeout(()=>d.remove(),12000);
    }});
  }}catch(e){{}}
}}
setInterval(pollReminders, 5000); pollReminders();
</script>
</body></html>"""
    return html.encode("utf-8")


def flash_banner(flash) -> str:
    if not flash:
        return ""
    kind, msg = flash
    return f'<div class="banner {kind}">{_esc(msg)}</div>'


def status_pill(s: str) -> str:
    names = {
        "draft": "草稿", "pending": "待审核", "approved": "已审核",
        "published": "已发布", "rejected": "已驳回",
        "planned": "待出发", "prepared": "已出餐", "closed": "已关闭",
        "in_transit": "配送中", "handed_over": "已交接", "completed": "已完成",
        "labeled": "已贴标", "delivering": "配送中", "delivered": "已送达",
        "refused": "拒餐", "quarantined": "已隔离", "void": "已作废",
        "resident_scanned": "已确认住户", "box_scanned": "已核对餐盒",
        "exception": "异常", "requested": "待确认", "confirmed": "已确认",
        "fulfilled": "已执行", "open": "待处置", "handled": "已处置",
        "needs_confirm": "待第二人确认", "active": "生效中",
    }
    return f'<span class="pill {s}">{names.get(s, s)}</span>'


def dish_tags(dish_row) -> str:
    out = []
    for a in jloads(dish_row["allergens"], []):
        out.append(f'<span class="tag severe">⚠ {_esc(a)}</span>')
    for t in jloads(dish_row["diet_tags"], []):
        out.append(f'<span class="tag diet">{_esc(t)}</span>')
    out.append(f'<span class="tag gray">{svc.TEXTURE_NAMES.get(dish_row["texture"], dish_row["texture"])}</span>')
    return "".join(out)


def allergy_bar(rv: dict) -> str:
    severe = [a for a in rv["allergies"] if a["level"] == "severe"]
    warn = [a for a in rv["allergies"] if a["level"] != "severe"]
    if not severe:
        inner = '<span class="muted" style="color:#CFE3DF;font-weight:600">无严重过敏原</span>'
    else:
        inner = '<span class="items">' + "".join(
            f'<span>⛔ {_esc(a["allergen"])}</span>' for a in severe) + '</span>'
    if warn:
        inner += '<span style="font-size:12.5px;font-weight:600">慎食：' + \
                 "、".join(_esc(a["allergen"]) for a in warn) + "</span>"
    return (f'<div class="allergy-bar"><span class="ico">{SVG["warn"]}</span>'
            f'<span>过敏拦截</span>{inner}</div>')


def qr_img(payload: str, size: int = 11) -> str:
    q = urllib.parse.quote(payload, safe="")
    return f'<img src="/qr?payload={q}&px={size}" width="{int(size)*30}" height="{int(size)*30}" alt="二维码" style="image-rendering:pixelated">'


# ---------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "MealCare/1.0"

    def log_message(self, fmt, *args):  # 安静些
        if os.environ.get("MEAL_HTTP_LOG"):
            super().log_message(fmt, *args)

    # --- 基础 ------------------------------------------------------------
    def _db(self):
        return connect(DB_FILE)

    def _send(self, content: bytes, status=200, ctype="text/html; charset=utf-8",
              extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, path: str):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self) -> str:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["sid"].value if "sid" in c else ""

    def _session(self) -> dict | None:
        with _sess_lock:
            return SESSIONS.get(self._cookie())

    def _staff(self, conn):
        sess = self._session()
        if not sess:
            return None
        return conn.execute("SELECT * FROM staff WHERE id=? AND active=1",
                            (sess["staff_id"],)).fetchone()

    def _require_login(self):
        conn = self._db()
        st = self._staff(conn)
        if not st:
            self._redirect("/login")
            return None, None
        return conn, st

    def _flash_get(self, sess):
        return sess.pop("flash", None) if sess else None

    def _flash_set(self, kind, msg, path):
        sess = self._session()
        if sess:
            sess["flash"] = [kind, msg]
        self._redirect(path)

    def _parse_qs(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _read_form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _render(self, title, body, st, active="", extra=""):
        sess = self._session()
        banner = flash_banner(sess.pop("flash", None) if sess else None)
        self._send(shell(title, banner + body, st, active, extra))

    def _fail(self, st, active, msg, page_title, body_extra=""):
        self._render(page_title,
                     f'<div class="banner err">⚠ {_esc(msg)}</div>{body_extra}',
                     st, active)

    # --- GET 路由 --------------------------------------------------------
    def do_GET(self):
        path, qs = self._parse_qs()
        if path == "/qr":
            return self._qr(qs)
        if path == "/login":
            return self._page_login()
        if path == "/logout":
            sid = self._cookie()
            with _sess_lock:
                SESSIONS.pop(sid, None)
            return self._redirect("/login")
        if path == "/api/reminders":
            return self._api_reminders()

        conn, st = self._require_login()
        if st is None:
            return
        try:
            if path == "/" or path == "/residents":
                return self._page_residents(conn, st)
            if path.startswith("/residents/"):
                return self._page_resident(conn, st, path.rsplit("/", 1)[1])
            if path == "/menus":
                return self._page_menus(conn, st)
            if path.startswith("/menus/"):
                return self._page_menu(conn, st, path.rsplit("/", 1)[1])
            if path == "/kitchen":
                return self._page_kitchen(conn, st, qs)
            if path.startswith("/labels/"):
                return self._page_labels(conn, st, path.rsplit("/", 1)[1])
            if path == "/routes":
                return self._page_routes(conn, st)
            if path.startswith("/routes/"):
                return self._page_route(conn, st, path.rsplit("/", 1)[1])
            if path == "/exceptions":
                return self._page_exceptions(conn, st)
            if path == "/family":
                return self._page_family(conn, st)
            self._send(b"<h1>404</h1>", 404)
        finally:
            conn.close()

    # --- POST 路由 -------------------------------------------------------
    def do_POST(self):
        path, _ = self._parse_qs()
        if path == "/login":
            return self._do_login()
        conn, st = self._require_login()
        if st is None:
            return
        form = self._read_form()
        try:
            if path == "/residents/propose":
                return self._do_propose(conn, st, form)
            if path == "/residents/confirm":
                return self._do_confirm(conn, st, form)
            if path == "/menus/add_item":
                return self._do_menu_add(conn, st, form)
            if path == "/menus/submit":
                return self._do_menu_submit(conn, st, form)
            if path == "/menus/review":
                return self._do_menu_review(conn, st, form)
            if path == "/dishes/review":
                return self._do_dish_review(conn, st, form)
            if path == "/kitchen/generate":
                return self._do_batch_generate(conn, st, form)
            if path == "/kitchen/prepare":
                return self._do_batch_prepare(conn, st, form)
            if path == "/boxes/temp":
                return self._do_box_temp(conn, st, form)
            if path == "/boxes/void":
                return self._do_box_void(conn, st, form)
            if path == "/boxes/substitute":
                return self._do_box_substitute(conn, st, form)
            if path == "/boxes/quarantine":
                return self._do_box_quarantine(conn, st, form)
            if path == "/routes/start":
                return self._do_route_start(conn, st, form)
            if path == "/routes/handover":
                return self._do_route_handover(conn, st, form)
            if path == "/routes/complete":
                return self._do_route_complete(conn, st, form)
            if path == "/api/scan":
                return self._do_scan_api(conn, st, form)
            if path == "/stops/refuse":
                return self._do_stop_refuse(conn, st, form)
            if path == "/deliveries/note":
                return self._do_delivery_note(conn, st, form)
            if path == "/events/handle":
                return self._do_event_handle(conn, st, form)
            if path == "/family/handle":
                return self._do_family_handle(conn, st, form)
            if path == "/family/fulfill":
                return self._do_family_fulfill(conn, st, form)
            self._send(b"not found", 404)
        except svc.DomainError as e:
            back = form.get("back", "/")
            self._flash_set("err", str(e), back)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 二维码
    # ------------------------------------------------------------------
    def _qr(self, qs):
        payload = qs.get("payload", [""])[0]
        px = int(qs.get("px", ["10"])[0])
        try:
            mat = qradapter.encode(payload)
            svg = qradapter.to_svg(mat, px=px).encode("utf-8")
            self._send(svg, ctype="image/svg+xml")
        except Exception as e:
            self._send(str(e).encode(), 400, "text/plain")

    def _api_reminders(self):
        items = REMINDER_FEED[-10:]
        self._send(json.dumps({"items": items}, ensure_ascii=False).encode(),
                   ctype="application/json")

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def _page_login(self):
        conn = self._db()
        users = conn.execute("SELECT id,name,role,passcode FROM staff ORDER BY id").fetchall()
        opts = "".join(
            f'<option value="{u["id"]}">{_esc(u["name"])}（{_role_badge(u["role"])}，'
            f'口令 {u["passcode"]}）</option>' for u in users)
        body = f"""
        <div class="card" style="max-width:460px;margin:60px auto">
          <h2>登录膳食核对平台</h2>
          <p class="muted">演示环境：选择员工并输入对应四位数口令。所有住户姓名与健康信息均为匿名演示数据。</p>
          <form method="post" action="/login">
            <label class="fld"><span>员工</span>
              <select name="staff_id" style="width:100%">{opts}</select></label>
            <label class="fld"><span>口令</span>
              <input name="passcode" inputmode="numeric" style="width:100%" autofocus></label>
            <button class="btn orange" style="width:100%">登录</button>
          </form>
        </div>"""
        self._send(shell("登录", body, None))

    def _do_login(self):
        form = self._read_form()
        conn = self._db()
        row = conn.execute("SELECT * FROM staff WHERE id=?", (form.get("staff_id"),)).fetchone()
        if not row or row["passcode"] != form.get("passcode", ""):
            body = '<div class="banner err">口令不正确</div><p><a href="/login">返回</a></p>'
            return self._send(shell("登录失败", body, None))
        import uuid
        sid = uuid.uuid4().hex
        with _sess_lock:
            SESSIONS[sid] = {"staff_id": row["id"]}
        audit(conn, row["id"], "login", "staff", row["id"])
        conn.commit()
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/residents")
        self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    # 1) 住户饮食卡
    # ------------------------------------------------------------------
    def _page_residents(self, conn, st):
        rows = svc.list_residents(conn)
        pending = svc.pending_changes(conn)
        cards = []
        for r in rows:
            rv = svc.resident_view(r)
            avoids = "".join(f'<span class="tag gray">🚫 {_esc(a)}</span>'
                             for a in rv["avoidances"])
            diet = f'<span class="tag diet">🍽 {_esc(rv["special_diet"])}</span>' if rv["special_diet"] else ""
            status = status_pill(rv["card_status"])
            cards.append(f"""
            <div class="card">
              {allergy_bar(rv)}
              <div class="row" style="justify-content:space-between">
                <h3>{_esc(rv["alias"])} <span class="muted" style="font-size:13px">
                  {_esc(rv["floor"])}·{_esc(rv["room"])}</span></h3>{status}
              </div>
              <div style="margin:6px 0">{avoids}{diet}
                <span class="tag teal">🦷 {rv["chewing_name"]}</span>
                <span class="tag gray">生效 {_esc(rv["effective_date"])}</span></div>
              <div class="row" style="justify-content:space-between;margin-top:8px">
                <a class="btn sm ghost" href="/residents/{rv["id"]}">饮食卡 / 腕带码</a>
                {('<a class="btn sm" href="/residents/'+rv["id"]+'#changes">待确认修改</a>')
                 if any(p["resident_id"] == rv["id"] for p in pending) else ""}
              </div>
            </div>""")
        pending_html = ""
        if pending:
            items = "".join(
                f"""<tr><td>{_esc(p["resident_alias"])}</td>
                <td><code>{_esc(p["patch"])}</code></td>
                <td>{_esc(p["reason"])}</td><td>{_esc(p["created_by_name"])}</td>
                <td>{_esc(p["created_at"])}</td></tr>""" for p in pending)
            can_confirm = svc.can(st["role"], "profile.confirm")
            pending_html = f"""
            <div class="card" id="confirm-panel">
              <h3>🔎 档案修改待第二人确认（{len(pending)}）</h3>
              <p class="muted">发起人不可自行确认；{"你有确认权限" if can_confirm else "仅护理员/管理员可确认"}。</p>
              <table><tr><th>住户</th><th>修改内容</th><th>原因</th><th>发起人</th><th>时间</th><th></th></tr>
              {items}
              <tr><td colspan="6"><a class="btn sm" href="/residents/{pending[0]["resident_id"]}#changes">前往确认</a></td></tr>
              </table></div>"""
        body = f"""
        <div class="row" style="justify-content:space-between">
          <h2>住户饮食卡</h2>
          <span class="muted">供餐日期 {TODAY} · 共 {len(rows)} 人 · 饮食限制置顶显示</span>
        </div>
        {pending_html}
        <div class="grid cards">{''.join(cards)}</div>"""
        self._render("住户饮食卡", body, st, "/residents")

    def _page_resident(self, conn, st, rid):
        r = svc.resident(conn, rid)
        if not r:
            return self._send(b"not found", 404)
        rv = svc.resident_view(r)
        payload = qradapter.build_payload("RES", rv["code"])
        can_edit = svc.can(st["role"], "profile.propose")
        can_confirm = svc.can(st["role"], "profile.confirm")

        # 近期交付
        deliveries = conn.execute(
            """SELECT dl.*, s.name AS by_name FROM deliveries dl
               JOIN staff s ON s.id=dl.delivered_by
               WHERE dl.resident_id=? ORDER BY dl.delivered_at DESC LIMIT 6""",
            (rid,)).fetchall()
        dl_html = "".join(
            f"""<tr><td>{_esc(d["delivered_at"])}</td>
             <td>{('已送达' if d["result"]=="delivered" else "拒餐")}</td>
             <td>{_esc(d["by_name"])}</td></tr>""" for d in deliveries) or \
            '<tr><td colspan="3" class="muted">暂无</td></tr>'

        changes = conn.execute(
            """SELECT pc.*, s1.name AS by_name, s2.name AS conf_name
               FROM profile_changes pc JOIN staff s1 ON s1.id=pc.created_by
               LEFT JOIN staff s2 ON s2.id=pc.confirmed_by
               WHERE pc.resident_id=? ORDER BY pc.created_at DESC LIMIT 8""",
            (rid,)).fetchall()
        ch_rows = []
        for c in changes:
            if c["status"] == "pending":
                act = ""
                if can_confirm and c["created_by"] != st["id"]:
                    act = (f'<form method="post" action="/residents/confirm" class="row">'
                           f'<input type="hidden" name="change_id" value="{c["id"]}">'
                           f'<input name="note" placeholder="核对备注" style="width:150px">'
                           f'<button name="accept" value="1" class="btn sm">确认生效</button>'
                           f'<button name="accept" value="0" class="btn sm danger">驳回</button></form>')
                elif c["created_by"] == st["id"]:
                    act = '<span class="tag warning">需第二名员工确认</span>'
            else:
                act = (f'{status_pill("confirmed" if c["status"]=="confirmed" else "rejected")} '
                       f'<span class="muted">{_esc(c["conf_name"] or "")}</span>')
            ch_rows.append(
                f"<tr><td><code>{_esc(c['patch'])}</code></td><td>{_esc(c['reason'])}</td>"
                f"<td>{_esc(c['by_name'])}</td><td>{_esc(c['created_at'])}</td><td>{act}</td></tr>")
        ch_html = "".join(ch_rows) or '<tr><td colspan="5" class="muted">暂无修改记录</td></tr>'

        edit_form = ""
        if can_edit:
            allergy_text = ",".join(f"{a['allergen']}:{a['level']}" for a in rv["allergies"])
        chew_opts = "".join(
            f'<option value="{k}" {"selected" if k==rv["chewing"] else ""}>{v}</option>'
            for k, v in svc.TEXTURE_NAMES.items())
        edit_form = f"""
            <div class="card" id="edit">
              <h3>发起档案修改（需第二名员工确认后生效）</h3>
              <form method="post" action="/residents/propose">
                <input type="hidden" name="resident_id" value="{rid}">
                <div class="row">
                  <label class="fld" style="flex:1"><span>过敏项（名称:等级，逗号分隔，如 花生:severe,牛奶:warning）</span>
                    <input name="allergies" style="width:100%" value="{_esc(allergy_text)}"></label>
                </div>
                <div class="row">
                  <label class="fld" style="flex:1"><span>忌口（逗号分隔）</span>
                    <input name="avoidances" style="width:100%" value="{_esc(",".join(rv["avoidances"]))}"></label>
                  <label class="fld"><span>咀嚼要求</span>
                    <select name="chewing">{chew_opts}</select></label>
                  <label class="fld"><span>特殊餐</span>
                    <input name="special_diet" value="{_esc(rv["special_diet"])}" placeholder="如 低盐/糖尿病餐"></label>
                  <label class="fld"><span>生效日期</span>
                    <input type="date" name="effective_date" value="{_esc(rv["effective_date"])}"></label>
                </div>
                <label class="fld"><span>修改原因（必填）</span>
                  <input name="reason" style="width:100%" placeholder="如：医嘱/复查结果"></label>
                <button class="btn orange">提交修改申请</button>
                <span class="muted">提交后档案进入「待第二人确认」，本人不能确认。</span>
              </form>
            </div>"""

        body = f"""
        <p><a href="/residents">← 全部住户</a></p>
        <div class="grid" style="grid-template-columns:1fr 320px">
          <div class="card">
            {allergy_bar(rv)}
            <h3>{_esc(rv["alias"])}（{_esc(rv["floor"])}·{_esc(rv["room"])}）{status_pill(rv["card_status"])}</h3>
            <p><b>严重过敏：</b>{'、'.join('<span class="tag severe">⛔ '+_esc(a["allergen"])+'</span>' for a in rv["allergies"] if a["level"]=="severe") or '<span class="muted">无</span>'}</p>
            <p><b>慎食：</b>{'、'.join(_esc(a["allergen"]) for a in rv["allergies"] if a["level"]!="severe") or '<span class="muted">无</span>'}</p>
            <p><b>忌口：</b>{'、'.join(_esc(a) for a in rv["avoidances"]) or '<span class="muted">无</span>'}</p>
            <p><b>咀嚼要求：</b>{rv["chewing_name"]}　<b>特殊餐：</b>{_esc(rv["special_diet"] or "无")}　<b>生效：</b>{_esc(rv["effective_date"])}</p>
            <h4>近期送餐记录（只可追加说明）</h4>
            <table><tr><th>时间</th><th>结果</th><th>经手人</th></tr>{dl_html}</table>
          </div>
          <div class="card" style="text-align:center">
            <h3>腕带 / 床头卡二维码</h3>
            {qr_img(payload, 9)}
            <div class="muted" style="font-family:monospace">{_esc(payload)}</div>
            <p class="muted">送餐端扫描此码确认住户身份</p>
          </div>
        </div>
        <div class="card" id="changes" style="margin-top:14px">
          <h3>档案修改与双人确认</h3>
          <table><tr><th>内容</th><th>原因</th><th>发起人</th><th>时间</th><th>确认</th></tr>{ch_html}</table>
        </div>
        {edit_form}"""
        self._render(_esc(rv["alias"]), body, st, "/residents")

    def _do_propose(self, conn, st, form):
        patch = {}
        if form.get("allergies", "").strip():
            items = []
            for part in form["allergies"].split(","):
                part = part.strip()
                if not part:
                    continue
                name, _, level = part.partition(":")
                items.append({"allergen": name.strip(),
                              "level": (level.strip() or "warning").lower()})
            patch["allergies"] = items
        if form.get("avoidances", "").strip():
            patch["avoidances"] = [x.strip() for x in form["avoidances"].split(",") if x.strip()]
        patch["chewing"] = form.get("chewing", "normal")
        if form.get("special_diet", "").strip():
            patch["special_diet"] = form["special_diet"].strip()
        if form.get("effective_date"):
            patch["effective_date"] = form["effective_date"]
        rid = form["resident_id"]
        svc.propose_profile_change(conn, st["id"], rid, patch, form.get("reason", ""))
        self._flash_set("ok", "修改申请已提交，等待第二名员工确认", f"/residents/{rid}#changes")

    def _do_confirm(self, conn, st, form):
        cid = form["change_id"]
        accept = form.get("accept") == "1"
        c = conn.execute("SELECT * FROM profile_changes WHERE id=?", (cid,)).fetchone()
        svc.confirm_profile_change(conn, cid, st["id"], accept, form.get("note", ""))
        self._flash_set("ok", "修改已确认生效" if accept else "修改已驳回",
                        f"/residents/{c['resident_id']}#changes")

    # ------------------------------------------------------------------
    # 2) 菜单与营养审核
    # ------------------------------------------------------------------
    def _page_menus(self, conn, st):
        menus = conn.execute("SELECT * FROM menus ORDER BY serve_date, meal_session").fetchall()
        rows = "".join(
            f"""<tr><td>{_esc(m["serve_date"])} {'午餐' if m["meal_session"]=="lunch" else "晚餐"}</td>
            <td>{status_pill(m["status"])}</td>
            <td>{_esc(m["review_note"] or "—")}</td>
            <td><a class="btn sm ghost" href="/menus/{m["id"]}">查看/审核</a></td></tr>"""
            for m in menus)
        # 菜品库营养审核
        pending_dishes = conn.execute(
            "SELECT * FROM dishes WHERE review_status!='approved' ORDER BY id").fetchall()
        dish_block = ""
        if pending_dishes:
            can_dish_review = svc.can(st["role"], "dish.review")
            drows = []
            for d in pending_dishes:
                act = ""
                if can_dish_review:
                    act = (f'<form method="post" action="/dishes/review" class="row">'
                           f'<input type="hidden" name="dish_id" value="{d["id"]}">'
                           f'<input name="note" placeholder="审核意见" style="width:130px">'
                           f'<button name="approve" value="1" class="btn sm">通过</button>'
                           f'<button name="approve" value="0" class="btn sm danger">不通过</button></form>')
                drows.append(
                    f"<tr><td><b>{_esc(d['name'])}</b></td><td>{dish_tags(d)}</td>"
                    f"<td>{d['energy_kcal']} kcal</td><td>{status_pill(d['review_status'])}</td>"
                    f"<td>{act}</td></tr>")
            dish_block = f"""
            <div class="card" style="margin-top:16px">
              <h3>菜品营养审核（{len(pending_dishes)} 道未通过）</h3>
              <p class="muted">未通过营养审核的菜品不能加入菜单，也不能作为替换餐。</p>
              <table><tr><th>菜品</th><th>过敏原/标签/质地</th><th>热量</th><th>状态</th>
              <th>{'审核' if can_dish_review else '仅营养师可审核'}</th></tr>{''.join(drows)}</table>
            </div>"""
        body = f"""
        <h2>菜单与营养审核</h2>
        <p class="muted">规则：只有全部菜品已通过营养审核的菜单才能发布；已发布菜单不可再增改菜品。</p>
        <table><tr><th>供餐</th><th>状态</th><th>审核备注</th><th></th></tr>{rows}</table>
        {dish_block}"""
        self._render("菜单审核", body, st, "/menus")

    def _page_menu(self, conn, st, mid):
        m = svc.menu(conn, mid)
        if not m:
            return self._send(b"not found", 404)
        items = svc.menu_dishes(conn, mid)
        item_rows = "".join(
            f"""<tr><td><b>{_esc(d["name"])}</b></td><td>{_esc(d["category"])}</td>
            <td>{dish_tags(d)}</td><td>{d["energy_kcal"]} kcal</td>
            <td>{status_pill(d["review_status"])}</td></tr>""" for d in items)
        report = svc.menu_safety_report(conn, mid)
        allergen_chips = "".join(
            f'<span class="tag severe">⚠ {_esc(a)}</span> '
            f'<span class="muted">{"、".join(_esc(n) for n in names)}</span><br>'
            for a, names in sorted(report["allergens"].items())) or \
            '<span class="muted">本菜单无过敏原</span>'
        aff_rows = "".join(
            f"""<tr><td>{_esc(a["resident"])}（{_esc(a["room"])}）</td>
            <td>{''.join(f'<span class="tag severe">⛔ {_esc(x)}</span>' for x in a["severe"]) or '<span class="muted">—</span>'}</td>
            <td>{"; ".join(f'<b>{_esc(h["dish"])}</b>：{_esc("、".join(h["issues"]))}' for h in a["hits"])}</td></tr>"""
            for a in report["affected"]) or \
            '<tr><td colspan="3" class="muted">按当前住户档案，菜单菜品均无需拦截/提示</td></tr>'

        review_form = ""
        if svc.can(st["role"], "menu.review") and m["status"] in ("pending", "rejected"):
            review_form = f"""
            <form method="post" action="/menus/review" class="row">
              <input type="hidden" name="menu_id" value="{mid}">
              <input type="hidden" name="back" value="/menus/{mid}">
              <input name="note" placeholder="营养审核意见" style="flex:1;min-width:220px">
              <button name="approve" value="1" class="btn">审核通过并发布</button>
              <button name="approve" value="0" class="btn danger">驳回</button>
            </form>"""

        add_form = ""
        if svc.can(st["role"], "menu.create") and m["status"] in ("draft", "pending", "rejected"):
            avail = conn.execute(
                "SELECT * FROM dishes WHERE review_status='approved' ORDER BY id").fetchall()
            opts = "".join(f'<option value="{d["id"]}">{_esc(d["name"])}</option>' for d in avail)
            submit = ""
            if m["status"] == "draft" and svc.can(st["role"], "menu.create"):
                submit = (f'<form method="post" action="/menus/submit" style="display:inline">'
                          f'<input type="hidden" name="menu_id" value="{mid}">'
                          f'<button class="btn orange">提交营养审核</button></form>')
            add_form = f"""
            <div class="card">
              <h3>添加菜品（仅限已审核菜品）</h3>
              <form method="post" action="/menus/add_item" class="row">
                <input type="hidden" name="menu_id" value="{mid}">
                <select name="dish_id">{opts}</select>
                <button class="btn">加入菜单</button>{submit}
              </form>
            </div>"""
        session_name = "午餐" if m["meal_session"] == "lunch" else "晚餐"
        body = f"""
        <p><a href="/menus">← 菜单列表</a></p>
        <div class="row" style="justify-content:space-between">
          <h2>{_esc(m["serve_date"])} {session_name} {status_pill(m["status"])}</h2>
        </div>
        <div class="grid" style="grid-template-columns:1.2fr .8fr">
          <div class="card">
            <h3>菜单菜品</h3>
            <table><tr><th>菜品</th><th>类别</th><th>标识</th><th>热量</th><th>审核</th></tr>
            {item_rows or '<tr><td colspan="5" class="muted">暂无菜品</td></tr>'}</table>
            <div style="margin-top:10px">{review_form}</div>
          </div>
          <div class="card">
            <h3>营养安全核查</h3>
            <p><b>菜单含过敏原</b></p><p>{allergen_chips}</p>
            <p><b>受影响住户（拦截/慎食）</b></p>
            <table><tr><th>住户</th><th>严重过敏</th><th>命中菜品</th></tr>{aff_rows}</table>
          </div>
        </div>
        {add_form}"""
        self._render("菜单详情", body, st, "/menus")

    def _do_menu_add(self, conn, st, form):
        svc.add_menu_item(conn, st["id"], form["menu_id"], form["dish_id"])
        self._flash_set("ok", "已加入（菜品均已通过营养审核）",
                        f'/menus/{form["menu_id"]}')

    def _do_menu_submit(self, conn, st, form):
        svc.submit_menu(conn, st["id"], form["menu_id"])
        self._flash_set("ok", "菜单已提交营养审核", f'/menus/{form["menu_id"]}')

    def _do_menu_review(self, conn, st, form):
        svc.review_menu(conn, st["id"], form["menu_id"], form.get("approve") == "1",
                        form.get("note", ""))
        self._flash_set("ok",
                        "菜单已审核通过并发布" if form.get("approve") == "1" else "菜单已驳回",
                        f'/menus/{form["menu_id"]}')

    def _do_dish_review(self, conn, st, form):
        svc.review_dish(conn, st["id"], form["dish_id"], form.get("approve") == "1",
                        form.get("note", ""))
        self._flash_set("ok", "菜品审核已更新", "/kitchen")

    # ------------------------------------------------------------------
    # 3) 厨房批次与标签
    # ------------------------------------------------------------------
    def _page_kitchen(self, conn, st, qs):
        batches = svc.list_batches(conn, TODAY)
        cards = []
        for b in batches:
            boxes = svc.batch_boxes(conn, b["id"])
            n = len(boxes)
            voided = sum(1 for x in boxes if x["status"] == "void")
            quar = sum(1 for x in boxes if x["status"] == "quarantined")
            cards.append(f"""
            <div class="card">
              <div class="row" style="justify-content:space-between">
                <h3>🍳 {_esc(b["id"])}（{'午餐' if b["meal_session"]=="lunch" else "晚餐"}）
                  {status_pill(b["status"])}</h3>
                <span class="muted">{n} 张标签（作废 {voided} · 隔离 {quar}）</span>
              </div>
              <p class="muted">{_esc(b["note"] or "")}　创建 {_esc(b["created_at"])}</p>
              <div class="row">
                <a class="btn sm ghost" href="/kitchen?batch={b["id"]}#boxes">餐盒与标签</a>
                <a class="btn sm ghost" href="/labels/{b["id"]}" target="_blank">🖨 整批标签</a>
                {('<form method="post" action="/kitchen/prepare" style="display:inline">'
                  f'<input type="hidden" name="batch_id" value="{b["id"]}">'
                  '<button class="btn sm orange">登记出餐</button></form>')
                 if b["status"] == "planned" and svc.can(st["role"], "batch.prepare") else ""}
              </div>
            </div>""")
        # 已发布但无批次的菜单可生成
        menus = conn.execute(
            """SELECT m.* FROM menus m WHERE m.status='published'
               AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.menu_id=m.id)
               AND m.serve_date=? ORDER BY m.meal_session""", (TODAY,)).fetchall()
        gen = ""
        if menus and svc.can(st["role"], "batch.create"):
            gen = '<div class="card"><h3>按已审核菜单生成批次</h3><div class="row">' + "".join(
                f"""<form method="post" action="/kitchen/generate">
                <input type="hidden" name="menu_id" value="{m["id"]}">
                <button class="btn">生成 {('午餐' if m["meal_session"]=="lunch" else "晚餐")}批次（配餐+标签）</button>
                </form>""" for m in menus) + "</div></div>"

        batch_id = qs.get("batch", [batches[0]["id"] if batches else ""])[0]
        boxes_html = self._kitchen_boxes(conn, st, batch_id) if batch_id else ""
        body = f"""
        <h2>厨房批次与餐盒标签</h2>
        <p class="muted">批次只能由已发布菜单生成；每个餐盒标签全局唯一，替换餐另发新标签且旧标签立即作废。</p>
        {gen}
        <div class="grid">{''.join(cards) or '<p class="muted">暂无批次</p>'}</div>
        {boxes_html}"""
        self._render("厨房批次", body, st, "/kitchen")

    def _kitchen_boxes(self, conn, st, batch_id):
        b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            return ""
        boxes = svc.batch_boxes(conn, batch_id)
        approved = conn.execute(
            "SELECT * FROM dishes WHERE review_status='approved' ORDER BY category,id").fetchall()
        dish_opts = "".join(
            f'<option value="{d["id"]}">{_esc(d["name"])}（{"、".join(jloads(d["allergens"],[])) or "无过敏原"}）</option>'
            for d in approved)
        rows = []
        can_temp = svc.can(st["role"], "temp.record")
        can_sub = svc.can(st["role"], "box.substitute")
        for x in boxes:
            rv = svc.resident_view(svc.resident(conn, x["resident_id"])) if x["resident_id"] else None
            rname = f'{_esc(rv["alias"])} <span class="muted">{_esc(rv["room"])}</span>' if rv else '<span class="tag gray">备用餐</span>'
            severe = "".join(f'<span class="tag severe">⛔{_esc(a["allergen"])}</span>'
                             for a in (rv["allergies"] if rv else []) if a["level"] == "severe")
            dish_names = []
            for did in jloads(x["dishes"], []):
                d = conn.execute("SELECT name FROM dishes WHERE id=?", (did,)).fetchone()
                dish_names.append(_esc(d["name"]) if d else did)
            temp = f'{x["temp_c"]:.1f}℃' if x["temp_c"] is not None else "未测温"
            temp_cls = "" if x["temp_c"] is None or x["temp_c"] >= 60 else ' style="color:var(--orange-d);font-weight:800"'
            acts = []
            if can_temp and x["status"] not in ("void", "delivered", "quarantined"):
                acts.append(
                    f'<form method="post" action="/boxes/temp" class="row">'
                    f'<input type="hidden" name="box_id" value="{x["id"]}">'
                    f'<input type="hidden" name="back" value="/kitchen?batch={batch_id}#boxes">'
                    f'<input name="temp_c" inputmode="decimal" style="width:72px" placeholder="℃">'
                    f'<button class="btn sm">测温</button></form>')
            if can_sub and x["status"] not in ("void", "delivered", "quarantined"):
                acts.append(
                    f'<details style="display:inline"><summary class="btn sm danger">替换餐</summary>'
                    f'<form method="post" action="/boxes/substitute" style="margin-top:6px;border:1px solid var(--line);padding:8px;border-radius:8px">'
                    f'<input type="hidden" name="box_id" value="{x["id"]}">'
                    f'<input type="hidden" name="back" value="/kitchen?batch={batch_id}#boxes">'
                    f'<label class="fld"><span>新菜品（可多选，仅限已审核）</span>'
                    f'<select name="dish_ids" multiple size="4">{dish_opts}</select></label>'
                    f'<label class="fld"><span>替换原因</span><input name="reason" required></label>'
                    f'<button class="btn orange sm">生成新标签并作废旧标签</button></form></details>')
                acts.append(
                    f'<form method="post" action="/boxes/void" style="display:inline">'
                    f'<input type="hidden" name="box_id" value="{x["id"]}">'
                    f'<input type="hidden" name="back" value="/kitchen?batch={batch_id}#boxes">'
                    f'<input type="hidden" name="reason" value="厨房作废">'
                    f'<button class="btn sm gray">作废</button></form>')
            rows.append(f"""
            <tr>
              <td><b>{_esc(x["label_code"])}</b>{' <span class="sub">替换</span>' if x["is_substitution"] else ""}<br>
                <span class="muted" style="font-size:12px">{_esc(x["sub_reason"] or "")}</span></td>
              <td>{rname}<br>{severe}</td>
              <td>{"、".join(dish_names)}</td>
              <td{temp_cls}>{temp}</td>
              <td>{status_pill(x["status"])}</td>
              <td><div class="row">{"".join(acts)}</div></td>
            </tr>""")
        body = f"""
        <div class="card" id="boxes" style="margin-top:14px">
          <div class="row" style="justify-content:space-between">
            <h3>餐盒与标签 · {_esc(batch_id)}</h3>
            <a class="btn sm ghost" href="/labels/{batch_id}" target="_blank">🖨 打印整批标签（大字+图标）</a>
          </div>
          <table><tr><th>标签号</th><th>住户</th><th>盒内菜品</th><th>中心温度</th><th>状态</th><th>操作</th></tr>
          {''.join(rows)}</table>
        </div>"""
        return body

    def _page_labels(self, conn, st, key):
        """整批或单盒标签打印：大字 + 图标 + 二维码。"""
        if key.startswith("B"):
            boxes = svc.batch_boxes(conn, key)
        else:
            boxes = [svc.box(conn, key)] if svc.box(conn, key) else []
        labels = []
        for x in boxes:
            rv = svc.resident_view(svc.resident(conn, x["resident_id"])) if x["resident_id"] else None
            payload = qradapter.build_payload("MEAL", x["label_code"])
            icons = [SVG["bowl"]]
            warn = ""
            if rv:
                if any(a["level"] == "severe" for a in rv["allergies"]):
                    icons.append(SVG["warn"])
                if rv["chewing"] in ("minced", "pureed"):
                    icons.append('<span style="font-size:24px">🦷</span>')
            if x["is_substitution"]:
                icons.append(SVG["ban"])
                warn = '<div class="l-warn">替换餐 · 旧标签已作废，以本标签为准</div>'
            severe = "、".join(_esc(a["allergen"]) for a in (rv["allergies"] if rv else [])
                               if a["level"] == "severe")
            if severe:
                warn += f'<div class="l-warn">⛔ 严禁含：{severe}</div>'
            labels.append(f"""
            <div class="label {'voided' if x["status"]=="void" else ''}">
              <div class="l-top">
                <div><div class="l-code">{_esc(x["label_code"])}</div>
                  <div class="l-res">{_esc(rv["alias"]) if rv else "备用替换餐"}</div>
                  <div class="l-room">{_esc(rv["floor"]+" "+rv["room"]) if rv else "按需指派"}
                    {'·替换餐 <span class="sub">SUB</span>' if x["is_substitution"] else ""}</div></div>
                {qr_img(payload, 6)}
              </div>
              <div class="l-icons">{"".join(icons)}</div>
              <div style="font-size:15px;font-weight:700">
                质地：{svc.TEXTURE_NAMES[rv["chewing"]] if rv else "—"}
                {("　特殊餐："+_esc(rv["special_diet"])) if rv and rv["special_diet"] else ""}</div>
              {warn}
              <div class="muted" style="margin-top:6px;font-size:12px">{_esc(x["sub_reason"] or "")}
                打印 {now()}</div>
            </div>""")
        body = ('<p><a href="/kitchen">← 返回厨房批次</a></p>'
                '<div class="row" style="align-items:flex-start">' + "".join(labels) + "</div>")
        self._render("餐盒标签", body, st, "/kitchen",
                     extra='<style>header.top{position:static}</style>')

    def _do_batch_generate(self, conn, st, form):
        try:
            bid = svc.generate_batch(conn, st["id"], form["menu_id"])
            self._flash_set("ok", f"批次 {bid} 已生成，餐盒标签已创建",
                            f"/kitchen?batch={bid}#boxes")
        except svc.DomainError as e:
            if e.code == "partial":
                self._flash_set("err", str(e),
                                f'/kitchen?batch={e.extra.get("batch_id")}#boxes')
            else:
                raise

    def _do_batch_prepare(self, conn, st, form):
        svc.prepare_batch(conn, st["id"], form["batch_id"])
        self._flash_set("ok", "批次已登记出餐", f'/kitchen?batch={form["batch_id"]}#boxes')

    def _do_box_temp(self, conn, st, form):
        ok = svc.record_temperature(conn, st["id"], form["box_id"],
                                    float(form["temp_c"]))
        self._flash_set("ok" if ok else "err",
                        f'温度 {float(form["temp_c"]):.1f}℃ 已记录'
                        + ("" if ok else "，低于 60℃，已登记温度不合格事件"),
                        form.get("back", "/kitchen"))

    def _do_box_void(self, conn, st, form):
        svc.void_box(conn, st["id"], form["box_id"], form.get("reason", "作废"))
        self._flash_set("ok", "标签已作废，不能继续使用", form.get("back", "/kitchen"))

    def _do_box_substitute(self, conn, st, form):
        ids = form.getlist("dish_ids") if hasattr(form, "getlist") else form.get("dish_ids", "").split(",")
        # _read_form 只取单值：多选控件改用 raw 再解析
        raw_ids = self._raw_multi.get("dish_ids", []) if hasattr(self, "_raw_multi") else ids
        new_id = svc.substitute_box(conn, st["id"], form["box_id"], raw_ids,
                                    form.get("reason", "替换餐"))
        self._flash_set("ok", f"替换餐 {new_id} 已生成，旧标签已作废并通过过敏拦截",
                        form.get("back", "/kitchen"))

    def _do_box_quarantine(self, conn, st, form):
        svc.quarantine_box(conn, st["id"], form["box_id"], form.get("reason", "异常隔离"))
        self._flash_set("ok", "异常餐盒已立即隔离", form.get("back", "/exceptions"))

    # ------------------------------------------------------------------
    # 4) 送餐路线（一屏核对）
    # ------------------------------------------------------------------
    def _page_routes(self, conn, st):
        routes = svc.list_routes(conn, TODAY)
        cards = []
        for rt in routes:
            stops = svc.route_stops(conn, rt["id"])
            done = sum(1 for s in stops if s["status"] == "delivered")
            refused = sum(1 for s in stops if s["status"] in ("refused", "exception"))
            courier = conn.execute("SELECT name FROM staff WHERE id=?",
                                   (rt["courier_id"],)).fetchone()["name"]
            hv = ""
            if rt["handover_at"]:
                hv = f'<br><span class="muted" style="font-size:12px">交接：{_esc(rt["handover_at"])}</span>'
            cards.append(f"""
            <div class="card">
              <div class="row" style="justify-content:space-between">
                <h3>{SVG["truck"]} {_esc(rt["name"])}</h3>{status_pill(rt["status"])}
              </div>
              <p class="muted">送餐员：{_esc(courier)}　站点 {len(stops)}
                （已送 {done} · 异常/拒 {refused}）{hv}</p>
              <a class="btn" href="/routes/{rt["id"]}">打开送餐核对一屏</a>
            </div>""")
        body = f"""
        <h2>送餐路线</h2>
        <p class="muted">交接后送餐员在一屏内依次完成：扫住户腕带 → 扫餐盒标签，双码通过才交付；异常餐盒立即隔离。</p>
        <div class="grid cards">{''.join(cards)}</div>"""
        self._render("送餐路线", body, st, "/routes")

    def _page_route(self, conn, st, rid):
        rt = svc.route(conn, rid)
        if not rt:
            return self._send(b"not found", 404)
        stops = svc.route_stops(conn, rid)
        # 当前操作站点：第一个非完成站点
        active_stop = next((s for s in stops if s["status"] not in
                            ("delivered", "refused", "exception")), None)

        # 顶部流程/操作
        can_route = svc.can(st["role"], "route.start")
        top_acts = []
        if rt["status"] == "planned" and can_route:
            top_acts.append(
                f'<form method="post" action="/routes/start" style="display:inline">'
                f'<input type="hidden" name="route_id" value="{rid}">'
                f'<button class="btn orange">▶ 出发开始配送</button></form>')
        if rt["status"] in ("planned", "in_transit") and svc.can(st["role"], "route.handover"):
            couriers = conn.execute(
                "SELECT id,name FROM staff WHERE role='courier' AND id!=? ORDER BY id",
                (st["id"],)).fetchall()
            copts = "".join(f'<option value="{c["id"]}">{_esc(c["name"])}</option>' for c in couriers)
            top_acts.append(
                f'<details style="display:inline-block"><summary class="btn ghost">🔁 路线交接</summary>'
                f'<form method="post" action="/routes/handover" style="margin-top:8px;padding:10px;'
                f'border:1px solid var(--line);border-radius:10px">'
                f'<input type="hidden" name="route_id" value="{rid}">'
                f'<label class="fld"><span>接收送餐员</span><select name="to_courier_id">{copts}</select></label>'
                f'<p class="muted">需扫描路线交接二维码确认：</p>'
                f'<div class="row">{qr_img(qradapter.build_payload("ROUTE", rid), 6)}'
                f'<button class="btn orange">扫码确认交接</button></div></form></details>')
        if rt["status"] == "in_transit" and not active_stop and svc.can(st["role"], "route.complete"):
            top_acts.append(
                f'<form method="post" action="/routes/complete" style="display:inline">'
                f'<input type="hidden" name="route_id" value="{rid}">'
                f'<button class="btn">✔ 全部站点完成，结束路线</button></form>')

        # 一屏核对主面板
        board = self._scan_board(conn, st, rt, active_stop)

        # 站点列表
        stop_rows = []
        for s in stops:
            rv = svc.resident_view(conn.execute(
                "SELECT * FROM residents WHERE id=?", (s["resident_id"],)).fetchone())
            marks = []
            if s["resident_scan_at"]:
                marks.append("住户✓")
            if s["box_scan_at"]:
                marks.append("餐盒✓")
            severe_chips = "".join(
                f'<span class="tag severe">⛔{_esc(a["allergen"])}</span>'
                for a in rv["allergies"] if a["level"] == "severe")
            stop_rows.append(
                f'<tr class="{"" if s["id"] == (active_stop["id"] if active_stop else None) else ""}">'
                f'<td>{s["seq"]}</td><td><b>{_esc(rv["alias"])}</b> {_esc(rv["room"])}<br>'
                f'{severe_chips}'
                f'</td><td><code>{_esc(s["label_code"] or "—")}</code></td>'
                f'<td>{status_pill(s["status"])}</td><td class="muted" style="font-size:12px">'
                f'{"、".join(marks) or "—"}</td></tr>')
        body = f"""
        <p><a href="/routes">← 路线列表</a></p>
        <div class="row" style="justify-content:space-between">
          <h2>{SVG["truck"]} {_esc(rt["name"])} {status_pill(rt["status"])}</h2>
          <div class="row">{"".join(top_acts)}</div>
        </div>
        {board}
        <div class="card" style="margin-top:14px">
          <h3>站点清单</h3>
          <table><tr><th>#</th><th>住户</th><th>餐盒标签</th><th>状态</th><th>扫描</th></tr>
          {''.join(stop_rows)}</table>
        </div>"""
        self._render("路线核对", body, st, "/routes")

    def _scan_board(self, conn, st, rt, active_stop) -> str:
        if not active_stop:
            return '<div class="card scan-main" style="margin-top:12px"><h3>' \
                   '本路线所有站点均已处理。</h3></div>'
        s = active_stop
        r = svc.resident(conn, s["resident_id"])
        rv = svc.resident_view(r)
        payload_res = qradapter.build_payload("RES", rv["code"])
        payload_box = qradapter.build_payload("MEAL", s["label_code"]) if s["label_code"] else ""
        # 其它码（演示错扫）
        other_res = conn.execute(
            "SELECT code,alias,room FROM residents WHERE id!=? LIMIT 1",
            (rv["id"],)).fetchone()
        other_box = conn.execute(
            "SELECT label_code FROM meal_boxes WHERE label_code!=? AND status='labeled' LIMIT 1",
            (s["label_code"],)).fetchone()
        void_box = conn.execute(
            "SELECT label_code FROM meal_boxes WHERE status='void' LIMIT 1").fetchone()

        step_res = "done" if s["resident_scan_at"] else "on"
        step_box = "on" if s["resident_scan_at"] and not s["box_scan_at"] else (
            "done" if s["box_scan_at"] else "")
        step_done = "done" if s["status"] == "delivered" else ""
        in_transit = rt["status"] == "in_transit"

        # 已交付则显示追加说明区
        delivered = conn.execute(
            """SELECT dl.* FROM deliveries dl JOIN route_stops rs ON rs.id=dl.stop_id
               WHERE rs.id=? ORDER BY dl.delivered_at DESC LIMIT 1""",
            (s["id"],)).fetchone()
        note_html = ""
        if delivered:
            notes = svc.delivery_notes(conn, delivered["id"])
            note_html = f"""
            <div class="card" style="margin-top:10px">
              <b>已送达记录（只允许追加说明）</b>
              <ul>{''.join(f'<li>{_esc(n["note"])} <span class="muted">— {_esc(n["created_by_name"])} {_esc(n["created_at"])}</span></li>' for n in notes) or '<li class="muted">暂无追加说明</li>'}</ul>
              <form method="post" action="/deliveries/note" class="row">
                <input type="hidden" name="delivery_id" value="{delivered["id"]}">
                <input type="hidden" name="back" value="/routes/{rt["id"]}">
                <input name="note" placeholder="追加说明（不可修改原记录）" style="flex:1">
                <button class="btn sm">追加</button>
              </form>
            </div>"""

        refuse_form = ""
        if in_transit and svc.can(st["role"], "delivery.refuse"):
            refuse_form = f"""
            <details style="margin-top:8px"><summary class="btn sm danger">登记拒餐</summary>
              <form method="post" action="/stops/refuse" class="row" style="margin-top:8px">
                <input type="hidden" name="stop_id" value="{s["id"]}">
                <input type="hidden" name="back" value="/routes/{rt["id"]}">
                <input name="reason" placeholder="拒餐原因" style="flex:1" required>
                <button class="btn danger sm">确认拒餐</button>
              </form></details>"""

        # 手动输入（扫码枪/摄像头）
        manual = f"""
        <form method="post" action="/api/scan" class="row" style="margin-top:8px">
          <input type="hidden" name="stop_id" value="{s["id"]}">
          <input type="hidden" name="back" value="/routes/{rt["id"]}">
          <input name="raw" placeholder="扫码枪/摄像头解码结果（RES: 或 MEAL:）" style="flex:1;min-width:200px">
          <button class="btn sm ghost">核对</button>
        </form>"""

        def sim_btn(label, raw, cls, extra=""):
            return (f'<form method="post" action="/api/scan" style="margin:0">'
                    f'<input type="hidden" name="stop_id" value="{s["id"]}">'
                    f'<input type="hidden" name="back" value="/routes/{rt["id"]}">'
                    f'<input type="hidden" name="raw" value="{_esc(raw)}">'
                    f'<button class="bigbtn {cls}" {extra if in_transit else "disabled"}>'
                    f'{label}</button></form>')

        return f"""
        <div class="scanboard" style="margin-top:10px">
          <div class="scan-main">
            {allergy_bar(rv)}
            <div class="flow">
              <div class="f {step_res}">① 住户身份</div>
              <div class="f {step_box}">② 餐盒标签</div>
              <div class="f {step_done}">③ 完成交付</div>
            </div>
            <div class="scan-step">当前站点：{s["seq"]} 号 · {_esc(rv["alias"])}（{_esc(rv["floor"])}·{_esc(rv["room"])}）</div>
            <div class="muted">应送餐盒：<code style="font-size:15px">{_esc(s["label_code"] or "—")}</code>
              {'<span class="sub">替换餐</span>' if s["is_substitution"] else ""}</div>
            {sim_btn("① 扫描住户腕带　" + ("✓ 已确认" if s["resident_scan_at"] else "(点击模拟扫码)"),
                     payload_res, "res", "disabled" if s["resident_scan_at"] else "")}
            {sim_btn("② 扫描餐盒标签　" + ("✓ 已核对并交付" if s["box_scan_at"] else "(点击模拟扫码)"),
                     payload_box, "box", "disabled" if not s["resident_scan_at"] or s["box_scan_at"] else "")}
            {manual}
            {refuse_form}
            {note_html}
            {('<div class="banner info">路线尚未出发或正在交接，扫描按钮暂不可用。</div>'
              if not in_transit else "")}
          </div>
          <div class="card">
            <h3>异常演练（扫描即留痕）</h3>
            <p class="muted">重复扫描、错扫他人码、旧标签都会被拦截并记录。</p>
            {sim_btn("重扫本住户码（重复扫描）", payload_res, "res")}
            {sim_btn(f'错扫他人住户码（{_esc(other_res["alias"])}）',
                     qradapter.build_payload("RES", other_res["code"]), "res")}
            {sim_btn("错扫他人餐盒（自动隔离错餐盒）",
                     qradapter.build_payload("MEAL", other_box["label_code"]), "box")}
            {sim_btn("扫描已作废旧标签",
                     qradapter.build_payload("MEAL", void_box["label_code"]), "box")}
            <div class="row" style="margin-top:8px">
              <span>{qr_img(payload_res, 5)}</span>
              <span>{qr_img(payload_box, 5) if payload_box else ""}</span>
            </div>
            <p class="muted" style="font-size:12px">左：住户腕带　右：餐盒标签</p>
          </div>
        </div>"""

    def _do_route_start(self, conn, st, form):
        svc.start_route(conn, st["id"], form["route_id"])
        self._flash_set("ok", "路线已出发，开始扫码核对", f'/routes/{form["route_id"]}')

    def _do_route_handover(self, conn, st, form):
        rid = form["route_id"]
        # 交接必须先扫路线码
        svc.scan_route_code(conn, st["id"], rid, qradapter.build_payload("ROUTE", rid))
        svc.handover_route(conn, st["id"], rid, form["to_courier_id"])
        to = conn.execute("SELECT name FROM staff WHERE id=?",
                          (form["to_courier_id"],)).fetchone()["name"]
        self._flash_set("ok", f"路线已交接给 {to}，配送继续", f"/routes/{rid}")

    def _do_route_complete(self, conn, st, form):
        svc.complete_route(conn, st["id"], form["route_id"])
        self._flash_set("ok", "路线已完成", f'/routes/{form["route_id"]}')

    def _do_scan_api(self, conn, st, form):
        # 表单是页面提交（非 XHR）：完成业务后跳回路线页
        svc.scan_at_stop(conn, st["id"], form["stop_id"], form["raw"])
        self._flash_set("ok", "扫描通过", form.get("back", "/routes"))

    def _do_stop_refuse(self, conn, st, form):
        svc.refuse_stop(conn, st["id"], form["stop_id"], form.get("reason", ""))
        self._flash_set("ok", "已登记拒餐", form.get("back", "/routes"))

    def _do_delivery_note(self, conn, st, form):
        svc.append_delivery_note(conn, st["id"], form["delivery_id"], form.get("note", ""))
        self._flash_set("ok", "说明已追加（原记录不可修改）", form.get("back", "/routes"))

    # ------------------------------------------------------------------
    # 5) 异常餐盒与事件
    # ------------------------------------------------------------------
    def _page_exceptions(self, conn, st):
        events = svc.list_events(conn)
        quar = conn.execute(
            """SELECT mb.*, r.alias FROM meal_boxes mb
               LEFT JOIN residents r ON r.id=mb.resident_id
               WHERE mb.status='quarantined' ORDER BY mb.updated_at DESC""").fetchall()
        can_handle = svc.can(st["role"], "event.handle")
        can_quar = svc.can(st["role"], "box.quarantine")
        ev_rows = []
        for e in events:
            act = ""
            if e["status"] == "open" and can_handle:
                act = (f'<form method="post" action="/events/handle" class="row">'
                       f'<input type="hidden" name="event_id" value="{e["id"]}">'
                       f'<input name="resolution" placeholder="处置结果" style="width:180px">'
                       f'<button class="btn sm">处置完成</button></form>')
            temp = f' <b style="color:var(--orange-d)">{e["temp_c"]:.1f}℃</b>' if e["temp_c"] is not None else ""
            ev_rows.append(
                f'<tr><td>{svc.EVENT_NAMES.get(e["type"], e["type"])}</td>'
                f'<td>{_esc(e["resident_alias"] or "—")}</td>'
                f'<td><code>{_esc(e["label_code"] or "—")}</code>{temp}</td>'
                f'<td>{_esc(e["detail"])}</td><td>{status_pill(e["status"])}'
                f'<div class="muted" style="font-size:12px">{_esc(e["resolution"] or "")}</div></td>'
                f'<td>{act}</td></tr>')
        q_rows = []
        for x in quar:
            reason = next((ev["detail"] for ev in events if ev["meal_box_id"] == x["id"]), "异常隔离")
            btn = ""
            if can_quar:
                btn = ('<span class="tag severe">保持隔离，等待核查</span>')
            q_rows.append(
                f'<tr><td><code>{_esc(x["label_code"])}</code></td>'
                f'<td>{_esc(x["alias"] or "备用餐")}</td><td>{_esc(reason)}</td>'
                f'<td>{_esc(x["updated_at"])}</td><td>{btn}</td></tr>')
        body = f"""
        <h2>异常餐盒与事件</h2>
        <p class="muted">错餐、漏餐、拒餐、温度不合格、家属调整分别登记；异常餐盒立即隔离，处置闭环留痕。</p>
        <div class="card">
          <h3>⛔ 已隔离餐盒（{len(q_rows)}）</h3>
          <table><tr><th>标签</th><th>登记住户</th><th>隔离原因</th><th>时间</th><th>处置</th></tr>
          {''.join(q_rows) or '<tr><td colspan="5" class="muted">暂无隔离餐盒</td></tr>'}</table>
        </div>
        <div class="card" style="margin-top:14px">
          <h3>事件记录（{len(ev_rows)}）</h3>
          <table><tr><th>类型</th><th>住户</th><th>餐盒/温度</th><th>详情</th><th>状态</th><th>处置</th></tr>
          {''.join(ev_rows)}</table>
        </div>"""
        self._render("异常餐盒", body, st, "/exceptions")

    def _do_event_handle(self, conn, st, form):
        svc.handle_event(conn, st["id"], form["event_id"], form.get("resolution", ""))
        self._flash_set("ok", "事件已处置闭环", "/exceptions")

    # ------------------------------------------------------------------
    # 6) 家属确认
    # ------------------------------------------------------------------
    def _page_family(self, conn, st):
        rows = svc.list_family_adjustments(conn)
        approved_dishes = conn.execute(
            "SELECT * FROM dishes WHERE review_status='approved' ORDER BY category,id").fetchall()
        can_family = svc.can(st["role"], "family.handle")
        out = []
        type_names = {"hold": "暂停供餐", "swap": "换餐", "extra": "加餐"}
        for fa in rows:
            acts = ""
            if fa["status"] == "requested" and can_family:
                acts = f"""
                <form method="post" action="/family/handle" class="row">
                  <input type="hidden" name="fa_id" value="{fa["id"]}">
                  <input name="note" placeholder="与家属复核备注" style="width:170px">
                  <button name="approve" value="1" class="btn sm">电话复核确认</button>
                  <button name="approve" value="0" class="btn sm danger">驳回</button>
                </form>"""
            fulfill = ""
            if fa["status"] == "confirmed" and can_family:
                if fa["change_type"] == "hold":
                    fulfill = (f'<form method="post" action="/family/fulfill" class="row" style="margin-top:6px">'
                               f'<input type="hidden" name="fa_id" value="{fa["id"]}">'
                               f'<input type="hidden" name="kind" value="hold">'
                               f'<button class="btn sm orange">执行：暂停供餐并作废对应餐盒</button></form>')
                else:
                    opts = "".join(
                        f'<option value="{d["id"]}">{_esc(d["name"])}</option>'
                        for d in approved_dishes)
                    fulfill = (f'<form method="post" action="/family/fulfill" class="row" style="margin-top:6px">'
                               f'<input type="hidden" name="fa_id" value="{fa["id"]}">'
                               f'<input type="hidden" name="kind" value="swap">'
                               f'<select name="dish_ids">{opts}</select>'
                               f'<button class="btn sm orange">执行：换餐（过敏拦截+旧标签作废）</button></form>')
            out.append(f"""
            <tr><td><b>{_esc(fa["resident_alias"])}</b> {_esc(fa["room"])}<br>
                <span class="muted">{_esc(fa["family_name"])} · 凭证 {_esc(fa["contact_code"])}</span></td>
              <td>{_esc(fa["serve_date"])} {'午餐' if fa["meal_session"]=="lunch" else "晚餐"}<br>
                <span class="tag teal">{type_names.get(fa["change_type"], fa["change_type"])}</span></td>
              <td>{_esc(fa["detail"])}</td>
              <td>{status_pill(fa["status"])}<div class="muted" style="font-size:12px">
                {_esc(fa["handle_note"] or "")}</div>{fulfill}</td>
              <td>{acts}</td></tr>""")
        body = f"""
        <h2>家属临时调整确认</h2>
        <p class="muted">家属来电的暂停/换餐先由护理员电话复核确认，再执行；换餐强制走替换餐规则（已审核菜品 + 过敏拦截 + 旧标签作废）。</p>
        <div class="card"><table>
          <tr><th>住户/家属</th><th>餐次/类型</th><th>调整内容</th><th>状态与执行</th><th>复核</th></tr>
          {''.join(out) or '<tr><td colspan="5" class="muted">暂无家属调整</td></tr>'}
        </table></div>"""
        self._render("家属确认", body, st, "/family")

    def _do_family_handle(self, conn, st, form):
        svc.handle_family_adjustment(conn, st["id"], form["fa_id"],
                                     form.get("approve") == "1", form.get("note", ""))
        self._flash_set("ok",
                        "已与家属复核确认" if form.get("approve") == "1" else "已驳回",
                        "/family")

    def _do_family_fulfill(self, conn, st, form):
        fa_id = form["fa_id"]
        if form.get("kind") == "hold":
            fa = svc.family_adjustment(conn, fa_id)
            svc.fulfill_family_hold(conn, st["id"], fa_id, fa["detail"])
        else:
            svc.fulfill_family_swap(conn, st["id"], fa_id,
                                    self._raw_multi.get("dish_ids", ["D13"]))
        self._flash_set("ok", "家属调整已执行", "/family")

    # 多选表单解析：覆盖 _read_form 的单值限制
    def _read_form(self) -> dict[str, str]:  # noqa: F811
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        parsed = urllib.parse.parse_qs(raw)
        self._raw_multi = {k: v for k, v in parsed.items()}
        return {k: v[-1] for k, v in parsed.items()}


# ---------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------

def main():
    if not os.path.exists(DB_FILE):
        print("未发现数据库，自动生成演示数据 ...")
        seed(reset=False)
    init_db(DB_FILE)
    worker = ReminderWorker(lambda: connect(DB_FILE))
    worker.start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"膳食核对平台已启动：http://localhost:{PORT}")
    print(f"数据库：{DB_FILE}")
    print("演示登录：任意员工 + 四位数口令（如护理员 3333 / 送餐员 5555）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        worker.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
