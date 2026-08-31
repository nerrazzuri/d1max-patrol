"""NavBackend 的行为约定。

只依赖 backends.base 与 protocol.nav_types。见本目录 README 的规矩。
"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    LocStatusEvent,
    MappingStatusEvent,
    NavRequestError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.protocol.nav_types import (
    NAV_TERMINAL,
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)

pytestmark = pytest.mark.contract


async def _drain_until(queue, predicate, timeout_s: float = 15.0):
    async def loop():
        while True:
            event = await queue.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(loop(), timeout=timeout_s)


async def _wait_status(backend, wanted: NavStatus, timeout_s: float = 5.0) -> None:
    """轮询等 `nav_status()` 落到 `wanted`。

    仿真器终态之后有个短驻留(`terminal_hold_s`)才回落 StandBy,期间
    再次 `goto()` 会被拒绝——这是设备状态机本身的约束,不是竞态,只许等。
    """
    async def loop():
        while await backend.nav_status() is not wanted:
            await asyncio.sleep(0.02)
    await asyncio.wait_for(loop(), timeout=timeout_s)


# --------------------------------------------------------------- 建图与地图


async def test_初始没有地图(nav_backend):
    assert await nav_backend.list_maps() == []


async def test_建图产出一张可用的地图(ready_backend):
    backend, map_id = ready_backend
    assert map_id in await backend.list_maps()
    assert await backend.mapping_status() is MappingStatus.MAPPING_SAVE_END


async def test_栅格地图形状合法(ready_backend):
    backend, map_id = ready_backend
    grid = await backend.get_map_grid(map_id)
    info = grid["info"]
    assert info["width"] > 0 and info["height"] > 0
    assert info["resolution"] > 0
    assert len(grid["data"]) == info["width"] * info["height"]


async def test_重命名后旧名字消失(ready_backend):
    backend, map_id = ready_backend
    await backend.rename_map(map_id, "契约图")
    names = await backend.list_maps()
    assert "契约图" in names and map_id not in names


async def test_删除地图(ready_backend):
    backend, map_id = ready_backend
    await backend.remove_maps([map_id])
    assert await backend.list_maps() == []


# --------------------------------------------------------------- 路径


async def test_路径的保存读取覆盖与删除(ready_backend):
    backend, map_id = ready_backend
    assert await backend.list_paths(map_id) == {}

    points = [Waypoint("配电柜", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
              Waypoint("水泵", Pose.from_xy_yaw(1.0, 1.0, 1.5))]
    await backend.save_path(map_id, "线路A", points)

    paths = await backend.list_paths(map_id)
    assert [w.name for w in paths["线路A"]] == ["配电柜", "水泵"]
    assert paths["线路A"][1].pose.position.y == pytest.approx(1.0)

    await backend.save_path(map_id, "线路A", points[:1])
    assert len((await backend.list_paths(map_id))["线路A"]) == 1

    await backend.remove_path(map_id, "线路A")
    assert await backend.list_paths(map_id) == {}


async def test_空路径也能存(ready_backend):
    """厂商 App 里画了一半的路径,读回来不能炸。"""
    backend, map_id = ready_backend
    await backend.save_path(map_id, "空线", [])
    assert (await backend.list_paths(map_id))["空线"] == []


# --------------------------------------------------------------- 定位


async def test_加载地图后定位健康(ready_backend):
    backend, _ = ready_backend
    assert await backend.loc_status() is LocStatus.CONTINUOUS_LOC


async def test_定位与建图的状态变化也产生事件(nav_backend):
    """契约不只保证导航事件:建图与定位的变化,订阅者同样收得到。"""
    queue = nav_backend.subscribe()

    await nav_backend.start_mapping()
    await _drain_until(queue, lambda e: isinstance(e, MappingStatusEvent)
                       and e.status is MappingStatus.MAPPING_RUNNING)
    await nav_backend.stop_mapping()
    await _drain_until(queue, lambda e: isinstance(e, MappingStatusEvent)
                       and e.status is MappingStatus.MAPPING_SAVE_END)

    map_id = (await nav_backend.list_maps())[0]
    await nav_backend.load_map(map_id)
    await _drain_until(queue, lambda e: isinstance(e, LocStatusEvent)
                       and e.status is LocStatus.CONTINUOUS_LOC)


# --------------------------------------------------------------- 导航


async def test_导航到点走到成功(ready_backend):
    backend, _ = ready_backend
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED
    assert await backend.nav_status() in NAV_TERMINAL | {NavStatus.STANDBY}


async def test_连续走多个点(ready_backend):
    """全逐点执行:路线由我们排序,后端只管一次一个点。"""
    backend, _ = ready_backend
    for target in (Pose.from_xy_yaw(1.0, 0.0, 0.0),
                   Pose.from_xy_yaw(1.0, 1.0, 1.5708),
                   Pose.from_xy_yaw(0.0, 0.0, 3.1416)):
        await _wait_status(backend, NavStatus.STANDBY)
        waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=20.0))
        await asyncio.sleep(0.05)
        await backend.goto(target)
        assert await waiter is NavStatus.SUCCEED


async def test_停止导航进入取消(ready_backend):
    backend, _ = ready_backend
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await backend.goto(Pose.from_xy_yaw(9.0, 0.0, 0.0))
    await asyncio.sleep(0.2)
    await backend.stop()
    assert await waiter is NavStatus.CANCELLED


async def test_暂停与继续(ready_backend):
    backend, _ = ready_backend
    queue = backend.subscribe()
    await backend.goto(Pose.from_xy_yaw(3.0, 0.0, 0.0))
    await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                       and e.status is NavStatus.ACTIVE)
    await backend.pause()
    await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                       and e.status is NavStatus.PAUSE)
    await backend.resume()
    await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                       and e.status is NavStatus.SUCCEED)


async def test_未加载定位地图时导航被拒(nav_backend):
    with pytest.raises(NavRequestError):
        await nav_backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))


async def test_等待终态会超时而不是永久挂起(ready_backend):
    backend, _ = ready_backend
    with pytest.raises(NavTimeoutError):
        await backend.wait_nav_terminal(timeout_s=0.2)


# --------------------------------------------------------------- 速度


async def test_速度读写往返(nav_backend):
    speed = await nav_backend.get_speed()
    assert {"x", "y", "z"} <= set(speed)
    assert all(isinstance(v, float) for v in speed.values())

    await nav_backend.set_speed(0.5, y=0.3, z=1.0)
    got = await nav_backend.get_speed()
    assert got["x"] == pytest.approx(0.5)
    assert got["y"] == pytest.approx(0.3)
    assert got["z"] == pytest.approx(1.0)


# --------------------------------------------------------------- 事件订阅


async def test_多个订阅者互不抢事件(ready_backend):
    backend, _ = ready_backend
    a, b = backend.subscribe(), backend.subscribe()
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    ea = await _drain_until(a, lambda e: isinstance(e, NavStatusEvent)
                            and e.status is NavStatus.SUCCEED)
    eb = await _drain_until(b, lambda e: isinstance(e, NavStatusEvent)
                            and e.status is NavStatus.SUCCEED)
    assert ea.status is eb.status


async def test_退订后不再收事件(ready_backend):
    backend, _ = ready_backend
    queue = backend.subscribe()
    backend.unsubscribe(queue)
    while not queue.empty():
        queue.get_nowait()
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    await asyncio.sleep(0.5)
    assert queue.empty()


# --------------------------------------------------------------- 生命周期


async def test_关闭后不再连接(nav_backend):
    await nav_backend.close()
    assert nav_backend.connected is False
    await nav_backend.close()          # 可重复调用
