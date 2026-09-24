"""站点的排程执行器(W00c2a,吸收 W06;设计决定三 A:排程在站点执行)。

每拍(``serve`` 里 30 s 一次)对**当前包**的每条排程用契约的 ``decide``(与狗上老执行器同一份
纯函数)判一次,``pick`` 选出这一拍起跑哪一条,选狗、派 ``patrol``。判据与账:

- ``last_started`` 落库(``schedule_state``),站点重启从库里恢复 —— 同一轮不起第二次。
  **在命令发出去之前就记**(连同一行 ``started``,带预先定好的 task_id):回执可能丢而狗其实收到了,
  发完再记的话下一拍会再派一趟,狗会排在第一趟后面再跑一遍。回执超时 → 这一轮就算起跑过了;
  狗明确拒收 → 这一行改记 ``dispatch_failed``,这一轮也不再重派(每 30 s 重发一条注定被拒的命令
  没有意义)。
- **任务包导入之前就到点的那一轮不算**:导入一个带 08:00 ``run_late`` 的包不会立刻补跑一趟迟了
  十几个小时的;也因此不同任务包里同名的排程不会互相串账(``last_started`` 按排程 id 存)。
- 每拍按优先级把到点的排程逐条过:每条挑自己能派的狗,已经被更优先的排程在这一拍占了的狗不算。
  只有「本来能派、狗被更优先的占了」才记 ``displaced``;一条派不出去不挡别的狗的排程。
- 每条排程每一轮的去向都记一行 ``schedule_runs``(同一轮同一种去向只记一次):
  ``started``(派出去了,带 task_id,结果由事件回写 ``result``)、``skip``/``alarm``(``decide`` 按
  ``on_missed`` 判的)、``no_robot``(到点了没有能派的狗)、``ambiguous``(不止一台能派、排程
  没指定,站点不替人挑)、``displaced``(同一拍有更优先的)、``dispatch_failed``(派了但被拒)、
  ``skew``(钟不可信)。
- 狗与站点断开时到点:``no_robot`` 记一笔;窗口过了 ``decide`` 给出 ``skip``/``alarm``,也记账。
  **站点不在狗离线时替它补跑**(设计决定三 A 的代价)。
- 钟:给了参照(``time_reference``)且偏差超阈值就不起,沿用 ``clock_skew``。

不做:抢占规则、待命点(W00c2b)。正在跑的不打断 —— 选狗时跳过忙着的狗。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from d1max_contract.dispatch import DispatchTimeout
from d1max_contract.messages import Ack, AckResult, Event
from d1max_contract.schedule import (
    Decision,
    DecisionKind,
    ScheduleEntry,
    clock_skew,
    decide,
    next_run,
    pick,
)
from d1max_site.catalog import ActiveBundle, active_bundle
from d1max_site.db import SiteDB
from d1max_site.dispatcher import Dispatcher, DispatchRefused

log = logging.getLogger(__name__)

PERIOD_S = 30.0
_TERMINAL = {"task_done": "done", "task_failed": "failed", "task_aborted": "aborted",
             "task_preempted": "preempted"}


class SiteScheduler:
    def __init__(self, db: SiteDB, dispatcher: Dispatcher, *, now_ms: Callable[[], int],
                 time_reference: Callable[[], tuple[int, str] | None] | None = None) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self._ref = time_reference
        self.last_error = ""
        dispatcher.on_event(self._on_event)
        dispatcher.on_ack(self._on_ack)

    # ------------------------------------------------------------ 账

    def last_started(self, entry_id: str) -> int | None:
        rows = self.db.query("SELECT last_started_ms FROM schedule_state WHERE entry_id=?",
                             (entry_id,))
        return rows[0]["last_started_ms"] if rows else None

    def _record(self, entry: ScheduleEntry, scheduled_ms: int, outcome: str, *,
                robot_id: str | None = None, task_id: str | None = None,
                note: str = "") -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO schedule_runs(entry_id, scheduled_ms, outcome, "
                      "robot_id, task_id, note, decided_at) VALUES (?,?,?,?,?,?,?)",
                      (entry.id, scheduled_ms, outcome, robot_id, task_id, note, self._now()))

    def runs(self, entry_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if entry_id is None:
            rows = self.db.query("SELECT * FROM schedule_runs ORDER BY id DESC LIMIT ?",
                                 (limit,))
        else:
            rows = self.db.query("SELECT * FROM schedule_runs WHERE entry_id=? "
                                 "ORDER BY id DESC LIMIT ?", (entry_id, limit))
        return [dict(r) for r in rows]

    def _on_event(self, robot_id: str, e: Event) -> None:
        result = _TERMINAL.get(e.kind)
        task_id = e.data.get("task_id") if isinstance(e.data, dict) else None
        if result is None or not task_id:
            return
        with self.db.tx() as c:
            c.execute("UPDATE schedule_runs SET result=? WHERE task_id=?", (result, task_id))

    def _on_ack(self, ack: Ack) -> None:
        """回执(包括超时之后才到的):狗明确没收,那一行从 started 改成 dispatch_failed。"""
        if ack.result in (AckResult.ACCEPTED, AckResult.DUPLICATE):
            return
        with self.db.tx() as c:
            c.execute("UPDATE OR IGNORE schedule_runs SET outcome='dispatch_failed', note=? "
                      "WHERE task_id=? AND outcome='started'",
                      (f"狗回 {ack.result.value}: {ack.reason}", ack.task_id))

    # ------------------------------------------------------------ 每拍

    async def tick(self) -> None:
        act = active_bundle(self.db)
        if act is None or not act.schedule.entries:
            return
        now_ms = self._now()
        now = datetime.fromtimestamp(now_ms / 1000, tz=act.schedule.tz())
        decisions = []
        for e in act.schedule.entries:
            d = decide(e, now=now, last_started_ms=self.last_started(e.id))
            if d.scheduled_ms is not None and d.scheduled_ms < act.imported_at:
                continue                     # 导入之前就到点的那一轮:这个包那时还不存在
            decisions.append((e, d))
        for e, d in decisions:
            if d.kind in (DecisionKind.SKIP, DecisionKind.ALARM) and d.scheduled_ms is not None:
                self._record(e, d.scheduled_ms, d.kind.value,
                             note=f"迟了 {d.late_min} 分钟,按 on_missed={e.on_missed}")
        ref = self._ref() if self._ref is not None else None
        if ref is not None:
            skew = clock_skew(local_ms=now_ms, reference_ms=ref[0], source=ref[1])
            if skew.alarm:
                for e, d in decisions:
                    if d.kind in (DecisionKind.DUE, DecisionKind.LATE):
                        self._record(e, d.scheduled_ms or 0, "skew",
                                     note=f"站点的钟跟 {skew.source} 差 {skew.skew_s:.0f} 秒")
                return
        picked = pick(decisions, running=None)
        order = ([picked.chosen] if picked.chosen is not None else []) + list(picked.displaced)
        claimed: dict[str, str] = {}                     # robot_id → 这一拍占了它的排程
        for e, d in order:
            await self._start(act, e, d, now_ms=now_ms, claimed=claimed)

    def _candidates(self, act: ActiveBundle, entry: ScheduleEntry,
                    claimed: dict[str, str]) -> tuple[list[str], list[str], str]:
        """→ (能派的, 本来能派但这一拍被占了的, 都不能派时的理由)。"""
        mission = act.missions[entry.mission]
        ids = [entry.robot] if entry.robot else [
            r.robot_id for r in self.dispatcher.registry.list() if not r.revoked]
        ok, taken, why = [], [], []
        for rid in ids:
            if rid in claimed:               # 这一拍刚派给了更优先的(它此刻已经报忙了)
                taken.append(rid)
                continue
            reason = self.dispatcher.dispatchable(rid, "patrol")
            if not reason and self.dispatcher.busy(rid) is not None:
                reason = f"{rid} 正在跑 {self.dispatcher.busy(rid)}"
            if not reason:
                caps = self.dispatcher.clients[rid].capabilities
                loaded = caps.tasks.get("patrol", {}).get("map_id") if caps else None
                if loaded != mission.map_id:
                    reason = f"{rid} 加载的地图是 {loaded!r},任务要 {mission.map_id!r}"
            (why.append(reason) if reason else ok.append(rid))
        return ok, taken, ";".join(why) or "没有登记的狗"

    async def _start(self, act: ActiveBundle, entry: ScheduleEntry, d: Decision, *,
                     now_ms: int, claimed: dict[str, str]) -> None:
        scheduled_ms = d.scheduled_ms or 0
        ok, taken, why = self._candidates(act, entry, claimed)
        if not ok:
            if taken:
                self._record(entry, scheduled_ms, "displaced",
                             note="; ".join(f"{r} 这一拍给了更优先的 {claimed[r]}"
                                            for r in taken)
                             + (f"(on_missed={entry.on_missed})"
                                if entry.on_missed == "alarm" else ""))
            else:
                self._record(entry, scheduled_ms, "no_robot", note=why)
            return
        if len(ok) > 1:
            self._record(entry, scheduled_ms, "ambiguous",
                         note=f"能派的狗不止一台({', '.join(ok)}),排程没写 robot")
            return
        rid = ok[0]
        claimed[rid] = entry.id
        task_id = f"sched-{uuid.uuid4().hex[:12]}"

        def 发之前记账(_cmd) -> None:
            with self.db.tx() as c:
                c.execute("INSERT INTO schedule_state(entry_id, last_started_ms) VALUES (?,?) "
                          "ON CONFLICT(entry_id) DO UPDATE SET last_started_ms=excluded."
                          "last_started_ms", (entry.id, now_ms))
            self._record(entry, scheduled_ms, "started", robot_id=rid, task_id=task_id,
                         note=f"{d.kind.value},已发出")

        try:
            r = await self.dispatcher.patrol(rid, act.missions[entry.mission].to_wire(),
                                             issued_by=f"schedule:{entry.id}",
                                             priority=entry.priority, task_id=task_id,
                                             before_send=发之前记账)
        except DispatchRefused as exc:                    # 发之前就被拦下:这一轮没消耗
            self._record(entry, scheduled_ms, "dispatch_failed", robot_id=rid, note=str(exc))
            return
        except DispatchTimeout:
            with self.db.tx() as c:
                c.execute("UPDATE schedule_runs SET note=? WHERE task_id=?",
                          (f"{d.kind.value},回执超时:可能已在跑,这一轮不再派", task_id))
            return
        if r["ack"]["result"] == "accepted":
            log.info("排程 %s 到点(%s),派 %s 给 %s", entry.id, d.kind.value, entry.mission, rid)

    # ------------------------------------------------------------ 视图

    def view(self) -> dict[str, Any]:
        act = active_bundle(self.db)
        if act is None:
            return {"bundle": None, "entries": []}
        now = datetime.fromtimestamp(self._now() / 1000, tz=act.schedule.tz())
        entries = []
        for e in act.schedule.entries:
            nxt = next_run(e, now=now)
            runs = self.runs(e.id, limit=1)
            entries.append({**e.to_wire(), "next_run": nxt.isoformat() if nxt else "",
                            "last_started_ms": self.last_started(e.id),
                            "last": runs[0] if runs else None})
        return {"bundle": {"bundle_id": act.bundle_id, "version": act.version},
                "timezone": act.schedule.timezone, "missions": sorted(act.missions),
                "entries": entries, "last_error": self.last_error,
                # 站点的钟有没有参照核对过。W00c2a 的 serve 没接参照(站点主机靠 NTP),这里如实报。
                "clock_checked": self._ref is not None}
