"""对桩、充电、出桩(W13,决策 25、45;规矩见 :mod:`d1max_contract.charging`)。

站点先派 ``goto`` 把狗带到桩前对准点,再派这一趟。步骤:

1. ``recharge_start``(HAL:厂家回充,或者以后自己认二维码对桩);**按电池确认充上了**
   (``charging`` 变真)—— 厂家回充从不报成功。``dock_timeout_s`` 内没充上:``recharge_stop``、失败
   (「没对上桩」)。
2. 充着:电量到 ``resume_pct`` 就 ``undock``;充着充着不充了(碰掉了、断电)超过 :data:`LOST_S`:
   出桩、失败(「充电断了」);总共充了 ``max_s`` 还没到:出桩、失败。
3. 出桩:``charging`` 变假、再稳 :data:`SETTLE_S` 才算出了桩,完成;``UNDOCK_TIMEOUT_S`` 出不来:失败
   (「出不了桩」,狗可能还在桩上 —— 站点报 P1 叫人)。

**被抢、被中止**(比如入侵派遣,决策 45):还在桩上就**先出桩**,出了才进终态 —— 不许在桩上就开走。
还在对桩、没充上过:停对桩就进终态,**不发出桩**(厂家的出桩不在桩上也会往后退)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from d1max_agent.tasks.base import Task
from d1max_contract.charging import UNDOCK_TIMEOUT_S, DockRequest
from d1max_contract.hal import RobotHAL
from d1max_contract.messages import TaskState

log = logging.getLogger(__name__)

#: 充着充着不充了,等这么久(秒)再判断开了。
LOST_S = 30.0
#: ``charging`` 变假之后再稳这么久(秒)才算出了桩。
SETTLE_S = 5.0


class DockTask(Task):
    def __init__(self, *, task_id: str, req: DockRequest, hal: RobotHAL,
                 now_ms: Callable[[], int], priority: int = 0) -> None:
        super().__init__(task_id=task_id, kind="dock", priority=priority)
        self.req = req
        self._hal = hal
        self._now = now_ms
        #: ``docking`` 对桩中、``charging`` 充着、``undocking`` 出桩中。
        self.phase = "docking"
        self._t0 = 0
        self._phase_ms = 0
        self._lost_ms: int | None = None
        self._free_ms: int | None = None
        #: 出桩之后的去向(终态, 原因);``None`` = 还没到那一步。
        self._then: tuple[TaskState, str] | None = None
        self.percent: float | None = None
        #: 充上过没有(上过桩)。没上过桩的不发出桩 —— 厂家的出桩不在桩上也会往后退。
        self._docked = False

    @property
    def aborting(self) -> bool:
        return self._then is not None and self._then[0] is not TaskState.DONE

    def _go(self, phase: str) -> None:
        self.phase, self._phase_ms = phase, self._now()

    async def start(self) -> None:
        self.state = TaskState.RUNNING
        self._t0 = self._now()
        self._go("docking")
        try:
            await self._hal.recharge_start()
        except Exception as exc:  # noqa: BLE001 - HAL 说不行:直接失败
            self._finish(TaskState.FAILED, f"回充起不来: {exc}")
            return
        log.info("开始对桩:%s", self.task_id)

    async def abort(self, reason: str) -> None:
        if self.done:
            return
        state = TaskState.PREEMPTED if reason == "preempted" else TaskState.ABORTED
        if self.phase == "docking":
            try:
                await self._hal.recharge_stop()
            except Exception:
                log.exception("停对桩没成")
        self._undock_then(state, reason)

    def _finish(self, state: TaskState, reason: str) -> None:
        self.state = state
        self.detail = {"reason": reason, **({"percent": round(self.percent, 1)}
                                            if self.percent is not None else {})}
        log.info("对桩、充电结束:%s(%s)", self.task_id, reason)

    def _undock_then(self, state: TaskState, reason: str) -> None:
        """先出桩,出了再进 ``state``。没上过桩:直接进(不发出桩)。"""
        if not self._docked:
            self._finish(state, reason)
            return
        if self._then is None:
            self._then = (state, reason)
            self._go("undocking")
            self._undock_sent = False

    async def step(self, dt_s: float) -> None:
        if self.state is not TaskState.RUNNING:
            return
        now = self._now()
        try:
            b = await self._hal.battery()
        except Exception:
            log.exception("读不了电池")
            return
        self.percent, charging = b.percent, bool(b.charging)
        if self.phase == "docking":
            if charging:
                log.info("充上了(%.0f%%)", b.percent)
                self._docked = True
                self._go("charging")
                return
            if now - self._phase_ms >= self.req.dock_timeout_s * 1000:
                try:
                    await self._hal.recharge_stop()
                except Exception:
                    log.exception("停对桩没成")
                self._undock_then(TaskState.FAILED,
                                  f"没对上桩({self.req.dock_timeout_s} 秒没充上电)")
            return
        if self.phase == "charging":
            if b.percent >= self.req.resume_pct:
                self._undock_then(TaskState.DONE, f"充到 {b.percent:.0f}%")
            elif not charging:
                self._lost_ms = self._lost_ms or now
                if now - self._lost_ms >= LOST_S * 1000:
                    self._undock_then(TaskState.FAILED, "充电断了(碰掉了、桩断电?)")
            else:
                self._lost_ms = None
            if self.phase == "charging" and now - self._t0 >= self.req.max_s * 1000:
                self._undock_then(TaskState.FAILED, f"充了 {self.req.max_s} 秒还没到")
            return
        # undocking
        if not getattr(self, "_undock_sent", False):
            self._undock_sent = True
            try:
                await self._hal.undock()
            except Exception:
                log.exception("出桩命令没发出去(下一拍再发)")
                self._undock_sent = False
                return
        if charging:
            self._free_ms = None
        else:
            self._free_ms = self._free_ms or now
            if now - self._free_ms >= SETTLE_S * 1000:
                assert self._then is not None
                self._finish(*self._then)
                return
        if now - self._phase_ms >= UNDOCK_TIMEOUT_S * 1000:
            self._finish(TaskState.FAILED, f"出不了桩({UNDOCK_TIMEOUT_S} 秒还在充),"
                         + (self._then[1] if self._then else ""))
