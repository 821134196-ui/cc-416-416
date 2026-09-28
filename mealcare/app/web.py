"""HTTP 服务与全部页面:纯标准库实现,无第三方框架。

页面:
  /                     首页概览
  /residents            住户饮食卡
  /menus                菜单审核
  /batches              厨房批次(含大字标签与替换)
  /routes               送餐路线列表
  /routes/scan?route=   送餐端一屏核对(固定高对比过敏提示)
  /incidents            异常餐盒
  /family               家属确认
  /delivery/<id>        已送达记录(只读 + 追加说明)
"""
from __future__ import annotations

import html
import json
import os
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from . import qr
from . import services as svc
from .db import connect, now_iso
from .reminders import ReminderBus

MEAL_TYPE_CN = {"lunch": "午餐", "dinner": "晚餐"}
ACTION_CN = {"cancel": "停餐", "replace_soft": "换软食",
             "extra_portion": "加半份主食", "late_serve": "延后送餐"}
KIND_CN = {"wrong_meal": "错餐", "missed": "漏餐", "refused": "拒餐",
           "temperature": "温度不合格", "family_adjust": "家属临时调整"}
ROLE_CN = {"manager": "护理主管", "dietitian": "营养师",
           "kitchen": "厨房", "courier": "送餐员"}

