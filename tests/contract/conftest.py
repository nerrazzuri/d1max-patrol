"""契约测试的后端工厂。

这是整个 tests/contract 目录里唯一知道"跑的是哪个实现"的文件。
将来加 Nav2Backend,只需在 BACKEND_FACTORIES 里加一行。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

import pytest

from d1max_patrol.backends.base import NavBackend
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus
from d1max_sim.nav_server import SimNavServer


@contextlib.asynccontextmanager
async def _vendor_against_sim() -> AsyncIterator[NavBackend]:
    """厂商后端 + 仿真设备。"""
    sim = SimNavServer(tick_hz=100.0)
    await sim.start()
    backend = VendorNavBackend(NavConfig(
        url=sim.url,
        request_timeout_s=3.0,
        status_poll_interval_s=0.05,
        reconnect_min_s=0.05,
        reconnect_max_s=0.2,
    ))
    try:
        await backend.connect()
        yield backend
    finally:
        await backend.close()
        await sim.stop()


#: 后端名 -> 上下文管理器工厂。加新后端只改这里。
BACKEND_FACTORIES: dict[str, Callable[[], contextlib.AbstractAsyncContextManager]] = {
    "vendor": _vendor_against_sim,
}


@pytest.fixture(params=sorted(BACKEND_FACTORIES), ids=sorted(BACKEND_FACTORIES))
async def nav_backend(request) -> AsyncIterator[NavBackend]:
    async with BACKEND_FACTORIES[request.param]() as backend:
        yield backend


@pytest.fixture
async def ready_backend(nav_backend) -> tuple[NavBackend, str]:
    """建好图、加载好定位的后端。契约测试的常用起点。"""
    await nav_backend.start_mapping()
    await _until(lambda: nav_backend.mapping_status(),
                 MappingStatus.MAPPING_RUNNING)
    await nav_backend.stop_mapping()
    await _until(lambda: nav_backend.mapping_status(),
                 MappingStatus.MAPPING_SAVE_END)

    maps = await nav_backend.list_maps()
    assert maps, "建图完成后应至少有一张地图"
    map_id = maps[0]

    await nav_backend.load_map(map_id)
    await _until(lambda: nav_backend.loc_status(), LocStatus.CONTINUOUS_LOC)
    return nav_backend, map_id


async def _until(getter, wanted, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")
