"""导航后端抽象。

上层任务逻辑只依赖这个文件。厂商 WebSocket 协议、Nav2 action、乃至将来
某个新固件的怪癖,都被关在各自的实现类里。

命名刻意去厂商化: `goto` 而不是 `start_nav`,`load_map` 而不是
`loc_load_map`。一旦上层出现厂商接口名,这层抽象就失效了。
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import (
    NAV_TERMINAL,
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)

# --------------------------------------------------------------------- 异常


class NavBackendError(Exception):
    """导航后端相关错误的基类。"""


class NavConnectionError(NavBackendError):
    """连不上,或链路中断。"""


class NavTimeoutError(NavBackendError):
    """等待响应或等待状态变化超时。"""


class NavRequestError(NavBackendError):
    """设备明确回了 error。"""

    def __init__(self, operation: str, message: str) -> None:
        super().__init__(f"{operation} 被设备拒绝: {message}")
        #: 被拒绝的操作名。厂商后端把自己的 req_func 映射进来,
        #: Nav2 后端映射自己的 action 名 —— 这一层不认识 req_func 这个词。
        self.operation = operation
        self.message = message


# --------------------------------------------------------------------- 事件


@dataclass(frozen=True)
class NavStatusEvent:
    status: NavStatus
    previous: NavStatus | None = None


@dataclass(frozen=True)
class LocStatusEvent:
    status: LocStatus
    previous: LocStatus | None = None


@dataclass(frozen=True)
class MappingStatusEvent:
    status: MappingStatus
    previous: MappingStatus | None = None


@dataclass(frozen=True)
class AlgErrorEvent:
    items: tuple[AlgErrorItem, ...]
    time_stamp_ms: int = 0


@dataclass(frozen=True)
class BackendDisconnected:
    reason: str


@dataclass(frozen=True)
class BackendReconnected:
    pass


Event = (
    NavStatusEvent
    | LocStatusEvent
    | MappingStatusEvent
    | AlgErrorEvent
    | BackendDisconnected
    | BackendReconnected
)


# --------------------------------------------------------------------- 抽象


class NavBackend(ABC):
    """一台可导航设备。

    订阅模型: 每个消费者拿一条自己的无界队列,互不抢事件。队列无界是刻意的
    —— 广播端绝不能因为某个慢消费者而卡住状态轮询。
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []

    # ------------------------------------------------------------ 事件分发

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    @contextlib.contextmanager
    def subscription(self) -> Iterator[asyncio.Queue[Event]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    def emit(self, event: Event) -> None:
        """向所有订阅者广播。同步、不阻塞、不抛错。"""
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def wait_nav_terminal(
        self, timeout_s: float,
        queue: asyncio.Queue[Event] | None = None,
    ) -> NavStatus:
        """等到导航进入终态。

        **有竞态的用法**:先 `await goto()` 再调用本方法。终态可能在这两步
        之间就推过来了,那条事件没人订阅、直接丢掉,本方法一路等到超时。

        **没有竞态的用法**:自己先订阅,再下发,再把队列交给本方法 ——
        订阅与下发之间没有 await,不存在让出点::

            with backend.subscription() as q:
                await backend.goto(pose)
                status = await backend.wait_nav_terminal(30.0, queue=q)

        不传 `queue` 时本方法自己订阅,只适合"订阅时导航已经在跑"的场合。

        注:用 `asyncio.wait_for` 而不是 `asyncio.timeout` —— 后者要
        Python 3.11+,而全局约束是 3.10。
        """
        if queue is not None:
            return await self._wait_on(queue, timeout_s)
        with self.subscription() as own_queue:
            return await self._wait_on(own_queue, timeout_s)

    async def _wait_on(
        self, queue: asyncio.Queue[Event], timeout_s: float,
    ) -> NavStatus:
        try:
            return await asyncio.wait_for(self._await_terminal(queue), timeout_s)
        except asyncio.TimeoutError as exc:
            raise NavTimeoutError(f"等待导航终态超过 {timeout_s}s") from exc

    async def _await_terminal(self, queue: asyncio.Queue[Event]) -> NavStatus:
        while True:
            event = await queue.get()
            if isinstance(event, BackendDisconnected):
                raise NavConnectionError(
                    f"等待导航终态期间链路断开: {event.reason}")
            if isinstance(event, NavStatusEvent) and event.status in NAV_TERMINAL:
                return event.status

    # ------------------------------------------------------------ 生命周期

    @abstractmethod
    async def connect(self) -> None:
        """建立连接。已连接时应为空操作。"""

    @abstractmethod
    async def close(self) -> None:
        """断开并释放所有后台任务。可重复调用。"""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """链路当前是否可用。"""

    # ------------------------------------------------------------ 地图

    @abstractmethod
    async def list_maps(self) -> list[str]: ...

    @abstractmethod
    async def remove_maps(self, map_ids: Sequence[str]) -> None: ...

    @abstractmethod
    async def rename_map(self, old_id: str, new_id: str) -> None: ...

    @abstractmethod
    async def get_map_grid(self, map_id: str) -> dict[str, Any]:
        """返回 ROS OccupancyGrid 形状的栅格地图。"""

    # ------------------------------------------------------------ 建图

    @abstractmethod
    async def start_mapping(self) -> None: ...

    @abstractmethod
    async def stop_mapping(self) -> None: ...

    @abstractmethod
    async def mapping_status(self) -> MappingStatus | None:
        """未知状态返回 None,而不是抛错 —— 固件可能新增枚举值。"""

    # ------------------------------------------------------------ 路径

    @abstractmethod
    async def list_paths(self, map_id: str) -> dict[str, list[Waypoint]]: ...

    @abstractmethod
    async def save_path(self, map_id: str, path_id: str,
                        waypoints: Sequence[Waypoint]) -> None:
        """新增或覆盖一条路径。"""

    @abstractmethod
    async def remove_path(self, map_id: str, path_id: str) -> None: ...

    # ------------------------------------------------------------ 定位

    @abstractmethod
    async def load_map(self, map_id: str) -> None:
        """加载定位地图。返回时只表示指令被接受,不表示定位已就绪。"""

    @abstractmethod
    async def reset_localization(self) -> None: ...

    @abstractmethod
    async def loc_status(self) -> LocStatus | None: ...

    # ------------------------------------------------------------ 导航

    @abstractmethod
    async def goto(self, pose: Pose) -> None:
        """下发单点导航。返回时只表示指令被接受,不表示已到达。"""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def resume(self) -> None: ...

    @abstractmethod
    async def nav_status(self) -> NavStatus | None: ...

    # ------------------------------------------------------------ 速度

    @abstractmethod
    async def get_speed(self) -> dict[str, float]:
        """返回 {"x": .., "y": .., "z": ..},单位 m/s 与 rad/s。"""

    @abstractmethod
    async def set_speed(self, x: float, y: float | None = None,
                        z: float | None = None) -> dict[str, float]:
        """设置速度,返回设备回报的生效值(可能补齐了未传的分量)。"""
