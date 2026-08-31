"""状态轮询、断链与重连。第四个地雷:设备没有状态推送通道。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    BackendDisconnected,
    BackendReconnected,
    LocStatusEvent,
    MappingStatusEvent,
    NavBackendError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
)
from d1max_sim.nav_server import SimNavServer

FAST = dict(request_timeout_s=3.0, status_poll_interval_s=0.05,
            connect_timeout_s=2.0, reconnect_min_s=0.05, reconnect_max_s=0.2)


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def backend(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


async def _drain_until(queue, predicate, timeout_s: float = 10.0):
    async def loop():
        while True:
            event = await queue.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(loop(), timeout=timeout_s)


async def _ready(backend) -> str:
    await backend.start_mapping()
    while await backend.mapping_status() is not MappingStatus.MAPPING_RUNNING:
        await asyncio.sleep(0.02)
    await backend.stop_mapping()
    while await backend.mapping_status() is not MappingStatus.MAPPING_SAVE_END:
        await asyncio.sleep(0.02)
    map_id = (await backend.list_maps())[0]
    await backend.load_map(map_id)
    while await backend.loc_status() is not LocStatus.CONTINUOUS_LOC:
        await asyncio.sleep(0.02)
    return map_id


async def test_轮询器开局就广播当前状态(backend):
    q = backend.subscribe()
    event = await _drain_until(q, lambda e: isinstance(e, NavStatusEvent))
    assert event.status is NavStatus.STANDBY
    assert event.previous is None


async def test_状态不变时不重复发事件(backend):
    await asyncio.sleep(0.3)          # 让轮询器跑好几轮
    q = backend.subscribe()
    await asyncio.sleep(0.3)
    assert q.empty()


async def test_导航状态变化被转成事件(backend):
    await _ready(backend)
    q = backend.subscribe()
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    event = await _drain_until(
        q, lambda e: isinstance(e, NavStatusEvent) and e.status is NavStatus.SUCCEED)
    assert event.previous in (NavStatus.ACTIVE, NavStatus.INITIALIZING)


async def test_定位与建图状态也有事件(backend):
    q = backend.subscribe()
    await _ready(backend)
    # 等 MAPPING_SAVE_END 而不是 MAPPING_RUNNING: 后者只在 _ready() 发现它、
    # 立刻 stop_mapping() 之间存在 0~20ms,比后端 0.05s 的轮询周期还短,轮询
    # 注定经常抓不住 —— 这是轮询式状态跟踪的固有性质,不是 bug(真机待验证
    # 清单第 10 条)。SAVE_END 是终态,状态机里它后面没有任何排期,会一直保持。
    # 不要"优化"回 MAPPING_RUNNING。
    await _drain_until(
        q, lambda e: isinstance(e, MappingStatusEvent)
        and e.status is MappingStatus.MAPPING_SAVE_END)
    await _drain_until(
        q, lambda e: isinstance(e, LocStatusEvent)
        and e.status is LocStatus.CONTINUOUS_LOC)


async def test_等待导航终态可用(backend):
    await _ready(backend)
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=15.0))
    await asyncio.sleep(0.05)         # 确保订阅已建立再下发
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED


async def test_导航失败也是终态(sim, backend):
    await _ready(backend)
    sim.faults.fail_next_nav = True
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=15.0))
    await asyncio.sleep(0.05)
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.FAILED


async def test_断链发出断开事件(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    event = await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    assert event.reason


async def test_自动重连并重新对齐状态(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    await _drain_until(q, lambda e: isinstance(e, BackendReconnected), timeout_s=15.0)

    assert backend.reconnect_attempts >= 1
    assert backend.connected is True
    # 重连后缓存作废,当前状态被重新广播一遍
    event = await _drain_until(
        q, lambda e: isinstance(e, NavStatusEvent) and e.previous is None)
    assert event.status is NavStatus.STANDBY
    assert await backend.nav_status() is NavStatus.STANDBY


async def test_断链期间的请求抛错而不是永久挂起(sim, backend):
    sim.faults.disconnect_seconds = 1.0
    await asyncio.sleep(0.3)
    with pytest.raises(NavBackendError):
        await backend.nav_status()


async def test_等待重连(sim, backend):
    sim.faults.disconnect_seconds = 0.3
    await asyncio.sleep(0.2)
    await backend.wait_connected(timeout_s=15.0)
    assert backend.connected is True


async def test_等待重连超时(sim, backend):
    sim.faults.disconnect_seconds = 30.0
    await asyncio.sleep(0.2)
    with pytest.raises(NavTimeoutError):
        await backend.wait_connected(timeout_s=0.5)


async def test_关闭时不再重连(sim, backend):
    sim.faults.disconnect_seconds = 0.3
    await asyncio.sleep(0.1)
    await backend.close()
    before = backend.reconnect_attempts
    await asyncio.sleep(0.8)
    assert backend.reconnect_attempts == before
    assert backend.connected is False


async def test_关掉自动重连时只发断开事件(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST), auto_reconnect=False)
    await b.connect()
    try:
        q = b.subscribe()
        sim.faults.disconnect_seconds = 0.2
        await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
        await asyncio.sleep(0.8)
        assert b.connected is False
        assert b.reconnect_attempts == 0
    finally:
        await b.close()


async def test_轮询遇到超时不会杀死轮询器(sim, backend):
    sim.faults.response_delay_s = 4.0
    await asyncio.sleep(0.5)
    sim.faults.response_delay_s = 0.0
    q = backend.subscribe()
    # 轮询器还活着 —— 状态重新流动起来
    await _drain_until(q, lambda e: isinstance(e, NavStatusEvent), timeout_s=10.0)
