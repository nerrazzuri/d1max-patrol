"""厂商后端的响应匹配。协议地雷全在这里拆。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    NavConnectionError,
    NavRequestError,
    NavTimeoutError,
)
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus, NavStatus
from d1max_sim.nav_server import SimNavServer


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
    b = VendorNavBackend(NavConfig(url=sim.url, request_timeout_s=3.0))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


async def _await_status(backend, req, wanted: str, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await backend.request(req) == wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{req.req_func} 未在 {timeout_s}s 内变为 {wanted}")


async def test_连接后可用(backend):
    assert backend.connected is True
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_未连接时请求抛连接错误(sim):
    b = VendorNavBackend(NavConfig(url=sim.url))
    with pytest.raises(NavConnectionError):
        await b.request(R.get_nav_status())


async def test_连不上的地址抛连接错误():
    b = VendorNavBackend(NavConfig(url="ws://127.0.0.1:1", connect_timeout_s=1.0))
    with pytest.raises(NavConnectionError):
        await b.connect()


async def test_重复连接与重复关闭都安全(backend):
    await backend.connect()
    assert backend.connected is True
    await backend.close()
    await backend.close()
    assert backend.connected is False


async def test_连续请求不留挂起也不降级(backend):
    for _ in range(5):
        await backend.request(R.get_nav_status())
    assert backend.fallback_matches == 0
    assert backend.dropped_frames == 0
    assert backend.pending_count == 0


async def test_并发请求各自拿到自己的响应(backend):
    """并发下一旦匹配写错,这里就会串台。"""
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
        backend.request(R.get_mapping_status()),
        backend.request(R.get_all_pgm_map()),
    )
    assert results[0] == NavStatus.STANDBY.value
    assert results[1] == LocStatus.INIT.value
    assert results[2] == MappingStatus.PASSIVE.value
    assert R.parse_map_ids(results[3]) == []


async def test_乱序到达也不串台(sim, backend):
    """注入乱序:一半响应被延迟,到达顺序与发出顺序不同。"""
    sim.faults.response_delay_s = 0.15
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
    )
    assert results == [NavStatus.STANDBY.value, LocStatus.INIT.value,
                       NavStatus.STANDBY.value, LocStatus.INIT.value]
    assert backend.fallback_matches == 0


async def test_地雷1_速度接口的嵌套外壳被剥掉(backend):
    speed = await backend.request(R.get_navigation_speed())
    assert set(speed) == {"x", "y", "z"}          # 不是 {"AppReponseObjectData"}


async def test_地雷2_响应函数名与请求不同也能匹配(sim, backend):
    """loc_load_map 的响应叫 load_localization_map。"""
    map_id = sim.store.create_map()
    await backend.request(R.loc_load_map(map_id))
    assert backend.fallback_matches == 0          # 走的是主匹配,不是降级
    assert backend.dropped_frames == 0


async def test_地雷3_建图推送不会污染匹配表(backend):
    """notify_stop_mapping_status 顶着 app_resp 和 frame_count=1 进来。"""
    await backend.request(R.start_mapping())
    await _await_status(backend, R.get_mapping_status(),
                        MappingStatus.MAPPING_RUNNING.value)
    await backend.request(R.stop_mapping())
    await _await_status(backend, R.get_mapping_status(),
                        MappingStatus.MAPPING_SAVE_END.value)

    assert backend.fallback_matches == 0
    assert backend.dropped_frames == 0            # 推送被识别,不算丢帧
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_帧号归零时降级到按名字匹配(sim, backend):
    sim.faults.frame_count_zero = True
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value
    assert backend.fallback_matches == 1


async def test_降级匹配按先进先出认领同名响应(sim, backend):
    sim.faults.frame_count_zero = True
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
    )
    assert results == [NavStatus.STANDBY.value, LocStatus.INIT.value]
    assert backend.fallback_matches == 2


async def test_设备回error抛请求错误(backend):
    with pytest.raises(NavRequestError) as info:
        await backend.request(R.loc_load_map("不存在的图"))
    assert info.value.operation == "loc_load_map"
    assert "不存在" in info.value.message


async def test_无人认领的响应被计入丢帧(sim, backend):
    """构造一条谁也不认的响应,链路必须活着。

    必须从**服务端**推。`backend._ws.send()` 是客户端→服务端方向,报文会被
    仿真器按 `app_req` 校验拒掉("忽略畸形报文: 报文不是 app_req"),根本回不到
    本端读循环,`dropped_frames` 永远是 0。这条测试要验的是读循环收到一条
    无人认领的响应之后:计数加一、且**不把读循环带崩**——所以最后一行的
    `request()` 才是这条测试的重点,不能改成直接调 `_route()` 了事。
    """
    await sim._broadcast({
        "head": {"type": "app_resp", "frame_count": 9999, "source": "app"},
        "data": {"req_result": {"req_func": "nobody_asked", "status": "ok"}},
    })
    await asyncio.sleep(0.1)
    assert backend.dropped_frames == 1
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_请求超时后挂起表被清理(sim, backend):
    sim.faults.response_delay_s = 5.0
    with pytest.raises(NavTimeoutError):
        await backend.request(R.get_nav_status())
    assert backend.pending_count == 0
    sim.faults.response_delay_s = 0.0


async def test_故障码推送转成事件(sim, backend):
    q = backend.subscribe()
    sim.faults.queued_alg_errors.append(AlgErrorItem(13330, "路径被挡", 2))
    event = await asyncio.wait_for(q.get(), timeout=3.0)
    assert isinstance(event, AlgErrorEvent)
    assert event.items[0].code == 13330


async def test_关闭时挂起请求被唤醒而不是永久卡住(sim, backend):
    sim.faults.response_delay_s = 5.0
    task = asyncio.create_task(backend.request(R.get_nav_status()))
    await asyncio.sleep(0.1)
    await backend.close()
    with pytest.raises(NavConnectionError):
        await task
