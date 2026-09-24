"""命令处理器:校验链 → 回执 → 调度到任务(设计 §2、§3;总设计 §3.2、§4.2)。

校验顺序**固定**,测试逐条钉着:
schema 主版本 → 认证(主题里的 site/robot 与注册一致,注册在有效期)→ control_epoch
(小的拒 ``stale_epoch``,大的采纳并落盘)→ ``expires_at``(过了回 ``expired``)→
``command_id`` 见过(回 ``duplicate`` + 原结果,不执行)→ 能力(``supported_tasks``)→
载荷(goto 的地图版本要与已加载一致)→ 前置条件 → 资源(占着且优先级不高 → ``busy``;
优先级高 → 抢占)→ ``accepted``。

**每条回执都进幂等存储**,包括拒绝的。

抢占(§4.2):新任务先记成 pending;叫旧任务 ``abort("preempted")``;每拍看旧任务进了
终态、资源释放了,**再**让新任务拿 motion 起跑。绝不先给资源再等旧的停。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_agent.events import EventBook
from d1max_agent.idempotency import IdempotencyStore
from d1max_agent.resources import ResourceLedger
from d1max_agent.tasks.base import Task
from d1max_contract.errors import ContractError, SchemaMismatch
from d1max_contract.messages import (
    Ack,
    AckResult,
    Command,
    MapPose,
    TaskState,
    TaskSummary,
)
from d1max_contract.registration import Registration
from d1max_contract.resources import resources_for
from d1max_contract.topics import Topics

log = logging.getLogger(__name__)

TaskFactory = Callable[[Command], Task]


class CommandProcessor:
    def __init__(self, *, registration: Registration, now_ms: Callable[[], int],
                 idem: IdempotencyStore, events: EventBook, ledger: ResourceLedger,
                 supported_tasks: set[str], loaded_map: tuple[str, str] | None,
                 state_path: Path, task_factory: TaskFactory) -> None:
        self.registration = registration
        self._now = now_ms
        self.idem = idem
        self.events = events
        self.ledger = ledger
        self.supported = set(supported_tasks)
        self.loaded_map = loaded_map
        self._state_path = Path(state_path)
        self._factory = task_factory
        self.control_epoch = self._load_epoch()
        #: 当前占着资源在跑(或在停)的任务。
        self.current: Task | None = None
        #: 已接受、等资源的任务(抢占时的新任务)。
        self.pending: list[Task] = []
        self.finished: list[Task] = []

    # ------------------------------------------------------------ 代次落盘

    def _load_epoch(self) -> int:
        try:
            return int(json.loads(self._state_path.read_text(encoding="utf-8"))["control_epoch"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def _save_epoch(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"control_epoch": self.control_epoch}),
                                    encoding="utf-8")

    # ------------------------------------------------------------ 状态摘要

    def task_summary(self) -> TaskSummary | None:
        t = self.current or (self.pending[0] if self.pending else None)
        if t is None:
            return None
        return TaskSummary(task_id=t.task_id, kind=t.kind, state=t.state)

    def _find_task(self, task_id: str) -> Task | None:
        for t in [self.current, *self.pending, *reversed(self.finished)]:
            if t is not None and t.task_id == task_id:
                return t
        return None

    # ------------------------------------------------------------ 校验链

    async def handle(self, wire: Any, topic: str) -> Ack:
        """一条命令进来,一条回执出去。永远回得出回执 —— 不成形的命令也回 rejected。"""
        cid = wire.get("command_id") if isinstance(wire, dict) else None
        tid = wire.get("task_id") if isinstance(wire, dict) else None
        cid = cid if isinstance(cid, str) and cid else "?"
        tid = tid if isinstance(tid, str) and tid else "?"
        try:
            cmd = Command.from_wire(wire)
        except SchemaMismatch as exc:
            return self._finish(Ack(cid, tid, AckResult.REJECTED, reason=f"schema: {exc}"))
        except ContractError as exc:
            return self._finish(Ack(cid, tid, AckResult.REJECTED, reason=f"malformed: {exc}"))

        try:
            site_id, robot_id, kind = Topics.parse(topic)
        except ContractError:
            return self._finish(self._rej(cmd, "auth"))
        now = self._now()
        if kind != "cmd" or not self.registration.matches(site_id=site_id, robot_id=robot_id) \
                or not self.registration.valid_at(now):
            return self._finish(self._rej(cmd, "auth"))

        if cmd.control_epoch < self.control_epoch:
            return self._finish(self._rej(cmd, "stale_epoch"))
        if cmd.control_epoch > self.control_epoch:
            log.info("站点代次 %d → %d,旧代次的命令从此拒", self.control_epoch, cmd.control_epoch)
            self.control_epoch = cmd.control_epoch
            self._save_epoch()

        if cmd.expires_at <= now:
            return self._finish(Ack(cmd.command_id, cmd.task_id, AckResult.EXPIRED))

        seen = self.idem.lookup(cmd.command_id)
        if seen is not None:
            return Ack(cmd.command_id, cmd.task_id, AckResult.DUPLICATE, original=seen.to_wire())

        if cmd.kind == "abort":
            return self._finish(await self._handle_abort(cmd))
        if cmd.kind not in self.supported:
            return self._finish(self._rej(cmd, "unsupported"))
        try:
            resources_for(cmd.kind)
        except KeyError:
            return self._finish(self._rej(cmd, "unsupported"))

        reason = self._check_payload(cmd)
        if reason:
            return self._finish(self._rej(cmd, reason))

        if cmd.precondition is not None and cmd.precondition.expect_task_state is not None:
            t = self._find_task(cmd.task_id)
            if t is None or t.state is not cmd.precondition.expect_task_state:
                return self._finish(self._rej(cmd, "precondition"))

        conflicts = self.ledger.conflicts(cmd.kind)
        blockers = [self._find_task(t) for t in conflicts]
        if any(b is not None and b.priority >= cmd.priority for b in blockers):
            return self._finish(self._rej(cmd, "busy"))

        task = self._factory(cmd)
        for b in blockers:
            if b is not None and not b.done:
                await b.abort("preempted")
        self.pending.append(task)
        return self._finish(Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED))

    def _check_payload(self, cmd: Command) -> str:
        if cmd.kind != "goto":
            return ""
        try:
            target = MapPose.from_wire(cmd.payload.get("target"))
        except ContractError as exc:
            return f"payload: target 不成形: {exc}"
        speed = cmd.payload.get("max_speed_mps", None)
        if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float))
                                  or speed <= 0):
            return "payload: max_speed_mps 要是正数"
        if self.loaded_map is None or (target.map_id, target.map_version) != self.loaded_map:
            return "map_mismatch"
        return ""

    async def _handle_abort(self, cmd: Command) -> Ack:
        t = self._find_task(cmd.task_id)
        if t is None:
            return self._rej(cmd, "no_such_task")
        if cmd.precondition is not None and cmd.precondition.expect_task_state is not None \
                and t.state is not cmd.precondition.expect_task_state:
            return self._rej(cmd, "precondition")
        if t.done:
            return self._rej(cmd, "already_finished")
        reason = cmd.payload.get("reason") if isinstance(cmd.payload.get("reason"), str) else ""
        if t in self.pending:                         # 还没起跑的,直接撤
            self.pending.remove(t)
            t.state = TaskState.ABORTED
            self.finished.append(t)
            self.events.emit("task_aborted", {"task_id": t.task_id, "reason": reason})
            return Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED)
        await t.abort(reason or "abort")
        return Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED)

    def _rej(self, cmd: Command, reason: str) -> Ack:
        return Ack(cmd.command_id, cmd.task_id, AckResult.REJECTED, reason=reason)

    def _finish(self, ack: Ack) -> Ack:
        if ack.command_id != "?":
            self.idem.remember(ack)
        return ack

    # ------------------------------------------------------------ 调度

    async def step(self, dt_s: float) -> None:
        """每拍:推进当前任务;它进了终态就释放资源、发事件;然后让 pending 里第一个
        能拿到资源的起跑。抢占的顺序就在这两句的先后里。"""
        cur = self.current
        if cur is not None:
            await cur.step(dt_s)
            if cur.done:
                self.ledger.release(cur.task_id)
                self.finished.append(cur)
                self.current = None
                self.events.emit(_event_for(cur.state),
                                 {"task_id": cur.task_id, **cur.detail})
        if self.current is None and self.pending:
            nxt = self.pending[0]
            if not self.ledger.conflicts(nxt.kind):
                self.pending.pop(0)
                self.ledger.acquire(nxt.task_id, nxt.kind)
                self.current = nxt
                await nxt.start()


def _event_for(state: TaskState) -> str:
    return {TaskState.DONE: "task_done", TaskState.FAILED: "task_failed",
            TaskState.ABORTED: "task_aborted", TaskState.PREEMPTED: "task_preempted"}[state]
