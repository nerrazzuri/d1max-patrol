"""保持距离任务(W25,决策 40;规矩见 :mod:`d1max_contract.standoff`)。

每拍:人在 3 m 内就从几个退法里挑一个(远离人的那头直退、往左带弧、往右带弧),每个都要
- 过避障守卫(``check``:扫过区没挡、障碍数据新鲜、看得见);
- 不出拴绳:落点离**拦截点**(``center``,站点给的地图位姿,同一场固定)不超过 ``leash_m``;
- **整段扫过区不碰禁行区**(W25 外审 3):从现在到前推再加一段停车的时间,每一步的机身轮廓(带余量)
  都按地图位姿查禁行区,不只查落点;定位不可信也算不行;
- 往前推 :data:`HORIZON_S` 秒,离人的距离真的变大(人当它站着不动)。
挑离人最远的那个下一拍的速度(有效期很短,一拍不来就停);一个都不行 → **无路可退**:停车、原地站定,
``state`` 变成 ``cornered``(站点看能力报 P1)。**从不往前顶人**:速度方向总是远离人那一头。

结束(到 ``max_s``、被中止、被抢):先停车、等停稳,再进终态(同遥控)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from d1max_agent.tasks.base import Task
from d1max_contract.hal import RobotHAL, VelocityCommand
from d1max_contract.messages import TaskState
from d1max_contract.standoff import CLEAR_M, TOO_CLOSE_M, StandoffRequest

log = logging.getLogger(__name__)

#: 退的速度(m/s)、带弧时的转速(rad/s):慢,人看得清狗在让。
RETREAT_V = 0.3
RETREAT_W = 0.4
#: 往前推多久看「离人更远了没有、出没出拴绳、进没进禁行区」(秒)。
HORIZON_S = 1.5
#: 推一下至少要拉开这么多(米),不然算没用(比如人在正侧面,直退拉不开)。
GAIN_M = 0.15
#: 查禁行区时在前推之外再多推这么久(秒):停车要的路。
STOP_EXTRA_S = 0.5
#: 查禁行区的时间步长(秒)、机身轮廓的采样间距(米)、离禁行区至少多远(米)。
SWEEP_DT_S = 0.1
SWEEP_STEP_M = 0.15
NOGO_MARGIN_M = 0.15
#: 机身(跟避障守卫一样)。
BODY_LEN, BODY_WID = 0.93, 0.48
#: 每拍下的速度命令的有效期(毫秒):一拍不来 HAL 自己停。
CMD_TTL_MS = 300
STOP_CONFIRM_TIMEOUT_S = 5.0


def _ahead(vx: float, wz: float, t: float) -> tuple[float, float, float]:
    """按 (vx, wz) 走 t 秒后的位姿(此刻狗身系)。"""
    if abs(wz) < 1e-9:
        return (vx * t, 0.0, 0.0)
    th = wz * t
    r = vx / wz
    return (r * math.sin(th), r * (1 - math.cos(th)), th)


def _outline() -> list[tuple[float, float]]:
    """机身轮廓上的采样点(狗身系)。"""
    hl, hw = BODY_LEN / 2, BODY_WID / 2
    nx, ny = int(2 * hl / SWEEP_STEP_M) + 1, int(2 * hw / SWEEP_STEP_M) + 1
    pts = []
    for i in range(nx + 1):
        x = -hl + 2 * hl * i / nx
        pts += [(x, -hw), (x, hw)]
    for j in range(1, ny):
        y = -hw + 2 * hw * j / ny
        pts += [(-hl, y), (hl, y)]
    return pts


_OUTLINE = _outline()


def _compose(a: tuple[float, float, float], b: tuple[float, float, float]
             ) -> tuple[float, float, float]:
    c, s = math.cos(a[2]), math.sin(a[2])
    return (a[0] + c * b[0] - s * b[1], a[1] + s * b[0] + c * b[1], a[2] + b[2])


class StandoffTask(Task):
    def __init__(self, *, task_id: str, req: StandoffRequest, hal: RobotHAL,
                 now_ms: Callable[[], int],
                 target: Callable[[], tuple[float, float] | None],
                 check: Callable[[float, float], Any],
                 here: Callable[[], tuple[float, float, float] | None],
                 nogo: Callable[[float, float, float], str],
                 priority: int = 0) -> None:
        super().__init__(task_id=task_id, kind="standoff", priority=priority)
        self.req = req
        self._hal = hal
        self._now = now_ms
        self._target = target
        self._check = check
        self._here = here
        self._nogo = nogo
        self._until: int | None = None
        self.mode = "hold"
        #: 无路可退的原因(给人看)。
        self.why = ""
        self._moving = False
        self._vseq = 0
        self._ending: tuple[TaskState, str] | None = None
        self._stop_sent = False
        self._stop_wait_s = 0.0

    @property
    def aborting(self) -> bool:
        return self._ending is not None

    async def start(self) -> None:
        self.state = TaskState.RUNNING
        self._until = self._now() + self.req.max_s * 1000
        log.info("保持距离开始:%s(最多 %d 秒,拴绳 %.1f m)", self.task_id, self.req.max_s,
                 self.req.leash_m)

    async def abort(self, reason: str) -> None:
        self._end(TaskState.PREEMPTED if reason == "preempted" else TaskState.ABORTED, reason)

    async def on_offline(self, safe: bool) -> None:
        """断了站点照样守(判定全在狗上,``max_s`` 兜底)。"""

    def _end(self, state: TaskState, reason: str) -> None:
        if self._ending is None and not self.done:
            self._ending = (state, reason)

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        if self.state is not TaskState.RUNNING:
            return
        if self._ending is None and self._until is not None and self._now() >= self._until:
            self._end(TaskState.DONE, "max_s")
        if self._ending is not None:
            await self._finish(dt_s)
            return
        t = self._target()
        need = t is not None and t[1] < (CLEAR_M if self.mode in ("retreat", "cornered")
                                         else TOO_CLOSE_M)
        if not need:
            self._set("hold", "")
            await self._halt()
            return
        assert t is not None
        cmd, why = self._choose(*t)
        if cmd is None:
            if self.mode != "cornered":
                log.warning("无路可退(人在 %.1f m):原地站定。%s", t[1], why)
            self._set("cornered", why)
            await self._halt()
            return
        self._set("retreat", "")
        self._vseq += 1
        got = await self._hal.set_velocity(VelocityCommand(
            seq=self._vseq, ttl_ms=CMD_TTL_MS, frame="base", vx=cmd[0], vy=0.0, wz=cmd[1]))
        if got.rejected:
            log.warning("退的速度被 HAL 拒了(%s)", got.reason)
        self._moving = not got.rejected

    def _set(self, mode: str, why: str) -> None:
        self.mode, self.why = mode, why

    async def _halt(self) -> None:
        if self._moving:
            self._moving = False
            await self._hal.stop()

    def _choose(self, bearing_deg: float, range_m: float
                ) -> tuple[tuple[float, float] | None, str]:
        """挑一个退法:``((vx, wz), "")``;都不行 ``(None, 原因)``。"""
        b = math.radians(bearing_deg)
        px, py = range_m * math.cos(b), range_m * math.sin(b)
        sign = -1.0 if math.cos(b) >= 0 else 1.0           # 人在前半边就往后退,在后半边就往前走
        here = self._here()
        c0 = self.req.center
        whys: list[str] = []
        best: tuple[float, tuple[float, float]] | None = None
        for wz in (0.0, RETREAT_W, -RETREAT_W):
            vx = sign * RETREAT_V
            ex, ey, _ = _ahead(vx, wz, HORIZON_S)
            d = math.hypot(px - ex, py - ey)
            if d < range_m + GAIN_M:
                whys.append("拉不开距离")
                continue
            if here is None:
                whys.append("定位不可信,查不了拴绳和禁行区")
                continue
            mx, my, _ = _compose(here, (ex, ey, 0.0))
            if math.hypot(mx - c0.x, my - c0.y) > self.req.leash_m:
                whys.append(f"再退就离拦截点超过 {self.req.leash_m:g} m")
                continue
            z = self._swept_nogo(here, vx, wz)
            if z:
                whys.append(z)
                continue
            v = self._check(vx, wz)
            if not getattr(v, "ok", False):
                whys.append(str(getattr(v, "reason", "") or "避障不让走"))
                continue
            if best is None or d > best[0]:
                best = (d, (vx, wz))
        if best is not None:
            return best[1], ""
        uniq = list(dict.fromkeys(whys))
        return None, ";".join(uniq)[:200]

    def _swept_nogo(self, here: tuple[float, float, float], vx: float, wz: float) -> str:
        """从现在到前推再加停车的时间,每一步机身轮廓碰不碰禁行区(地图系)。碰了回原因。"""
        n = int((HORIZON_S + STOP_EXTRA_S) / SWEEP_DT_S)
        for k in range(n + 1):
            pose = _compose(here, _ahead(vx, wz, k * SWEEP_DT_S))
            for p in _OUTLINE:
                x, y, _ = _compose(pose, (p[0], p[1], 0.0))
                z = self._nogo(x, y, NOGO_MARGIN_M)
                if z:
                    return z
        return ""

    async def _finish(self, dt_s: float) -> None:
        if not self._stop_sent:
            self._stop_sent = True
            self._moving = False
            try:
                await self._hal.stop()
            except Exception:
                log.exception("保持距离结束时停车失败")
        stopped = False
        try:
            stopped = await self._hal.stopped()
        except Exception:
            log.exception("保持距离结束时问停没停失败")
        self._stop_wait_s += dt_s
        if not stopped and self._stop_wait_s < STOP_CONFIRM_TIMEOUT_S:
            return
        state, reason = self._ending  # type: ignore[misc]
        if not stopped:
            reason = f"{reason}; stop_unconfirmed"
        self.mode = "idle"
        self.state = state
        self.detail = {"reason": reason}
        log.info("保持距离结束:%s(%s)", self.task_id, reason)
