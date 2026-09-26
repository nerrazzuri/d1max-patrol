"""把 HAL、两个桥和 MissionEngine 装成一组(W00b)。``d1max_agent.main`` 与测试都从这里拿。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from d1max_agent.bridges.hal_device import HalDeviceBackend
from d1max_agent.bridges.hal_nav import HalNavBackend
from d1max_agent.engine.homing import HomePoint
from d1max_agent.engine.machine import MissionEngine
from d1max_agent.engine.removable import Removable
from d1max_contract.hal import RobotHAL
from d1max_patrol.protocol.nav_types import Pose


class NoRemovableProbe:
    """「扫过了,一块都没有」。sim 上没有盘;真机上 W00d 换成 DEFAULT_PROBE。"""

    async def scan(self) -> tuple[Removable, ...]:
        return ()


@dataclass
class EngineParts:
    hal: RobotHAL
    nav: HalNavBackend
    device: HalDeviceBackend
    engine: MissionEngine
    map_id: str
    #: 原点(换电位/待命位/返航目标),带地图 id 与标记时刻。``None`` = 没标过,
    #: 预飞检查 home 那一项会红,goto 变 failed。
    home: HomePoint | None

    def switch_map(self, map_id: str, home: Pose | None, *, now_ms: int) -> None:
        """换图(W00c5d 第二部分):导航桥认新的地图号;原点跟着图走(图里没带就是没标过)。"""
        self.map_id = map_id
        self.nav._map_id = map_id
        self.home = None if home is None else HomePoint(map_id=map_id, pose=home,
                                                        marked_at_ms=now_ms,
                                                        note="W00c5d:随图下发的原点")

    async def step(self, dt_s: float) -> None:
        """两个桥各推一拍。HAL 自己的推进(sim 的 ``tick``)由调用方负责。"""
        await self.nav.step(dt_s)
        await self.device.step(dt_s)


def build_engine(hal: RobotHAL, *, runs_root: Path, now_ms: Callable[[], int],
                 monotonic: Callable[[], float] = time.monotonic, map_id: str,
                 home: Pose | None, removable=None, init_delay_s: float = 0.3,
                 terminal_hold_s: float = 0.5, media: dict | None = None) -> EngineParts:
    nav = HalNavBackend(hal, now_ms=now_ms, map_id=map_id, init_delay_s=init_delay_s,
                        terminal_hold_s=terminal_hold_s)
    home_point: HomePoint | None = None
    if home is not None:
        home_point = HomePoint(map_id=map_id, pose=home, marked_at_ms=now_ms(),
                               note="W00b:装配时给的原点")
    device = HalDeviceBackend(hal, now_ms=now_ms)
    engine = MissionEngine(nav, device, dict(media or {}), Path(runs_root), clock=monotonic,
                           wall_ms=now_ms,
                           removable=removable if removable is not None else NoRemovableProbe())
    return EngineParts(hal=hal, nav=nav, device=device, engine=engine, map_id=map_id,
                       home=home_point)
