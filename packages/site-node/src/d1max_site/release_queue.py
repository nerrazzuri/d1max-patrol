"""等充完电、空闲时再换的升级(W44 第四期,设计 §5.3「软件升级」:「现在装」或
「等机器人都在充电时装」)。

换版本(``release_activate``)要机器人空闲(机器人自己查),充电那一趟也是任务,所以不能真在桩上换;
规矩是:**在线新鲜、没有任务、电量 ≥ :data:`MIN_BATTERY`**(充完电、停下来的时候)才发。
发出去机器人收下了就出队;
拒了(比如这时候被派走了)留着下一拍再试。每台一条,后排的盖前面的。

每次排队一个不复用的编号(``id``,外审 R7):回执回来只删**发出去的那一条**;等回执的时候业主又排了
一个,新的那条留着,下一拍照样发。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MIN_BATTERY = 80.0


class ReleaseQueue:
    def __init__(self, db: Any, dispatcher: Any, *, now_ms: Callable[[], int],
                 payload: Callable[[str], dict[str, Any]]) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self._payload = payload

    def add(self, robot_id: str, name: str, *, by: str) -> int:
        """排一个;这台原来排着的被盖掉。→ 这一次的编号。"""
        with self.db.tx() as c:
            c.execute("DELETE FROM release_queue WHERE robot_id=?", (robot_id,))
            return c.execute("INSERT INTO release_queue(robot_id, name, by, at_ms) "
                             "VALUES (?,?,?,?)", (robot_id, name, by, self._now())).lastrowid

    def done(self, robot_id: str, intent: int) -> bool:
        """``intent`` 那一次发出去、机器人收下了:只删那一条(后来又排的不动)。"""
        with self.db.tx() as c:
            return c.execute("DELETE FROM release_queue WHERE robot_id=? AND id=?",
                             (robot_id, intent)).rowcount > 0

    def remove(self, robot_id: str) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM release_queue WHERE robot_id=?",
                             (robot_id,)).rowcount > 0

    def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM release_queue ORDER BY robot_id")]

    def _ready(self, robot_id: str) -> bool:
        c = self.dispatcher.clients.get(robot_id)
        if not self.dispatcher._fresh(c):
            return False
        if getattr(getattr(c, "status", None), "task", None) is not None:
            return False
        batt = getattr(getattr(c, "telemetry", None), "battery_pct", None)
        return isinstance(batt, (int, float)) and batt >= MIN_BATTERY

    async def tick(self) -> None:
        for row in self.list():
            rid = row["robot_id"]
            if not self._ready(rid):
                continue
            try:
                got = await self.dispatcher.map_command(
                    rid, "release_activate", self._payload(row["name"]),
                    issued_by=f"{row['by']}(排队)")
            except Exception:
                log.exception("%s 排队的升级 %s 没发出去,下一拍再试", rid, row["name"])
                continue
            if (got or {}).get("ack", {}).get("result") == "accepted":
                self.done(rid, row["id"])
                log.info("%s 充完电空闲了,换成 %s", rid, row["name"])
