"""``PlannedNavBackend``(W10,W08 决定 6、7):在规划栅格上规划、沿路径跟踪、遵守禁行区与限速区。

跟直线桥(:class:`HalNavBackend`)同一套状态机与定位看门(受理、执行、终态驻留、回落待命;丢定位自己停),
不同的只有三处:

- ``goto``:**规划完再返回**;没图、没定位、没路、起终点不合法、超时 → 同步抛
  ``NavRequestError``,狗不动。
  规划期间被 ``stop()`` 了,晚到的结果作废(W08 决定 9 停车规则)。
- 每拍的速度环换成跟路径:前视点、弯大就原地转、限速区、到点对朝向;偏离路径太远就从当前位置重规划
  (后台跑,期间发零速原地等),重规划 3 次还偏 → FAILED。
- 回家:``return_to(原点)`` 规划回去;规划失败就抛,引擎原地停、按返航失败中止(不再沿来路回)。

区域(:mod:`d1max_contract.zones`)由运行时经 :meth:`set_zones` 换上:收紧的随时换;正在走的路径碰到新的
致命格 → 原地等、重规划;狗在新禁行区里面 → 原地停、FAILED、发事件 ``inside_nogo``,不自己往外走
(W08 决定 5)。

障碍、被挡、绕行不在这里(W11):这一版只看静态图 + 区域。
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from d1max_agent.bridges.hal_nav import HalNavBackend, _wrap
from d1max_agent.planning import costmap as cmod
from d1max_agent.planning.astar import PlanError
from d1max_agent.planning.costmap import Costmap, CostmapError
from d1max_agent.planning.planner import PlannedPath, Planner
from d1max_contract.hal import RobotHAL
from d1max_contract.zones import Zone, ZoneSet, point_in_polygon
from d1max_patrol.backends.base import NavRequestError
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

log = logging.getLogger(__name__)

LOOKAHEAD_M = 0.6
ARRIVE_TOL_M = 0.15
YAW_TOL_RAD = 0.15
TURN_IN_PLACE_RAD = 0.5
MAX_DEVIATION_M = 1.0
MAX_REPLANS = 3
MAX_SIGMA_MARGIN_M = 0.5
K_LIN = 1.0
K_ANG = 2.0


class _Stale(Exception):
    """算代价图的时候图或区域换了:这张作废,重算。"""


class PlannedNavBackend(HalNavBackend):
    PATH_KIND = "planned"

    def __init__(self, hal: RobotHAL, *, now_ms: Callable[[], int], map_id: str,
                 planner: Planner | None = None, robot_radius_m: float = cmod.ROBOT_RADIUS_M,
                 **kw: Any) -> None:
        super().__init__(hal, now_ms=now_ms, map_id=map_id, **kw)
        self._planner = planner if planner is not None else Planner()
        self.robot_radius_m = robot_radius_m
        self._base: tuple[np.ndarray, float, tuple[float, float]] | None = None
        #: 规划不了的原因(没载规划栅格、栅格坏了);``None`` = 能规划。
        self.plan_problem: str | None = "没有规划栅格"
        self.zones: ZoneSet | None = None
        self._cms: dict[int, Costmap] = {}
        #: 图或区域每换一次 +1:线程里算到一半的旧代价图不许写回缓存(换区域之后还拿旧图规划)。
        self._epoch = 0
        self._cm: Costmap | None = None              # 当前路径用的那张
        self._path: PlannedPath | None = None
        self._seg = 0
        self._aligning = False
        self._gen = 0
        self._planning = False
        self._replans = 0
        self._replan: asyncio.Task | None = None
        self._replan_out: tuple[str, Any] | None = None
        #: 最近一次规划出的路径长度(米):引擎按它算返航超时(W08 决定 9)。
        self.planned_length_m: float | None = None
        self._len_cache: dict[tuple, float] = {}
        self._len_task: asyncio.Task | None = None
        #: 运行时挂上来的事件出口 ``(kind, data)``。
        self.on_event: Callable[[str, dict], None] | None = None

    # ------------------------------------------------------------ 图与区域

    def _invalidate(self) -> None:
        self._epoch += 1
        self._cms = {}
        self._len_cache = {}

    def load_grid(self, map_dir: Path | None) -> None:
        """换图时载规划栅格(阻塞,运行时放线程里调)。坏了记原因、不能规划,不抛。"""
        self._invalidate()
        if map_dir is None:
            self._base, self.plan_problem = None, "没有规划栅格"
            return
        try:
            free, res, origin = cmod.load_floor(Path(map_dir))
            blocked, pres = cmod.downsample(free, res)
        except (OSError, CostmapError, ValueError) as exc:
            log.error("规划栅格载不了:%s", exc)
            self._base, self.plan_problem = None, f"规划栅格载不了:{exc}"[:200]
            return
        self._invalidate()
        self._base, self.plan_problem = (blocked, pres, origin), None

    def close_planner(self) -> None:
        self._planner.close()

    @property
    def plan_ok(self) -> bool:
        return self._base is not None

    def _costmap(self, margin_m: float) -> Costmap:
        key = int(math.ceil(max(0.0, margin_m) / 0.1 - 1e-9))
        cms, epoch, base, zs = self._cms, self._epoch, self._base, self.zones
        cm = cms.get(key)
        if cm is None:
            if base is None:
                raise NavRequestError("goto", f"规划不了:{self.plan_problem}")
            blocked, res, origin = base
            cm = cmod.build(blocked, res, origin, zs.zones if zs is not None else (),
                            robot_radius_m=self.robot_radius_m, nogo_margin_m=key * 0.1)
            cms[key] = cm                     # 写进算的时候那一份缓存:换过就是丢掉的那份
            if epoch != self._epoch:
                raise _Stale()
        return cm

    async def _costmap_async(self, margin_m: float) -> Costmap:
        """在线程里算(百万格要几百毫秒);算到一半图或区域换了就重算。"""
        for _ in range(3):
            try:
                return await asyncio.to_thread(self._costmap, margin_m)
            except _Stale:
                continue
        raise NavRequestError("goto", "图或区域一直在换,代价图算不出来")

    def nogo_at(self, x: float, y: float) -> Zone | None:
        for z in self.zones.nogo() if self.zones is not None else ():
            if point_in_polygon(x, y, z.polygon):
                return z
        return None

    async def set_zones(self, zs: ZoneSet | None) -> None:
        """换上一份区域(收紧还是放宽由运行时判;放宽的它等狗空闲再调)。正在走的:狗在新禁行区里 →
        停、FAILED、发事件;路径碰上新的致命格 → 原地等、重规划。"""
        self.zones = zs
        self._invalidate()
        if self._status not in (NavStatus.ACTIVE, NavStatus.PAUSE, NavStatus.INITIALIZING):
            return
        here = await self._here()
        if here is not None:
            z = self.nogo_at(here.x, here.y)
            if z is not None:
                await self._inside_nogo(z, here)
                return
        if self._path is None or self._cm is None:
            return
        margin = self._margin(here)
        cm = await self._costmap_async(margin)
        if self._crosses(cm, here):
            log.warning("新区域挡住了正在走的路,原地等、重规划")
            self._cm = cm
            self._start_replan()
        else:
            self._cm = cm

    def _crosses(self, cm: Costmap, here: Any) -> bool:
        """剩下的路(从狗现在的位置起)有没有压上致命格。"""
        from d1max_agent.planning.astar import LETHAL, line_cells
        assert self._path is not None
        start = [] if here is None else [(here.x, here.y)]
        pts = start + list(self._path.points[self._seg + 1:])
        cells = [cm.cell_of(*p) for p in pts]
        prev = None
        for rc in cells:
            if rc is None:
                return True
            if prev is not None:
                for r, c in line_cells(prev, rc):
                    if cm.cost[r, c] == LETHAL:
                        return True
            prev = rc
        return False

    async def _inside_nogo(self, z: Zone, here: Any) -> None:
        log.error("狗在禁行区 %s 里面:原地停,等人处理", z.id)
        if self.on_event is not None:
            self.on_event("inside_nogo", {"zone": z.id, "label": z.label,
                                          "x": round(here.x, 2), "y": round(here.y, 2)})
        await self._enter_terminal(NavStatus.FAILED)

    # ------------------------------------------------------------ 规划

    async def _here(self) -> Any:
        o = await self._hal.odometry()
        return self.anchor.estimate((o.x, o.y, o.yaw))

    @staticmethod
    def _margin(est: Any) -> float:
        s = getattr(est, "sigma_xy_m", 0.0) if est is not None else 0.0
        if not math.isfinite(s):
            return MAX_SIGMA_MARGIN_M
        return min(max(float(s), 0.0), MAX_SIGMA_MARGIN_M)

    async def _plan_from_here(self, pose: Pose, op: str) -> tuple[PlannedPath, Costmap]:
        if self._base is None:
            raise NavRequestError(op, f"规划不了:{self.plan_problem}")
        here = await self._here()
        if here is None or self._loc is LocStatus.LOC_LOST:
            raise NavRequestError(op, "没有可信定位,不规划")
        z = self.nogo_at(here.x, here.y)
        if z is not None:
            raise NavRequestError(op, f"狗在禁行区 {z.id} 里面,不自己往外走")
        for _ in range(3):
            epoch = self._epoch
            cm = await self._costmap_async(self._margin(here))
            try:
                path = await self._planner.plan(cm, (here.x, here.y),
                                                (pose.position.x, pose.position.y))
            except PlanError as exc:
                if epoch != self._epoch:
                    continue                  # 规划期间图或区域换了:按新的再来
                raise NavRequestError(op, f"规划失败({exc.reason}):{exc}") from None
            if epoch == self._epoch:
                return path, cm
        raise NavRequestError(op, "规划期间图或区域一直在换")

    async def goto(self, pose: Pose) -> None:
        await self._go(pose, "goto")

    async def return_to(self, home: Pose) -> None:
        """回原点(引擎给原点):规划回去;规划失败就抛,狗不动(W08 决定 6)。"""
        await self._go(home, "return_home")

    async def return_home(self) -> None:
        raise NavRequestError("return_home", "规划后端要引擎给原点(return_to)")

    async def _go(self, pose: Pose, op: str) -> None:
        if self._status is not NavStatus.STANDBY or self._planning:
            raise NavRequestError(op, f"只能在 StandBy 下启动,当前 {self._status.value}"
                                  + (",正在规划" if self._planning else ""))
        self._gen += 1
        gen = self._gen
        self._planning = True
        try:
            path, cm = await self._plan_from_here(pose, op)
        finally:
            self._planning = False
        if gen != self._gen:
            raise NavRequestError(op, "规划期间被叫停,结果作废")
        if self._status is not NavStatus.STANDBY:
            raise NavRequestError(op, f"规划完状态变了({self._status.value}),结果作废")
        self._path, self._cm, self._seg = path, cm, 0
        self._aligning = False
        self._replans = 0
        self._replan_out = None
        self.planned_length_m = path.length_m
        log.info("%s:规划出 %d 个点、%.1f m", op, len(path.points), path.length_m)
        await super().goto(pose)

    def _start_replan(self) -> None:
        if self._replan is not None and not self._replan.done():
            return
        if self._replans >= MAX_REPLANS:
            self._replan_out = ("fail", f"重规划了 {MAX_REPLANS} 次还是走不上路径")
            return
        self._replans += 1
        gen = self._gen
        target = self._target
        assert target is not None

        async def run() -> None:
            try:
                path, cm = await self._plan_from_here(target, "replan")
            except NavRequestError as exc:
                if gen == self._gen:
                    self._replan_out = ("fail", str(exc))
                return
            if gen == self._gen:
                self._replan_out = ("ok", (path, cm))
        self._replan = asyncio.get_running_loop().create_task(run())

    async def stop(self) -> None:
        self._gen += 1
        if self._replan is not None:
            self._replan.cancel()
        await super().stop()

    async def _enter_terminal(self, status: NavStatus) -> None:
        self._path = None
        self._aligning = False
        self._replan_out = None
        if self._replan is not None:
            self._replan.cancel()
            self._replan = None
        await super()._enter_terminal(status)

    # ------------------------------------------------------------ 跟踪

    def _project(self, x: float, y: float) -> tuple[int, float, tuple[float, float]]:
        """从当前段往后找最近点(只往前,不回头)→ (段号, 偏离距离, 最近点)。"""
        assert self._path is not None
        pts = self._path.points
        best = (self._seg, math.inf, pts[self._seg])
        for i in range(self._seg, len(pts) - 1):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(x - px, y - py)
            if d < best[1] - 1e-9:
                best = (i, d, (px, py))
        return best

    def _lookahead(self, seg: int, p: tuple[float, float]) -> tuple[float, float]:
        assert self._path is not None
        pts = self._path.points
        left = LOOKAHEAD_M
        cur = p
        for i in range(seg + 1, len(pts)):
            d = math.dist(cur, pts[i])
            if d >= left:
                f = left / d
                return (cur[0] + f * (pts[i][0] - cur[0]), cur[1] + f * (pts[i][1] - cur[1]))
            left -= d
            cur = pts[i]
        return pts[-1]

    def _turn(self, err: float) -> float:
        wz = max(-self._wmax, min(self._wmax, K_ANG * err))
        dead = getattr(self._caps, "deadband_wz", 0.0) or 0.0
        if 0 < abs(wz) < dead:
            wz = math.copysign(dead, wz)
        return wz

    async def _drive(self, here: Any, dt_s: float) -> None:
        target = self._target
        assert target is not None
        z = self.nogo_at(here.x, here.y)
        if z is not None:
            await self._inside_nogo(z, here)
            return
        out, self._replan_out = self._replan_out, None
        if out is not None:
            if out[0] == "fail":
                log.warning("重规划没成:%s", out[1])
                await self._enter_terminal(NavStatus.FAILED)
                return
            self._path, self._cm = out[1]
            self._seg = 0
            self.planned_length_m = self._path.length_m
        if self._replan is not None and not self._replan.done():
            await self._send(0.0, 0.0, dt_s)            # 原地等新路径
            return
        if self._path is None or self._cm is None:
            await self._enter_terminal(NavStatus.FAILED)
            return
        dgoal = math.hypot(target.position.x - here.x, target.position.y - here.y)
        if self._aligning or dgoal <= ARRIVE_TOL_M:
            self._aligning = True
            err = _wrap(target.yaw - here.yaw)
            if abs(err) <= YAW_TOL_RAD:
                await self._enter_terminal(NavStatus.SUCCEED)
                return
            await self._send(0.0, self._turn(err), dt_s)
            return
        seg, dev, proj = self._project(here.x, here.y)
        if dev > MAX_DEVIATION_M:
            log.warning("偏离路径 %.2f m,从当前位置重规划", dev)
            self._start_replan()
            await self._send(0.0, 0.0, dt_s)
            return
        self._seg = seg
        look = self._lookahead(seg, proj)
        bearing = _wrap(math.atan2(look[1] - here.y, look[0] - here.x) - here.yaw)
        wz = self._turn(bearing)
        if abs(bearing) > TURN_IN_PLACE_RAD:
            vx = 0.0
        else:
            limit = min(self._vmax, self._cm.speed_at(here.x, here.y),
                        self._cm.speed_at(*look))
            vx = max(min(limit, K_LIN * dgoal), self._caps.deadband_vx)
        await self._send(vx, wz, dt_s)

    # ------------------------------------------------------------ 给引擎的路长估计

    def path_length_hint(self, a: Pose, b: Pose) -> float | None:
        """``a`` 到 ``b`` 的规划路长(米),**不阻塞**:算过的直接给;没算过就在后台算、这次给 ``None``
        (引擎退回直线 × 绕路系数)。按 0.5 m 取整缓存,换图、换区域清空。"""
        key = tuple(round(v * 2) for v in (a.position.x, a.position.y,
                                            b.position.x, b.position.y))
        if key in self._len_cache:
            return self._len_cache[key]
        if self._base is None or (self._len_task is not None and not self._len_task.done()):
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        async def run() -> None:
            epoch = self._epoch
            try:
                cm = await self._costmap_async(0.0)
                p = await self._planner.plan(cm, (a.position.x, a.position.y),
                                             (b.position.x, b.position.y))
            except (PlanError, NavRequestError):
                return
            if epoch == self._epoch:
                self._len_cache[key] = p.length_m
        self._len_task = loop.create_task(run())
        return None
