"""导航后端抽象层。这里只测与厂商无关的那部分:订阅、广播、终态等待。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    LocStatusEvent,
    NavBackend,
    NavBackendError,
    NavRequestError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus


class _Stub(NavBackend):
    """只实现抽象方法签名,不做任何事 —— 用来测基类的具体逻辑。"""

    async def connect(self): ...
    async def close(self): ...
    async def list_maps(self): return []
    async def remove_maps(self, map_ids): ...
    async def rename_map(self, old_id, new_id): ...
    async def get_map_grid(self, map_id): return {}
    async def start_mapping(self): ...
    async def stop_mapping(self): ...
    async def mapping_status(self): return None
    async def list_paths(self, map_id): return {}
    async def save_path(self, map_id, path_id, waypoints): ...
    async def remove_path(self, map_id, path_id): ...
    async def load_map(self, map_id): ...
    async def reset_localization(self): ...
    async def loc_status(self): return None
    async def goto(self, pose): ...
    async def stop(self): ...
    async def pause(self): ...
    async def resume(self): ...
    async def nav_status(self): return None
    async def get_speed(self): return {}
    async def set_speed(self, x, y=None, z=None): return {}


def test_抽象类不能直接实例化():
    with pytest.raises(TypeError):
        NavBackend()


def test_异常层次():
    for exc in (NavTimeoutError, NavRequestError, NavBackendError):
        assert issubclass(exc, Exception)
    assert issubclass(NavTimeoutError, NavBackendError)
    assert issubclass(NavRequestError, NavBackendError)


def test_请求错误带上接口名与设备消息():
    exc = NavRequestError("start_nav", "定位未就绪")
    assert exc.req_func == "start_nav"
    assert exc.message == "定位未就绪"
    assert "start_nav" in str(exc) and "定位未就绪" in str(exc)


async def test_订阅者各自收到全部事件():
    backend = _Stub()
    a, b = backend.subscribe(), backend.subscribe()
    event = NavStatusEvent(NavStatus.ACTIVE, NavStatus.INITIALIZING)
    backend.emit(event)
    assert await a.get() is event
    assert await b.get() is event


async def test_退订后不再收到事件():
    backend = _Stub()
    q = backend.subscribe()
    backend.unsubscribe(q)
    backend.emit(BackendDisconnected("test"))
    assert q.empty()


async def test_没有订阅者时广播不报错():
    _Stub().emit(BackendDisconnected("nobody listening"))


async def test_订阅上下文管理器自动退订():
    backend = _Stub()
    with backend.subscription() as q:
        backend.emit(BackendDisconnected("x"))
        assert q.qsize() == 1
    backend.emit(BackendDisconnected("y"))
    assert q.qsize() == 1


async def test_等待导航终态():
    backend = _Stub()

    async def drive():
        await asyncio.sleep(0.01)
        backend.emit(NavStatusEvent(NavStatus.ACTIVE, NavStatus.INITIALIZING))
        await asyncio.sleep(0.01)
        backend.emit(NavStatusEvent(NavStatus.SUCCEED, NavStatus.ACTIVE))

    task = asyncio.create_task(drive())
    assert await backend.wait_nav_terminal(timeout_s=2.0) is NavStatus.SUCCEED
    await task


async def test_等待导航终态_失败也是终态():
    backend = _Stub()
    asyncio.get_running_loop().call_later(
        0.01, backend.emit, NavStatusEvent(NavStatus.FAILED, NavStatus.ACTIVE))
    assert await backend.wait_nav_terminal(timeout_s=2.0) is NavStatus.FAILED


async def test_等待导航终态_超时():
    backend = _Stub()
    with pytest.raises(NavTimeoutError):
        await backend.wait_nav_terminal(timeout_s=0.05)


async def test_等待导航终态_期间断链直接抛错():
    """断链后再等终态没有意义 —— 状态已经不可知,必须让调用方立刻知道。"""
    backend = _Stub()
    asyncio.get_running_loop().call_later(
        0.01, backend.emit, BackendDisconnected("链路断开"))
    with pytest.raises(NavBackendError, match="链路断开"):
        await backend.wait_nav_terminal(timeout_s=2.0)


async def test_等待导航终态_忽略无关事件():
    backend = _Stub()

    async def noise():
        await asyncio.sleep(0.01)
        backend.emit(LocStatusEvent(LocStatus.CONTINUOUS_LOC, LocStatus.INIT))
        backend.emit(AlgErrorEvent((AlgErrorItem(13330, "路径被挡", 2),), 1))
        backend.emit(NavStatusEvent(NavStatus.SUCCEED, NavStatus.ACTIVE))

    task = asyncio.create_task(noise())
    assert await backend.wait_nav_terminal(timeout_s=2.0) is NavStatus.SUCCEED
    await task


async def test_慢订阅者不阻塞广播():
    """队列无界:仿真器狂推故障码时,广播端绝不能卡住。"""
    backend = _Stub()
    q = backend.subscribe()
    for _ in range(1000):
        backend.emit(BackendDisconnected("flood"))
    assert q.qsize() == 1000
