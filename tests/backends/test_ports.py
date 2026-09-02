"""三个端口的接口约定。本卷只实现 NavBackend,另两个先把词汇定死。"""

import inspect

import pytest

from d1max_patrol.backends.base import (
    BatteryEvent,
    ControlLostEvent,
    DeviceBackend,
    DevicePoseEvent,
    EventEmitter,
    FaultEvent,
    Frame,
    MediaSource,
    NavBackend,
)
from d1max_patrol.protocol.nav_types import Pose

DEVICE_METHODS = {
    "connect", "close", "acquire_control", "release_control",
    "stand", "lie", "walk", "set_light", "set_gimbal", "take_photo",
    "battery", "emergency", "has_control",
}
MEDIA_METHODS = {"open", "close", "grab", "healthy"}


def _abstracts(cls) -> set[str]:
    return set(cls.__abstractmethods__)


def test_三个端口都不能直接实例化():
    for cls in (NavBackend, DeviceBackend, MediaSource):
        with pytest.raises(TypeError):
            cls()


def test_设备端口的方法集():
    assert _abstracts(DeviceBackend) == DEVICE_METHODS


def test_取图端口的方法集():
    assert _abstracts(MediaSource) == MEDIA_METHODS


def test_所有端口方法都是协程():
    for cls, names in ((DeviceBackend, DEVICE_METHODS), (MediaSource, MEDIA_METHODS)):
        for name in names:
            func = getattr(cls, name)
            assert inspect.iscoroutinefunction(func), f"{cls.__name__}.{name} 应为协程"


def test_导航端口与设备端口共用同一套事件分发():
    assert issubclass(NavBackend, EventEmitter)
    assert issubclass(DeviceBackend, EventEmitter)


def test_事件分发基类可独立使用():
    emitter = EventEmitter()
    queue = emitter.subscribe()
    event = BatteryEvent(percent=42.0, charging=False)
    emitter.emit(event)
    assert queue.get_nowait() is event


def test_设备事件字段():
    assert BatteryEvent(80.0, True).charging is True
    assert FaultEvent(("电机过温",), fatal=True).items == ("电机过温",)
    assert ControlLostEvent("被手柄接管").reason == "被手柄接管"
    assert DevicePoseEvent(Pose.from_xy_yaw(1.0, 2.0)).pose.position.x == 1.0


def test_帧对象():
    frame = Frame(data=b"\xff\xd8", mime="image/jpeg", captured_at_ms=1700000000000)
    assert frame.mime == "image/jpeg"
    with pytest.raises(AttributeError):
        frame.data = b""          # frozen


async def test_返航默认是明确拒绝而不是静默无操作():
    """引擎在电量低的时候调它 —— 静默无操作等于狗停在原地等着没电。"""
    from d1max_patrol.backends.base import NavRequestError

    class _Nav(NavBackend):
        pass

    for name in _abstracts(NavBackend):
        setattr(_Nav, name, lambda self, *a, **k: None)
    _Nav.__abstractmethods__ = frozenset()
    with pytest.raises(NavRequestError, match="返航"):
        await _Nav().return_home()


def test_可选能力默认全支持():
    """新写一个后端忘了声明,顶多是页面多画一个按钮,而不是少画一个。

    反过来(默认全不支持)的话,厂商后端哪天漏了这一行,建图按钮就整个消失,
    而且没人会想到去看这个属性。
    """
    class _Nav(NavBackend):
        pass

    for name in _abstracts(NavBackend):
        setattr(_Nav, name, lambda self, *a, **k: None)
    _Nav.__abstractmethods__ = frozenset()
    assert _Nav().capabilities == NavBackend.ALL_CAPABILITIES


def test_可选能力就这三项():
    """界面按这三个名字灰按钮,加一项要同时改界面 —— 所以定死在这里。"""
    assert NavBackend.ALL_CAPABILITIES == {"mapping", "map_admin", "reloc"}


def test_可选能力不是抽象方法():
    """它是有默认值的属性。要是变成抽象的,现有后端全得改。"""
    assert "capabilities" not in _abstracts(NavBackend)


def test_自建导航一项可选能力都不声明():
    """建图是 ROS 侧离线重建、删改图是文件系统的事、重定位 slam_toolbox 自己管。"""
    from d1max_patrol.backends.local_nav import LocalNavBackend

    assert LocalNavBackend.capabilities.fget(None) == frozenset()
