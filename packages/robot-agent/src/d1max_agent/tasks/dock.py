"""对桩、充电、出桩(W13,决策 25、45;规矩见 :mod:`d1max_contract.charging`)。

站点先派 ``goto`` 把狗带到桩前对准点,再派这一趟。**在不在桩上一律按此刻实测**(电池 ``charging``、
HAL 的 ``recharge_status``),不信任务自己记的(W13 外审 1)。步骤:

1. **对桩**:``recharge_start``;实测在充了(厂家回充从不报成功)→ 充着;``dock_timeout_s`` 没充上 →
   停对桩、确认停住了才失败(「没对上桩」)。
2. **充着**:电量到 ``resume_pct`` 就出桩;不充了超过 :data:`LOST_S`(碰掉了、断电)→ 出桩、失败;总共
   充了 ``max_s`` → 出桩、失败。
3. **出桩**:``undock``;实测不充了、也不在桩上,再稳 :data:`SETTLE_S` 才算出了桩,才进终态。
4. **停对桩**(对桩中被抢、被中止、超时):``recharge_stop``,实测没在对桩也没上桩、稳 :data:`SETTLE_S`
   才进终态;停的时候已经上桩了(刚充上)→ 改走出桩。没上过桩不发出桩(厂家的出桩不在桩上也会往后退)。

**被抢、被中止**:按实测走 3 或 4,确认安全了才交出运动资源。

**说不清的时候不交**(W13 外审 1、2、3):出桩、停对桩到了 :data:`SAFE_TIMEOUT_S` 还确认不了
(命令发不出去、电池读不到、狗还在充),这一趟照样结束(失败),但先叫 ``on_hazard`` 挂上「桩上危险」:
代理的运动闸从此不收、不起跑任何会动的任务,直到实测确认离了桩(代理每拍看,见 ``runtime``),站点报 P1。
**期限检查不依赖 HAL 调用成没成**:每拍先看期限,再读电池、发命令。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from d1max_agent.tasks.base import Task
from d1max_contract.charging import UNDOCK_TIMEOUT_S, DockRequest
from d1max_contract.hal import HalUnsupported, RobotHAL
from d1max_contract.messages import TaskState

log = logging.getLogger(__name__)

#: 充着充着不充了,等这么久(秒)再判断开了。
LOST_S = 30.0
#: 不充了、不在桩上之后再稳这么久(秒)才算安全离桩。
SETTLE_S = 5.0
#: 出桩、停对桩最多等这么久(秒)确认安全;确认不了就挂「桩上危险」。
SAFE_TIMEOUT_S = float(UNDOCK_TIMEOUT_S)
_ON_DOCK = ("docked", "docking", "charging")


class DockTask(Task):
    def __init__(self, *, task_id: str, req: DockRequest, hal: RobotHAL,
                 now_ms: Callable[[], int], priority: int = 0,
                 on_hazard: Callable[[str], None] | None = None) -> None:
        super().__init__(task_id=task_id, kind="dock", priority=priority)
        self.req = req
        self._hal = hal
        self._now = now_ms
        self._on_hazard = on_hazard
        #: ``docking`` 对桩中、``charging`` 充着、``undocking`` 出桩中、``stopping`` 停对桩中。
        self.phase = "docking"
        self._t0 = 0
        self._phase_ms = 0
        self._lost_ms: int | None = None
        self._safe_ms: int | None = None
        #: 出桩 / 停对桩之后的去向(终态, 原因)。
        self._then: tuple[TaskState, str] | None = None
        self.percent: float | None = None
        self._cmd_ok = False                         # 出桩 / 停对桩的命令发成了没有

    @property
    def aborting(self) -> bool:
        return self._then is not None and self._then[0] is not TaskState.DONE

    def _go(self, phase: str) -> None:
        self.phase, self._phase_ms = phase, self._now()
        self._safe_ms, self._cmd_ok = None, False

    async def start(self) -> None:
        self.state = TaskState.RUNNING
        self._t0 = self._now()
        self._go("docking")
        try:
            await self._hal.recharge_start()
        except HalUnsupported as exc:
            self._finish(TaskState.FAILED, f"回充起不来: {exc}")   # 根本不会对桩:没东西要停
            return
        except Exception as exc:  # noqa: BLE001 - 发没发出去说不清:停对桩、确认停住再失败
            self._leave(TaskState.FAILED, f"回充起不来: {exc}", undock=False)
            return
        log.info("开始对桩:%s", self.task_id)

    async def abort(self, reason: str) -> None:
        if self.done or self._then is not None:
            return
        state = TaskState.PREEMPTED if reason == "preempted" else TaskState.ABORTED
        # 按此刻实测决定出桩还是停对桩(刚充上、任务还没走下一拍,内存里还以为在对桩)
        charging, _, _ = await self._sense()
        self._leave(state, reason, undock=bool(charging) or self.phase == "charging")

    def _leave(self, state: TaskState, reason: str, *, undock: bool) -> None:
        """要走了:出桩(在桩上)或停对桩(还在对),确认安全了才进 ``state``。"""
        if self._then is None:
            self._then = (state, reason)
        self._go("undocking" if undock else "stopping")

    def _finish(self, state: TaskState, reason: str) -> None:
        self.state = state
        self.detail = {"reason": reason, **({"percent": round(self.percent, 1)}
                                            if self.percent is not None else {})}
        log.info("对桩、充电结束:%s(%s)", self.task_id, reason)

    async def _sense(self) -> tuple[bool | None, bool | None, float | None]:
        """此刻 ``(在充, 在桩上或正在对桩, 电量)``;读不到是 ``None``。「在充」决定充上了没有,
        「在桩上或正在对桩」决定能不能开走。"""
        try:
            b = await self._hal.battery()
        except Exception:
            log.exception("读不了电池")
            return None, None, None
        charging = bool(b.charging)
        busy = charging
        try:
            busy = charging or str(await self._hal.recharge_status()) in _ON_DOCK
        except Exception:  # noqa: BLE001 - 状态读不到:只按电池
            pass
        return charging, busy, b.percent

    async def step(self, dt_s: float) -> None:
        if self.state is not TaskState.RUNNING:
            return
        now = self._now()
        # 期限先看(W13 外审 3):HAL 读不到、命令发不出去,期限照样到
        if self.phase == "docking" and now - self._phase_ms >= self.req.dock_timeout_s * 1000:
            self._leave(TaskState.FAILED, f"没对上桩({self.req.dock_timeout_s} 秒没充上电)",
                        undock=False)
        elif self.phase == "charging" and now - self._t0 >= self.req.max_s * 1000:
            self._leave(TaskState.FAILED, f"充了 {self.req.max_s} 秒还没到", undock=True)
        elif self.phase in ("undocking", "stopping") and \
                now - self._phase_ms >= SAFE_TIMEOUT_S * 1000:
            why = ("出不了桩" if self.phase == "undocking" else "停不住对桩") + \
                f"({SAFE_TIMEOUT_S:.0f} 秒确认不了离了桩)"
            if self._on_hazard is not None:
                self._on_hazard(why)                  # 运动闸挂上:离桩确认之前不许动
            log.error("%s:%s,运动锁住", self.task_id, why)
            self._finish(TaskState.FAILED, why + ";" + (self._then[1] if self._then else ""))
            return
        charging, busy, pct = await self._sense()
        if pct is not None:
            self.percent = pct
        if self.phase == "docking":
            if charging:
                log.info("充上了(%.0f%%)", pct or 0)
                self._go("charging")
            return
        if self.phase == "charging":
            if pct is not None and pct >= self.req.resume_pct:
                self._leave(TaskState.DONE, f"充到 {pct:.0f}%", undock=True)
            elif charging is False:
                self._lost_ms = self._lost_ms or now
                if now - self._lost_ms >= LOST_S * 1000:
                    self._leave(TaskState.FAILED, "充电断了(碰掉了、桩断电?)", undock=True)
            elif charging:
                self._lost_ms = None
            return
        # undocking / stopping:实测安全了才进终态
        if self.phase == "stopping" and charging:
            # 停的时候已经上桩了(刚充上,或者停对桩的命令没成):改走出桩
            log.info("停对桩时已经上桩了:改出桩")
            then = self._then
            self._go("undocking")
            self._then = then
            return
        if not self._cmd_ok:                          # 发命令,没成下一拍再发
            try:
                if self.phase == "undocking":
                    await self._hal.undock()
                else:
                    await self._hal.recharge_stop()
                self._cmd_ok = True
            except Exception:
                log.exception("%s 命令没发出去(下一拍再发)",
                              "出桩" if self.phase == "undocking" else "停对桩")
                return
        if busy is False:
            self._safe_ms = self._safe_ms or now
            if now - self._safe_ms >= SETTLE_S * 1000:
                assert self._then is not None
                self._finish(*self._then)
        else:
            self._safe_ms = None                      # 还在桩上,或者读不到:不算
