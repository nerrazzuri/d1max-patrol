"""故障场景的测试台。

与 tests/contract 相反,这里刻意与实现耦合:要断言的正是降级行为本身。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus
from d1max_sim.inject import apply_command
from d1max_sim.nav_server import SimNavServer


@dataclass
class Rig:
    sim: SimNavServer
    backend: VendorNavBackend
    map_id: str

    def inject(self, command: str) -> str:
        """等价于通过控制通道下发一条注入命令。"""
        return apply_command(self.sim.faults, command)


async def _until(getter, wanted, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")


@pytest.fixture
async def rig():
    sim = SimNavServer(tick_hz=100.0)
    await sim.start()
    backend = VendorNavBackend(NavConfig(
        url=sim.url,
        request_timeout_s=3.0,
        status_poll_interval_s=0.05,
        reconnect_min_s=0.05,
        reconnect_max_s=0.2,
    ))
    await backend.connect()
    try:
        await backend.start_mapping()
        await _until(backend.mapping_status, MappingStatus.MAPPING_RUNNING)
        await backend.stop_mapping()
        await _until(backend.mapping_status, MappingStatus.MAPPING_SAVE_END)
        map_id = (await backend.list_maps())[0]
        await backend.load_map(map_id)
        await _until(backend.loc_status, LocStatus.CONTINUOUS_LOC)
        yield Rig(sim, backend, map_id)
    finally:
        await backend.close()
        await sim.stop()
