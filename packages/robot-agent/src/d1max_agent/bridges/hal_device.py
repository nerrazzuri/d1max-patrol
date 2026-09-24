"""``HalDeviceBackend``:RobotHAL → DeviceBackend。引擎问它电量、急停、控制权;
灯与云台在 HAL 报没有时变成记日志的空操作(任务动作不该因为这台狗没灯而中止);
拍照不归它(媒体走 ``MediaSource``),``take_photo`` 抛 ``MediaError``;
``stand/lie/walk`` 是 SDK 级动作,桥不做;``halt`` = ``hal.stop()``。

``step(dt)``:控制权由有变无 → ``ControlLostEvent``;每拍一条 ``BatteryEvent``
(引擎的低电返航/中止靠它)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from d1max_contract.hal import HalUnsupported, RobotHAL
from d1max_patrol.backends.base import (
    BatteryEvent,
    ControlLostEvent,
    DeviceBackend,
    DeviceBackendError,
    Frame,
    MediaError,
)

log = logging.getLogger(__name__)


class HalDeviceBackend(DeviceBackend):
    def __init__(self, hal: RobotHAL, *, now_ms: Callable[[], int]) -> None:
        super().__init__()
        self._hal = hal
        self._now = now_ms
        self._had_control = False
        self._light_warned = self._gimbal_warned = False

    async def connect(self) -> None:
        await self._hal.connect()

    async def close(self) -> None:
        await self._hal.close()

    async def acquire_control(self) -> None:
        await self._hal.acquire_control()
        self._had_control = True

    async def release_control(self) -> None:
        await self._hal.release_control()
        self._had_control = False

    async def has_control(self) -> bool:
        return (await self._hal.control_status()).held

    async def stand(self) -> None:
        raise DeviceBackendError("HAL 桥不做站立:那是 SDK 级动作,归 adapter-d1max")

    async def lie(self) -> None:
        raise DeviceBackendError("HAL 桥不做趴下:那是 SDK 级动作,归 adapter-d1max")

    async def walk(self, seconds: float, forward: float, lateral: float = 0.0,
                   yaw: float = 0.0) -> None:
        raise DeviceBackendError("HAL 桥不做定时行走:导航走 NavBackend")

    async def halt(self) -> None:
        await self._hal.stop()

    async def emergency_stop(self, on: bool = True) -> None:
        await self._hal.emergency_stop(on)

    async def set_light(self, on: bool) -> None:
        try:
            await self._hal.light("front", on)
        except HalUnsupported:
            if not self._light_warned:
                log.info("这台机器没有灯,set_light 按空操作处理(只提示这一次)")
                self._light_warned = True

    async def set_gimbal(self, pitch: float, yaw: float) -> None:
        try:
            await self._hal.head(yaw, pitch)
        except HalUnsupported:
            if not self._gimbal_warned:
                log.info("这台机器没有云台,set_gimbal 按空操作处理(只提示这一次)")
                self._gimbal_warned = True

    async def take_photo(self) -> Frame:
        raise MediaError("拍照不走设备桥,走 MediaSource")

    async def battery(self) -> float:
        return (await self._hal.battery()).percent

    async def emergency(self) -> bool:
        return await self._hal.estop_status()

    async def step(self, dt_s: float) -> None:
        """每拍:控制权丢失事件、电量事件。"""
        held = (await self._hal.control_status()).held
        if self._had_control and not held:
            self.emit(ControlLostEvent(reason="HAL 报控制权不在手里"))
        self._had_control = held
        b = await self._hal.battery()
        self.emit(BatteryEvent(percent=b.percent, charging=b.charging))
