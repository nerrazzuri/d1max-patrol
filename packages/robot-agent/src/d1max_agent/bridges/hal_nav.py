"""``HalNavBackend``:RobotHAL → NavBackend。**步进式**,``await step(dt)`` 由运行时每拍调。

状态机(照 ``d1max_sim/nav_state.py`` 的时序,引擎压在这上面):
``goto`` → INITIALIZING →(``init_delay_s``)→ ACTIVE(开始发速度)→ 到点 SUCCEED /
HAL 拒速度或定位丢失 FAILED / ``stop()`` CANCELLED →(``terminal_hold_s``,期间 ``goto`` 被拒)
→ STANDBY。终态一进就 ``hal.stop()``;每一次状态变化都发 ``NavStatusEvent``,定位质量变化发
``LocStatusEvent``。

只支持点到点导航与停/暂停/继续;建图、地图管理、路径、重定位都不支持(``capabilities``
为空,对应方法抛 ``NavRequestError``)——总设计 §5:规划/避障上移到代理,适配器只留速度与里程。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from typing import Any

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
                 init_delay_s: float = 0.3, terminal_hold_s: float = 0.5) -> None:
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
        self._home: Pose | None = None
        self._due: tuple[int, NavStatus] | None = None      # (时刻, 到时进入的状态)
        self._seq = 0

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> None:
        if not self._connected:
            await self._hal.connect()
            self._connected = True

    async def close(self) -> None:
        self._connected = False
        self._target = None
        self._due = None

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
        raise NavRequestError("reset_localization", "HAL 桥不做重定位")

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

    def set_home(self, pose: Pose) -> None:
        self._home = pose

    async def return_home(self) -> None:
        if self._home is None:
            raise NavRequestError("return_home", "没登记原点")
        await self.goto(self._home)

    async def get_speed(self) -> dict[str, float]:
        return {"x": self._vmax, "y": 0.0, "z": self._wmax}

    async def set_speed(self, x: float, y: float | None = None,
                        z: float | None = None) -> dict[str, float]:
        if y is not None and abs(float(y)) > 1e-9:
            raise NavRequestError("set_speed", "HAL 桥不做侧移,y 只能是 0")
        if not math.isfinite(x) or x <= 0:
            raise NavRequestError("set_speed", f"前进上限要是正的有限数,给的是 {x!r}")
        self._vmax = min(float(x), self._caps.max_vx)
        if z is not None:
            if not math.isfinite(z) or z <= 0:
                raise NavRequestError("set_speed", f"转向上限要是正的有限数,给的是 {z!r}")
            self._wmax = min(float(z), self._caps.max_wz)
        return await self.get_speed()

    # ------------------------------------------------------------ 每拍

    def _advance_timers(self) -> None:
        """定时转移:Initializing → Active、终态 → StandBy。"""
        if self._due is not None and self._now() >= self._due[0]:
            _, nxt = self._due
            self._due = None
            self._set_status(nxt)

    async def step(self, dt_s: float) -> None:
        """推进一拍:定时转移 + 定位质量 + 速度环。运行时每拍 ``await`` 一次。"""
        self._advance_timers()
        health = await self._hal.health()
        loc = LocStatus.CONTINUOUS_LOC if health.loc_quality > 0.0 else LocStatus.LOC_LOST
        if loc is not self._loc:
            prev, self._loc = self._loc, loc
            self.emit(LocStatusEvent(loc, prev))
        if self._status is not NavStatus.ACTIVE:
            return
        if loc is LocStatus.LOC_LOST:
            await self._enter_terminal(NavStatus.FAILED)
            return
        assert self._target is not None
        odom = await self._hal.odometry()
        dx, dy = self._target.position.x - odom.x, self._target.position.y - odom.y
        dist = math.hypot(dx, dy)
        if dist <= POSITION_TOL_M:
            await self._enter_terminal(NavStatus.SUCCEED)
            return
        bearing = _wrap(math.atan2(dy, dx) - odom.yaw)
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
        await self._hal.stop()
        self._set_status(status)
        self._due = (self._now() + self._hold_ms, NavStatus.STANDBY)
