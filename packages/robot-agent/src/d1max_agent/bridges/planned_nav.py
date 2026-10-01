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
from d1max_agent.localization import DRIFT_XY_PER_M, OdomAnchor
from d1max_agent.planning import costmap as cmod
from d1max_agent.planning.astar import PlanError
from d1max_agent.planning.costmap import Costmap, CostmapError
from d1max_agent.planning.planner import PlannedPath, Planner
from d1max_contract.hal import RobotHAL
from d1max_contract.zones import Zone, ZoneSet, distance_to_polygon
from d1max_patrol.backends.base import (
    AlgErrorEvent,
    NavCancelledError,
    NavNotLocalizedError,
    NavRequestError,
)
from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import ALG_LIDAR_DISCONNECTED, LocStatus, NavStatus, Pose

log = logging.getLogger(__name__)

LOOKAHEAD_M = 0.6
#: 离拐点这么近就换下一段(W10 外审 1:前视点只在当前这一段上找,不越过拐点抄近路切角)。
VERTEX_TOL_M = 0.1
#: 限速看前面多长一段路(W10 外审 4:沿路逐格取最低,留出减速距离;0.6 m/s 刹停约 0.2 m)。
SPEED_LOOK_M = 1.0
ARRIVE_TOL_M = 0.15
YAW_TOL_RAD = 0.15
TURN_IN_PLACE_RAD = 0.5
#: 被挡(W11,W08 决定 8):挡了这么久就把挡住的格子记进临时障碍层、绕;之后每隔这么久再试;
#: 满这么久还没走成就放弃(FAILED,引擎按点位失败走);满这么久发一次事件。临时障碍这么久过期。
BLOCK_DETOUR_S = 2.0
BLOCK_RETRY_S = 5.0
BLOCK_GIVEUP_S = 20.0
BLOCK_REPORT_S = 5.0
TEMP_OBSTACLE_S = 30.0
#: 原地转被「看不见」挡住(机身两侧是两台半球雷达的盲带,刚起来、站久了没有记忆):前面看得清是空的,
#: 就笔直往前挪这么远(这么快)再转 —— 挪过一个机身长,两侧就在刚才看过的范围里了。每次被挡最多挪一次。
CREEP_M = 1.0
CREEP_V = 0.2
#: 偏离路径多远就停下重规划(W10 内审应修 5:原来 1 m,比膨胀半径还大)。
MAX_DEVIATION_M = 0.3
#: 狗身中心离禁行区这么近就算压进去了(机身半宽 0.24 m;规划出的路离禁行区至少外接圆半径)。
ZONE_STOP_M = 0.25
MAX_REPLANS = 3
MAX_SIGMA_MARGIN_M = 0.5
K_LIN = 1.0
K_ANG = 2.0
#: 近障降速(W12,人也是障碍;认不出人):行进方向前面、机身两边各多 :data:`NEAR_BAND_M`
#: 的带子里最近的「挡」
#: 在 :data:`NEAR_SLOW_FROM_M` 以内就开始降,到 :data:`NEAR_SLOW_AT_M` 降到 :data:
#: `NEAR_MIN_MPS`(线性)。
#: 刹停归守卫(按实测速度);这里只是让狗在人边上不急着走。
NEAR_BAND_M = 0.5
NEAR_SLOW_FROM_M = 2.0
NEAR_SLOW_AT_M = 0.5
NEAR_MIN_MPS = 0.25


