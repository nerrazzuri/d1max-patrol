"""``goto`` 跑在 MissionEngine 上(W00b 决定 2)。契约层看到的还是 W00 那套:
``task_progress/done/failed/aborted/preempted``、先停止确认再终态、断线不安全时停住等待。

映射:
``engine.start()`` 一趟只有一个航点的 Mission → RunState 进终态:
``DONE`` → ``DONE``;``ABORTED`` 且是我们自己发的 abort → ``ABORTED``(``preempted`` 时
``PREEMPTED``);``ABORTED`` 是引擎自己收的尾(预飞没过、急停、定位丢、安全裁定)→
``FAILED``,理由用引擎的 ``reason``。终态之前还要等 ``hal.stopped()`` 为真——停止请求与确认分开;
还要等导航回到 StandBy:终态一出,资源就放给下一趟,下一趟的预飞 ``nav_ready`` 要看到 StandBy,
导航还在 Cancelled/Succeed 的驻留期里的话,抢占就变成「旧的停了、新的也 failed」。
停止确认等不过 ``STOP_CONFIRM_TIMEOUT_S`` 就再发一次停、按 ``stop_unconfirmed`` 失败收尾。
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
from d1max_patrol.protocol.nav_types import NavStatus, Pose

log = logging.getLogger(__name__)

PROGRESS_EVERY_M = 0.5
#: 引擎进终态之后,最多等这么久确认机器停了(秒)。等不到就再发一次停车、按 ``stop_unconfirmed``
#: 失败收尾 —— 不然 ``stopped()`` 的阈值不合真机(W00d 待测)时任务永远卡在 RUNNING、资源不放。
STOP_CONFIRM_TIMEOUT_S = 5.0
_TERMINAL = frozenset({RunState.DONE, RunState.ABORTED})


class EngineMissionTask(Task):
    """在 MissionEngine 上跑一趟 ``Mission`` 的任务(goto 与 patrol 共用)。子类给出这趟的
    ``Mission``(``_mission``),可选地每拍报进度(``_progress``)。"""

    def __init__(self, *, task_id: str, kind: str, max_speed_mps: float | None,
                 parts: EngineParts, events: EventBook, now_ms: Callable[[], int],
                 priority: int = 0) -> None:
        super().__init__(task_id=task_id, kind=kind, priority=priority)
        self._max_speed = max_speed_mps
        self._parts = parts
        self._events = events
        self._now = now_ms
        self._abort_reason: str | None = None
        self._holding = False
        self._started = False
        self._reported_results = 0
        self._unconfirmed_s = 0.0
        #: 这一趟的「归档写不进去」报过没有(W00c6a:一趟报一条)。
        self._archive_reported = False
        #: 引擎任务死了的原因(W00c6a 看门);非空之后按终态走:等停车确认、导航回待命,再报失败。
        self._dead: str | None = None

    def _mission(self) -> Mission:
        raise NotImplementedError

    @property
    def aborting(self) -> bool:
        return self._abort_reason is not None and not self.done

    async def _progress(self) -> dict | None:
        """每拍的进度;返回终态 detail 里要带的东西(goto 带 distance_m)。"""
        return None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        p = self._parts
        if self._abort_reason is not None:
            # 还没起就被 abort / 抢占了:不动,直接终态。
            self._finish_aborted()
            return
        try:
            if self._max_speed is None:
                await p.nav.reset_speed()            # 上一趟的限速不带到这一趟
            else:
                await p.nav.set_speed(self._max_speed)
        except Exception as exc:  # noqa: BLE001 - 设不进去就不走:按 HAL 上限走会比要求的快
            self._fail(f"max_speed_mps {self._max_speed!r} 设不进去: {exc}")
            return
        mission = self._mission()
        try:
            await p.engine.start(mission, home=p.home)
        except EngineBusy as exc:
            self._fail(f"busy: {exc}")
            return
        except Exception as exc:
            log.exception("引擎起不来")
            self._fail(f"engine start: {exc}")
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
        extra = await self._progress()
        engine = self._parts.engine
        snap = engine.snapshot
        self._report_waypoints(snap.results)
        err = engine.archive_error
        if err and not self._archive_reported:
            # 盘满、只读重挂(W00c6a):这一趟照跑,但照片、记录没存下 —— 要让人知道,证据缺了。
            self._archive_reported = True
            self._events.emit("archive_write_failed", {"task_id": self.task_id,
                                                       "reason": err[:200]})
        if snap.state not in _TERMINAL and self._dead is None:
            if not engine.running:
                # 看门的最后一道(W00c6a):引擎任务已经结束、快照却不是终态。正常路径走不到
                # (收尾一定落成终态);真走到了,不等一个死掉的引擎 —— 停车,然后跟正常收尾一样
                # 等停车确认、导航回待命,再按失败收尾(内审:不然下一趟在驻留期起跑、预飞变红)。
                await self._engine_died(engine.crash or "引擎任务已经结束")
            else:
                return
        if not await self._parts.hal.stopped():
            self._unconfirmed_s += dt_s              # 引擎收尾了,机器还在制动:等确认
            if self._unconfirmed_s < STOP_CONFIRM_TIMEOUT_S:
                return
            log.error("任务 %s:引擎收尾 %.1fs 了还确认不了停车,再发一次停,按失败收尾",
                      self.task_id, self._unconfirmed_s)
            try:
                await self._parts.hal.stop()
            except Exception:
                log.exception("再发一次停车也失败了")
            self._fail("stop_unconfirmed")
            return
        if await self._parts.nav.nav_status() is not NavStatus.STANDBY:
            return                                   # 导航还在终态驻留期:下一趟现在起会被预飞拒
        if self._abort_reason is not None:
            # 请求过中止(叫停、抢占、人工)就报中止,不让引擎恰好同时跑完的 DONE 抢先(W00c6a 内审)。
            self._finish_aborted()
        elif self._dead is not None:
            self._fail(f"engine_died: {self._dead}"[:200])
        elif snap.state is RunState.DONE:
            # 引擎的 DONE 是「这趟跑完了」,航点本身可能是按 retry_then_skip 跳过的;
            # 对只有一个航点的 goto,航点没到就是任务没成。
            bad = [r for r in snap.results if not r.ok]
            m = self._mission()
            expected = len(m.waypoints) * m.policy.loops
            if engine.returned:
                # 半路返航(电量到线)回到了原点:引擎落 DONE,可点位没跑完(W00c6b 内审阻断 1)。报失败、
                # 原因带上返航起因 —— 站点按电量告警,也不会当成「停在最后一个点」派回程巡检。
                self.state = TaskState.FAILED
                self.detail = {"reason": f"{engine.returned}(半路返航,已回到原点)"[:200]}
            elif len(snap.results) < expected:
                self.state = TaskState.FAILED
                self.detail = {"reason": f"只跑了 {len(snap.results)}/{expected} 个点"}
            elif bad:
                self.state = TaskState.FAILED
                self.detail = {"reason": self._failed_reason(bad)}
            else:
                self.state = TaskState.DONE
                self.detail = dict(extra or {})
        else:
            self.state = TaskState.FAILED
            self.detail = {"reason": snap.reason or "engine aborted"}

    def _failed_reason(self, bad) -> str:
        return bad[0].note or "waypoint failed"

    def _report_waypoints(self, results) -> None:
        """引擎每走完(或跳过)一个航点,结果里多一条;子类决定要不要发事件。"""
        self._reported_results = len(results)

    def _fail(self, reason: str) -> None:
        self.state = TaskState.FAILED
        self.detail = {"reason": reason}

    async def _engine_died(self, why: str) -> None:
        log.error("任务 %s:引擎任务死了(%s),停车、等停稳后按失败收尾", self.task_id, why)
        self._dead = why
        for stop in (self._parts.nav.stop, self._parts.hal.stop):
            try:
                await stop()
            except Exception:
                log.exception("引擎死了之后停车失败")

    def _finish_aborted(self) -> None:
        assert self._abort_reason is not None
        self.state = (TaskState.PREEMPTED if self._abort_reason == "preempted"
                      else TaskState.ABORTED)
        self.detail = {"reason": self._abort_reason}



class EngineGotoTask(EngineMissionTask):
    """``goto``:一个航点的 Mission。"""

    def __init__(self, *, task_id: str, target: MapPose, max_speed_mps: float | None,
                 parts: EngineParts, events: EventBook, now_ms: Callable[[], int],
                 priority: int = 0) -> None:
        super().__init__(task_id=task_id, kind="goto", max_speed_mps=max_speed_mps,
                         parts=parts, events=events, now_ms=now_ms, priority=priority)
        self.target = target
        self._last_reported_m: float | None = None

    def _mission(self) -> Mission:
        return Mission(
            mission=self.task_id, map_id=self.target.map_id,
            waypoints=(MissionWaypoint(
                name="target", pose=Pose.from_xy_yaw(self.target.x, self.target.y,
                                                     self.target.yaw)),),
            policy=Policy())

    async def _progress(self) -> dict:
        # 按地图位姿算(W00c6e 内审:以前按原始里程,锚在 (10, 5) 时报 12 m、真距离 2 m)。
        here = await self._parts.nav.current_pose()
        if here is None:
            return {}
        dist = math.hypot(self.target.x - here.position.x, self.target.y - here.position.y)
        if self._last_reported_m is None or abs(self._last_reported_m - dist) >= PROGRESS_EVERY_M:
            self._last_reported_m = dist
            self._events.emit("task_progress", {"task_id": self.task_id,
                                                 "distance_m": round(dist, 3)})
        return {"distance_m": round(dist, 3)}
