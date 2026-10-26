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

**每拍的禁行区回退检查**(能力对账 S7,2026-10-14;原来只看狗身中心离禁行区 0.25 m,机头正对着走过去
能先进去约 0.2 m 才停):按**机身矩形**(:mod:`d1max_agent.zone_guard`)、**定位 σ**、**此刻速度下的刹车
距离**查,跟规划、「先挪」一样把限速低到守不住的限速区当禁行(:meth:`_effective`;原来回退检查和保持
距离用的 ``nogo_near`` 只看原始禁行区):

- 机身已经压上 → 停、FAILED、事件 ``inside_nogo``(站点 P1),跟原来一样;
- 照现在的速度再走一拍、再刹停,机身扫过的那一段离(当禁行的)区域不到 σ + :data:`BACKSTOP_MARGIN_M`、
  而且不是越走越远 → **刹车**(零速)、从此刻的位置按此刻的 σ 重规划(路会先离开它);重规划不出、
  次数用光 → FAILED。还没进去,不发 P1;
- 正常走的时候不让上面那条被碰到:往禁行区那边走时按刹车距离**减速**,留出 σ + :data:`BRAKE_MARGIN_M`
  (:meth:`_zone_cap`)。规划出的路离禁行区 0.57 m(外接圆 + 余量,再按 σ 加宽),平着走、绕角都碰不到;
  正对着禁行区走到拐点会慢下来(刹车距离的占位数是保守的,真机量,3e.12)。原地转不在这里管(规划按
  外接圆膨胀)。

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

