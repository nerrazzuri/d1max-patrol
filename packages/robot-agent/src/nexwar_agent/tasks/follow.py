"""跟人任务(W74 二期;规矩见 :mod:`nexwar_contract.follow`)。

一段一段走:每段是一趟单点 ``goto``(:class:`~nexwar_agent.tasks.engine_goto.EngineGotoTask`,
同一个任务号),
目标是人后方 ``keep_m``、朝着人;走着的时候人挪开了 :data:`RETARGET_M` 以上,就让引擎**中途换目标**
(:meth:`~nexwar_agent.engine.machine.MissionEngine.retarget`,不停车),最快每 :data:`RETARGET_EVERY_S`
秒一次。一段走到了、人还在 ``keep_m`` 附近 → 站着等;人又走远了 → 再起一段。

**人在哪**:人员检测给的最近一个人(方向 + 距离,狗身系)加上此刻的地图位姿,算出地图上的点。
连起来跟
同一个人:新的点离上一个点比人能走的还远(:data:`GATE_M` + :data:`PERSON_MAX_MPS` × 隔了多久)就当是
别人、不认;被跟的人 :data:`SWITCH_S` 秒没看到以后,谁近跟谁。只有方向、没测到距离的帧不算
(不知道在哪)。

**跟丢**(``lost_s`` 秒没看到被跟的人、规划不出去人那儿的路)、**到上限**(``max_s`` / ``max_m``)→
走着的
那一段停下,回最后看见人的地方、朝人离开的方向(没算出方向就朝人最后所在的方向),到了任务 ``DONE``,
``result`` 写 ``lost`` / ``limit``。一开始就没看到过人:原地不动,到时限按跟丢收。半路电量到线、引擎
返航:任务失败(不再跟)。中止、抢占:那一段停稳再终态。
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from typing import Any

from nexwar_agent.assembly import EngineParts
from nexwar_agent.tasks.base import Task
from nexwar_agent.tasks.engine_goto import EngineGotoTask
from nexwar_contract.follow import TRACK_EVERY_S, TRACK_KEEP, FollowRequest
from nexwar_contract.messages import MapPose, TaskState

log = logging.getLogger(__name__)

#: 目标挪开这么多(米)才换目标;最快多久换一次(秒)。
RETARGET_M = 1.0
RETARGET_EVERY_S = 1.0
#: 离人比 ``keep_m`` 远这么多(米)才起新的一段。
START_EXTRA_M = 1.0
#: 认同一个人:人最快走多快(米/秒)、再宽出这么多(米)。
PERSON_MAX_MPS = 3.0
GATE_M = 1.5
#: 被跟的人多久没看到以后,谁近跟谁(秒)。
SWITCH_S = 2.0
#: 人走了这么远(米)才算得出方向。
HEADING_MIN_M = 0.8
#: 方向按最近这么久(秒)的轨迹算。
HEADING_SPAN_S = 4.0
#: 每隔这么久(秒)记一个轨迹点。
TRACK_STEP_S = 0.5


class FollowTask(Task):
    def __init__(self, *, task_id: str, req: FollowRequest, parts: EngineParts, events: Any,
                 now_ms: Callable[[], int], wall_ms: Callable[[], int],
                 target: Callable[[], tuple[float, float] | None],
                 here: Callable[[], tuple[float, float, float] | None],
                 map_ref: tuple[str, str], priority: int = 80) -> None:
        super().__init__(task_id=task_id, kind="follow", priority=priority)
        self.req = req
        self._parts = parts
        self._events = events
        self._mono = now_ms
        self._wall = wall_ms
        self._target = target
        self._here = here
        self._map = map_ref
        #: ``follow`` 跟着;``back`` 回最后看见人的地方;收尾了按 ``_result`` 进终态。
        self.phase = "follow"
        self._leg: EngineGotoTask | None = None
        self._leg_goal: tuple[float, float] | None = None
        self._leg_n = 0
        self._retarget_at = -math.inf
        self._t0 = 0
        self._dist_m = 0.0
        self._prev_here: tuple[float, float] | None = None
        #: 被跟的人:最后一次在哪、什么时候(单调毫秒)。
        self._person: tuple[float, float] | None = None
        self._seen_ms = 0
        #: 轨迹:``(单调毫秒, x, y, 墙钟毫秒)``。
        self._track: deque[tuple[int, float, float, int]] = deque(maxlen=TRACK_KEEP)
        self._unsent: list[dict[str, Any]] = []
        self._sent_at = 0
        self._result = ""
        self._abort_reason: str | None = None
        self._final: tuple[TaskState, dict[str, Any]] | None = None
        #: 跟丢 / 到上限了、还没起回最后位置的那一段。
        self._pending_back = False

    @property
    def aborting(self) -> bool:
        return self._abort_reason is not None and not self.done

    # ------------------------------------------------------------ 能力里给站点看的

    def caps(self) -> dict[str, Any]:
        d: dict[str, Any] = {"state": self.phase if not self.done else "idle"}
        if self._person is not None:
            d["person"] = {"x": round(self._person[0], 2), "y": round(self._person[1], 2)}
        return d

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self.state = TaskState.RUNNING
        self._t0 = self._mono()
        self._seen_ms = self._t0
        self._sent_at = self._t0
        log.info("跟人 %s 开始:保持 %.1f m,最多 %d 秒 / %.0f m", self.task_id, self.req.keep_m,
                 self.req.max_s, self.req.max_m)

    async def abort(self, reason: str) -> None:
        if self.done:
            return
        self._abort_reason = reason or "abort"
        if self._leg is not None and not self._leg.done:
            await self._leg.abort(self._abort_reason)

    async def on_offline(self, safe: bool) -> None:
        # 跟随在机器人上做:断网照样跟(设计 §三)。不安全的断线才让那一段暂停。
        if self._leg is not None and not self._leg.done:
            await self._leg.on_offline(safe)

    async def on_online(self) -> None:
        if self._leg is not None and not self._leg.done:
            await self._leg.on_online()

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        if self.state is not TaskState.RUNNING:
            return
        now = self._mono()
        here = self._here()
        if here is not None:
            if self._prev_here is not None:
                self._dist_m += math.dist(self._prev_here, here[:2])
            self._prev_here = (here[0], here[1])
        if self._leg is not None:
            await self._leg.step(dt_s)
            if self._leg.done:
                leg, self._leg, self._leg_goal = self._leg, None, None
                if self._leg_finished(leg):
                    return
        if self._abort_reason is not None:
            if self._leg is None:
                self.state = (TaskState.PREEMPTED if self._abort_reason == "preempted"
                              else TaskState.ABORTED)
                self.detail = {"reason": self._abort_reason, **self._report()}
            return
        if self._final is not None:
            if self._leg is None:
                self.state, self.detail = self._final
            return
        self._observe(now, here)
        self._send_track(now)
        if self.phase == "follow":
            await self._follow(now, here)
        elif self._pending_back and self._final is None:
            if self._leg is not None:
                if not self._leg.aborting:
                    await self._leg.abort("follow_back")     # 先停下跟着的那一段
            else:
                self._pending_back = False
                await self._go_back()

    def _leg_finished(self, leg: EngineGotoTask) -> bool:
        """一段走完了;回 True = 任务这一拍已经收尾。"""
        if leg.state is TaskState.FAILED:
            why = str(leg.detail.get("reason", ""))
            if self._parts.engine.returned or "返航" in why:
                self.state = TaskState.FAILED
                self.detail = {"reason": f"跟人途中电量到线、返航:{why}"[:200], **self._report()}
                return True
            if self.phase == "back":
                # 回最后位置也走不到:停在这儿,照样算跟丢收尾
                self._final = (TaskState.DONE, {"result": self._result or "lost",
                                                "reason": f"回不到最后看见人的地方:{why}"[:200],
                                                **self._report()})
                return False
            log.info("跟人 %s:去人那儿的路走不通(%s),按跟丢办", self.task_id, why)
            self._lose("lost")
            return False
        if self.phase == "back" and leg.state is TaskState.DONE:
            self._final = (TaskState.DONE, {"result": self._result, **self._report()})
        return False

    # ------------------------------------------------------------ 人在哪

    def _observe(self, now: int, here: tuple[float, float, float] | None) -> None:
        obs = self._target()
        if obs is not None and here is not None:
            b, r = obs
            a = here[2] + math.radians(b)
            p = (here[0] + r * math.cos(a), here[1] + r * math.sin(a))
            if self._same(p, now):
                self._person, self._seen_ms = p, now
                if not self._track or now - self._track[-1][0] >= TRACK_STEP_S * 1000:
                    self._track.append((now, p[0], p[1], self._wall()))
                    self._unsent.append({"x": round(p[0], 2), "y": round(p[1], 2),
                                         "at_ms": self._wall()})
                if self.phase == "back" and self._result == "lost":
                    log.info("跟人 %s:又看见了,接着跟", self.task_id)
                    self.phase, self._result, self._pending_back = "follow", "", False
        if self.phase != "follow" or self._final is not None:
            return
        if now - self._seen_ms >= self.req.lost_s * 1000:
            self._lose("lost")
        elif now - self._t0 >= self.req.max_s * 1000 or self._dist_m >= self.req.max_m:
            self._lose("limit")

    def _same(self, p: tuple[float, float], now: int) -> bool:
        if self._person is None:
            return True
        gap_s = (now - self._seen_ms) / 1000
        if gap_s >= SWITCH_S:
            return True                                  # 被跟的人好一会儿没看到了:谁近跟谁
        return math.dist(p, self._person) <= GATE_M + PERSON_MAX_MPS * gap_s

    def _heading(self) -> float | None:
        """人往哪走(弧度):最近 :data:`HEADING_SPAN_S` 秒的头尾连线;走得太近算不出。"""
        if len(self._track) < 2:
            return None
        end = self._track[-1]
        start = next((t for t in self._track if end[0] - t[0] <= HEADING_SPAN_S * 1000),
                     self._track[0])
        if start is end or math.dist(start[1:3], end[1:3]) < HEADING_MIN_M:
            start = self._track[0]
        if math.dist(start[1:3], end[1:3]) < HEADING_MIN_M:
            return None
        return math.atan2(end[2] - start[2], end[1] - start[1])

    # ------------------------------------------------------------ 怎么跟

    async def _follow(self, now: int, here: tuple[float, float, float] | None) -> None:
        if self._person is None or here is None or now - self._seen_ms > SWITCH_S * 1000:
            return                                       # 不知道人在哪 / 自己在哪:不动
        px, py = self._person
        d = math.dist((px, py), here[:2])
        yaw = math.atan2(py - here[1], px - here[0])
        k = max(0.0, d - self.req.keep_m) / d if d > 1e-6 else 0.0
        goal = (here[0] + (px - here[0]) * k, here[1] + (py - here[1]) * k)
        if self._leg is None:
            if d < self.req.keep_m + START_EXTRA_M:
                return
            await self._start_leg(goal, yaw)
            return
        if self._leg_goal is not None and math.dist(goal, self._leg_goal) >= RETARGET_M \
                and now - self._retarget_at >= RETARGET_EVERY_S * 1000:
            from nexwar_patrol.protocol.nav_types import Pose
            self._retarget_at = now
            self._leg_goal = goal
            await self._parts.engine.retarget(Pose.from_xy_yaw(goal[0], goal[1], yaw))

    async def _start_leg(self, goal: tuple[float, float], yaw: float) -> None:
        self._leg_n += 1
        leg = EngineGotoTask(task_id=self.task_id,
                             target=MapPose(map_id=self._map[0], map_version=self._map[1],
                                            frame_id="map",
                                            x=goal[0], y=goal[1], yaw=yaw),
                             max_speed_mps=self.req.max_speed_mps, parts=self._parts,
                             events=self._events, now_ms=self._wall, priority=self.priority)
        await leg.start()
        if leg.done:
            log.warning("跟人 %s:这一段起不来(%s)", self.task_id, leg.detail.get("reason"))
            if self.phase == "back":
                self._final = (TaskState.DONE, {"result": self._result,
                                                "reason": str(leg.detail.get("reason", ""))[:200],
                                                **self._report()})
            else:
                self._lose("lost")
            return
        self._leg, self._leg_goal = leg, goal
        self._retarget_at = self._mono()

    def _lose(self, result: str) -> None:
        """跟丢 / 到上限:停下这一段,回最后看见人的地方、朝人离开的方向。"""
        self.phase, self._result = "back", result
        log.info("跟人 %s:%s,回最后看见人的地方", self.task_id,
                 "跟丢了" if result == "lost" else "到上限了")
        self._events.emit("follow_lost", {"task_id": self.task_id, "result": result,
                                          **self._last()})
        if self._person is None:
            self._final = (TaskState.DONE, {"result": result, **self._report()})
            return
        self._pending_back = True

    async def _go_back(self) -> None:
        """跟丢:去最后看见人的那个点。到上限(人可能还在眼前):停在离那个点 ``keep_m`` 的地方,
        不往人身上走。朝人离开的方向;算不出方向就朝那个点。"""
        here = self._here()
        px, py = self._person  # type: ignore[misc]
        h = self._heading()
        if h is None:
            h = math.atan2(py - here[1], px - here[0]) if here is not None else 0.0
        goal = (px, py)
        if self._result == "limit" and here is not None:
            d = math.dist((px, py), here[:2])
            k = max(0.0, d - self.req.keep_m) / d if d > 1e-6 else 0.0
            goal = (here[0] + (px - here[0]) * k, here[1] + (py - here[1]) * k)
            if k == 0.0:
                self._final = (TaskState.DONE, {"result": self._result, **self._report()})
                return
        await self._start_leg(goal, h)

    # ------------------------------------------------------------ 报告

    def _last(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self._person is not None and self._track:
            out["last"] = {"x": round(self._person[0], 2), "y": round(self._person[1], 2),
                           "at_ms": self._track[-1][3]}
        h = self._heading()
        if h is not None:
            out["heading_deg"] = round(math.degrees(h), 1)
        return out

    def _report(self) -> dict[str, Any]:
        return {**self._last(), "dist_m": round(self._dist_m, 1),
                "secs": round((self._mono() - self._t0) / 1000),
                "track": [{"x": round(x, 2), "y": round(y, 2), "at_ms": w}
                          for _, x, y, w in self._track]}

    def _send_track(self, now: int) -> None:
        if not self._unsent or now - self._sent_at < TRACK_EVERY_S * 1000:
            return
        self._sent_at = now
        pts, self._unsent = self._unsent, []
        self._events.emit("follow_track", {"task_id": self.task_id, "points": pts, **self._last()})
