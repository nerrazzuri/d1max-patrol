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
from typing import Any, Generic, TypeVar

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


# --------------------------------------------------------------------- 事件分发


_E = TypeVar("_E")


class EventEmitter(Generic[_E]):
    """一对多的事件分发。

    每个消费者拿一条自己的无界队列,互不抢事件。队列无界是刻意的 ——
    广播端绝不能因为某个慢消费者而卡住状态轮询。

    子类若自定义 `__init__`,**必须**调用 `super().__init__()`,否则
    `_subscribers` 不存在,第一次 `subscribe()` 就 AttributeError。
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[_E]] = []

    def subscribe(self) -> asyncio.Queue[_E]:
        queue: asyncio.Queue[_E] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[_E]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    @contextlib.contextmanager
    def subscription(self) -> Iterator[asyncio.Queue[_E]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    def emit(self, event: _E) -> None:
        """向所有订阅者广播。同步、不阻塞、不抛错。"""
        for queue in list(self._subscribers):
            queue.put_nowait(event)


# --------------------------------------------------------------------- 抽象


class NavBackend(EventEmitter[Event], ABC):
    """一台可导航设备。

    订阅模型: 每个消费者拿一条自己的无界队列,互不抢事件。队列无界是刻意的
    —— 广播端绝不能因为某个慢消费者而卡住状态轮询。
    """

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

        传 `queue` 时反过来要当心**陈旧终态**:队列里若还压着上一次导航的
        终态事件,这里会把它当成本次的终态立刻返回。一次导航一条订阅,
        别跨多次导航复用同一条长命队列 —— 详见 `_await_terminal` 的说明。

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
        """从 `queue` 里取到第一条终态事件为止。

        MIN-9: 这里认的是"队列里下一条终态事件",**不是**"本次导航的终态"
        —— 本方法没有、也拿不到任何把事件与某一次 `goto()` 关联起来的凭据
        (协议里没有任务 id)。两面都要记住:

        * 队列是**新订阅**的(`wait_nav_terminal` 不传 `queue=` 的分支):
          订阅之前的事件一条都收不到,所以不会认领陈旧终态;代价是
          `goto()` 与订阅之间那条终态会被漏掉,一路等到超时。
        * 队列是**外部传进来的**(`queue=` 分支,推荐用法):`goto()` 之前的
          那条终态不会漏 —— 但反过来,**这条队列里若还压着上一次导航的
          终态,本方法会把它当成这一次的终态立刻返回**。所以外部队列的正确
          用法是"一次导航一条订阅"(`with backend.subscription() as q:`
          包住 goto + 等待),不要跨多次导航复用同一条长命队列;
          `test_连续走多个点` 就是照这个形状写的。
        """
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


# ------------------------------------------------------------------ 设备端口


class DeviceBackendError(Exception):
    """本体动作或遥测相关错误。"""


@dataclass(frozen=True)
class BatteryEvent:
    #: 电量百分比,量纲 0-100(与 DeviceBackend.battery 一致,非 0-1 的小数)。
    percent: float
    charging: bool = False


@dataclass(frozen=True)
class FaultEvent:
    items: tuple[str, ...]
    fatal: bool = False


@dataclass(frozen=True)
class ControlLostEvent:
    """控制权被别人拿走了。见规范 §5.3 —— 拿不到控制权就不许下动作指令。"""

    reason: str


@dataclass(frozen=True)
class DevicePoseEvent:
    pose: Pose


DeviceEvent = BatteryEvent | FaultEvent | ControlLostEvent | DevicePoseEvent


class DeviceBackend(EventEmitter[DeviceEvent], ABC):
    """本体动作与遥测。

    本卷不实现 —— 它要靠 C++ 旁路进程(第 2 卷)。这里先把词汇定死,
    免得第 2 卷写着写着又发明一套名字。

    真理源规则(规范 §3.4): 导航进展以 NavBackend 为准,本端口的遥测
    只作交叉校验与安全兜底。两边不一致时停车记异常,不做猜测性推断。
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def acquire_control(self) -> None:
        """申请控制权。拿不到时抛 DeviceBackendError。"""

    @abstractmethod
    async def release_control(self) -> None: ...

    @abstractmethod
    async def has_control(self) -> bool: ...

    @abstractmethod
    async def stand(self) -> None: ...

    @abstractmethod
    async def lie(self) -> None: ...

    @abstractmethod
    async def walk(self, seconds: float, forward: float,
                   lateral: float = 0.0, yaw: float = 0.0) -> None:
        """开环直驱行走,固定时长后自行停下。

        本卷把它提到端口上,是因为自建导航(``LocalNavBackend``)整条路线都
        踩在它上面 —— 那条路线没有厂商的规划器,"走到某个点"就是一串脉冲式
        的 ``walk``。任何 DeviceBackend 实现少了它,乙路线就无从谈起。

        量纲是**百分比**不是 m/s(清单 #37/#38),而且有不小的死区:
        ``forward=0.11`` 几乎不动,``0.3~0.5`` 才真的走。上层选控制量时
        必须避开死区 —— 这正是自建导航不接 Nav2 的原因(Nav2 末段会把速度
        降到 0.05 量级,在这台机器上会被静默吞掉)。
        """

    @abstractmethod
    async def set_light(self, on: bool) -> None: ...

    @abstractmethod
    async def set_gimbal(self, pitch: float, yaw: float) -> None:
        # 假设(待真机验证): 云台角度单位取 rad。厂商文档未给单位,常见做法
        # 是度数,接真机时第一件事就是确认这个。
        """云台角度,单位 rad。"""

    @abstractmethod
    async def take_photo(self) -> Frame:
        # 假设(待真机验证): 拍照直接回图像字节。若真机只回一个文件路径或
        # 一个 URL,这个签名要改成回路径,由上层再去取。
        """用机身相机拍一张。MediaSource 的备用取图路径。"""

    @abstractmethod
    async def battery(self) -> float:
        # 假设(待真机验证): 量纲取 0-100 的百分数,不是 0-1 的小数。
        """当前电量百分比 (0-100)。"""


# ------------------------------------------------------------------ 取图端口


class MediaError(Exception):
    """取图失败。"""


@dataclass(frozen=True)
class Frame:
    """一帧图像。"""

    data: bytes
    mime: str
    captured_at_ms: int


class MediaSource(ABC):
    """到点取图。

    本卷不实现。主路径是 RTSP 抽帧,备用路径是 DeviceBackend.take_photo。
    不继承 EventEmitter —— 取图是请求式的,没有事件流。
    """

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def grab(self) -> Frame:
        """取一帧。取不到时抛 MediaError。"""

    @abstractmethod
    async def healthy(self) -> bool:
        """流是否还活着。用于巡检前的预检。"""