from d1max_agent import zone_guard as zg
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
#: 原地转、或者带转向的弧线被「看不见」挡住(机身两侧是两台半球雷达的盲带,刚起来、站久了没有
#: 记忆;转向时机身角甩进去):前面看得清是空的,就笔直往前挪这么远(这么快)再接着走 —— 挪过一个
#: 机身长,两侧就在刚才看过的范围里了。每次被挡最多挪一次。W34:2026-10-08 C40221 起步是弧线,
#: 以前只有纯原地转才挪,前后雷达都标过也起不了步。
#: W35(2026-10-13):原来挪 1.0 m(一个机身长 0.93 m 多一点)—— 不够。挪完要原地转,转身扫的是外接圆
#: (半径 0.59 m);圆里最靠后的那一条,在起步时离机头不到一格,正好落在机头前那圈看不见的地方
#: (雷达从机头前 0.47 m 起才看得见,栅格 0.1 m)。挪完转到一半被**一格**看不见的卡住,原地等、
#: 两侧的记忆 10 s 过期,再也转不动,等满 20 s 放弃(现场日志「有挡 0 处、看不见 1 处」)。
#: 要挪过:外接圆半径 0.59 + 机头盲区 0.47 + 一格 0.1 + 感知延迟那几厘米 ≈ 1.2,取 1.3。
CREEP_M = 1.3
CREEP_V = 0.2
#: W41(2026-10-26 C40221):「挪」往哪边、挪不挪 —— 现场两个毛病:①正前方被挡时往前挪不动,转身又被
#: 两侧看不见拦住,原地等满 20 s 放弃;②离点只剩 0.2 m 也往前挪满 1.3 m,沿当时的朝向冲过头 1 m 多、
#: 往展台那侧偏 0.7 m;终点在身后也背着终点挪。现在按这个顺序挑:
#:   1. 往前挪**顺路**(挪完离路径不超过 :data:`CREEP_OFF_PATH_M`、而且挪完离终点还有一段)→ 往前;
#:   2. 不顺路就**往后退**一个机身长(后雷达看着、跟往前挪一样查地图和区域)—— 后雷达标过才过得了守卫;
#:   3. 退不了,往前挪不会冲过终点(终点在身后、或者远在 1.3 m 之外)→ 还按老办法往前挪;
#:   4. 都不行 → 原地等(到时候放弃)。
CREEP_OFF_PATH_M = 0.4
#: 同一段最多挪几次(往后退完又走回来、两侧还是看不见:不来回折腾)。
MAX_CREEPS = 3
#: 对朝向的时候被挪开(往后退)、或者定位一跳,离点超过到点容差 + 这么多 → 不算在对朝向了,重新走过去
#: (W41:原来进了「对朝向」就一直算在点上,挪了 1.3 m 照样记「到了」,狗停在 0.93 m 外)。
REALIGN_M = 0.15
#: 偏离路径多远就停下重规划(W10 内审应修 5:原来 1 m,比膨胀半径还大)。
MAX_DEVIATION_M = 0.3
#: 每拍回退检查(S7):机身扫过的那一段(此刻速度再走一拍、再刹停)离当禁行的区域不到 σ + 这么多、而且
#: 不是越走越远 → 刹车、重规划(原来是「狗身中心离禁行区 0.25 m」:机身半宽 0.24 m,机头正对着能先
#: 进去 0.2 m)。
BACKSTOP_MARGIN_M = 0.02
#: 往禁行区那边走时按刹车距离减速,留出 σ + 这么多(比上面那条宽,正常走不会碰到它)。
BRAKE_MARGIN_M = 0.10
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
#: 限着速(W29 下雨、雷暴)时近障减速从多远开始的倍数。
WET_NEAR_SCALE = 1.5


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
        #: 禁行区的包围盒(每拍查「狗在不在禁行区里」先按它筛,内审小 13)。含当禁行的限速区(S7)。
        self._boxes: list[tuple[float, float, float, float, Zone]] = []
        #: 同一批(当禁行的)区域,给按机身查的回退检查用(S7)。
        self._closed: list = []
        #: 最近一拍发出去的前进速度(回退检查按它和实测取大算刹车距离)。
        self._last_vx = 0.0
        self._near_told = False
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
        #: 这次挪的方向:+1 顺着行进方向往前,−1 往后退(W41)。
        self._creep_way = 1
        self._crept = False
        #: 这一段挪了几次(W41,:data:`MAX_CREEPS`)。
        self._creeps = 0
        #: 挪完了、下一次偏离重规划不算次数(挪本来就是离开路径)。
        self._after_creep = False
        #: 最近一次「真有挡」的原因和时刻(W41:放弃时只写最后一次转身的「有挡 0 处」会误导)。
        #: 现场是被箱子挡 → 2 s 绕行、开始转(算「不被挡」了一下)→ 转身被两侧看不见拦住:
        #: 跨过中间那一下也要记得。
        self._hit_reason = ""
        self._hit_at = -math.inf
        #: 临时障碍层:地图规划栅格的格 → 过期时刻(秒,后端的钟)。
        self._temp: dict[tuple[int, int], float] = {}

    # ------------------------------------------------------------ 图与区域

    def _invalidate(self) -> None:
        self._epoch += 1
        self._cms = {}
        self._len_cache = {}

    def load_grid(self, map_dir: Path | None) -> None:
        """读 + 装(阻塞)。坏了记原因、不能规划,不抛。运行时换图不用它:读放线程里(:meth:`read_grid`),
        装在事件循环里且只装当前这次换图的(:meth:`install_grid`,外审 W39-I1)。"""
        self.install_grid(*self.read_grid(map_dir))

    @staticmethod
    def read_grid(map_dir: Path | None) -> tuple[Any, str | None]:
        """读、算规划栅格,**不碰后端**(可以放线程里,晚回来的结果由调用方决定装不装)。回
        ``(栅格 | None, 问题 | None)``,不抛。"""
        if map_dir is None:
            return None, "没有规划栅格"
        try:
            free, res, origin = cmod.load_floor(Path(map_dir))
            blocked, pres = cmod.downsample(free, res)
        except (OSError, CostmapError, ValueError) as exc:
            log.error("规划栅格载不了:%s", exc)
            return None, f"规划栅格载不了:{exc}"[:200]
        return (blocked, pres, origin), None

    def install_grid(self, base: Any, problem: str | None) -> None:
        """装上 :meth:`read_grid` 的结果(事件循环里调)。"""
        self._invalidate()
        self._base, self.plan_problem = base, problem

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
        # S7(能力对账附录 A 说明 3):跟规划、「先挪」同一个口径 —— 限速低到守不住的限速区也当禁行。
        # 原来这里只取原始禁行区,每拍回退检查和保持距离(``nogo_near``)都漏了那一类。
        # 抹掉区(地图台账 44)对狗就是禁行区
        closed = [z for z in self._effective(self.zones) if z.keep_out]
        self._boxes = [(min(p[0] for p in z.polygon), min(p[1] for p in z.polygon),
                        max(p[0] for p in z.polygon), max(p[1] for p in z.polygon), z)
                       for z in closed]
        self._closed = zg.boxed(closed)

    def nogo_near(self, x: float, y: float, d: float = 0.0) -> Zone | None:
        """离哪个禁行区(含当禁行的限速区)不到 ``d`` 米(``d=0`` 就是在里面);先按包围盒筛。"""
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
            verdict, z = ("", None) if here is None else self._backstop(here)
            if verdict == "inside":
                if self._status in (NavStatus.ACTIVE, NavStatus.PAUSE, NavStatus.INITIALIZING):
                    await self._inside_nogo(z, here)
                else:
                    self._report_nogo(z, here, idle=True)
                return
            # 离新区域太近(还没压上):核对期间原地等着(``_hold``),下面照常看路要不要重规划;
            # 每拍的回退检查接着管
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
                self._start_replan(why="zones")
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
        await self._fail(f"狗进了禁行区 {z.label or z.id}:停,不自己往外走")

    async def _near_nogo(self, z: Zone, here: Any, dt_s: float) -> None:
        """S7:再走就离(当禁行的)区域太近了 —— 刹车,从这儿按此刻的 σ 重规划(路先离开它);重规划
        不出来、次数用光,下一拍按重规划失败收尾。"""
        if not self._near_told:
            self._near_told = True
            log.warning("离禁行区 %s 太近(机身 + 定位偏差 %.2f m + 刹车距离):刹车、重规划",
                        z.id, zg.sigma_of(here))
        await self._send(0.0, 0.0, dt_s)
        if (self._replan is None or self._replan.done()) and self._replan_out is None \
                and self._target is not None:
            self._start_replan(why="near_nogo")

    # ------------------------------------------------------------ 每拍的禁行区回退检查(S7)

    def _travel(self, here: Any) -> tuple[tuple[float, float, float], float]:
        """``(此刻位姿, 平移方向)``:狗尾为前时往狗尾那头走(朝向加 π)。"""
        return (here.x, here.y, here.yaw), _heading(here.yaw, self._dir or 1)

    def _backstop(self, here: Any) -> tuple[str, Zone | None]:
        """S7:``("inside", 区域)`` 机身已经压上(当禁行的)区域;``("near", 区域)`` 照现在的速度
        再走一拍、再刹停,机身扫过的那一段离它不到 σ + :data:`BACKSTOP_MARGIN_M`、而且不是越走越远
        (更近、平着走都算);都没有 ``("", None)``。站着不动不算「near」—— 原地转靠规划的
        外接圆膨胀管。"""
        if not self._closed:
            return "", None
        pose, h = self._travel(here)
        need = zg.sigma_of(here) + BACKSTOP_MARGIN_M
        v = max(abs(self._v_meas), abs(self._last_vx))
        d = v * zg.TICK_S + zg.stop_distance(v)
        poses = zg.sweep(*pose, d, 0.0, heading=h)[1:] if d > 1e-6 else []
        for z in self._closed:
            c0 = zg.clearance(pose, z, need)
            if c0 <= 0.0:
                return "inside", z.zone
            for p in poses:
                c = zg.clearance(p, z, need)
                if c < need and c <= c0 + 1e-3:
                    return "near", z.zone
        return "", None

    def _zone_cap(self, here: Any) -> float:
        """往(当禁行的)区域那边走时的限速(S7):再走一拍、再刹停,机身扫过的那一段离它留出
        σ + :data:`BRAKE_MARGIN_M`(越走越近的那一段才算;平着走、往外走不限)。"""
        if not self._closed:
            return math.inf
        pose, h = self._travel(here)
        need = zg.sigma_of(here) + BRAKE_MARGIN_M
        vmax = self._caps.max_vx
        dmax = vmax * zg.TICK_S + zg.stop_distance(vmax)
        poses = zg.sweep(*pose, dmax, 0.0, heading=h)
        n = len(poses) - 1
        free = dmax
        for z in self._closed:
            c0 = zg.clearance(pose, z, need)
            for i, p in enumerate(poses[1:], 1):
                c = zg.clearance(p, z, need)
                if c < need and c < c0 - 1e-3:
                    free = min(free, dmax * (i - 1) / n)
                    break
        if free >= dmax:
            return math.inf
        # 最快多快:v·一拍 + v·延迟 + v²/(2a) ≤ free
        a, t = zg.DECEL_MPS2, zg.TICK_S + zg.LATENCY_S
        return a * (-t + math.sqrt(t * t + 2.0 * free / a))

    async def _send(self, vx: float, wz: float, dt_s: float) -> None:
        self._last_vx = vx
        await super()._send(vx, wz, dt_s)

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

    def current_path(self) -> tuple[tuple[float, float], ...] | None:
        """正在走的规划路径(地图坐标,米);没在走(没规划、收尾了)是 ``None``。给遥测上报用。"""
        return self._path.points if self._path is not None else None

    def _start_replan(self, *, detour: bool = False, why: str = "off_route",
                      free: bool = False) -> None:
        if self._replan is not None and not self._replan.done():
            return
        self._replan_kind = "detour" if detour else ""
        if detour or free:
            pass                         # 绕障、挪完不算偏离重规划的次数(被挡另有 20 s 的账)
        elif self._replans >= MAX_REPLANS:
            self._replan_out = ("fail", f"重规划了 {MAX_REPLANS} 次还是走不上路径", 0)
            return
        else:
            self._replans += 1
        if self.on_event is not None:
            # 运营可视化:站点据此在状态里写「重规划了几次、为什么」(why:
            # off_route 偏离路径 / zones 区域换了 / near_nogo 离禁行区太近 / detour 绕障)
            self.on_event("nav_replan", {"why": "detour" if detour else why, "n": self._replans})
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
        self._creeps = 0
        self._after_creep = False
        self._hit_reason = ""
        self._hit_at = -math.inf
        self._blocked_since = None
        self._blocked_told = False
        self._detoured = False
        self._last_vx = 0.0
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
        verdict, z = self._backstop(here)                # S7:机身 + σ + 刹车距离
        if verdict == "inside":
            await self._inside_nogo(z, here)
            return
        if verdict == "near":
            await self._near_nogo(z, here, dt_s)
            return
        self._near_told = False
        out, self._replan_out = self._replan_out, None
        if out is not None:
            if out[0] == "fail":
                if self._replan_kind == "detour":
                    log.info("绕不过去:%s;接着等、过一会儿再试", out[1])
                    await self._send(0.0, 0.0, dt_s)
                    return
                log.warning("重规划没成:%s", out[1])
                await self._fail(f"重规划没成:{out[1]}")
                return
            if out[2] != self._epoch:
                # 重规划完到这一拍之间区域又换了(内审应修 9):旧区域上的路不装,再来一次
                self._start_replan(why="zones")
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
            await self._fail("没有路径")
            return
        dgoal = math.hypot(target.position.x - here.x, target.position.y - here.y)
        if self._aligning and dgoal > ARRIVE_TOL_M + REALIGN_M and self._creep_from is None:
            log.info("对朝向的时候离点 %.2f m 了:重新走过去", dgoal)
            self._aligning = False                       # W41:不在点上就不算到了
        if self._aligning or dgoal <= ARRIVE_TOL_M:
            if self._creep_from is not None and self._creep_way > 0:
                self._creep_from = None                  # W41:往前挪到了点上就不再往前挪
            self._aligning = True
            err = _wrap(target.yaw - here.yaw)
            if abs(err) <= YAW_TOL_RAD:
                await self._enter_terminal(NavStatus.SUCCEED)
                return
            await self._move(0.0, self._turn(err), dt_s, here)
            return
        seg, dev, proj = self._project(here.x, here.y)
        # 正往前挪着(W35):挪本来就是离开路径,挪完再重规划一次 —— 原来挪 1 m 的路上每偏 0.3 m 重规划
        # 一次,正好用光 3 次的额度,再多一次就「走不上路径」失败
        if dev > MAX_DEVIATION_M and self._creep_from is None:
            log.warning("偏离路径 %.2f m,从当前位置重规划", dev)
            # 挪完离开了路径(挪本来就是为了绕):记成绕障,不算偏离重规划的次数
            self._start_replan(free=self._after_creep, why="detour" if self._after_creep
                               else "off_route")
            self._after_creep = False
            await self._send(0.0, 0.0, dt_s)
            return
        self._after_creep = False
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
            limit = min(self._vlim(), self._path_speed(seg, proj),
                        self._cm.speed_at(here.x, here.y), self._near_cap(d),
                        self._zone_cap(here))
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
        # 限着速(W29 下雨)时近障从更远就开始减速:湿地刹车距离长
        start = NEAR_SLOW_FROM_M * (WET_NEAR_SCALE if math.isfinite(self.speed_cap) else 1.0)
        if near >= start:
            return math.inf
        lo = max(NEAR_MIN_MPS, self._caps.deadband_vx)
        f = max(0.0, (near - NEAR_SLOW_AT_M) / (start - NEAR_SLOW_AT_M))
        return lo + (max(self._vlim(), lo) - lo) * f

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
                # 挪着看两侧(守卫照样查)
                vx = (self._dir or 1) * self._creep_way * self._creep_speed(here, self._creep_way)
                wz = 0.0
            else:
                log.info("%s了 %.1f m,两侧看过了,接着走", self._creep_word(), moved)
                self._creep_from = None
                self._after_creep = True
        if self.obstacles.state() in ("lost", "extrinsic_bad", "none"):
            from d1max_agent.obstacles import describe
            why = f"障碍数据{describe(self.obstacles.state())}"
            log.error("%s:停", why)
            self.emit(AlgErrorEvent((AlgErrorItem(ALG_LIDAR_DISCONNECTED, why, 2),),
                                    self._now()))
            await self._fail(why)
            return
        v = self.guard.check(vx, wz, self._v_meas, self.obstacles, self._odom_pose)
        if not v.ok and abs(vx) > 1e-6 and abs(wz) > 1e-6 and self._creep_from is None:
            # 边走边转的弧线被拦(W35:急弯的弧线往里扫出去一米,扫进机身侧面的盲带),原地转却通得过:
            # 先原地转正,转正了下一拍就是直走 —— 不为一条弧线原地等到放弃。发出去的那条照样过了守卫
            turn = self.guard.check(0.0, wz, self._v_meas, self.obstacles, self._odom_pose)
            if turn.ok:
                vx, v = 0.0, turn
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
        await self._blocked(v, here, dt_s, vx, wz)

    async def _blocked(self, v: Any, here: Any, dt_s: float, vx: float = 0.0,
                       wz: float = 0.0) -> None:
        now = self._secs()
        if self._blocked_since is None:
            self._blocked_since = now
            self._next_detour = now + BLOCK_DETOUR_S
            log.info("被挡:%s", v.reason)
        if v.hits:
            self._hit_reason, self._hit_at = v.reason, now
        reason = self._block_reason(v, now)
        waited = now - self._blocked_since
        await self._send(0.0, 0.0, dt_s)                 # 原地等(零速保住步态)
        if waited >= BLOCK_GIVEUP_S:
            log.warning("被挡 %.0f s 还没走成:放弃这一段(%s)", waited, reason)
            await self._fail(f"被挡 {waited:.0f} 秒还没走成({reason})")
            return
        if waited >= BLOCK_REPORT_S and not self._blocked_told:
            self._blocked_told = True
            if self.on_event is not None:
                self.on_event("nav_blocked", {"reason": reason, "x": round(here.x, 2),
                                              "y": round(here.y, 2),
                                              "waited_s": round(waited, 1)})
        if (self._creep_from is None and not self._crept and self._creeps < MAX_CREEPS
                and not v.hits and abs(wz) > 1e-6 and waited >= BLOCK_DETOUR_S):
            way = self._pick_creep(here)
            if way:
                # 带转向(原地转、弧线)只被「看不见」挡住:往前挪 / 往后退一个机身长再接着走
                self._creep_way = way
                log.info("转向被看不见的格子挡住(机身两侧没看过):先%s %.1f m",
                         self._creep_word(), CREEP_M)
                self._creep_from = (self._odom_pose[0], self._odom_pose[1])
                self._crept = True
                self._creeps += 1
                return
        if self._creep_from is not None:
            self._creep_from = None                     # 挪的时候前面也挡了:不挪了,接着等
        if now >= self._next_detour and v.hits and self._base is not None:
            self._next_detour = now + BLOCK_RETRY_S
            self._remember(v.hits, here, now)
            self._start_replan(detour=True)

    def _block_reason(self, v: Any, now: float) -> str:
        """给值班员看的被挡原因(W41):最后一次查的是转身(只有看不见),而这一回早先前面真有挡 →
        两样都写,不然「有挡 0 处」会让人以为前面是空的。"""
        if v.hits or not self._hit_reason or now - self._hit_at > TEMP_OBSTACLE_S:
            return v.reason
        return f"{self._hit_reason};绕开要转身,机身两侧看不见 {v.unknown} 处"

    def _creep_word(self) -> str:
        return "往前挪" if self._creep_way > 0 else "往后退"

    def _can_creep(self, here: Any, way: int) -> bool:
        """往 ``way`` 那头(+1 行进方向往前、−1 往后退)直着挪 :data:`CREEP_M`:守卫现看得清是空的、
        地图和区域也让走。"""
        return (self.guard.check((self._dir or 1) * way * CREEP_V, 0.0, self._v_meas,
                                 self.obstacles, self._odom_pose).ok
                and self._creep_clear(here, way))

    def _creep_along(self, here: Any) -> float:
        """终点在往前挪那条线上有多远(米;负的 = 在身后)。"""
        assert self._target is not None
        h = _heading(here.yaw, self._dir or 1)
        return ((self._target.position.x - here.x) * math.cos(h)
                + (self._target.position.y - here.y) * math.sin(h))

    def _off_path(self, x: float, y: float) -> float:
        """点离这条路径(从当前段往后)多远。"""
        if self._path is None:
            return math.inf
        pts = self._path.points
        best = math.inf
        for i in range(self._seg, len(pts) - 1):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            best = min(best, math.hypot(x - ax - t * dx, y - ay - t * dy))
        return best

    def _pick_creep(self, here: Any) -> int:
        """挪的方向(W41,见 :data:`CREEP_OFF_PATH_M` 上面的说明):+1 往前、−1 往后退、0 不挪。对朝向的
        时候不往前挪(挪了就不在点上了),往后退可以(退完重新走回来,两侧就看过了)。"""
        along = self._creep_along(here) if self._target is not None else math.inf
        fwd = not self._aligning and self._can_creep(here, 1)
        if fwd and along >= CREEP_M + ARRIVE_TOL_M:
            h = _heading(here.yaw, self._dir or 1)
            if self._off_path(here.x + CREEP_M * math.cos(h),
                              here.y + CREEP_M * math.sin(h)) <= CREEP_OFF_PATH_M:
                return 1                                 # 顺路:往前挪本来就是往终点走
        if self._can_creep(here, -1):
            return -1
        if fwd and not (0.0 < along < CREEP_M + ARRIVE_TOL_M):
            return 1                                     # 退不了、也不会冲过终点:老办法
        return 0

    def _creep_clear(self, here: Any, way: int = 1) -> bool:
        """往前挪那一段在地图上也得能走:机身(含余量)扫过的那一条不压地图里的挡、不出图,离禁行区
        够远(内审阻断 1:原来只看守卫,禁行区要等狗身中心压上去才停 —— 落差在真机验过之前只靠禁行区
        挡)。

        W35(2026-10-13 C40221):原来查的是**膨胀过**的代价图上中心走的那条线 —— 膨胀半径是机身
        外接圆(0.5 m 多),狗离桌子、墙半米以内就哪儿都不让挪,路线一弯就起不了步。直着挪用不着
        外接圆:按机身矩形查没膨胀的图;东西真在那儿守卫现看着。狗此刻机身底下本来就压着的格子
        不算(定位差几厘米、贴着东西站)。"""
        if self._base is None:
            return False
        blocked, res, origin = self._base
        rows, cols = blocked.shape
        h = _heading(here.yaw, (self._dir or 1) * way)
        c, s = math.cos(h), math.sin(h)
        hl = self.guard.body_len / 2 + self.guard.margin
        hw = self.guard.body_wid / 2 + self.guard.margin
        step = min(0.05, res / 2)
        nx, ny = int(math.ceil(2 * hl / step)), int(math.ceil(2 * hw / step))
        body = [(-hl + 2 * hl * i / nx, -hw + 2 * hw * j / ny)
                for i in range(nx + 1) for j in range(ny + 1)]

        def cells(d: float) -> set[tuple[int, int]]:
            out = set()
            for lx, ly in body:
                x, y = here.x + c * (d + lx) - s * ly, here.y + s * (d + lx) + c * ly
                out.add((int(math.floor((y - origin[1]) / res)),
                         int(math.floor((x - origin[0]) / res))))
            return out
        under = cells(0.0)
        for i in range(1, int(math.ceil(CREEP_M / step)) + 1):
            for r, k in cells(min(i * step, CREEP_M)) - under:
                if not (0 <= r < rows and 0 <= k < cols) or blocked[r, k]:
                    return False
        # 区域的规矩一条不少(W35 外审 I2):禁行区,**加上限速低到这台狗守不住、规划当禁行的限速区**
        # (:meth:`_effective`)。改查没膨胀的图时只看了原始的禁行区,把后一类漏了 —— 人划的区雷达
        # 看不见,守卫补不上。放松的只是实物那一圈外接圆的膨胀,不是区域。
        # S7:跟每拍回退检查同一个口径 —— 机身矩形挪完再刹停,离它不到 σ + 余量、越挪越近就不挪
        # (原来是中心线离 0.25 m,不加 σ)。
        pose = (here.x, here.y, here.yaw)
        need = zg.sigma_of(here) + BACKSTOP_MARGIN_M
        poses = zg.sweep(*pose, CREEP_M + zg.stop_distance(CREEP_V), 0.0, heading=h)[1:]
        for z in self._closed:
            c0 = zg.clearance(pose, z, need)
            if c0 <= 0.0 or any(c < need and c < c0 - 1e-3
                                for c in (zg.clearance(p, z, need) for p in poses)):
                return False
        return True

    def _creep_speed(self, here: Any, way: int = 1) -> float:
        """往前挪的速度:不超过 :data:`CREEP_V`,也不超过机身前沿再往前半米这一段里限速区的上限
        (W35 外审 I2:原来固定 0.2 m/s,挪进限速 0.1 的区照样 0.2)。"""
        v = CREEP_V
        if self._cm is not None:
            h = _heading(here.yaw, (self._dir or 1) * way)
            c, s = math.cos(h), math.sin(h)
            ahead = self.guard.body_len / 2 + self.guard.margin + 0.5
            for i in range(int(ahead / 0.1) + 1):
                v = min(v, self._cm.speed_at(here.x + c * i * 0.1, here.y + s * i * 0.1))
        return max(v, self._caps.deadband_vx)

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
