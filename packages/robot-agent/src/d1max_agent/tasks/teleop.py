"""遥控任务(W00c5c,受决策 7 约束)。站点转发经过认证、持有租约的人类实时操作输入;规矩在这里执行。

- **优先级最高**(``TELEOP_PRIORITY`` = 100,高于事件 80):授予租约就抢占自动任务。抢占走调度器现成
  那条路 —— 被抢的任务先停稳才进终态,这一趟才起跑(「先停车再移交」)。遥控期间来的自动任务回 busy。
- **按帧走**:每帧先过 :class:`~d1max_agent.teleop_frames.FrameGate`(代次、序号、在途),收下的那一帧
  在它自己的有效期(默认 300 ms)里按 ``set_velocity`` 执行;**帧不来了就停**(这边一拍一看,HAL 与
  旁路进程两层到期也自停)。
- **限速**:HAL 能力的一半(决策 7 追加条件),站点也夹一次。
- **人眼里的「往前」**(W09i):HAL 是机身系(x 朝狗头);遥控的「往前」朝**会话开始时**被选作头的那一头
  —— 狗尾为前时往狗尾那头走(跟 W09i 之前一样:那时 HAL 直接用 SDK 的「往前」)。
- **一次会话里头尾不许变**(W11/W11a/W09i 外审阻断):有人拿厂家遥控器、App 调了头(包括变成「不知道」),
  有效期里的同一帧下一拍就成了反方向。每拍都核:变了就丢掉手上的帧、停车、结束(``head_changed``),
  之后来的帧一律不执行;要接着遥控得重新拿租约(站点续租被拒两次就关掉这次会话)。
- **没画面不动**:``video_live()`` 为假(狗上一路都没在推)就不下速度、在动就停 —— 站点那头还有一道。
- **结束**(放租、halt、租约到期、断线、被中止):先停车、等停稳(最多 ``STOP_CONFIRM_TIMEOUT_S``),
  再进终态;结束中的帧一律不执行。断线 = 结束,**不续**。
- **硬禁区**(决策 65):给了 ``zone_guard``(:class:`~d1max_agent.zone_guard.TeleopZoneGuard`)就每拍
  先过它 —— 会把机身(加定位 σ、刹车距离)带进硬禁区的那一下截掉(先减速、再只许转、再零速),往外开、
  平着开照发;定位不可信时全程限速。普通禁行区、没有硬禁区照旧不管。为什么截了随续租回执给站点
  (:meth:`TeleopTask.zone_note`),站点转给手机。
- **横移**(2026-10-30,左杆左右):HAL 报 ``lateral`` 才认 ``vy``,限速同样是 HAL 的一半;前后和横移
  同时给了只走大的那一个(真机遥控器推 45° 也只走一个轴);狗尾为前时左右也反。有硬禁区时横移另过守卫
  (:meth:`~d1max_agent.zone_guard.TeleopZoneGuard.lateral`):位置不可信、扫过去会更靠近硬禁区就不横移。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from d1max_agent.events import EventBook
from d1max_agent.tasks.base import Task
from d1max_agent.teleop_frames import FrameGate
from d1max_contract.hal import RobotHAL, VelocityCommand
from d1max_contract.messages import TaskState
from d1max_contract.teleop import TELEOP_PRIORITY, TeleopFrame

log = logging.getLogger(__name__)

#: 结束时最多等这么久确认停稳(秒);等不到也进终态并记下来(同 goto 的兜底)。
STOP_CONFIRM_TIMEOUT_S = 5.0
#: 速度命令有效期的下限(毫秒),跟 HAL/旁路进程的下限一致。
_MIN_TTL_MS = 50


class TeleopTask(Task):
    def __init__(self, *, task_id: str, lease_epoch: int, operator: str, lease_ttl_ms: int,
                 hal: RobotHAL, now_ms: Callable[[], int], video_live: Callable[[], bool],
                 events: EventBook, priority: int = TELEOP_PRIORITY,
                 zone_guard: Any = None) -> None:
        super().__init__(task_id=task_id, kind="teleop", priority=priority)
        self.lease_epoch = lease_epoch
        self.operator = operator
        self.gate = FrameGate(lease_epoch=lease_epoch)
        self._hal = hal
        self._now = now_ms
        self._video_live = video_live
        self._events = events
        self._lease_ttl = lease_ttl_ms
        caps = hal.hal_capabilities()
        self._vmax, self._wmax = caps.max_vx / 2.0, caps.max_wz / 2.0
        self._vymax = caps.max_vy / 2.0 if caps.lateral else 0.0
        self._deadband = caps.deadband_vx
        self._until: int | None = None
        #: 最近收下的那一帧:(vx, wz, 有效到哪一刻(狗钟), 帧的有效期, vy)。
        self._cmd: tuple[float, float, int, int, float] | None = None
        self._moving = False
        self._vseq = 0
        #: 结束的去向 (终态, 原因);None = 还在遥控。
        self._ending: tuple[TaskState, str] | None = None
        self._stop_sent = False
        self._stop_wait_s = 0.0
        #: 会话开始时的头尾方向(``head`` / ``tail`` / ``unknown``:老旁路进程不报,
        #: 照 SDK 的「往前」)。
        self._head = "unknown"
        #: 硬禁区(决策 65);``None`` = 不守(没载图、老测试)。
        self._guard = zone_guard
        self._guard_state = "ok"

    async def _read_head(self) -> str:
        try:
            return str(getattr(await self._hal.health(), "head", "unknown"))
        except Exception:
            log.exception("遥控读不到头尾方向(按不知道算)")
            return "unknown"

    async def start(self) -> None:
        self.state = TaskState.RUNNING
        self._head = await self._read_head()
        self._until = self._now() + self._lease_ttl
        # 操作者是谁只记在站点(审计);狗上的日志不留人名(决策 8)。
        log.info("遥控开始:%s 代次 %d", self.task_id, self.lease_epoch)

    # ------------------------------------------------------------ 进来的东西

    def on_frame(self, frame: TeleopFrame, *, rx_ms: int) -> str:
        """一帧遥控。收下返回空串,丢掉返回原因(不回执)。"""
        if self.state is not TaskState.RUNNING or self._ending is not None:
            return "ended"
        why = self.gate.accept(frame, rx_ms=rx_ms)
        if not why:
            self._cmd = (frame.vx, frame.wz, rx_ms + frame.ttl_ms, frame.ttl_ms, frame.vy)
        return why

    def renew(self, lease_ttl_ms: int) -> None:
        if self._ending is None:
            self._until = self._now() + lease_ttl_ms

    def zone_note(self) -> dict[str, Any] | None:
        """硬禁区守卫这会儿的判断(决策 65):续租回执里带给站点、站点转给手机。没守卫回 ``None``。"""
        return None if self._guard is None else self._guard.note()

    def release(self) -> None:
        self._end(TaskState.DONE, "released")

    async def abort(self, reason: str) -> None:
        self._end(TaskState.PREEMPTED if reason == "preempted" else TaskState.ABORTED, reason)

    async def on_offline(self, safe: bool) -> None:
        # 决策 7:不在操作者断线后延续运动。安不安全都一样:停、结束。
        self._end(TaskState.FAILED, "offline")

    def _end(self, state: TaskState, reason: str) -> None:
        if self._ending is None and not self.done:
            self._ending = (state, reason)
            self._cmd = None

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        if self.state is not TaskState.RUNNING:
            return
        now = self._now()
        if self._ending is None and self._until is not None and now >= self._until:
            self._end(TaskState.FAILED, "lease_expired")
        if self._ending is None:
            head = await self._read_head()
            if head != self._head:
                # 头尾变了:手上那一帧的「往前」已经是另一头了,不许接着按它走
                log.warning("遥控期间头尾从 %s 变成 %s:停车、结束这次遥控", self._head, head)
                self._end(TaskState.ABORTED, "head_changed")
        if self._ending is not None:
            await self._finish(dt_s)
            return
        c = self._cmd
        live = c is not None and now < c[2] and self._video_live()
        vx = wz = vy = 0.0
        if live:
            vx = max(-self._vmax, min(self._vmax, c[0]))
            wz = max(-self._wmax, min(self._wmax, c[1]))
            vy = max(-self._vymax, min(self._vymax, c[4]))
            if abs(vx) < self._deadband:
                vx = 0.0                                  # 死区以下的平移不下(HAL 会拒)
            if vx and vy:                                 # 前后、横移只走大的那一个
                if abs(vy) > abs(vx):
                    vx = 0.0
                else:
                    vy = 0.0
            if self._head == "tail":
                vx, vy = -vx, -vy                         # 人眼里的「往前」是狗尾那头,左右也反
        if self._guard is not None:
            vx, wz = await self._zone_check(vx, wz)       # 硬禁区(决策 65):机身系里查
            if abs(vx) < self._deadband:
                vx = 0.0
            if vy:
                vy = await self._lateral_check(vy)
        if live and vx == 0.0 and wz == 0.0 and vy == 0.0:
            live = False
        if live:
            self._vseq += 1
            ttl = max(_MIN_TTL_MS, min(c[3], c[2] - now))
            got = await self._hal.set_velocity(VelocityCommand(
                seq=self._vseq, ttl_ms=ttl, frame="base", vx=vx, vy=vy, wz=wz))
            if got.rejected:
                log.warning("遥控的速度被 HAL 拒了(%s)", got.reason)
            self._moving = not got.rejected
        elif self._moving:
            await self._hal.stop()
            self._moving = False

    async def _zone_check(self, vx: float, wz: float) -> tuple[float, float]:
        """过硬禁区守卫(决策 65)。没在动也问一次(手机上显示「在边上」「位置不可信」要新鲜)。
        守卫自己出错不许让遥控开着冲:当零速、记下来。"""
        try:
            o = await self._hal.odometry()
            v = self._guard.check(vx, wz, (o.x, o.y, o.yaw), float(getattr(o, "vx", 0.0) or 0.0))
        except Exception:
            log.exception("硬禁区守卫出错:这一拍按零速")
            return 0.0, 0.0
        if v.state != self._guard_state:
            if v.state in ("blocked", "limited", "inside", "untrusted"):
                log.info("遥控硬禁区:%s %s", v.state, v.reason)
            self._guard_state = v.state
        return v.vx, v.wz

    async def _lateral_check(self, vy: float) -> float:
        """横移过硬禁区守卫。守卫出错同样当零。"""
        try:
            o = await self._hal.odometry()
            return float(self._guard.lateral(vy, (o.x, o.y, o.yaw)))
        except Exception:
            log.exception("硬禁区守卫(横移)出错:这一拍不横移")
            return 0.0

    async def _finish(self, dt_s: float) -> None:
        """结束:先停车、等停稳,再进终态。"""
        if not self._stop_sent:
            self._stop_sent = True
            try:
                await self._hal.stop()
            except Exception:
                log.exception("遥控结束时停车失败")
        stopped = False
        try:
            stopped = await self._hal.stopped()
        except Exception:
            log.exception("遥控结束时问停没停失败")
        self._stop_wait_s += dt_s
        if not stopped and self._stop_wait_s < STOP_CONFIRM_TIMEOUT_S:
            return
        state, reason = self._ending  # type: ignore[misc]
        if not stopped:
            log.error("遥控结束 %.1fs 还确认不了停车(%s)", self._stop_wait_s, reason)
            reason = f"{reason}; stop_unconfirmed"
        self.state = state
        # 丢帧计数随终态事件上站点:真机上看 4G 抖动(积压)、有没有别的代次或乱序的帧。
        self.detail = {"reason": reason, "dropped": dict(self.gate.dropped)}
        log.info("遥控结束:%s(%s),丢帧 %s", self.task_id, reason, self.gate.dropped)
