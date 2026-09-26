"""站点的告警台(W00c5a):``AlertBook`` 接上站点库与 SSE。

每一次变化(新起、吸收、确认、解决、升级)都**写穿**到 ``alerts`` 表,再推一帧
``{"kind": "alert", "alert": …}`` 给 SSE。站点重启时从库里读回(``AlertBook.restore``):
未解决的告警还在、升到第几档还在、序号接着走。

确认人由调用方(API 层)从**登录账号**取,不信请求体(W00c3 的规矩)。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from d1max_site.alerts import Alert, AlertBook, Channel, Level
from d1max_site.db import SiteDB

log = logging.getLogger(__name__)

#: 开张时内存里读回多少条最近的(已解决的也算);未解决的全读回。
RESTORE_RECENT = 200

_COLS = ("key", "level", "kind", "robot", "title", "detail", "first_ms", "last_ms", "count",
         "acked_by", "acked_ms", "resolved_by", "resolved_ms", "escalated")


def _from_row(r: Any) -> Alert:
    return Alert(key=r["key"], level=Level(r["level"]), kind=r["kind"], robot=r["robot"],
                 title=r["title"], detail=r["detail"], first_ms=r["first_ms"],
                 last_ms=r["last_ms"], count=r["count"], acked_by=r["acked_by"],
                 acked_ms=r["acked_ms"], resolved_by=r["resolved_by"],
                 resolved_ms=r["resolved_ms"], escalated=r["escalated"])


class AlertDesk:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int],
                 publish: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.db = db
        self._now = now_ms
        self._publish = publish
        self.book = AlertBook(sink=self._write)
        # 只读回未解决的与最近 RESTORE_RECENT 条:整张历史表读进内存会一直涨,升级每 5 s 还要遍历。
        # 序号从整张表的键里算(只读键),读回的只是一部分也不撞号。
        max_seq: dict[str, int] = {}
        for (key,) in db.query("SELECT key FROM alerts"):
            group, _, seq = key.rpartition("#")
            if seq.isdigit():
                max_seq[group] = max(max_seq.get(group, 0), int(seq))
        rows = db.query("SELECT * FROM alerts WHERE resolved_ms IS NULL UNION "
                        "SELECT * FROM (SELECT * FROM alerts ORDER BY last_ms DESC LIMIT ?)",
                        (RESTORE_RECENT,))
        self.book.restore((_from_row(r) for r in rows), max_seq=max_seq)

    # ------------------------------------------------------------ 写穿

    def _write(self, a: Alert) -> None:
        w = a.to_wire()
        vals = tuple(w[c] for c in _COLS)
        with self.db.tx() as c:
            c.execute(f"INSERT INTO alerts({', '.join(_COLS)}) VALUES "
                      f"({', '.join('?' * len(_COLS))}) ON CONFLICT(key) DO UPDATE SET "
                      + ", ".join(f"{k}=excluded.{k}" for k in _COLS[1:]), vals)
        if self._publish is not None:
            try:
                self._publish({"kind": "alert", "alert": w})
            except Exception:
                log.exception("告警推不出去(%s),库里已经记下", a.key)

    # ------------------------------------------------------------ 操作

    def raise_alert(self, *, kind: str, robot: str, title: str, detail: str = "") -> Alert:
        return self.book.raise_alert(kind=kind, robot=robot, title=title, detail=detail,
                                     now_ms=self._now())

    def absorbing(self, robot: str, kind: str) -> Alert | None:
        return self.book.absorbing(robot, kind, now_ms=self._now())

    def ack(self, key: str, *, who: str) -> Alert:
        return self.book.ack(key, who=who, now_ms=self._now())

    def resolve(self, key: str, *, who: str) -> Alert:
        return self.book.resolve(key, who=who, now_ms=self._now())

    def escalate(self) -> list[tuple[Alert, Channel]]:
        """P1 未确认的,到时限就升一档(换通道)。站点主循环每 5 s 调一次。"""
        return list(self.book.due_escalations(now_ms=self._now()))

    # ------------------------------------------------------------ 读

    def open(self) -> list[dict[str, Any]]:
        """未解决的,P1 在最上面。"""
        return [a.to_wire() for a in self.book.open()]

    def trim(self) -> int:
        """内存里多余的已解决告警放掉(库里一条不少)。站点主循环每拍调。"""
        return self.book.trim()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """最近的(含已解决),按最后一次触发倒序。从库里读:内存里的已解决的会被修剪。"""
        rows = self.db.query("SELECT * FROM alerts ORDER BY last_ms DESC, key LIMIT ?",
                             (limit,))
        return [_from_row(r).to_wire() for r in rows]

    def open_counts(self) -> dict[str, dict[str, int]]:
        """每台狗(以及 ``site``)未解决的告警按级别计数。"""
        out: dict[str, dict[str, int]] = {}
        for a in self.book.open():
            per = out.setdefault(a.robot, {"P1": 0, "P2": 0, "P3": 0})
            per[a.level.value] += 1
        return out


class LoopAlerts:
    """别的线程(判读、备份、接收口)报告警用的:**跳到事件循环里去报**。告警簿只许在事件循环里改
    (W00c5d 内部评审:别的线程直接改,循环里正在遍历的那一边会炸「字典在遍历时变了」,同一组两条
    还可能撞同一个键丢一条)。"""

    def __init__(self, desk: AlertDesk, loop) -> None:
        self.desk = desk
        self.loop = loop

    def raise_alert(self, **kw: Any) -> Any:
        t = getattr(self.loop, "_thread", None)
        if t is not None and t is threading.current_thread():
            return self.desk.raise_alert(**kw)     # 已经在事件循环里:直接报(等自己会死锁)

        async def go() -> Any:
            return self.desk.raise_alert(**kw)
        return self.loop.call(go, timeout_s=10.0)
