"""最小 goto(设计 §5):按里程计向目标点走。W00 里它走的是 HAL 速度环,不是
``MissionEngine``;W00b 接引擎时换实现、契约不变。

控制律刻意简单:朝向差大先转不走;朝向对了按距离比例给前进速度,夹在
``min(命令 max_speed, HAL max_vx)`` 之内、不低于 HAL 死区。每拍发一条带 ``ttl`` 的速度
命令 —— 任务卡死了适配器自己会停(总设计 §2.2)。

停止语义:``abort()`` 只登记请求;随后的 ``step`` 先 ``hal.stop()``,再每拍问 ``stopped()``,
确认了才进终态(``ABORTED``,或抢占时 ``PREEMPTED``)。到点也一样:先停、确认、再 ``DONE``。
速度命令被 HAL 拒(急停、控制权没了)→ ``FAILED``,理由就是 HAL 的理由。

断线(总设计 §4.3 ``continue_if_safe``):``on_offline(safe=False)`` → 停住等待(状态仍
``RUNNING``);``on_online()`` → 接着走。``safe=True`` 什么都不变。
"""

from __future__ import annotations

import math
from collections.abc import Callable

from d1max_agent.events import EventBook
from d1max_agent.tasks.base import Task
from d1max_contract.hal import RobotHAL, VelocityCommand
from d1max_contract.messages import MapPose, TaskState

POSITION_TOL_M = 0.1
BEARING_THRESH_RAD = 0.3
K_LIN = 1.0
K_ANG = 2.0
PROGRESS_EVERY_M = 0.5
BATTERY_FLOOR_PCT = 15.0


def _wrap(a: float) -> float:
    w = math.remainder(a, 2 * math.pi)
    return math.pi if w == -math.pi else w


class GotoTask(Task):
    def __init__(self, *, task_id: str, target: MapPose, max_speed_mps: float | None,
                 hal: RobotHAL, events: EventBook, now_ms: Callable[[], int],
                 priority: int = 0) -> None:
        super().__init__(task_id=task_id, kind="goto", priority=priority)
        self.target = target
        caps = hal.hal_capabilities()
        self._vmax = min(max_speed_mps or caps.max_vx, caps.max_vx)
        self._wmax = caps.max_wz
        self._deadband = caps.deadband_vx
        self._hal = hal
        self._events = events
        self._now = now_ms
        self._seq = 0
        self._abort_reason: str | None = None
        self._stop_sent = False
        self._arrived = False
        self._holding = False
        self._last_reported_m: float | None = None

    # ------------------------------------------------------------ 外部请求

    async def abort(self, reason: str) -> None:
        if self.done:
            return
        self._abort_reason = reason or "abort"
        self._stop_sent = False

    async def on_offline(self, safe: bool) -> None:
        if not safe:
            self._holding = True
            self._stop_sent = False

    async def on_online(self) -> None:
        self._holding = False

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        if self.state is not TaskState.RUNNING:
            return
        if self._abort_reason is not None:
            await self._settle(TaskState.PREEMPTED if self._abort_reason == "preempted"
                               else TaskState.ABORTED, {"reason": self._abort_reason})
            return
        if self._holding:
            if not self._stop_sent:
                await self._hal.stop()
                self._stop_sent = True
            return
        odom = await self._hal.odometry()
        if not odom.valid:
            # 定位丢失高于一切命令(总设计 §4.2):停、确认、failed。
            await self._settle(TaskState.FAILED, {"reason": "loc_lost"})
            return
        dx, dy = self.target.x - odom.x, self.target.y - odom.y
        dist = math.hypot(dx, dy)
        self._report(dist)
        if dist <= POSITION_TOL_M or self._arrived:
            self._arrived = True
            await self._settle(TaskState.DONE, {"distance_m": round(dist, 3)})
            return
        bearing = _wrap(math.atan2(dy, dx) - odom.yaw)
        wz = max(-self._wmax, min(self._wmax, K_ANG * bearing))
        if abs(bearing) > BEARING_THRESH_RAD:
            vx = 0.0
        else:
            # 不低于死区(不然 HAL 拒),但**绝不**越过站点给的上限:上限本身低于死区时
            # 让 HAL 按死区拒、任务 failed(deadband),而不是偷偷跑得比要求快。
            vx = min(self._vmax, max(K_LIN * dist, self._deadband))
        self._seq += 1
        got = await self._hal.set_velocity(VelocityCommand(
            seq=self._seq, ttl_ms=max(300, int(dt_s * 3000)), frame="base", vx=vx, vy=0.0, wz=wz))
        if got.rejected:
            self.state = TaskState.FAILED
            self.detail = {"reason": got.reason}

    async def _settle(self, final: TaskState, detail: dict) -> None:
        """先停、确认停了、再进终态。"""
        if not self._stop_sent:
            await self._hal.stop()
            self._stop_sent = True
            return
        if await self._hal.stopped():
            self.state = final
            self.detail = detail

    def _report(self, dist: float) -> None:
        if self._last_reported_m is None or abs(self._last_reported_m - dist) >= PROGRESS_EVERY_M:
            self._last_reported_m = dist
            self._events.emit("task_progress", {"task_id": self.task_id,
                                                 "distance_m": round(dist, 3)})
