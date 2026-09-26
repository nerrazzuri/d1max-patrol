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

import dataclasses
import json
import logging
import math
import os
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
from d1max_contract.teleop import (
    TELEOP_PRIORITY,
    TeleopFrame,
    parse_teleop_grant,
    parse_teleop_lease,
)
from d1max_contract.topics import Topics
from d1max_contract.video import parse_video_payload

log = logging.getLogger(__name__)

#: 地图命令(W00c5d 第二部分)与发布命令(第三部分):不是任务,进幂等记录,交给 ``map_hook``。
MAP_KINDS = frozenset({"map_activate", "mapping", "map_build",
                       "release_install", "release_activate", "release_rollback",
                       "outbox_retry"})

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
        #: 按需推流(W00c5b,``VideoPusher``)。``None`` = 这台代理不推视频,
        #: ``video`` 命令回 unsupported。
        self.video: Any = None
        #: ``halt`` 时当场停车(W00c5c)。运行时接到 ``hal.stop``。
        self.halt_hook: Callable[[], Any] | None = None
        #: 任务命令的准入(W00c5d):返回非空 = 拒绝原因(发件箱满了回 ``storage_full``)。
        self.admit_hook: Callable[[Command], str] | None = None
        #: ``supervise``(W00c6i,监护租约的续与放):运行时按单调钟记;回空串 = 收下,否则拒收的理由。
        self.supervise_hook: Callable[[Command], str] | None = None
        #: ``abort_kinds`` 请求过中止的当前任务(不重复请求)。
        self._abort_requested: set[str] = set()
        #: 叫停栅栏(W00c6a 内审 B1):抢先那一路立起来、排队那一路处理完这条叫停撤掉。立着的时候不起新
        #: 任务、运动类命令一律拒收 —— 不然叫停之后,排队里已收下的、排在锁前面的,照样起跑。
        self._fence: str | None = None
        #: 地图命令(W00c5d 第二部分:``map_activate``/``mapping``/``map_build``,都不是任务):
        #: 返回非空 = 拒绝原因;空串 = 收下(后台做,做完发事件)。
        self.map_hook: Callable[[Command], Any] | None = None

    # ------------------------------------------------------------ 代次落盘

    def _load_epoch(self) -> int:
        try:
            return int(json.loads(self._state_path.read_text(encoding="utf-8"))["control_epoch"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def _save_epoch(self) -> None:
        """原子写:写一半掉电会让 ``_load_epoch`` 读回 0,代次倒退,旧代次命令重新被接受。"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"control_epoch": self.control_epoch}), encoding="utf-8")
        os.replace(tmp, self._state_path)

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

        if cmd.kind == "supervise":
            # 监护心跳每秒一条(W00c6i):不进幂等记录(同 teleop_lease),过期的也不记。
            if cmd.expires_at <= now:
                return Ack(cmd.command_id, cmd.task_id, AckResult.EXPIRED)
            if self.supervise_hook is None:
                return self._rej(cmd, "unsupported")
            reason = self.supervise_hook(cmd)
            return (self._rej(cmd, reason) if reason
                    else Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED))
        if cmd.expires_at <= now:
            return self._finish(Ack(cmd.command_id, cmd.task_id, AckResult.EXPIRED))

        # **优先级由代理定死**(W00c5c,决策 7 追加条件:人工遥控优先于所有自动任务):遥控一律
        # TELEOP_PRIORITY,别的命令最多到它下面一档 —— 站点出错送来一条优先级 500 的 goto,
        # 也抢不走遥控。
        prio = TELEOP_PRIORITY if cmd.kind == "teleop" else min(cmd.priority, TELEOP_PRIORITY - 1)
        if prio != cmd.priority:
            cmd = dataclasses.replace(cmd, priority=prio)

        if cmd.kind == "teleop_lease":
            # 续租每秒一条:不进幂等记录(同 video)。
            return self._handle_teleop_lease(cmd)
        seen = self.idem.lookup(cmd.command_id)
        if seen is not None:
            return Ack(cmd.command_id, cmd.task_id, AckResult.DUPLICATE, original=seen.to_wire())
        await self._admit(cmd)

        if cmd.kind == "abort":
            return self._finish(await self._handle_abort(cmd))
        if cmd.kind == "halt":
            return self._finish(await self._handle_halt(cmd))
        if cmd.kind == "video":
            # **不进幂等记录**:续期每 ttl/2 一条,记下来一路一天几十万行、代理起来还要全量重放。
            # 重投的旧 video 命令至多把推流续到它自己的有效期,无害。
            return self._handle_video(cmd)
        if cmd.kind in MAP_KINDS:
            if self.map_hook is None:
                return self._finish(self._rej(cmd, "unsupported"))
            reason = await self.map_hook(cmd)
            return self._finish(self._rej(cmd, reason) if reason
                                else Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED))
        if self._fence is not None and cmd.kind in _MOTION_KINDS:
            return self._finish(self._rej(cmd, "halting"))
        if cmd.kind not in self.supported:
            return self._finish(self._rej(cmd, "unsupported"))
        try:
            resources_for(cmd.kind)
        except KeyError:
            return self._finish(self._rej(cmd, "unsupported"))

        reason = self._check_payload(cmd)
        if reason:
            return self._finish(self._rej(cmd, reason))
        reason = self.admit_hook(cmd) if self.admit_hook is not None else ""
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
        task.expires_at = cmd.expires_at
        waited = False
        for b in blockers:
            if b is not None and not b.done:
                await b.abort("preempted")
                waited = True
        if waited:
            # 抢占要等(W00c5d 内部评审):等的时候可能开始换图、甚至换完了。醒来再查一遍地图版本
            # 与准入,不然这一条按老地图排进去。
            reason = self._check_payload(cmd) or \
                (self.admit_hook(cmd) if self.admit_hook is not None else "")
            if reason:
                return self._finish(self._rej(cmd, reason))
        # pending 按优先级排(高的在前,同级按到达顺序)。
        self.pending.append(task)
        self.pending.sort(key=lambda t: -t.priority)
        return self._finish(Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED))

    async def _admit(self, cmd: Command) -> None:
        """幂等查询之后、真正处理之前的 await 点。W00 里什么都不做;W00b 接引擎后这里
        就是真 I/O(问引擎状态、查地图)。**它存在是为了让串行化被测到**:两条重复命令
        几乎同时到时,没有 runtime 那把锁,两条都会先通过上面的幂等查询。"""

    def _check_payload(self, cmd: Command) -> str:
        if cmd.kind == "patrol":
            return self._check_patrol(cmd)
        if cmd.kind == "teleop":
            try:
                parse_teleop_grant(cmd.payload)
            except ContractError as exc:
                return f"payload: {exc}"
            return ""
        if cmd.kind != "goto":
            return ""
        try:
            target = MapPose.from_wire(cmd.payload.get("target"))
        except ContractError as exc:
            return f"payload: target 不成形: {exc}"
        speed = cmd.payload.get("max_speed_mps", None)
        if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float))
                                  or not math.isfinite(speed) or speed <= 0):
            # isfinite:json 认 NaN/Infinity,而 NaN <= 0 为假。
            return "payload: max_speed_mps 要是正的有限数"
        if self.loaded_map is None or (target.map_id, target.map_version) != self.loaded_map:
            return "map_mismatch"
        return ""

    def _check_patrol(self, cmd: Command) -> str:
        """``{"mission": <任务定义>, "map_version": str}``(W00c2a 设计决定二 A)。"""
        from d1max_contract.mission import MissionError, parse_mission
        try:
            mission = parse_mission(cmd.payload.get("mission"))
        except (MissionError, ValueError) as exc:
            return f"payload: mission 不成形: {exc}"
        if mission.route_source != "inline":
            return "payload: 只支持 inline 路线(厂商路径归 adapter-d1max)"
        version = cmd.payload.get("map_version")
        if not isinstance(version, str) or not version:
            return "payload: 要 map_version"
        if self.loaded_map is None or (mission.map_id, version) != self.loaded_map:
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

    def _teleop_task(self, epoch: int | None = None) -> Any:
        """当前或排队中的遥控任务(``epoch`` 给了就要代次对得上)。"""
        for t in ([self.current] if self.current is not None else []) + self.pending:
            if t.kind == "teleop" and not t.done and (epoch is None
                                                      or getattr(t, "lease_epoch", None) == epoch):
                return t
        return None

    def _handle_teleop_lease(self, cmd: Command) -> Ack:
        """续租、放租(W00c5c)。不是任务;找不到这一代的遥控就拒。"""
        try:
            lease = parse_teleop_lease(cmd.payload)
        except ContractError as exc:
            return self._rej(cmd, f"payload: {exc}")
        t = self._teleop_task(lease.lease_epoch)
        if t is None:
            return self._rej(cmd, "no_such_lease")
        if lease.action == "renew":
            t.renew(lease.lease_ttl_ms)
        else:
            t.release()
        return Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED)

    def on_teleop_frame(self, frame: TeleopFrame, *, rx_ms: int) -> str:
        """一帧遥控(专用主题,QoS 0)。只交给**当前**那一趟遥控;收下返回空串,否则原因(不回执)。"""
        cur = self.current
        if cur is None or cur.kind != "teleop" or cur.done:
            return "no_teleop"
        return cur.on_frame(frame, rx_ms=rx_ms)

    async def _handle_halt(self, cmd: Command) -> Ack:
        """停车(W00c5c):**不走遥控连接**。当场让 HAL 停,中止当前与排队中的一切任务。
        停车本身失败的话任务照样中止,但回执**拒收**(``stop_failed``):不许让站点、手机说「停了」
        而狗其实没停(W00c5e 内部评审)。"""
        # 先中止、再停车(W00c6a 内审 B2):见 ``abort_for_halt``。
        await self.abort_for_halt()
        stop_failed = ""
        if self.halt_hook is not None:
            try:
                r = self.halt_hook()
                if hasattr(r, "__await__"):
                    await r
            except Exception as exc:
                log.exception("halt 时停车失败,任务照样中止")
                stop_failed = f"stop_failed: {type(exc).__name__}: {exc}"[:120]
        if stop_failed:
            return self._rej(cmd, stop_failed)
        return Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED)

    def _handle_video(self, cmd: Command) -> Ack:
        """``video``:不是任务,不占资源、不进调度。载荷由契约校验,推流交给 ``VideoPusher``。"""
        if self.video is None:
            return self._rej(cmd, "unsupported")
        try:
            req = parse_video_payload(cmd.payload)
        except ContractError as exc:
            return self._rej(cmd, f"payload: {exc}")
        why = self.video.request(req)
        if why:
            return self._rej(cmd, why)
        return Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED)

    def fence(self, command_id: str) -> None:
        """立叫停栅栏(抢先的叫停)。"""
        self._fence = command_id

    def unfence(self, command_id: str) -> None:
        """撤栅栏:排队那一路处理完**这一条**叫停(不管收下、过期还是重复)。"""
        if self._fence == command_id:
            self._fence = None

    async def abort_for_halt(self) -> None:
        """叫停要中止的:排队的直接进终态、发事件;当前的请求中止。**先于停车**(W00c6a 内审 B2):
        中止请求先进引擎的队列,停车引出的导航 Cancelled 排在它后面 —— 引擎不会把 Cancelled 当
        「这个点没到」去重发这个点(真 HAL 停车要等回执,这段时间足够引擎重发)。"""
        for t in list(self.pending):
            self.pending.remove(t)
            t.state = TaskState.ABORTED
            t.detail = {"reason": "halt"}
            self.finished.append(t)
            self.events.emit("task_aborted", {"task_id": t.task_id, "reason": "halt"})
        if self.current is not None and not self.current.done:
            await self.current.abort("halt")

    async def abort_kinds(self, kinds: frozenset[str], reason: str) -> int:
        """中止当前与排队中的这几种任务(W00c6i:监护过期时中止 goto/巡检)。排队的直接进终态、发事件;
        当前的请求中止(任务在随后的拍里等停车确认再进终态)。同一个当前任务只请求一次。
        返回动了几个。"""
        n = 0
        for t in [t for t in self.pending if t.kind in kinds]:
            self.pending.remove(t)
            t.state = TaskState.ABORTED
            t.detail = {"reason": reason}
            self.finished.append(t)
            self.events.emit("task_aborted", {"task_id": t.task_id, "reason": reason})
            n += 1
        cur = self.current
        # 已经在中止的(叫停、抢占)不再请求一次:不改写它的中止原因(W00c6i 内审)。
        if cur is not None and not cur.done and cur.kind in kinds and not cur.aborting \
                and cur.task_id not in self._abort_requested:
            self._abort_requested.add(cur.task_id)
            await cur.abort(reason)
            n += 1
        return n

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
                self._abort_requested.discard(cur.task_id)
                self.events.emit(_event_for(cur.state),
                                 {"task_id": cur.task_id, **cur.detail})
        while self.current is None and self.pending and self._fence is None:
            nxt = self.pending[0]
            if self.ledger.conflicts(nxt.kind):
                break
            self.pending.pop(0)
            if nxt.expires_at is not None and nxt.expires_at <= self._now():
                # 在 pending 里等到过期了:不起跑。跟「不执行十分钟前的出动」同一条规矩。
                nxt.state = TaskState.FAILED
                nxt.detail = {"reason": "expired_before_start"}
                self.finished.append(nxt)
                self.events.emit("task_failed", {"task_id": nxt.task_id, **nxt.detail})
                continue
            self.ledger.acquire(nxt.task_id, nxt.kind)
            self.current = nxt
            await nxt.start()


#: 叫停栅栏立着的时候拒收的命令(会让狗动的任务)。
_MOTION_KINDS = frozenset({"goto", "patrol", "teleop"})


def _event_for(state: TaskState) -> str:
    return {TaskState.DONE: "task_done", TaskState.FAILED: "task_failed",
            TaskState.ABORTED: "task_aborted", TaskState.PREEMPTED: "task_preempted"}[state]