NAV = [
    ("/", "概览"),
    ("/residents", "住户饮食卡"),
    ("/menus", "菜单审核"),
    ("/batches", "厨房批次"),
    ("/routes", "送餐路线"),
    ("/incidents", "异常餐盒"),
    ("/family", "家属确认"),
]

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def h(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def qr_src(payload: str) -> str:
    return "/qr?d=" + quote(payload)


# ---------------------------------------------------------------------------
# 状态徽标
# ---------------------------------------------------------------------------

def severity_tag(sev: str) -> str:
    cls = {"mild": "amber", "moderate": "orange", "severe": "red",
           "anaphylaxis": "darkred",
           # 异常事件用 minor/major/critical
           "minor": "amber", "major": "red", "critical": "darkred"}.get(sev, "gray")
    label = svc.SEVERITY_CN.get(sev, {"minor": "一般", "major": "重大",
                                      "critical": "紧急"}.get(sev, sev))
    return f'<span class="tag {cls}">{h(label)}</span>'


def box_status_tag(s):
    return {
        "packed": '<span class="tag gray">已贴签</span>',
        "in_transit": '<span class="tag teal">在途</span>',
        "delivered": '<span class="tag green">已送达</span>',
        "refused": '<span class="tag amber">拒餐</span>',
        "quarantined": '<span class="tag red">隔离</span>',
        "substituted": '<span class="tag orange">已替换</span>',
        "returned": '<span class="tag gray">回收</span>',
    }.get(s, f'<span class="tag gray">{h(s)}</span>')


def route_status_tag(s):
    return {"planned": '<span class="tag gray">待交接</span>',
            "handed_over": '<span class="tag orange">送餐中</span>',
            "completed": '<span class="tag green">已完成</span>'}.get(s, s)


def route_stop_tag(s):
    return {"pending": '<span class="tag gray">待核对</span>',
            "ready": '<span class="tag orange">住户已扫·待餐盒</span>',
            "delivered": '<span class="tag green">已送达</span>',
            "refused": '<span class="tag amber">拒餐</span>',
            "skipped": '<span class="tag red">暂跳过·等替换</span>'}.get(s, s)


def menu_status_tag(s):
    return {"draft": '<span class="tag gray">草稿</span>',
            "pending_review": '<span class="tag orange">待营养审核</span>',
            "approved": '<span class="tag teal">审核通过</span>',
            "rejected": '<span class="tag red">驳回</span>',
            "published": '<span class="tag green">已发布可生产</span>'}.get(s, s)


def dish_review_badge(s):
    return {"approved": '<span class="tag green">已营养审核</span>',
            "rejected": '<span class="tag red">审核驳回</span>',
            "draft": '<span class="tag gray">待审核</span>'}.get(s, s)


def incident_kind_tag(k):
    cls = "red" if k in ("wrong_meal", "temperature") else "orange"
    return f'<span class="tag {cls}">{KIND_CN.get(k, k)}</span>'


def incident_status_tag(s):
    return {"open": '<span class="tag orange">待处理</span>',
            "quarantined": '<span class="tag red">已隔离待处置</span>',
            "resolved": '<span class="tag green">已闭环</span>'}.get(s, s)


def family_status_tag(s):
    return {"pending": '<span class="tag orange">待确认</span>',
            "confirmed": '<span class="tag teal">已确认执行</span>',
            "rejected": '<span class="tag gray">已拒绝</span>',
            "done": '<span class="tag green">已完成</span>'}.get(s, s)


# ---------------------------------------------------------------------------
# 页面片段
# ---------------------------------------------------------------------------

def incident_table(incs):
    if not incs:
        return '<p class="muted">太好了,暂无未处置异常。</p>'
    rows = []
    for i in incs:
        rows.append(f"""
        <tr><td>{incident_kind_tag(i['kind'])}</td>
        <td><b>{h(i['alias'] or '—')}</b></td>
        <td>{h(i['detail'][:60])}</td>
        <td>{incident_status_tag(i['status'])}</td></tr>""")
    return ("<table><tr><th>类型</th><th>住户</th><th>情况</th><th>状态</th></tr>"
            + "".join(rows) + "</table>")


def profile_change_list(conn, changes, with_actions=False):
    if not changes:
        return '<p class="muted">暂无待确认修改,生效档案稳定。</p>'
    staff = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM staff")}
    head = ("<table><tr><th>住户</th><th>变更内容(忌口/咀嚼/过敏)</th>"
            "<th>申请人</th>" + ("<th>第二人确认</th>" if with_actions else "") + "</tr>")
    rows = []
    for ch in changes:
        p = json.loads(ch["payload_json"])
        alls = "、".join(
            f'{a["name"]}·{svc.SEVERITY_CN[a["severity"]]}'
            for a in p.get("allergens", [])) or "无"
        action = ""
        if with_actions:
            action = f"""<td><form method="post" action="/profile/confirm" class="btn-row">
              <input type="hidden" name="change_id" value="{h(ch['id'])}">
              <button class="btn small" name="approve" value="1">第二人确认生效</button>
              <button class="btn small ghost" name="approve" value="0">驳回</button></form></td>"""
        rows.append(f"""
        <tr>
          <td><b>{h(ch['alias'])}</b></td>
          <td>忌口:{h(p.get('avoidances') or '—')}<br>
              咀嚼:{h(svc.TEXTURE_CN[p['chewing_level']])}<br>过敏:{h(alls)}</td>
          <td>{h(staff.get(ch['requested_by'], {}).get('name', '?'))}<br>
              <span class="muted">{h(ch['requested_at'][5:16])}</span></td>
          {action}
        </tr>""")
    return head + "".join(rows) + "</table>"


def route_list_table(routes):
    if not routes:
        return '<p class="muted">暂无路线</p>'
    rows = []
    for rt in routes:
        rows.append(f"""
        <tr>
          <td class="mono"><b>{h(rt['code'])}</b></td>
          <td>{h(rt['meal_date'])} {MEAL_TYPE_CN[rt['meal_type']]}</td>
          <td>{h(rt['courier_name'] or '—')}</td>
          <td>{rt['done']}/{rt['total']}</td>
          <td>{route_status_tag(rt['status'])}</td>
          <td><a class="btn small" href="/routes/scan?route={h(rt['id'])}">一屏核对</a></td>
        </tr>""")
    return ("<table><tr><th>路线</th><th>餐次</th><th>送餐员</th><th>进度</th>"
            "<th>状态</th><th>操作</th></tr>" + "".join(rows) + "</table>")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

def page_overview(conn, staff_id):
    def cnt(sql, *a):
        return conn.execute(sql, a).fetchone()["c"]
    n_res = cnt("SELECT COUNT(*) c FROM residents WHERE active=1")
    n_box = cnt("SELECT COUNT(*) c FROM boxes")
    n_done = cnt("SELECT COUNT(*) c FROM boxes WHERE status='delivered'")
    n_q = cnt("SELECT COUNT(*) c FROM boxes WHERE status='quarantined'")
    n_inc = cnt("SELECT COUNT(*) c FROM incidents WHERE status!='resolved'")
    n_pc = cnt("SELECT COUNT(*) c FROM profile_changes WHERE status='pending'")
    n_fa = cnt("SELECT COUNT(*) c FROM family_adjustments WHERE status='pending'")
    return f"""
    <div class="stat-row">
      <div class="stat"><div class="n">{n_res}</div><div class="l">在住住户</div></div>
      <div class="stat teal"><div class="n">{n_done}/{n_box}</div><div class="l">今日已送/总数</div></div>
      <div class="stat"><div class="n">{n_q}</div><div class="l">隔离餐盒</div></div>
      <div class="stat"><div class="n">{n_inc}</div><div class="l">未处置异常</div></div>
      <div class="stat teal"><div class="n">{n_pc}</div><div class="l">档案待双人确认</div></div>
      <div class="stat"><div class="n">{n_fa}</div><div class="l">家属调整待确认</div></div>
    </div>
    <div class="grid two-col">
      <div class="panel">
        <h2>未处置异常 <span class="tag orange">立即处理</span></h2>
        {incident_table(svc.list_incidents(conn, open_only=True)[:6])}
        <p class="muted"><a href="/incidents">进入异常餐盒页 →</a></p>
      </div>
      <div class="panel">
        <h2>档案修改待第二人确认</h2>
        {profile_change_list(conn, svc.pending_profile_changes(conn)[:5])}
        <p class="muted"><a href="/residents?show=changes">去确认 →</a></p>
      </div>
    </div>
    <div class="panel" style="margin-top:16px">
      <h2>今日路线</h2>
      {route_list_table(svc.list_routes(conn)[:5])}
    </div>"""


def page_residents(conn, staff_id, qs):
    cards = []
    for r in svc.list_residents(conn):
        prof = r["profile"]
        if r["allergens"]:
            all_html = "".join(
                f'<span style="display:inline-block;margin:2px;padding:2px 8px;border-radius:6px;'
                f'background:rgba(255,255,255,.18);font-size:13px">'
                f'{h(a["name"])}·{h(svc.SEVERITY_CN[a["severity"]])}</span>'
                for a in r["allergens"])
        else:
            all_html = '<span style="font-weight:700">无已知过敏</span>'
        avoid = h(prof["avoidances"]) if prof and prof["avoidances"] else "无忌口"
        cards.append(f"""
        <div class="panel res-card">
          <div class="restrict-strip">
            <div class="rt-title">⚠ 饮食限制(核对置顶)</div>
            <div class="rt-all">🚫 过敏:{all_html}</div>
            <div class="rt-avoid">🚫 忌口:{avoid} ｜ 咀嚼:{h(svc.TEXTURE_CN[prof['chewing_level']]) if prof else '—'}</div>
          </div>
          <div class="res-name">{h(r['alias'])}</div>
          <div class="res-meta">{h(r['care_zone'])} · {h(r['bed_no'])}床 · 档案 v{prof['version'] if prof else 0} · 生效 {h(prof['effective_date'] if prof else '—')}</div>
          <div class="btn-row">
            <a class="btn small ghost" href="/resident/{h(r['id'])}">查看饮食卡 / 二维码</a>
          </div>
        </div>""")
    changes = svc.pending_profile_changes(conn)
    show_changes = qs.get("show", [""])[0] == "changes"
    panel = f"""
    <div class="panel" style="margin-top:18px">
      <h2>档案变更 · 双人确认队列 <span class="tag orange">{len(changes)} 待处理</span></h2>
      {profile_change_list(conn, changes, with_actions=True)}
      <p class="muted">规则:档案修改由一名员工发起、<b>另一名不同员工</b>确认后才生效;
      申请人与确认人为同一人时会被拒绝。</p>
    </div>""" if (show_changes or changes) else ""
    return f"""
    <div class="page-head"><h1>住户饮食卡</h1>
      <span class="sub">饮食限制固定在卡片顶部 · 全部为匿名演示数据</span></div>
    <div class="grid cards">{''.join(cards)}</div>
    {panel}"""


def page_resident_detail(conn, staff_id, rid):
    r = svc.get_resident(conn, rid)
    prof = r["profile"]
    allergen_rows = "".join(
        f'<tr><td><b>{h(a["name"])}</b></td><td>{severity_tag(a["severity"])}</td></tr>'
        for a in r["allergens"]) or '<tr><td colspan="2" class="muted">无</td></tr>'
    stf = svc.get_staff(conn, staff_id)
    can_request = stf["role"] in svc.PERMISSIONS["profile.request"]
    chew_opts = "".join(
        f'<option value="{k}"{" selected" if prof and prof["chewing_level"] == k else ""}>{v}</option>'
        for k, v in svc.TEXTURE_CN.items())
    allergen_text = "\n".join(f'{a["name"]}={a["severity"]}' for a in r["allergens"])
    edit_form = f"""
    <hr class="soft">
    <h2>发起档案修改(需另一名员工确认)</h2>
    <form method="post" action="/resident/{h(rid)}/change" class="grid" style="gap:10px">
      <label class="fld">忌口(分号分隔)
        <input type="text" name="avoidances" value="{h(prof['avoidances'] if prof else '')}"></label>
      <label class="fld">咀嚼要求<select name="chewing_level">{chew_opts}</select></label>
      <label class="fld">生效日期
        <input type="date" name="effective_date" value="{h(prof['effective_date'] if prof else date.today().isoformat())}" required></label>
      <label class="fld">过敏项(每行:名称=等级 mild/moderate/severe/anaphylaxis)
        <textarea name="allergens" rows="3">{h(allergen_text)}</textarea></label>
      <div><button class="btn orange">提交变更请求(不直接生效)</button></div>
    </form>""" if can_request else (
        '<hr class="soft"><p class="muted">当前身份无权发起档案修改(仅护理主管/营养师)。</p>')
    return f"""
    <div class="page-head"><h1>{h(r['alias'])} 饮食卡</h1>
      <span class="sub">{h(r['care_zone'])}·{h(r['bed_no'])}床</span></div>
    <div class="grid two-col">
      <div class="panel">
        <h2>当前生效档案 <span class="tag teal">v{prof['version'] if prof else 0}</span></h2>
        <table>
          <tr><th>忌口</th><td>{h(prof['avoidances'] if prof and prof['avoidances'] else '—')}</td></tr>
          <tr><th>咀嚼要求</th><td>{h(svc.TEXTURE_CN[prof['chewing_level']]) if prof else '—'}</td></tr>
          <tr><th>生效日期</th><td>{h(prof['effective_date'] if prof else '—')}</td></tr>
        </table>
        <hr class="soft"><h2>过敏项与等级</h2>
        <table><tr><th>过敏原</th><th>等级</th></tr>{allergen_rows}</table>
        {edit_form}
      </div>
      <div class="panel">
        <h2>住户身份二维码</h2>
        <p class="muted">送餐员首扫此码确认人,再扫餐盒新标签。</p>
        <img class="qr-big" src="{qr_src(qr.resident_code(r['code']))}" alt="住户二维码">
        <p class="mono">{h(qr.resident_code(r['code']))}</p>
        <hr class="soft"><h2>今日餐盒与标签</h2>
        {resident_boxes(conn, rid)}
      </div>
    </div>"""


def resident_boxes(conn, rid):
    rows = conn.execute(
        "SELECT b.*, d.name dish_name, bl.token, bl.version, bl.status lbl, bl.reason, m.meal_date"
        " FROM boxes b JOIN dishes d ON d.id=b.dish_id"
        " JOIN batches bt ON bt.id=b.batch_id JOIN menus m ON m.id=bt.menu_id"
        " JOIN box_labels bl ON bl.box_id=b.id WHERE b.resident_id=? ORDER BY m.meal_date, bl.version",
        (rid,)).fetchall()
    if not rows:
        return '<p class="muted">今日暂无配餐</p>'
    out = ['<table><tr><th>标签</th><th>菜品</th><th>状态</th></tr>']
    for x in rows:
        void = x["lbl"] == "void"
        stamp = '<div class="void-stamp">作废</div>' if void else ""
        out.append(f"""<tr><td>
          <div class="label{" void" if void else ""}" style="padding:8px 10px;min-width:150px">{stamp}
            <div class="lb-token">{h(x['token'])}</div>
            <div class="lb-note">v{x['version']} {h(x['reason'] or '')}</div>
          </div></td>
          <td>{h(x['dish_name'])}<br><span class="muted">{h(svc.TEXTURE_CN.get(x['texture'], x['texture']))}</span></td>
          <td>{box_status_tag(x['status'])}</td></tr>""")
    return "".join(out) + "</table>"


def page_menus(conn, staff_id):
    stf = svc.get_staff(conn, staff_id)
    is_kitchen, is_nutr = stf["role"] == "kitchen", stf["role"] == "dietitian"
    cards = []
    for m in svc.list_menus(conn):
        items = svc.menu_detail(conn, m["id"])["items"]
        item_html = "".join(
            f'<tr><td>{h(d["name"])}</td><td>{h(d["category"])}</td>'
            f'<td>{h(d["contains_allergens"] or "—")}</td>'
            f'<td>{h(svc.TEXTURE_CN[d["texture"]])}</td>'
            f'<td>{d["kcal"]} kcal</td><td>{dish_review_badge(d["review_status"])}</td></tr>'
            for d in items)
        actions = []
        if m["status"] in ("draft", "rejected") and is_kitchen:
            actions.append(f'<form method="post" action="/menu/{h(m["id"])}/submit">'
                           f'<button class="btn small orange">提交营养审核</button></form>')
        if m["status"] == "pending_review" and is_nutr:
            actions.append(
                f'<form method="post" action="/menu/{h(m["id"])}/review" class="btn-row">'
                f'<button class="btn small" name="approve" value="1">审核通过并发布</button>'
                f'<button class="btn small danger" name="approve" value="0">驳回</button></form>')
        if m["status"] == "published" and is_kitchen:
            actions.append(
                '<form method="post" action="/batch/produce" class="btn-row">'
                f'<input type="hidden" name="menu_id" value="{h(m["id"])}">'
                '<button class="btn small orange">按此菜单生成厨房批次</button></form>')
        cards.append(f"""
        <div class="panel">
          <div class="btn-row" style="justify-content:space-between">
            <h2 style="margin:0">{h(m['meal_date'])} {MEAL_TYPE_CN[m['meal_type']]} {menu_status_tag(m['status'])}</h2>
            <div class="btn-row">{''.join(actions)}</div>
          </div>
          <table><tr><th>菜品</th><th>类别</th><th>含过敏原</th><th>质地</th><th>热量</th><th>营养审核</th></tr>
          {item_html}</table>
          {'<p class="muted">提示:当前身份无此操作权限,可在顶栏切换身份。</p>' if not actions else ''}
        </div>""")
    dish_rows = []
    for d in conn.execute("SELECT * FROM dishes ORDER BY review_status,code"):
        review_action = ""
        if d["review_status"] != "approved" and is_nutr:
            review_action = (
                '<form method="post" action="/dish/review" class="btn-row">'
                f'<input type="hidden" name="dish_id" value="{h(d["id"])}">'
                '<button class="btn small" name="approve" value="1">通过</button>'
                '<button class="btn small ghost" name="approve" value="0">驳回</button>'
                "</form>")
        dish_rows.append(
            f'<tr><td class="mono">{h(d["code"])}</td><td><b>{h(d["name"])}</b></td>'
            f'<td>{h(d["contains_allergens"] or "—")}</td><td>{h(svc.TEXTURE_CN[d["texture"]])}</td>'
            f'<td>{d["kcal"]}</td><td>{dish_review_badge(d["review_status"])}</td>'
            f'<td class="muted">{h(d["review_note"] or "")}</td><td>{review_action}</td></tr>')
    return f"""
    <div class="page-head"><h1>菜单营养审核</h1>
      <span class="sub">菜品/菜单发布前必须经营养师审核;未过审菜品不能进入菜单或替换餐</span></div>
    <div class="grid">{''.join(cards)}</div>
    <div class="panel" style="margin-top:16px">
      <h2>菜品审核台账(含待审 / 驳回样例)</h2>
      <table><tr><th>编码</th><th>菜名</th><th>含过敏原</th><th>切配质地</th><th>kcal</th><th>状态</th><th>审核意见</th><th>营养师操作</th></tr>
      {''.join(dish_rows)}
    </table>
    <p class="muted">仅营养师身份可审核菜品;切换身份后操作按钮出现。</p>
    </div>"""


def page_batches(conn, staff_id):
    panels = []
    for b in svc.list_batches(conn):
        detail = svc.batch_detail(conn, b["id"])
        approved = svc.approved_dishes(conn)
        cells = []
        for x in detail["boxes"]:
            alls = x["allergen_snapshot"] or "无已知过敏"
            icons = {"pureed": "🥣 糊状", "minced": "🍲 碎餐",
                     "soft": "🍲 软食", "normal": "🍚 普通"}.get(x["texture"], "🍚")
            void = x["label_status"] == "void"
            n_void = conn.execute(
                "SELECT COUNT(*) c FROM box_labels WHERE box_id=? AND status='void'",
                (x["id"],)).fetchone()["c"]
            void_note = (f'<div class="lb-note" style="color:#9a3b32;font-weight:800">'
                         f'⛔ 已有 {n_void} 张旧标签作废,扫码会被拒绝</div>') if n_void else ""
            opts = "".join(f'<option value="{d["id"]}">{h(d["name"])}</option>'
                           for d in approved if d["id"] != x["dish_id"])
            locked = x["status"] in ("delivered", "refused")
            cells.append(f"""
            <div class="label{" void" if void else ""}">
              {'<div class="void-stamp">作废</div>' if void else ""}
              <div class="lb-token">{h(x['label_token'])} <span class="tag gray">v{x['label_version']}</span></div>
              <div class="lb-dish">🍽 {h(x['dish_name'])}</div>
              <div class="lb-bed">🛏 {h(x['alias'])} · {h(x['care_zone'])}{h(x['bed_no'])}床</div>
              <div class="lb-icons">{icons}</div>
              <div class="lb-all">⚠ 过敏:{h(alls)}</div>
              <div class="lb-note">餐盒 {h(x['code'])} · {h(x['special_note'])} · {box_status_tag(x['status'])}</div>
              {void_note}
              <form method="post" action="/box/substitute" class="btn-row" style="margin-top:6px">
                <input type="hidden" name="box_id" value="{h(x['id'])}">
                <select name="new_dish_id" style="padding:4px 6px;font-size:12px;flex:1">{opts}</select>
                <input type="text" name="reason" placeholder="替换原因" style="padding:4px 6px;font-size:12px" required>
                <button class="btn small orange"{" disabled" if locked else ""}>换菜换签</button>
              </form>
              <small class="muted">仅可替换为已审核且不过敏菜品,提交后旧签立即作废</small>
            </div>""")
        panels.append(f"""
        <div class="panel">
          <h2>{h(b['code'])} · {h(b['meal_date'])} {MEAL_TYPE_CN[b['meal_type']]}
            <span class="tag teal">{b['box_count']} 盒</span>
            {'<span class="tag teal">已出餐</span>' if b['status']=='produced' else ''}</h2>
          <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(310px,1fr))">{''.join(cells)}</div>
        </div>""")
    return f"""
    <div class="page-head"><h1>厨房批次与餐盒标签</h1>
      <span class="sub">大字 + 图标标签 · 标签号全局唯一 · 替换只选已审核菜 · 旧标签立即作废</span></div>
    <div class="grid">{''.join(panels)}</div>"""


def page_routes(conn, staff_id):
    return f"""
    <div class="page-head"><h1>送餐路线</h1>
      <span class="sub">厨房交接后送餐员才可扫描交付 · 住户码 + 餐盒新标签双码齐全才完成</span></div>
    {route_list_table(svc.list_routes(conn))}"""


def page_scan(conn, staff_id, route_id):
    rt = svc.route_detail(conn, route_id)
    stops = rt["stops"]
    stf = svc.get_staff(conn, staff_id)
    handover = ""
    if rt["status"] == "planned":
        if stf["role"] == "kitchen":
            handover = f"""
            <form method="post" action="/route/{h(route_id)}/handover">
              <button class="btn orange" style="font-size:16px;padding:12px 24px">
                厨房交接给送餐员 {h(rt['courier_name'])} → 开始送餐</button>
            </form>"""
        else:
            handover = '<div class="flash err">路线尚未交接:请由<b>厨房</b>身份在顶栏切换后完成交接。</div>'

    # 固定位置高对比过敏提示:优先"速发(可致命)",其次"严重"住户
    def _risk_rank(s):
        snap = s["allergen_snapshot"] or ""
        if "速发" in snap:
            return 2
        if "严重" in snap:
            return 1
        return 0
    worst = max(stops, key=_risk_rank, default=None)
    worst = worst if _risk_rank(worst) > 0 else None
    fixed = f"""
    <div class="allergy-fixed">
      <div class="warn-ico">!</div>
      <div class="af-body">
        <div class="af-title">本线高风险住户 · 固定位置提示 · 先核对再交付</div>
        <div class="af-items">🚨 {h(worst['alias'])}({h(worst['care_zone'])}{h(worst['bed_no'])}床) — {h(worst['allergen_snapshot'])}</div>
      </div>
    </div>""" if worst else ""

    cards = []
    for s in stops:
        rcode = conn.execute("SELECT code FROM residents WHERE id=?",
                             (s["resident_id"],)).fetchone()["code"]
        chip_res = (f'<a class="qr-chip" data-scan="{h(qr.resident_code(rcode))}" title="无扫码枪时点击模拟扫描">'
                    f'<img src="{qr_src(qr.resident_code(rcode))}"><b>住户码</b></a>')
        chip_box = (f'<a class="qr-chip" data-scan="{h(qr.box_code(s["label_token"]))}" title="无扫码枪时点击模拟扫描">'
                    f'<img src="{qr_src(qr.box_code(s["label_token"]))}"><b>新标签 v{s["label_version"]}</b></a>')
        voids = conn.execute(
            "SELECT token FROM box_labels WHERE box_id=? AND status='void'",
            (s["box_id"],)).fetchall()
        old = ('<div class="muted" style="margin-top:6px">旧签(扫到应被拒绝/隔离):'
               + "".join(f' <a class="qr-chip" style="width:auto;padding:2px 8px" '
                         f'data-scan="{h(qr.box_code(v["token"]))}"><s>{h(v["token"])}</s></a>'
                         for v in voids) + "</div>") if voids else ""
        ops = ""
        if stf["role"] == "courier" and s["status"] not in ("delivered", "refused"):
            ops = f"""
            <div class="btn-row" style="margin-top:8px">
              <form method="post" action="/stop/{h(s['id'])}/temp" class="btn-row">
                <label class="fld">中心温度℃<input type="number" step="0.1" name="temp_c" style="width:88px" placeholder="65"></label>
                <button class="btn small">测温上报</button>
              </form>
              <form method="post" action="/stop/{h(s['id'])}/refuse" class="btn-row">
                <input type="text" name="reason" placeholder="拒餐原因(选填)" style="font-size:12px;padding:5px 8px">
                <button class="btn small ghost">登记拒餐</button>
              </form>
            </div>"""
        dv_link = ""
        if s["status"] == "delivered":
            dv = conn.execute("SELECT id FROM deliveries WHERE stop_id=?", (s["id"],)).fetchone()
            if dv:
                dv_link = f'<a class="btn small ghost" href="/delivery/{h(dv["id"])}">送达记录·追加说明</a>'
        cards.append(f"""
        <div class="panel stop-card" data-stop="{h(s['id'])}" data-resident="{h(s['resident_id'])}">
          <div class="btn-row" style="justify-content:space-between">
            <div><b>第 {s['seq']} 站</b> · {h(s['alias'])} · {h(s['care_zone'])}{h(s['bed_no'])}床 {route_stop_tag(s['status'])}</div>
            {dv_link}
          </div>
          <div class="label" style="margin-top:10px">
            <div class="lb-dish" style="font-size:22px">🍽 {h(s['dish_name'])}</div>
            <div class="lb-token">{h(s['label_token'])}</div>
            <div class="lb-all">⚠ {h(s['allergen_snapshot'] or '无已知过敏')}</div>
          </div>
          <div class="btn-row" style="margin-top:10px">{chip_res}{chip_box}</div>
          {old}{ops}
        </div>""")

    return f"""
    {fixed}
    <div class="page-head">
      <h1>一屏核对 · {h(rt['code'])}</h1>
      <span class="sub">{h(rt['meal_date'])} {MEAL_TYPE_CN[rt['meal_type']]} · 送餐员 {h(rt['courier_name'])} · {route_status_tag(rt['status'])}</span>
    </div>
    {handover}
    <div class="panel" style="margin-top:14px">
      <h2>扫码核对</h2>
      <div class="scan-stage">
        <div class="stage-box" id="stage1">
          <div class="step-no">1</div>
          <div><div class="sb-t">扫描住户码</div><div class="sb-s">确认送达对象</div></div>
        </div>
        <div class="stage-box" id="stage2">
          <div class="step-no">2</div>
          <div><div class="sb-t">扫描餐盒新标签</div><div class="sb-s">完成交付前最后核对</div></div>
        </div>
      </div>
      <div class="scan-bar">
        <input type="text" id="scan-input" placeholder="扫描住户码或餐盒新标签…" autocomplete="off" autofocus>
        <button class="btn orange" id="scan-send">核对</button>
      </div>
      <div class="scan-hint">有扫码枪:光标在框内直接扫描回车;无硬件:点击各站二维码模拟。
        必须先住户后餐盒;旧标签、人盒不符、未交接、重复扫描都会被即时拒绝。</div>
      <div class="scan-result" id="scan-result"></div>
    </div>
    <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(330px,1fr));margin-top:16px">
      {''.join(cards)}
    </div>
    <script>window.__ROUTE__="{h(route_id)}";window.__STAFF__="{h(staff_id)}";</script>"""


def page_incidents(conn, staff_id):
    stf = svc.get_staff(conn, staff_id)
    can_resolve = stf["role"] in svc.PERMISSIONS["incident.resolve"]
    rows = []
    for i in svc.list_incidents(conn):
        action = ""
        if i["status"] != "resolved" and can_resolve:
            action = f"""
            <form method="post" action="/incident/resolve" class="btn-row">
              <input type="hidden" name="incident_id" value="{h(i['id'])}">
              <input type="text" name="resolution" placeholder="处置说明" required>
              <button class="btn small">处置闭环</button>
            </form>"""
        temp = f'<br><span class="tag amber">实测 {i["temp_c"]:g}℃</span>' if i["temp_c"] is not None else ""
        res = f'<br><span class="muted">闭环:{h(i["resolution"])}</span>' if i["resolution"] else ""
        rows.append(f"""
        <tr>
          <td>{incident_kind_tag(i['kind'])}</td>
          <td><b>{h(i['alias'] or '—')}</b>{f'<br><span class="mono muted">{h(i["box_code"])}</span>' if i['box_code'] else ''}</td>
          <td>{h(i['detail'])}{temp}{res}</td>
          <td>{severity_tag(i['severity'])}</td>
          <td>{incident_status_tag(i['status'])}</td>
          <td>{action or '<span class="muted">—</span>'}</td>
        </tr>""")
    return f"""
    <div class="page-head"><h1>异常餐盒与事件</h1>
      <span class="sub">错餐 · 漏餐 · 拒餐 · 温度不合格 · 家属临时调整;异常盒立即隔离,替换后旧签作废</span></div>
    <div class="panel"><table>
      <tr><th>类型</th><th>住户/餐盒</th><th>情况与处置</th><th>级别</th><th>状态</th><th>闭环操作</th></tr>
      {''.join(rows)}
    </table></div>"""


def page_family(conn, staff_id):
    stf = svc.get_staff(conn, staff_id)
    can_confirm = stf["role"] in svc.PERMISSIONS["family.confirm"]
    rows = conn.execute(
        "SELECT fa.*, r.alias FROM family_adjustments fa JOIN residents r ON r.id=fa.resident_id"
        " ORDER BY CASE fa.status WHEN 'pending' THEN 0 ELSE 1 END, fa.created_at DESC").fetchall()
    trs = []
    for f in rows:
        act = ""
        if f["status"] == "pending" and can_confirm:
            act = f"""
            <form method="post" action="/family/confirm" class="btn-row">
              <input type="hidden" name="adjust_id" value="{h(f['id'])}">
              <button class="btn small" name="approve" value="1">确认执行</button>
              <button class="btn small ghost" name="approve" value="0">拒绝</button>
            </form>"""
        trs.append(f"""
        <tr>
          <td><b>{h(f['alias'])}</b></td>
          <td>{h(f['meal_date'])} {MEAL_TYPE_CN[f['meal_type']]}</td>
          <td>{h(ACTION_CN.get(f['action'], f['action']))}</td>
          <td>{h(f['detail'])}<br><span class="muted">来自:{h(f['contact'])} · {h(f['created_at'][5:16])}</span></td>
          <td>{family_status_tag(f['status'])}</td>
          <td>{act or '<span class="muted">—</span>'}</td>
        </tr>""")
    res_opts = "".join(f'<option value="{r["id"]}">{h(r["alias"])}</option>'
                       for r in svc.list_residents(conn))
    act_opts = "".join(f'<option value="{k}">{v}</option>' for k, v in ACTION_CN.items())
    return f"""
    <div class="page-head"><h1>家属临时调整确认</h1>
      <span class="sub">家属联系人匿名 · 先登记、员工确认后执行、单独留痕</span></div>
    <div class="panel" style="margin-bottom:16px">
      <h2>登记家属临时来电调整</h2>
      <form method="post" action="/family/create" class="form-inline">
        <label class="fld">住户<select name="resident_id">{res_opts}</select></label>
        <label class="fld">餐次<select name="meal_type"><option value="lunch">午餐</option><option value="dinner">晚餐</option></select></label>
        <label class="fld">要求<select name="action">{act_opts}</select></label>
        <label class="fld">说明<input type="text" name="detail" placeholder="家属要求明细" required></label>
        <button class="btn orange">登记(随后需确认)</button>
      </form>
    </div>
    <div class="panel"><table>
      <tr><th>住户</th><th>餐次</th><th>动作</th><th>说明/来源</th><th>状态</th><th>确认</th></tr>
      {''.join(trs)}
    </table></div>"""


def page_delivery(conn, staff_id, delivery_id):
    d = svc.delivery_detail(conn, delivery_id)
    notes = "".join(
        f'<li><b>{h(n["staff_name"])}</b> <span class="muted">{h(n["at"])}</span>:{h(n["note"])}</li>'
        for n in d["notes"]) or '<li class="muted">暂无追加说明</li>'
    return f"""
    <div class="page-head"><h1>送达记录(主记录只读)</h1>
      <span class="sub">已送达记录不允许修改,只能追加说明</span></div>
    <div class="grid two-col">
      <div class="panel">
        <h2>{h(d['alias'])} · {h(d['box_code'])}</h2>
        <table>
          <tr><th>结果</th><td>{'✅ 已送达' if d['result'] == 'delivered' else '拒餐'}</td></tr>
          <tr><th>住户扫码时刻</th><td class="mono">{h(d['resident_scan_at'])}</td></tr>
          <tr><th>餐盒扫码时刻</th><td class="mono">{h(d['box_scan_at'])}</td></tr>
          <tr><th>送达时刻</th><td class="mono">{h(d['delivered_at'])}</td></tr>
          <tr><th>送餐员</th><td>{h(d['staff_name'])}</td></tr>
        </table>
      </div>
      <div class="panel">
        <h2>追加说明(只增不改)</h2>
        <ul>{notes}</ul>
        <form method="post" action="/delivery/{h(delivery_id)}/note" class="btn-row">
          <input type="text" name="note" placeholder="补充说明(如:由护理员代收)" style="flex:1" required>
          <button class="btn orange">追加</button>
        </form>
      </div>
    </div>"""


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    bus: ReminderBus = None
    db_path = ""
    default_staff = "stf_run1"

    def log_message(self, fmt, *args):
        pass

    # -- 工具 --
    def _conn(self):
        return connect(self.db_path)

    def _staff(self) -> str:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip().startswith("staff="):
                return part.strip().split("=", 1)[1]
        return self.default_staff

    def _send(self, status, ctype, body: bytes, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, flash=None):
        if flash:
            sep = "&" if "?" in location else "?"
            location += f'{sep}msg={quote(flash[1])}&kind={flash[0]}'
        self._send(303, "text/plain", b"redirect", [("Location", location)])

    def _render(self, conn, body, title="膳食核对平台"):
        staff_id = self._staff()
        staff = svc.get_staff(conn, staff_id)
        nav = "".join(
            f'<a href="{u}"{" class=active" if self._nav_active(u) else ""}>{n}</a>'
            for u, n in NAV)
        opts = "".join(
            f'<option value="{r["id"]}"{" selected" if r["id"]==staff_id else ""}>'
            f'{h(r["name"])}｜{ROLE_CN.get(r["role"], r["role"])}</option>'
            for r in conn.execute("SELECT * FROM staff ORDER BY role"))
        doc = f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}</title><link rel="stylesheet" href="/static/style.css"></head><body>
