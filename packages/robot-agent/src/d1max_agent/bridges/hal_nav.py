"""``HalNavBackend``:RobotHAL → NavBackend。**步进式**,``await step(dt)`` 由运行时每拍调。

状态机(照 ``d1max_sim/nav_state.py`` 的时序,引擎压在这上面):
``goto`` → INITIALIZING →(``init_delay_s``)→ ACTIVE(开始发速度)→ 到点 SUCCEED /
HAL 拒速度或定位丢失 FAILED / ``stop()`` CANCELLED →(``terminal_hold_s``,期间 ``goto`` 被拒)
→ STANDBY。终态一进就 ``hal.stop()``;每一次状态变化都发 ``NavStatusEvent``,定位质量变化发
``LocStatusEvent``。

只支持点到点导航与停/暂停/继续;建图、地图管理、路径都不支持(``capabilities``
为空,对应方法抛 ``NavRequestError``);重定位是空操作,见 ``reset_localization``。
——总设计 §5:规划/避障上移到代理,适配器只留速度与里程。

**不认 ``return_home``**(W00c6b):这座桥走的是直线、不规划、不避障;直线回原点会穿墙。拒绝之后引擎走
W04 的沿来路回:按这一趟真到过的点、点位失败时停在哪倒着走,回到出发点为止;出发点就在原点边上才走
最后那一小段(见 ``MissionEngine._retrace_home``)。人接管过的那一段、``goto`` 半路返航(来路只有出发点
到目标那一条线)不在此列 —— 前者是人开的,后者就是去程那条线倒着走。规划器上线(W10)之后,回家由
新后端规划(W08 决定 6)。``current_pose`` 报运控里程(过渡期地图位姿就是里程,W08 决定 4)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from typing import Any

from d1max_agent.localization import OdomAnchor
from d1max_contract.hal import RobotHAL, VelocityCommand
from d1max_patrol.backends.base import (
    LocStatusEvent,
    NavBackend,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose, Waypoint

log = logging.getLogger(__name__)

POSITION_TOL_M = 0.1
BEARING_THRESH_RAD = 0.3
K_LIN = 1.0
K_ANG = 2.0
_MOVING = frozenset({NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.PAUSE})


def _wrap(a: float) -> float:
    w = math.remainder(a, 2 * math.pi)
    return math.pi if w == -math.pi else w


class HalNavBackend(NavBackend):
    def __init__(self, hal: RobotHAL, *, now_ms: Callable[[], int], map_id: str,
                 init_delay_s: float = 0.3, terminal_hold_s: float = 0.5,
                 anchor: OdomAnchor | None = None) -> None:
        super().__init__()
        self._hal = hal
        self._now = now_ms
        self._map_id = map_id
        self._init_delay_ms = int(init_delay_s * 1000)
        self._hold_ms = int(terminal_hold_s * 1000)
        caps = hal.hal_capabilities()
        self._caps = caps
        self._vmax = caps.max_vx
        self._wmax = caps.max_wz
        self._connected = False
        self._status = NavStatus.STANDBY
        self._loc = LocStatus.CONTINUOUS_LOC
        self._target: Pose | None = None
        self._due: tuple[int, NavStatus] | None = None      # (时刻, 到时进入的状态)
        self._seq = 0
        #: 最近一拍的里程新不新鲜(运控报的 ``loc_quality`` 改义为「里程新鲜」,W08 决定 9)。
        self.odom_ok = False
        self.use_anchor(anchor if anchor is not None else OdomAnchor(identity=True))

    def use_anchor(self, anchor: OdomAnchor) -> None:
        """地图位姿从哪来(W00c6e):里程锚定。仿真按原样(里程就是地图位姿);真狗没锚过就不可信。"""
        self.anchor = anchor
        if anchor.identity and anchor.map_ref is None:
            anchor.on_map((self._map_id, ""))
        self._loc = LocStatus.CONTINUOUS_LOC if anchor.anchored else LocStatus.LOC_LOST

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> None:
        if not self._connected:
            await self._hal.connect()
            self._connected = True

    async def close(self) -> None:
        """断开:目标与定时转移清掉,状态回 StandBy(不然再连上时卡在上一次的
        Initializing/Cancelled 里,``goto`` 永远被拒)。"""
        self._connected = False
        self._target = None
        self._due = None
        self._set_status(NavStatus.STANDBY)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset()

    # ------------------------------------------------------------ 地图 / 路径(只有当前地图)

    async def list_maps(self) -> list[str]:
        return [self._map_id]

    async def remove_maps(self, map_ids: Sequence[str]) -> None:
        raise NavRequestError("remove_maps", "HAL 桥不做地图管理")

    async def rename_map(self, old_id: str, new_id: str) -> None:
        raise NavRequestError("rename_map", "HAL 桥不做地图管理")

    async def get_map_grid(self, map_id: str) -> dict[str, Any]:
        return {}

    async def start_mapping(self) -> None:
        raise NavRequestError("start_mapping", "HAL 桥不建图")

    async def stop_mapping(self) -> None:
        raise NavRequestError("stop_mapping", "HAL 桥不建图")

    async def mapping_status(self):
        return None

    async def list_paths(self, map_id: str) -> dict[str, list[Waypoint]]:
        return {}

    async def save_path(self, map_id: str, path_id: str, waypoints: Sequence[Waypoint]) -> None:
        raise NavRequestError("save_path", "HAL 桥不存路径")

    async def remove_path(self, map_id: str, path_id: str) -> None:
        raise NavRequestError("remove_path", "HAL 桥不存路径")

    async def load_map(self, map_id: str) -> None:
        if map_id != self._map_id:
            raise NavRequestError("load_map", f"只加载了 {self._map_id!r},没有 {map_id!r}")

    async def reset_localization(self) -> None:
        """HAL 没有「重定位」原语(W00d 的 adapter-d1max 再映到厂商的重定位)。这里是空操作:
        引擎随后停着等 ``CONTINUOUS_LOC``(自带 30 s 上限),定位回来就重发当前点;
        拒绝的话,任何一次短暂的 LOC_LOST 都会让整趟中止。"""
        log.info("reset_localization:HAL 桥不做重定位,停着等定位质量回来")

    async def loc_status(self) -> LocStatus | None:
        return self._loc

    # ------------------------------------------------------------ 导航

    async def goto(self, pose: Pose) -> None:
        if self._status is not NavStatus.STANDBY:
            raise NavRequestError("goto", f"只能在 StandBy 下启动,当前 {self._status.value}")
        self._target = pose
        self._set_status(NavStatus.INITIALIZING)
        self._due = (self._now() + self._init_delay_ms, NavStatus.ACTIVE)

    async def stop(self) -> None:
        if self._status in _MOVING:
            await self._enter_terminal(NavStatus.CANCELLED)

    async def pause(self) -> None:
        if self._status is NavStatus.ACTIVE:
            self._set_status(NavStatus.PAUSE)
            await self._hal.stop()

    async def resume(self) -> None:
        if self._status is NavStatus.PAUSE:
            self._set_status(NavStatus.ACTIVE)

    async def nav_status(self) -> NavStatus | None:
        return self._status

    async def current_pose(self) -> Pose | None:
        """狗现在在地图上哪儿(引擎记出发点、来路用,W00c6b 内审):锚定后的地图位姿;没锚过是
        ``None``。"""
        o = await self._hal.odometry()
        est = self.anchor.estimate((o.x, o.y, o.yaw))
        return None if est is None else Pose.from_xy_yaw(est.x, est.y, est.yaw)

    #: 这座桥怎么走路(W00c6b):能力里报给站点(``tasks.goto.path``),站点据此决定巡检后怎么回待命点。
    PATH_KIND = "straight"

    async def return_home(self) -> None:
        raise NavRequestError("return_home", "直线桥不认返航,引擎沿来路回(W00c6b)")

    async def get_speed(self) -> dict[str, float]:
        return {"x": self._vmax, "y": 0.0, "z": self._wmax}

    async def set_speed(self, x: float, y: float | None = None,
                        z: float | None = None) -> dict[str, float]:
        if y is not None and abs(float(y)) > 1e-9:
            raise NavRequestError("set_speed", "HAL 桥不做侧移,y 只能是 0")
        if not math.isfinite(x) or x <= 0:
            raise NavRequestError("set_speed", f"前进上限要是正的有限数,给的是 {x!r}")
        if x < self._caps.deadband_vx:
            # 速度环发出去的命令低于死区,HAL 会拒(或机器根本不动):任务会卡在 Active 里
            # 一直走不到,不如在这里说清楚。
            raise NavRequestError("set_speed", f"前进上限 {x!r} 低于这台机器的死区 "
                                               f"{self._caps.deadband_vx!r} m/s")
        self._vmax = min(float(x), self._caps.max_vx)
        if z is not None:
            if not math.isfinite(z) or z <= 0:
                raise NavRequestError("set_speed", f"转向上限要是正的有限数,给的是 {z!r}")
            self._wmax = min(float(z), self._caps.max_wz)
        return await self.get_speed()

    async def reset_speed(self) -> None:
        """回到 HAL 上限。每趟任务开始时由任务调:上一趟的限速不能带到下一趟。"""
        self._vmax = self._caps.max_vx
        self._wmax = self._caps.max_wz

    # ------------------------------------------------------------ 每拍

    def _advance_timers(self) -> None:
        """定时转移:Initializing → Active、终态 → StandBy。"""
        if self._due is not None and self._now() >= self._due[0]:
            _, nxt = self._due
            self._due = None
            self._set_status(nxt)

    async def step(self, dt_s: float) -> None:
        """推进一拍:定时转移 + 定位质量 + 速度环。运行时每拍 ``await`` 一次。

        定位(W00c6e):每拍都喂一次里程给锚定(遥控开着走的也算距离);可信 = 锚过、里程新鲜、
        σ 没过线。"""
        self._advance_timers()
        health = await self._hal.health()
        odom = await self._hal.odometry()
        self.odom_ok = health.loc_quality > 0.0 and odom.valid
        self.anchor.update((odom.x, odom.y, odom.yaw))
        loc = LocStatus.CONTINUOUS_LOC if self.anchor.ok(self.odom_ok) else LocStatus.LOC_LOST
        if loc is not self._loc:
            prev, self._loc = self._loc, loc
            self.emit(LocStatusEvent(loc, prev))
        if self._status is not NavStatus.ACTIVE:
            return
        if loc is LocStatus.LOC_LOST:
            await self._enter_terminal(NavStatus.FAILED)
            return
        if self._target is None:
            # 停车正在路上(``_enter_terminal`` 先清目标、再等 HAL 回执;真 HAL 最长等 5 s):
            # 这一拍什么都不发,也不再叫一次停 —— 那会把这一拍也卡在回执上。停车抛错的话
            # ``_enter_terminal`` 的 ``finally`` 照样进终态(W00c6a 内审 S1),以前这里是
            # assert,每拍炸。
            return
        here = self.anchor.estimate((odom.x, odom.y, odom.yaw))
        if here is None:
            await self._enter_terminal(NavStatus.FAILED)
            return
        dx, dy = self._target.position.x - here.x, self._target.position.y - here.y
        dist = math.hypot(dx, dy)
        if dist <= POSITION_TOL_M:
            await self._enter_terminal(NavStatus.SUCCEED)
            return
        bearing = _wrap(math.atan2(dy, dx) - here.yaw)
        wz = max(-self._wmax, min(self._wmax, K_ANG * bearing))
        if abs(bearing) > BEARING_THRESH_RAD:
            vx = 0.0
        else:
            vx = min(self._vmax, max(K_LIN * dist, self._caps.deadband_vx))
        self._seq += 1
        got = await self._hal.set_velocity(VelocityCommand(
            seq=self._seq, ttl_ms=max(300, int(dt_s * 3000)), frame="base", vx=vx, vy=0.0, wz=wz))
        if got.rejected:
            log.warning("HAL 拒了速度命令(%s),导航按 Failed 收尾", got.reason)
            await self._enter_terminal(NavStatus.FAILED)

    # ------------------------------------------------------------ 内部

    def _set_status(self, status: NavStatus) -> None:
        prev, self._status = self._status, status
        if status is not prev:
            self.emit(NavStatusEvent(status, prev))

    async def _enter_terminal(self, status: NavStatus) -> None:
        self._target = None
        try:
            await self._hal.stop()
        finally:
            # 停车抛错也要进终态(W00c6a 内审 S1):不然桥卡在「ACTIVE 却没有目标」,之后每拍炸,
            # 运行时后面的步骤(任务看门、状态上报)一步都走不到。停车失败由调用方如实往上报。
            self._set_status(status)
            self._due = (self._now() + self._hold_ms, NavStatus.STANDBY)
