"""定位历史(运营可视化,能力台账 20):每台狗**定位丢了 / 变差**的每一段,单独记一张表。

以前只在告警历史里(丢定位的告警一解决就看不出是哪一段、在哪、跟哪个任务),不好回答「这块地方是不是
老丢定位」。这里每一段记:哪台狗、丢了还是变差、从什么时候到什么时候、在哪(进这一段时最后一个可信位置)、
为什么(狗在遥测 ``loc`` 里说的原因)、当时在跑哪个任务。

- **丢了(``lost``)**:遥测 ``loc.reason`` 非空(定位器不可信,狗没有地图位姿、导航暂停)。
  **变差(``degraded``)**:可信、但 ``loc.degraded`` 非空(在推算、σ 偏大,狗自动限速)。
- **不是质量分热力图**:不记「匹配分数」(错解也会打高分,见 B §5),只记狗**自己说了**
  不可信 / 变差的段。
- **去抖**:状态要连着 :data:`START_AFTER_MS` 才算开了一段(一两拍的闪烁不记);一段刚结束
  :data:`MERGE_GAP_MS` 内又来同样的,接着上一段(不拆成一串碎段)。
- **结束**:恢复了、转成另一种(丢了→变差)、狗掉线、遥测断了 :data:`SILENT_MS`、站点重启(``end_how``
  写明;后三种的结束时刻不是定位真的恢复的时刻)。
- **有界**:每台狗最多 :data:`PER_ROBOT` 段、全站最多 :data:`MAX_ROWS` 段、最多留
  :data:`KEEP_MS`(没结束的不删)。老狗(遥测没有 ``loc`` 块)不记 —— 不猜。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from d1max_contract.messages import Status, TaskState, Telemetry
from d1max_site.db import SiteDB

log = logging.getLogger(__name__)

START_AFTER_MS = 3_000
MERGE_GAP_MS = 10_000
SILENT_MS = 60_000
PER_ROBOT = 500
MAX_ROWS = 5_000
KEEP_MS = 90 * 24 * 3600 * 1000
#: 每隔这么久修剪一次(毫秒)。
PRUNE_EVERY_MS = 10 * 60_000
KINDS = ("lost", "degraded")


@dataclass
class _Pending:
    kind: str
    since_ms: int
    cause: str
    place: tuple[str, str, float, float] | None
    sigma: float | None
    source: str


@dataclass
class _Mem:
    cur_id: int | None = None
    cur_kind: str = ""
    pending: _Pending | None = None
    #: 最后一个可信的地图位姿 (map_id, version, x, y);丢定位时遥测没有位姿,地点用它。
    pose: tuple[str, str, float, float] | None = None
    task_id: str = ""
    seen_ms: int = 0


class LocHistory:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms
        self._m: dict[str, _Mem] = {}
        self._pruned_ms = 0
        # 站点重启:上次没结束的段结束不了了,封上(结束时刻是现在,不是真的恢复时刻)
        with self.db.tx() as c:
            c.execute("UPDATE loc_episodes SET end_ms=?, end_how='site_restart' "
                      "WHERE end_ms IS NULL", (self._now(),))

    def attach(self, dispatcher: Any) -> None:
        dispatcher.on_telemetry(self.on_telemetry)
        dispatcher.on_status(self.on_status)

    def _mem(self, rid: str) -> _Mem:
        return self._m.setdefault(rid, _Mem())

    # ------------------------------------------------------------ 上行

    def on_status(self, rid: str, s: Status) -> None:
        m = self._mem(rid)
        if not s.online:
            self._end(rid, m, "offline")
            m.pending = None
            return
        running = s.task is not None and s.task.state is TaskState.RUNNING
        m.task_id = s.task.task_id if running and s.task is not None else ""

    def on_telemetry(self, rid: str, t: Telemetry) -> None:
        m = self._mem(rid)
        now = self._now()
        m.seen_ms = now
        if t.pose is not None:
            m.pose = (t.pose.map_id, t.pose.map_version, round(t.pose.x, 2), round(t.pose.y, 2))
        if not isinstance(t.loc, dict):
            return                                    # 老狗没有定位块:不猜
        reason = str(t.loc.get("reason") or "")
        degraded = str(t.loc.get("degraded") or "")
        kind = "lost" if reason else "degraded" if degraded else ""
        if not kind:
            m.pending = None
            self._end(rid, m, "recovered")
            return
        if m.cur_id is not None:
            if m.cur_kind == kind:
                return
            self._end(rid, m, f"became_{kind}")       # 丢了→变差(或反过来):前一段到此为止
            m.pending = None
        sigma = t.loc.get("sigma_m")
        pend = m.pending
        if pend is None or pend.kind != kind:
            m.pending = _Pending(
                kind=kind, since_ms=now, cause=(reason or degraded)[:200], place=m.pose,
                sigma=float(sigma) if isinstance(sigma, (int, float)) else None,
                source=str(t.loc.get("source") or "")[:40])
            pend = m.pending
        if now - pend.since_ms >= START_AFTER_MS:
            self._open(rid, m, pend)
            m.pending = None

    def tick(self) -> None:
        """遥测断了太久的狗,封掉没结束的段;每隔一阵修剪。"""
        now = self._now()
        for rid, m in self._m.items():
            if m.cur_id is not None and now - m.seen_ms >= SILENT_MS:
                self._end(rid, m, "no_data")
            if m.pending is not None and now - m.seen_ms >= SILENT_MS:
                m.pending = None
        if now - self._pruned_ms >= PRUNE_EVERY_MS:
            self._pruned_ms = now
            self._prune()

    # ------------------------------------------------------------ 写

    def _open(self, rid: str, m: _Mem, p: _Pending) -> None:
        mp = p.place
        with self.db.tx() as c:
            prev = c.execute(
                "SELECT id, kind, end_ms, end_how FROM loc_episodes WHERE robot_id=? "
                "ORDER BY start_ms DESC, id DESC LIMIT 1", (rid,)).fetchone()
            if (prev is not None and prev["kind"] == p.kind and prev["end_ms"] is not None
                    and prev["end_how"] == "recovered"
                    and p.since_ms - prev["end_ms"] <= MERGE_GAP_MS):
                c.execute("UPDATE loc_episodes SET end_ms=NULL, end_how='' WHERE id=?",
                          (prev["id"],))                # 刚结束又来:接着上一段
                m.cur_id, m.cur_kind = int(prev["id"]), p.kind
                return
            cur = c.execute(
                "INSERT INTO loc_episodes(robot_id, kind, start_ms, map_id, map_version, x, y, "
                "cause, source, sigma_m, task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, p.kind, p.since_ms, mp[0] if mp else "", mp[1] if mp else "",
                 mp[2] if mp else None, mp[3] if mp else None, p.cause, p.source, p.sigma,
                 m.task_id))
            m.cur_id, m.cur_kind = int(cur.lastrowid), p.kind
        self._prune_robot(rid)

    def _end(self, rid: str, m: _Mem, how: str) -> None:
        if m.cur_id is None:
            return
        try:
            with self.db.tx() as c:
                c.execute("UPDATE loc_episodes SET end_ms=?, end_how=? WHERE id=? "
                          "AND end_ms IS NULL", (self._now(), how, m.cur_id))
        except Exception:
            log.exception("%s 定位历史封不上,下一拍再来", rid)
            return
        m.cur_id, m.cur_kind = None, ""

    def _prune_robot(self, rid: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM loc_episodes WHERE robot_id=? AND id NOT IN ("
                      "SELECT id FROM loc_episodes WHERE robot_id=? "
                      "ORDER BY start_ms DESC, id DESC LIMIT ?)", (rid, rid, PER_ROBOT))

    def _prune(self) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM loc_episodes WHERE end_ms IS NOT NULL AND end_ms < ?",
                      (self._now() - KEEP_MS,))
            c.execute("DELETE FROM loc_episodes WHERE id NOT IN ("
                      "SELECT id FROM loc_episodes ORDER BY start_ms DESC, id DESC LIMIT ?)",
                      (MAX_ROWS,))

    # ------------------------------------------------------------ 读

    def list(self, *, robot: str | None = None, kind: str | None = None,
             since_ms: int | None = None, until_ms: int | None = None,
             map_id: str | None = None, map_version: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        for col, val in (("robot_id", robot), ("kind", kind), ("map_id", map_id),
                         ("map_version", map_version)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if since_ms is not None:                       # 跟 [since, until] 有重叠的段
            where.append("(end_ms IS NULL OR end_ms>=?)")
            args.append(int(since_ms))
        if until_ms is not None:
            where.append("start_ms<=?")
            args.append(int(until_ms))
        rows = self.db.query(
            f"SELECT * FROM loc_episodes WHERE {' AND '.join(where)} "
            "ORDER BY start_ms DESC, id DESC LIMIT ?", (*args, max(1, min(int(limit), 1000))))
        now = self._now()
        out = []
        for r in rows:
            end = r["end_ms"]
            out.append({
                "id": r["id"], "robot_id": r["robot_id"], "kind": r["kind"],
                "start_ms": r["start_ms"], "end_ms": end, "ongoing": end is None,
                "duration_s": max(0, ((now if end is None else end) - r["start_ms"]) // 1000),
                "end_how": r["end_how"], "cause": r["cause"], "source": r["source"],
                "sigma_m": r["sigma_m"], "task_id": r["task_id"],
                "map_id": r["map_id"], "map_version": r["map_version"],
                "x": r["x"], "y": r["y"]})
        return out