<div class="toast-wrap" id="toast-wrap"></div>
<header class="topbar">
  <div class="brand">🍲 膳食核对平台</div>
  <nav>{nav}</nav>
  <div class="spacer"></div>
  <div class="bell" id="bell" title="送餐提醒">🔔<span class="dot" id="bell-dot" style="display:none">0</span></div>
  <form class="staff-switch" method="post" action="/switch-staff">
    <span>当前身份</span>
    <select name="staff_id" onchange="this.form.submit()">{opts}</select>
  </form>
</header>
<main class="wrap" id="main">{body}</main>
<script src="/app.js"></script></body></html>"""
        self._send(200, "text/html; charset=utf-8", doc.encode("utf-8"))

    def _nav_active(self, url):
        path = urlsplit(self.path).path
        return path == url or (url != "/" and path.startswith(url))

    def _error_page(self, message):
        conn = self._conn()
        try:
            body = (f'<div class="flash err">{h(message)}</div>'
                    '<p><a class="btn" href="/">返回概览</a></p>')
            self._render(conn, body, "操作被拒绝")
        finally:
            conn.close()

    # -- GET --
    def do_GET(self):
        parts = urlsplit(self.path)
        path, qs = parts.path, parse_qs(parts.query)
        staff_id = self._staff()
        try:
            if path == "/static/style.css":
                with open(os.path.join(STATIC_DIR, "style.css"), "rb") as f:
                    self._send(200, "text/css; charset=utf-8", f.read())
            elif path == "/app.js":
                with open(os.path.join(STATIC_DIR, "app.js"), "rb") as f:
                    self._send(200, "application/javascript; charset=utf-8", f.read())
            elif path == "/qr":
                self._send(200, "image/svg+xml; charset=utf-8",
                           qr.to_svg(qs.get("d", [""])[0], module=6).encode("utf-8"),
                           [("Cache-Control", "max-age=300")])
            elif path == "/api/reminders":
                items = self.bus.drain() if self.bus else []
                self._send(200, "application/json",
                           json.dumps(items, ensure_ascii=False).encode("utf-8"))
            elif path == "/api/scan":
                self._api_scan(qs.get("route", [""])[0], qs.get("q", [""])[0],
                               qs.get("stop", [""])[0] or None)
            else:
                self._page_get(path, qs, staff_id)
        except svc.DomainError as e:
            self._error_page(str(e))
        except Exception:
            traceback.print_exc()
            self.send_error(500)

    def _page_get(self, path, qs, staff_id):
        conn = self._conn()
        try:
            if path == "/":
                self._render(conn, page_overview(conn, staff_id))
            elif path == "/residents":
                self._render(conn, page_residents(conn, staff_id, qs))
            elif path.startswith("/resident/"):
                self._render(conn, page_resident_detail(conn, staff_id,
                                                        path.split("/")[2]))
            elif path == "/menus":
                self._render(conn, page_menus(conn, staff_id))
            elif path == "/batches":
                self._render(conn, page_batches(conn, staff_id))
            elif path == "/routes":
                self._render(conn, page_routes(conn, staff_id))
            elif path == "/routes/scan":
                self._render(conn, page_scan(conn, staff_id,
                                             qs.get("route", [""])[0]))
            elif path.startswith("/delivery/"):
                self._render(conn, page_delivery(conn, staff_id,
                                                 path.rsplit("/", 1)[-1]))
            elif path == "/incidents":
                self._render(conn, page_incidents(conn, staff_id))
            elif path == "/family":
                self._render(conn, page_family(conn, staff_id))
            else:
                self.send_error(404)
        finally:
            conn.close()

    def _api_scan(self, route, payload, active_stop):
        conn = self._conn()
        try:
            try:
                res = svc.scan(conn, route, payload, self._staff(),
                               active_stop_id=active_stop)
                conn.commit()
            except svc.DomainError as e:
                conn.rollback()
                res = {"accepted": False, "stage": "error", "message": str(e)}
            self._send(200, "application/json",
                       json.dumps(res, ensure_ascii=False).encode("utf-8"))
        finally:
            conn.close()

    # -- POST --
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        staff_id = self._staff()
        path = urlsplit(self.path).path
        back = self.headers.get("Referer", "/")

        def g(k, d=""):
            return form.get(k, [d])[0]

        try:
            conn = self._conn()
            try:
                if path == "/switch-staff":
                    self._send(303, "text/plain", b"ok",
                               [("Location", "/"),
                                ("Set-Cookie", f"staff={g('staff_id')}; Path=/; Max-Age=86400")])
                    return

                if path.startswith("/resident/") and path.endswith("/change"):
                    rid = path.split("/")[2]
                    allergens = []
                    for line in g("allergens").splitlines():
                        if "=" in line:
                            name, sev = line.split("=", 1)
                            allergens.append({"name": name.strip(),
                                              "severity": sev.strip()})
                    payload = {"avoidances": g("avoidances"),
                               "chewing_level": g("chewing_level"),
                               "effective_date": g("effective_date"),
                               "allergens": allergens}
                    svc.submit_profile_change(conn, rid, payload, staff_id)
                    conn.commit()
                    self._redirect(f"/resident/{rid}",
                                   ("ok", "变更请求已提交,等待第二名员工确认"))

                elif path == "/profile/confirm":
                    ok = g("approve") == "1"
                    svc.confirm_profile_change(conn, g("change_id"), staff_id, ok)
                    conn.commit()
                    self._redirect("/residents?show=changes",
                                   ("ok", "双人确认完成,档案已生效") if ok else
                                   ("ok", "变更已驳回"))

                elif path.startswith("/menu/") and path.endswith("/submit"):
                    svc.submit_menu(conn, path.split("/")[2])
                    conn.commit()
                    self._redirect("/menus", ("ok", "菜单已提交营养审核"))

                elif path.startswith("/menu/") and path.endswith("/review"):
                    ok = g("approve") == "1"
                    svc.review_menu(conn, path.split("/")[2], staff_id, ok)
                    conn.commit()
                    self._redirect("/menus",
                                   ("ok", "菜单已发布") if ok else ("ok", "菜单已驳回"))

                elif path == "/dish/review":
                    ok = g("approve") == "1"
                    svc.review_dish(conn, g("dish_id"), staff_id, ok,
                                    "营养师复核通过" if ok else "营养师复核驳回")
                    conn.commit()
                    self._redirect("/menus", ("ok", "菜品已通过营养审核") if ok
                                   else ("err", "菜品已驳回"))

                elif path == "/batch/produce":
                    menu_id = g("menu_id")
                    bid = svc.produce_batch(conn, menu_id, staff_id)
                    courier = conn.execute(
                        "SELECT id FROM staff WHERE role='courier' ORDER BY id LIMIT 1"
                    ).fetchone()["id"]
                    rid = svc.create_route_for_batch(conn, bid, courier)
                    conn.commit()
                    self._redirect(f"/routes/scan?route={rid}",
                                   ("ok", "批次已生成,路线已排程(待厨房交接)"))

                elif path == "/box/substitute":
                    svc.substitute_meal(conn, g("box_id"), g("new_dish_id"),
                                        staff_id, g("reason"))
                    conn.commit()
                    self._redirect(back, ("ok", "替换完成:旧标签已作废,请使用新标签"))

                elif path.startswith("/route/") and path.endswith("/handover"):
                    rid = path.split("/")[2]
                    _schedule_reminders(conn, rid)
                    svc.hand_over_route(conn, rid, staff_id)
                    conn.commit()
                    self._redirect(f"/routes/scan?route={rid}",
                                   ("ok", "交接完成,可以开始送餐核对"))

                elif path.startswith("/stop/") and path.endswith("/temp"):
                    res = svc.record_temperature(conn, path.split("/")[2],
                                                 staff_id, float(g("temp_c")))
                    conn.commit()
                    self._redirect(back, ("ok" if res["ok"] else "err", res["message"]))

                elif path.startswith("/stop/") and path.endswith("/refuse"):
                    svc.mark_refused(conn, path.split("/")[2], staff_id,
                                     g("reason", "住户拒餐"))
                    conn.commit()
                    self._redirect(back, ("ok", "已登记拒餐"))

                elif path == "/incident/resolve":
                    svc.resolve_incident(conn, g("incident_id"), staff_id,
                                         g("resolution"))
                    conn.commit()
                    self._redirect("/incidents", ("ok", "异常已处置闭环"))

                elif path == "/family/create":
                    svc.create_family_adjustment(
                        conn, g("resident_id"), date.today().isoformat(),
                        g("meal_type"), g("action"), g("detail"), staff_id)
                    conn.commit()
                    self._redirect("/family", ("ok", "家属调整已登记,等待员工确认"))

                elif path == "/family/confirm":
                    ok = g("approve") == "1"
                    svc.confirm_family_adjustment(conn, g("adjust_id"), staff_id, ok)
                    conn.commit()
                    self._redirect("/family",
                                   ("ok", "家属调整已确认并留痕") if ok else
                                   ("ok", "已拒绝该调整"))

                elif path.startswith("/delivery/") and path.endswith("/note"):
                    svc.add_delivery_note(conn, path.split("/")[2], staff_id, g("note"))
                    conn.commit()
                    self._redirect(back, ("ok", "说明已追加(历史记录未改动)"))

                else:
                    self.send_error(404)
            finally:
                conn.close()
        except svc.DomainError as e:
            self._error_page(str(e))
        except ValueError as e:
            self._error_page(f"输入有误:{e}")
        except Exception:
            traceback.print_exc()
            self.send_error(500)


def _schedule_reminders(conn, route_id):
    now = datetime.now()
    for i, (mins, title) in enumerate([
            (2, "路线已交接:请在 10 分钟内完成东区两站(高过敏风险)"),
            (12, "提醒:西区餐盒即将到达建议食用时限")]):
        conn.execute(
            "INSERT INTO reminders(id,route_id,title,fire_at,status,created_at)"
            " VALUES(?,?,?,?,'pending',?)",
            (svc.gid("rm"), route_id, title,
             (now + timedelta(minutes=mins)).isoformat(timespec="seconds"),
             now_iso()))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="膳食过敏与送餐核对平台")
    parser.add_argument("--db", default="data/mealcare.db")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args()

    if not args.no_seed and not os.path.exists(args.db):
        from .seed import seed
        seed(args.db)
        print(f"[startup] 已生成演示数据 → {args.db}")

    bus = ReminderBus()
    bus.start(args.db, poll_seconds=5)
    Handler.bus = bus
    Handler.db_path = args.db
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[startup] 平台运行中 → http://{args.host}:{args.port}")
    print("[startup] 顶栏可切换 护理主管/营养师/厨房/送餐员 身份体验字段权限")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bus.stop()


if __name__ == "__main__":
    main()
