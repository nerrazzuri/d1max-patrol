"""``goto`` 跑在 MissionEngine 上(W00b 决定 2)。契约层看到的还是 W00 那套:
``task_progress/done/failed/aborted/preempted``、先停止确认再终态、断线不安全时停住等待。

映射:
``engine.start()`` 一趟只有一个航点的 Mission → RunState 进终态:
``DONE`` → ``DONE``;``ABORTED`` 且是我们自己发的 abort → ``ABORTED``(``preempted`` 时
``PREEMPTED``);``ABORTED`` 是引擎自己收的尾(预飞没过、急停、定位丢、安全裁定)→
``FAILED``,理由用引擎的 ``reason``。终态之前还要等 ``hal.stopped()`` 为真——停止请求与确认分开。
``on_offline(safe=False)`` → ``engine.pause()``;``on_online()`` → ``engine.resume()``。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

from d1max_agent.assembly import EngineParts
from d1max_agent.engine.machine import EngineBusy, RunState
from d1max_agent.engine.mission import Mission, MissionWaypoint, Policy
from d1max_agent.events import EventBook
from d1max_agent.tasks.base import Task
from d1max_contract.messages import MapPose, TaskState
from d1max_patrol.protocol.nav_types import Pose

log = logging.getLogger(__name__)

PROGRESS_EVERY_M = 0.5
_TERMINAL = frozenset({RunState.DONE, RunState.ABORTED})


class EngineGotoTask(Task):
    def __init__(self, *, task_id: str, target: MapPose, max_speed_mps: float | None,
                 parts: EngineParts, events: EventBook, now_ms: Callable[[], int],
                 priority: int = 0) -> None:
        super().__init__(task_id=task_id, kind="goto", priority=priority)
        self.target = target
        self._max_speed = max_speed_mps
        self._parts = parts
        self._events = events
        self._now = now_ms
        self._abort_reason: str | None = None
        self._holding = False
        self._last_reported_m: float | None = None
        self._started = False

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        p = self._parts
        if self._max_speed is not None:
            try:
                await p.nav.set_speed(self._max_speed)
            except Exception as exc:  # noqa: BLE001 - 上限设不进去就按 HAL 上限走,记一笔
                log.warning("速度上限 %.2f 没设进去(%s),按 HAL 上限走", self._max_speed, exc)
        mission = Mission(
            mission=self.task_id, map_id=self.target.map_id,
            waypoints=(MissionWaypoint(
                name="target", pose=Pose.from_xy_yaw(self.target.x, self.target.y,
                                                     self.target.yaw)),),
            policy=Policy())
        try:
            await p.engine.start(mission, home=p.home)
        except EngineBusy as exc:
            self.state = TaskState.FAILED
            self.detail = {"reason": f"busy: {exc}"}
            return
        self._started = True
        self.state = TaskState.RUNNING

    async def abort(self, reason: str) -> None:
        if self.done:
            return
        self._abort_reason = reason or "abort"
        if self._started:
            await self._parts.engine.abort(self._abort_reason)

    async def on_offline(self, safe: bool) -> None:
        if not safe and self._started and not self._holding:
            self._holding = True
            await self._parts.engine.pause()

    async def on_online(self) -> None:
        if self._holding:
            self._holding = False
            await self._parts.engine.resume()

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        if self.state is not TaskState.RUNNING or not self._started:
            return
        odom = await self._parts.hal.odometry()
        dist = math.hypot(self.target.x - odom.x, self.target.y - odom.y)
        self._report(dist)
        snap = self._parts.engine.snapshot
        if snap.state not in _TERMINAL:
            return
        if not await self._parts.hal.stopped():
            return                                   # 引擎收尾了,机器还在制动:等确认
        if snap.state is RunState.DONE:
            # 引擎的 DONE 是「这趟跑完了」,航点本身可能是按 retry_then_skip 跳过的;
            # 对只有一个航点的 goto,航点没到就是任务没成。
            bad = [r for r in snap.results if not r.ok]
            if bad:
                self.state = TaskState.FAILED
                self.detail = {"reason": bad[0].note or "waypoint failed"}
            else:
                self.state = TaskState.DONE
                self.detail = {"distance_m": round(dist, 3)}
        elif self._abort_reason is not None:
            self.state = (TaskState.PREEMPTED if self._abort_reason == "preempted"
                          else TaskState.ABORTED)
            self.detail = {"reason": self._abort_reason}
        else:
            self.state = TaskState.FAILED
            self.detail = {"reason": snap.reason or "engine aborted"}

    def _report(self, dist: float) -> None:
        if self._last_reported_m is None or abs(self._last_reported_m - dist) >= PROGRESS_EVERY_M:
            self._last_reported_m = dist
            self._events.emit("task_progress", {"task_id": self.task_id,
                                                 "distance_m": round(dist, 3)})
