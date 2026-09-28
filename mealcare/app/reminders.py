"""进程内送餐提醒调度:后台守护线程轮询 reminders 表,
到点未触发的提醒标记为 fired 并推入内存通知队列,页面轮询读取。

不依赖外部消息队列/定时服务,适合单机演示。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from .db import connect


class ReminderBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._fired: list[dict] = []
        self._stop = threading.Event()
        self._thread = None
        self._db_path = None

    def start(self, db_path: str, poll_seconds: float = 10.0):
        self._db_path = db_path
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        args=(poll_seconds,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self, poll_seconds):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 调度失败不能拖垮进程
                print(f"[reminder] tick 失败: {exc}")
            self._stop.wait(poll_seconds)

    def tick(self) -> int:
        """检查并触发到期提醒,返回本次触发数量。"""
        if not self._db_path:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        conn = connect(self._db_path)
        fired_now = []
        try:
            rows = conn.execute(
                "SELECT rm.*, rt.code route_code FROM reminders rm "
                "LEFT JOIN routes rt ON rt.id=rm.route_id "
                "WHERE rm.status='pending' AND rm.fire_at<=?",
                (now,)).fetchall()
            for r in rows:
                conn.execute("UPDATE reminders SET status='fired' WHERE id=?", (r["id"],))
                fired_now.append({
                    "id": r["id"], "title": r["title"],
                    "route_code": r["route_code"], "fire_at": r["fire_at"],
                    "fired_at": now,
                })
            conn.commit()
        finally:
            conn.close()
        if fired_now:
            with self._lock:
                self._fired.extend(fired_now)
        return len(fired_now)

    def pending_count(self) -> int:
        if not self._db_path:
            return 0
        conn = connect(self._db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) c FROM reminders WHERE status='pending' "
                "AND fire_at > ?",
                (datetime.now().isoformat(timespec="seconds"),)).fetchone()["c"]
        finally:
            conn.close()

    def drain(self) -> list[dict]:
        """读取并清空已触发提醒(页面轮询消费)。"""
        with self._lock:
            out = self._fired[:]
            self._fired.clear()
            return out

    def peek(self) -> list[dict]:
        with self._lock:
            return self._fired[:]
