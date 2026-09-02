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