def _heading(yaw: float, d: int) -> float:
    """行进方向的朝向:狗头为前就是机身朝向,狗尾为前加 π。"""
    return yaw if d >= 0 else _wrap(yaw + math.pi)


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
        #: 正在核对新区域(W10 内审阻断 2):核完之前原地等,不拿旧代价图沿旧路走。
        self._hold = False
        #: 禁行区的包围盒(每拍查「狗在不在禁行区里」先按它筛,内审小 13)。
        self._boxes: list[tuple[float, float, float, float, Zone]] = []
        #: 路长估计单用一个规划器(内审应修 7:不跟 goto、重规划抢同一个子进程)。
        self._hint_planner: Planner | None = None
        #: 局部避障(W11):感知的滚动记忆与守卫;``None`` = 没配(``--obstacles none``)。
        self.obstacles: Any = None
        self.guard: Any = None
        self._v_meas = 0.0
        self._odom_pose: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._blocked_since: float | None = None
        self._next_detour = 0.0
        self._blocked_told = False
        self._detoured = False
        self._replan_kind = ""
        self._creep_from: tuple[float, float] | None = None
        self._crept = False
        #: 临时障碍层:地图规划栅格的格 → 过期时刻(秒,后端的钟)。
        self._temp: dict[tuple[int, int], float] = {}

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
        if self._hint_planner is not None and self._hint_planner is not self._planner:
            self._hint_planner.close()

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
            now = self._secs()
            temp = [k for k, t in tuple(self._temp.items()) if t > now]   # 过期的不算(内审阻断 2)
            if temp:
                blocked = blocked.copy()
                h, w = blocked.shape
                for (r, c) in temp:
                    if 0 <= r < h and 0 <= c < w:
                        blocked[r, c] = True
            cm = cmod.build(blocked, res, origin, self._effective(zs),
                            robot_radius_m=self.robot_radius_m + cmod.INFLATE_MARGIN_M,
                            nogo_margin_m=key * 0.1)
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

    def _effective(self, zs: ZoneSet | None) -> tuple[Zone, ...]:
        """规划用的区域:限速低于这台狗的前进死区的限速区守不住(再慢就不走了,内审应修 6),当禁行。"""
        if zs is None:
            return ()
        dead = self._caps.deadband_vx
        return tuple(Zone(z.id, "nogo", z.polygon, z.label)
                     if z.kind == "slow" and (z.max_speed_mps or 0.0) < dead else z
                     for z in zs.zones)

    def _rebox(self) -> None:
        self._boxes = [(min(p[0] for p in z.polygon), min(p[1] for p in z.polygon),
                        max(p[0] for p in z.polygon), max(p[1] for p in z.polygon), z)
                       for z in (self.zones.nogo() if self.zones is not None else ())]

    def nogo_near(self, x: float, y: float, d: float = 0.0) -> Zone | None:
        """离哪个禁行区不到 ``d`` 米(``d=0`` 就是在里面);先按包围盒筛。"""
        for x0, y0, x1, y1, z in self._boxes:
            if x0 - d <= x <= x1 + d and y0 - d <= y <= y1 + d \
                    and distance_to_polygon(x, y, z.polygon) <= d:
                return z
        return None

    def nogo_at(self, x: float, y: float) -> Zone | None:
        return self.nogo_near(x, y, 0.0)

    async def set_zones(self, zs: ZoneSet | None) -> None:
        """换上一份区域(收紧还是放宽由运行时判;放宽的它等狗空闲再调)。正在走的:核对完之前原地等
        (内审阻断 2:算新代价图要零点几秒,期间不许沿旧路走进新禁行区);狗压进新禁行区 → 停、FAILED、
        发事件;路径碰上新的致命格 → 重规划。空闲的狗被圈在里面:也发事件(内审小 17)。"""
        self.zones = zs
        self._invalidate()
        self._rebox()
        moving = self._status in (NavStatus.ACTIVE, NavStatus.PAUSE, NavStatus.INITIALIZING)
        if moving:
            self._hold = True
        try:
            here = await self._here()
            z = None if here is None else self.nogo_near(here.x, here.y, ZONE_STOP_M)
            if z is not None:
                if self._status in (NavStatus.ACTIVE, NavStatus.PAUSE, NavStatus.INITIALIZING):
                    await self._inside_nogo(z, here)
                else:
                    self._report_nogo(z, here, idle=True)
                return
            if not moving:
                return
            cm = await self._costmap_async(self._margin(here, self._target))
            if self._path is None or self._status not in (NavStatus.ACTIVE, NavStatus.PAUSE,
                                                          NavStatus.INITIALIZING):
                return                          # 核对期间走到头、失败、被叫停了
            here = await self._here()
            self._cm = cm
            if self._crosses(cm, here):
                log.warning("新区域挡住了正在走的路,原地等、重规划")
                self._start_replan()
        finally:
            self._hold = False

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

    def _report_nogo(self, z: Zone, here: Any, *, idle: bool = False) -> None:
        log.error("狗在禁行区 %s 里面(或压上了边):原地停,等人处理", z.id)
        if self.on_event is not None:
            self.on_event("inside_nogo", {"zone": z.id, "label": z.label, "idle": idle,
                                          "x": round(here.x, 2), "y": round(here.y, 2)})

    async def _inside_nogo(self, z: Zone, here: Any) -> None:
        self._report_nogo(z, here)
        await self._enter_terminal(NavStatus.FAILED)

    # ------------------------------------------------------------ 规划

    async def _here(self) -> Any:
        o = await self._hal.odometry()
        return self.anchor.estimate((o.x, o.y, o.yaw))

    def _margin(self, est: Any, goal: Pose | None = None) -> float:
        """禁行区按定位 σ 加宽(W08 决定 4)。里程锚定的 σ 边走边涨(内审应修 4):按走到终点时的 σ 算
        (现在的 σ + 每米漂移 × 直线距离),最多加宽 0.5 m。"""
        s = getattr(est, "sigma_xy_m", 0.0) if est is not None else 0.0
        if not isinstance(s, (int, float)) or not math.isfinite(s):
            return MAX_SIGMA_MARGIN_M
        s = max(float(s), 0.0)
        anchor = getattr(self, "anchor", None)
        if goal is not None and est is not None and isinstance(anchor, OdomAnchor) \
                and not anchor.identity:
            s += DRIFT_XY_PER_M * math.hypot(goal.position.x - est.x, goal.position.y - est.y)
        return min(s, MAX_SIGMA_MARGIN_M)

    async def _plan_from_here(self, pose: Pose, op: str) -> tuple[PlannedPath, Costmap]:
        if self._base is None:
            raise NavRequestError(op, f"规划不了:{self.plan_problem}")
        if self.obstacles is not None:
            st = self.obstacles.state()
            if st not in ("ok", "stale"):
                from d1max_agent.obstacles import describe
                raise NavRequestError(op, f"避障用不了:障碍数据{describe(st)}"
                                          + (f"({self.obstacles.reason})"
                                             if self.obstacles.reason else ""))
        here = await self._here()
        if here is None or self._loc is LocStatus.LOC_LOST:
            raise NavNotLocalizedError(op, "没有可信定位,不规划")
        z = self.nogo_at(here.x, here.y)
        if z is not None:
            raise NavRequestError(op, f"狗在禁行区 {z.id} 里面,不自己往外走")
        for _ in range(3):
            epoch = self._epoch
            cm = await self._costmap_async(self._margin(here, pose))
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
        self._expire_temp()
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
            raise NavCancelledError(op, "规划期间被叫停,结果作废")
        if self._status is not NavStatus.STANDBY:
            raise NavRequestError(op, f"规划完状态变了({self._status.value}),结果作废")
        self._path, self._cm, self._seg = path, cm, 0
        self._aligning = False
        self._replans = 0
        self._replan_out = None
        self.planned_length_m = path.length_m
        log.info("%s:规划出 %d 个点、%.1f m", op, len(path.points), path.length_m)
        await super().goto(pose)

    def _start_replan(self, *, detour: bool = False) -> None:
        if self._replan is not None and not self._replan.done():
            return
        self._replan_kind = "detour" if detour else ""
        if detour:
            pass                         # 绕障不算偏离重规划的次数(被挡另有 20 s 的账)
        elif self._replans >= MAX_REPLANS:
            self._replan_out = ("fail", f"重规划了 {MAX_REPLANS} 次还是走不上路径", 0)
            return
        else:
            self._replans += 1
        gen = self._gen
        target = self._target
        assert target is not None

        async def run() -> None:
            try:
                path, cm = await self._plan_from_here(target, "replan")
            except NavRequestError as exc:
                if gen == self._gen:
                    self._replan_out = ("fail", str(exc), 0)
                return
            if gen == self._gen:
                self._replan_out = ("ok", (path, cm), self._epoch)
        self._replan = asyncio.get_running_loop().create_task(run())

    async def stop(self) -> None:
        self._gen += 1
        if self._replan is not None:
            self._replan.cancel()
        await super().stop()

    async def _enter_terminal(self, status: NavStatus) -> None:
        if self._temp:
            self._temp = {}                              # 这一段的临时障碍不带到下一段(内审阻断 2)
            self._invalidate()
        self._creep_from = None
        self._crept = False
        self._blocked_since = None
        self._blocked_told = False
        self._detoured = False
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
        """前视点:从投影点沿**当前这一段**往前 ``LOOKAHEAD_M``,不越过这一段的终点(拐点)——
        越过拐点去追下一段的点,狗就从拐角内侧抄近路,离障碍比规划的近(W10 外审 1)。"""
        assert self._path is not None
        end = self._path.points[seg + 1]
        d = math.dist(p, end)
        if d <= LOOKAHEAD_M:
            return end
        f = LOOKAHEAD_M / d
        return (p[0] + f * (end[0] - p[0]), p[1] + f * (end[1] - p[1]))

    def _path_speed(self, seg: int, p: tuple[float, float]) -> float:
        """前面 ``SPEED_LOOK_M`` 这段路(沿路径,拐点也算)经过的每一格(超覆盖)里最低的限速:窄限速带
        夹在现在的位置和前视点之间也跨不过去(W10 外审 4)。"""
        from d1max_agent.planning.astar import iter_line
        assert self._path is not None and self._cm is not None
        cm = self._cm
        pts = self._path.points
        best = math.inf
        left = SPEED_LOOK_M
        cur = p
        for i in range(seg + 1, len(pts)):
            nxt = pts[i]
            d = math.dist(cur, nxt)
            if d > left:
                f = left / d
                nxt = (cur[0] + f * (nxt[0] - cur[0]), cur[1] + f * (nxt[1] - cur[1]))
            a, b = cm.cell_of(*cur), cm.cell_of(*nxt)
            if a is not None and b is not None:
                for r, c in iter_line(a, b):
                    best = min(best, float(cm.speed[r, c]))
            left -= d
            cur = nxt
            if left <= 0:
                break
        rc = cm.cell_of(*p)
        return min(best, float(cm.speed[rc])) if rc is not None else best

    def _turn(self, err: float) -> float:
        wz = max(-self._wmax, min(self._wmax, K_ANG * err))
        dead = getattr(self._caps, "deadband_wz", 0.0) or 0.0
        if 0 < abs(wz) < dead:
            wz = math.copysign(dead, wz)
        return wz

    async def _drive(self, here: Any, dt_s: float) -> None:
        target = self._target
        assert target is not None
        if self._hold:
            await self._send(0.0, 0.0, dt_s)            # 正在核对新区域:原地等
            return
        z = self.nogo_near(here.x, here.y, ZONE_STOP_M)
        if z is not None:
            await self._inside_nogo(z, here)
            return
        out, self._replan_out = self._replan_out, None
        if out is not None:
            if out[0] == "fail":
                if self._replan_kind == "detour":
                    log.info("绕不过去:%s;接着等、过一会儿再试", out[1])
                    await self._send(0.0, 0.0, dt_s)
                    return
                log.warning("重规划没成:%s", out[1])
                await self._enter_terminal(NavStatus.FAILED)
                return
            if out[2] != self._epoch:
                # 重规划完到这一拍之间区域又换了(内审应修 9):旧区域上的路不装,再来一次
                self._start_replan()
                await self._send(0.0, 0.0, dt_s)
                return
            self._path, self._cm = out[1]
            self._seg = 0
            self.planned_length_m = self._path.length_m
            if self._replan_kind == "detour":
                self._detoured = True
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
            await self._move(0.0, self._turn(err), dt_s, here)
            return
        seg, dev, proj = self._project(here.x, here.y)
        if dev > MAX_DEVIATION_M:
            log.warning("偏离路径 %.2f m,从当前位置重规划", dev)
            self._start_replan()
            await self._send(0.0, 0.0, dt_s)
            return
        pts = self._path.points
        while seg + 2 < len(pts) and math.dist((here.x, here.y), pts[seg + 1]) <= VERTEX_TOL_M:
            seg += 1                                     # 到了拐点:换下一段(对不准就原地转)
            proj = pts[seg]
        self._seg = seg
        look = self._lookahead(seg, proj)
        # 行进方向(W09i):狗尾为前时拿狗尾那头对着路走(朝向加 π)、往后退;到点对朝向仍按机身(拍照方向)
        d = self._dir or 1
        bearing = _wrap(math.atan2(look[1] - here.y, look[0] - here.x) - _heading(here.yaw, d))
        wz = self._turn(bearing)
        if abs(bearing) > TURN_IN_PLACE_RAD:
            vx = 0.0
        else:
            limit = min(self._vmax, self._path_speed(seg, proj),
                        self._cm.speed_at(here.x, here.y), self._near_cap(d))
            vx = d * max(min(limit, K_LIN * dgoal), self._caps.deadband_vx)
        await self._move(vx, wz, dt_s, here)

    # ------------------------------------------------------------ 避障(W11)

    def _near_cap(self, d: int) -> float:
        """近障降速(W12):前面最近的「挡」越近越慢;没配避障、前面没东西不限。"""
        if self.obstacles is None or self.guard is None:
            return math.inf
        g = self.guard
        margin = getattr(g, "margin", 0.05)
        near = self.obstacles.near_ahead(d, getattr(g, "body_len", 0.93) / 2 + margin,
                                         getattr(g, "body_wid", 0.48) / 2 + margin + NEAR_BAND_M)
        if near >= NEAR_SLOW_FROM_M:
            return math.inf
        lo = max(NEAR_MIN_MPS, self._caps.deadband_vx)
        f = max(0.0, (near - NEAR_SLOW_AT_M) / (NEAR_SLOW_FROM_M - NEAR_SLOW_AT_M))
        return lo + (max(self._vmax, lo) - lo) * f

    def tail_ok(self) -> bool:
        """狗尾为前能不能自己走(W09i):配了避障、感知报后雷达外参标过 —— 标过它的「空」才算,
        守卫往后扫
        才看得清;没标定的话身后全是「看不见」,守卫原地等到放弃,还不如一开始就不派。"""
        return (self.obstacles is not None and self.guard is not None
                and bool(getattr(self.obstacles, "rear_cal", False)))

    def _odom_seen(self, odom: Any) -> None:
        self._v_meas = float(getattr(odom, "vx", 0.0) or 0.0)
        self._odom_pose = (odom.x, odom.y, odom.yaw)
        if self.obstacles is not None:
            self.obstacles.note_odom(odom.x, odom.y, odom.yaw)

    def _secs(self) -> float:
        """被挡计时、临时障碍的过期用单调钟(内审小:墙钟一对时就跳);没配避障就按后端的钟。"""
        if self.obstacles is not None:
            return float(self.obstacles.monotonic())
        return self._now() / 1000.0

    def _expire_temp(self) -> None:
        """临时障碍到期了就忘掉、代价图重算(内审阻断 2:原来只在下一次被挡时才清)。"""
        if not self._temp:
            return
        now = self._secs()
        if min(self._temp.values()) <= now:
            self._temp = {k: t for k, t in self._temp.items() if t > now}
            self._invalidate()

    async def _move(self, vx: float, wz: float, dt_s: float, here: Any) -> None:
        """一条会动的命令:配了避障就先过守卫(W08 决定 8 第一层);挡了原地等、自己计时、自己绕,
        **不发 13330**;障碍数据断了太久发 13331(雷达掉线,引擎中止)。"""
        if self.obstacles is None or self.guard is None:
            await self._send(vx, wz, dt_s)
            return
        self._expire_temp()
        if self._creep_from is not None:
            moved = math.hypot(self._odom_pose[0] - self._creep_from[0],
                               self._odom_pose[1] - self._creep_from[1])
            if moved < CREEP_M:
                vx, wz = (self._dir or 1) * CREEP_V, 0.0  # 往前挪着看两侧(守卫照样查)
            else:
                log.info("往前挪了 %.1f m,两侧看过了,接着转", moved)
                self._creep_from = None
        if self.obstacles.state() in ("lost", "extrinsic_bad", "none"):
            from d1max_agent.obstacles import describe
            why = f"障碍数据{describe(self.obstacles.state())}"
            log.error("%s:停", why)
            self.emit(AlgErrorEvent((AlgErrorItem(ALG_LIDAR_DISCONNECTED, why, 2),),
                                    self._now()))
            await self._enter_terminal(NavStatus.FAILED)
            return
        v = self.guard.check(vx, wz, self._v_meas, self.obstacles, self._odom_pose)
        if v.ok:
            if self._blocked_since is not None:
                waited = self._secs() - self._blocked_since
                if self.on_event is not None and (self._blocked_told or self._detoured):
                    self.on_event("nav_unblocked", {"waited_s": round(waited, 1),
                                                    "detour": self._detoured})
                self._blocked_since = None
                self._blocked_told = False
                self._detoured = False
                if self._creep_from is None:
                    self._crept = False
            await self._send(vx, wz, dt_s)
            return
        await self._blocked(v, here, dt_s, vx)

    async def _blocked(self, v: Any, here: Any, dt_s: float, vx: float = 0.0) -> None:
        now = self._secs()
        if self._blocked_since is None:
            self._blocked_since = now
            self._next_detour = now + BLOCK_DETOUR_S
            log.info("被挡:%s", v.reason)
        waited = now - self._blocked_since
        await self._send(0.0, 0.0, dt_s)                 # 原地等(零速保住步态)
        if waited >= BLOCK_GIVEUP_S:
            log.warning("被挡 %.0f s 还没走成:放弃这一段(%s)", waited, v.reason)
            await self._enter_terminal(NavStatus.FAILED)
            return
        if waited >= BLOCK_REPORT_S and not self._blocked_told:
            self._blocked_told = True
            if self.on_event is not None:
                self.on_event("nav_blocked", {"reason": v.reason, "x": round(here.x, 2),
                                              "y": round(here.y, 2)})
        if (self._creep_from is None and not self._crept and not v.hits and abs(vx) < 1e-6
                and not self._aligning and waited >= BLOCK_DETOUR_S
                and self.guard.check((self._dir or 1) * CREEP_V, 0.0, self._v_meas,
                                     self.obstacles, self._odom_pose).ok
                and self._creep_clear(here)):
            # 原地转只被「看不见」挡住、前面看得清是空的:往前挪一个机身长再转
            log.info("原地转被看不见的格子挡住(机身两侧没看过):先往前挪 %.1f m", CREEP_M)
            self._creep_from = (self._odom_pose[0], self._odom_pose[1])
            self._crept = True
            return
        if self._creep_from is not None:
            self._creep_from = None                     # 挪的时候前面也挡了:不挪了,接着等
        if now >= self._next_detour and v.hits and self._base is not None:
            self._next_detour = now + BLOCK_RETRY_S
            self._remember(v.hits, here, now)
            self._start_replan(detour=True)

    def _creep_clear(self, here: Any) -> bool:
        """往前挪那一段(挪的距离 + 机身前沿)在地图上也得能走:不碰致命格、离禁行区够远(内审阻断 1:
        原来只看守卫,禁行区要等狗身中心压上去才停 —— 落差在真机验过之前只靠禁行区挡)。"""
        cm = self._cm
        if cm is None:
            return False
        from d1max_agent.planning.astar import LETHAL, iter_line
        h = _heading(here.yaw, self._dir or 1)
        c, s = math.cos(h), math.sin(h)
        reach = CREEP_M + self.guard.body_len / 2 + self.guard.margin
        end = (here.x + c * CREEP_M, here.y + s * CREEP_M)
        a, b = cm.cell_of(here.x, here.y), cm.cell_of(*end)
        if a is None or b is None:
            return False
        if any(cm.cost[r, k] == LETHAL and not (r, k) == a for r, k in iter_line(a, b)):
            return False
        for i in range(int(reach / 0.05) + 1):
            d = i * 0.05
            if self.nogo_near(here.x + c * d, here.y + s * d, ZONE_STOP_M) is not None:
                return False
        return True

    def _remember(self, hits: Any, here: Any, now: float) -> None:
        """挡住的点(此刻狗身系)→ 地图规划栅格的格,记进临时障碍层(过 30 s 忘掉);
        换了就得重算代价图。"""
        assert self._base is not None
        _, res, origin = self._base
        c, s = math.cos(here.yaw), math.sin(here.yaw)
        self._temp = {k: t for k, t in self._temp.items() if t > now}
        for px, py in hits:
            mx, my = here.x + c * px - s * py, here.y + s * px + c * py
            self._temp[(int(math.floor((my - origin[1]) / res)),
                        int(math.floor((mx - origin[0]) / res)))] = now + TEMP_OBSTACLE_S
        self._invalidate()

    # ------------------------------------------------------------ 给引擎的路长估计

    def path_length_hint(self, a: Pose, b: Pose) -> float | None:
        """``a`` 到 ``b`` 的规划路长(米),**不阻塞**:算过的直接给;没算过就在后台算、这次给 ``None``
        (引擎退回直线 × 绕路系数)。按 0.5 m 取整缓存,换图、换区域清空。"""
        key = tuple(round(v * 2) for v in (a.position.x, a.position.y,
                                            b.position.x, b.position.y))
        if key in self._len_cache:
            return self._len_cache[key]
        if self._base is None or self._planning \
                or (self._len_task is not None and not self._len_task.done()):
            return None
        if self._hint_planner is None:
            # 测试注入的规划器(线程里跑)就共用;真跑的另起一个子进程,不跟 goto 抢
            self._hint_planner = (self._planner if getattr(self._planner, "_in_process", False)
                                  else Planner(timeout_s=10.0))
        hp = self._hint_planner
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        async def run() -> None:
            epoch = self._epoch
            try:
                cm = await self._costmap_async(0.0)
                p = await hp.plan(cm, (a.position.x, a.position.y),
                                  (b.position.x, b.position.y))
            except (PlanError, NavRequestError):
                return
            if epoch == self._epoch:
                self._len_cache[key] = p.length_m
        self._len_task = loop.create_task(run())
        return None
