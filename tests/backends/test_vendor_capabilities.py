"""厂商后端的能力方法。每个方法都对着仿真器往返一次。"""

import asyncio

import pytest

from d1max_patrol.backends.base import NavRequestError
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)
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


async def _wait(getter, wanted, timeout_s: float = 10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")


async def _ready(backend) -> str:
    """建图 + 加载定位,返回 map_id。"""
    await backend.start_mapping()
    await _wait(backend.mapping_status, MappingStatus.MAPPING_RUNNING)
    await backend.stop_mapping()
    await _wait(backend.mapping_status, MappingStatus.MAPPING_SAVE_END)
    map_id = (await backend.list_maps())[0]
    await backend.load_map(map_id)
    await _wait(backend.loc_status, LocStatus.CONTINUOUS_LOC)
    return map_id


def test_不再是抽象类(sim):
    assert VendorNavBackend(NavConfig(url=sim.url)) is not None


async def test_建图状态与地图列表(backend):
    assert await backend.mapping_status() is MappingStatus.PASSIVE
    assert await backend.list_maps() == []
    map_id = await _ready(backend)
    assert await backend.list_maps() == [map_id]


async def test_地图重命名与删除(backend):
    map_id = await _ready(backend)
    await backend.rename_map(map_id, "厂区一层")
    assert await backend.list_maps() == ["厂区一层"]
    await backend.remove_maps(["厂区一层"])
    assert await backend.list_maps() == []


async def test_取栅格地图(backend):
    map_id = await _ready(backend)
    grid = await backend.get_map_grid(map_id)
    assert grid["info"]["width"] > 0
    assert len(grid["data"]) == grid["info"]["width"] * grid["info"]["height"]


async def test_路径保存读取与删除(backend):
    map_id = await _ready(backend)
    assert await backend.list_paths(map_id) == {}

    points = [Waypoint("P1", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
              Waypoint("P2", Pose.from_xy_yaw(1.0, 1.0, 1.5708))]
    await backend.save_path(map_id, "巡检一号线", points)

    paths = await backend.list_paths(map_id)
    assert list(paths) == ["巡检一号线"]
    got = paths["巡检一号线"]
    assert [w.name for w in got] == ["P1", "P2"]
    assert got[1].pose.position.y == 1.0
    assert got[1].pose.yaw == pytest.approx(1.5708, abs=1e-4)

    await backend.save_path(map_id, "巡检一号线", points[:1])   # 覆盖
    assert len((await backend.list_paths(map_id))["巡检一号线"]) == 1

    await backend.remove_path(map_id, "巡检一号线")
    assert await backend.list_paths(map_id) == {}


async def test_新增路径走add_同名覆盖走modify(backend, monkeypatch):
    """厂商把新增和修改拆成两个接口,仿真器对两者反应相同(都落到同一个
    store.set_path),只看结果分不出对错 —— 必须直接盯住发出去的接口名。
    发错了真机会拒绝,这里不会,所以这条分支只能这样测。"""
    map_id = await _ready(backend)
    points = [Waypoint("P1", Pose.from_xy_yaw(1.0, 0.0, 0.0))]

    sent: list[str] = []
    real = backend.request

    async def spy(req):
        sent.append(req.req_func)
        return await real(req)

    monkeypatch.setattr(backend, "request", spy)

    await backend.save_path(map_id, "路线甲", points)
    assert sent[-1] == "add_nav_path"      # save_path 会先 list_paths,取最后一条

    sent.clear()
    await backend.save_path(map_id, "路线甲", points)
    assert sent[-1] == "modify_nav_path"


async def test_导航到点并走到成功(backend):
    await _ready(backend)
    assert await backend.nav_status() is NavStatus.STANDBY
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    await _wait(backend.nav_status, NavStatus.SUCCEED)


async def test_停止导航(backend):
    await _ready(backend)
    await backend.goto(Pose.from_xy_yaw(9.0, 0.0, 0.0))
    await _wait(backend.nav_status, NavStatus.ACTIVE)
    await backend.stop()
    await _wait(backend.nav_status, NavStatus.CANCELLED)


async def test_暂停与继续(backend):
    await _ready(backend)
    await backend.goto(Pose.from_xy_yaw(9.0, 0.0, 0.0))
    await _wait(backend.nav_status, NavStatus.ACTIVE)
    await backend.pause()
    await _wait(backend.nav_status, NavStatus.PAUSE)
    await backend.resume()
    await _wait(backend.nav_status, NavStatus.ACTIVE)


async def test_定位未就绪时导航被拒(backend):
    with pytest.raises(NavRequestError):
        await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))


async def test_重置定位(backend):
    await _ready(backend)
    await backend.reset_localization()
    assert await backend.loc_status() is not None


async def test_读写速度(backend):
    assert set(await backend.get_speed()) == {"x", "y", "z"}
    got = await backend.set_speed(0.9)
    assert got["x"] == pytest.approx(0.9)
    assert (await backend.get_speed())["x"] == pytest.approx(0.9)

    got = await backend.set_speed(0.4, y=0.2, z=1.0)
    assert got == {"x": 0.4, "y": 0.2, "z": 1.0}


async def test_未知状态字符串返回None而不是抛错(backend, monkeypatch):
    """固件加了个新枚举值,不能让巡检整条线挂掉。"""
    async def fake_request(req):
        return "SomeBrandNewStatus"

    monkeypatch.setattr(backend, "request", fake_request)
    assert await backend.nav_status() is None
    assert await backend.loc_status() is None
    assert await backend.mapping_status() is None
