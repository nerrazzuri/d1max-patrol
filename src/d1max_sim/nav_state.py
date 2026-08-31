"""仿真设备的三套状态机:导航、定位、建图。

状态取值与迁移必须贴住 refs/nav-api 文档,因为契约测试用它们判定
"到点了没有"。凡是文档没写死、由本仿真器补齐的行为,一律在源码里加注待真机验证标记。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus, NavStatus, Pose

from .kinematics import Planar2DModel


class SimRejected(Exception):
    """设备在当前状态下拒绝该操作。仿真器据此回 status: "error"。"""


@dataclass
class _Schedule:
    """一串"多少秒之后发生什么"的延时事件,按入队顺序依次触发。"""

    _queue: list[tuple[float, Any]] = field(default_factory=list)

    def clear(self) -> None:
        self._queue.clear()

    def push(self, delay: float, value: Any) -> None:
        self._queue.append((delay, value))

    def tick(self, dt: float) -> list[tuple[Any, float]]:
        """推进 dt 秒,返回本次触发的 (值, 触发后本次 dt 的剩余量)。"""
        fired: list[tuple[Any, float]] = []
        remaining = dt
        while self._queue and remaining > 0.0:
            delay, value = self._queue[0]
            if delay > remaining:
                self._queue[0] = (delay - remaining, value)
                remaining = 0.0
            else:
                remaining -= delay
                self._queue.pop(0)
                fired.append((value, remaining))
        return fired


# ---------------------------------------------------------------------- 导航

@dataclass
class NavStateMachine:
    """导航功能状态,见 refs/nav-api §3.6。"""

    model: Planar2DModel
    # 假设(待真机验证): 以下两个延时参数是为使导航流程从初始化到活跃有明确的时间演变而设定的。
    # 文档未指定这些时间;在实际机器人上需验证:
    # 1. init_delay_s=0.3 s: 从启动导航到机器人可开始实际行走的初始化阶段耗时
    # 2. terminal_hold_s=0.5 s: 导航到达终态(成功/失败/取消)后回落至待命状态前的驻留时间
    #: StandBy -> Initializing -> Active 的过渡时长
    init_delay_s: float = 0.3
    # 假设(待真机验证): 终态(Succeed/Failed/Cancelled)保持多久后回落 StandBy。
    # 文档只说"仅在 StandBy 下可启动导航",没说终态如何退出,这里补一个短驻留。
    terminal_hold_s: float = 0.5

    status: NavStatus = NavStatus.STANDBY
    #: 一次性开关:下一次 start 直接失败,用于制造"某个航点走不到"
    fail_next_start: bool = False

    _schedule: _Schedule = field(default_factory=_Schedule, repr=False)

    def start(self, pose: Pose) -> None:
        if self.status is not NavStatus.STANDBY:
            raise SimRejected(f"导航只能在 StandBy 下启动,当前 {self.status.value}")
        self._schedule.clear()
        self.model.set_goal(pose)
        self.status = NavStatus.INITIALIZING
        if self.fail_next_start:
            self.fail_next_start = False
            self._schedule.push(self.init_delay_s, NavStatus.FAILED)
        else:
            self._schedule.push(self.init_delay_s, NavStatus.ACTIVE)

    def stop(self) -> None:
        if self.status not in (NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.PAUSE):
            raise SimRejected(f"当前 {self.status.value} 无导航可停止")
        self._enter_terminal(NavStatus.CANCELLED)

    def pause(self) -> None:
        if self.status is not NavStatus.ACTIVE:
            raise SimRejected(f"只能在 Active 下暂停,当前 {self.status.value}")
        self.status = NavStatus.PAUSE

    def resume(self) -> None:
        if self.status is not NavStatus.PAUSE:
            raise SimRejected(f"只能在 Pause 下继续,当前 {self.status.value}")
        self.status = NavStatus.ACTIVE

    def fail(self, reason: str = "") -> None:
        """外部注入导航失败。"""
        if self.status not in (NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.PAUSE):
            raise SimRejected(f"当前 {self.status.value} 无导航可失败")
        self._enter_terminal(NavStatus.FAILED)

    def _enter_terminal(self, status: NavStatus) -> None:
        self._schedule.clear()
        self.model.clear_goal()
        self.status = status
        self._schedule.push(self.terminal_hold_s, NavStatus.STANDBY)

    def step(self, dt: float) -> None:
        move_budget = dt
        for value, remaining in self._schedule.tick(dt):
            self.status = value
            # 进入 Active 之前的那段时间是初始化,不该算作行走
            if value is NavStatus.ACTIVE:
                move_budget = remaining
            elif value is NavStatus.FAILED:
                self.model.clear_goal()
                self._schedule.push(self.terminal_hold_s, NavStatus.STANDBY)
        if self.status is NavStatus.ACTIVE and move_budget > 0.0:
            self.model.step(move_budget)
            if self.model.arrived:
                self._enter_terminal(NavStatus.SUCCEED)


# ---------------------------------------------------------------------- 定位

@dataclass
class LocStateMachine:
    """定位功能状态,见 refs/nav-api §4.3。"""

    # 假设(待真机验证): 以下两个延时参数是地图加载和初始定位的预估耗时。
    # 文档未指定这些时间;在实际机器人上需验证:
    # 1. load_delay_s=0.2 s: 地图加载从启动到完成的耗时
    # 2. init_delay_s=0.3 s: 初始定位从加载完成到定位连续可用的耗时
    load_delay_s: float = 0.2
    init_delay_s: float = 0.3

    status: LocStatus = LocStatus.INIT
    loaded_map_id: str | None = None

    _schedule: _Schedule = field(default_factory=_Schedule, repr=False)

    @property
    def healthy(self) -> bool:
        return self.status is LocStatus.CONTINUOUS_LOC

    def load_map(self, map_id: str) -> None:
        self._schedule.clear()
        self.loaded_map_id = map_id
        self.status = LocStatus.MAP_LOADING
        self._schedule.push(self.load_delay_s, LocStatus.INIT_LOCALIZATION)
        self._schedule.push(self.init_delay_s, LocStatus.CONTINUOUS_LOC)

    def reset(self) -> None:
        self._schedule.clear()
        self.status = LocStatus.INIT_LOCALIZATION
        self._schedule.push(self.init_delay_s, LocStatus.CONTINUOUS_LOC)

    def lose(self) -> None:
        """注入定位丢失。不会自愈,必须显式 recover 或 reset。"""
        self._schedule.clear()
        self.status = LocStatus.LOC_LOST

    def recover(self) -> None:
        self.reset()

    def step(self, dt: float) -> None:
        for value, _ in self._schedule.tick(dt):
            self.status = value


# ---------------------------------------------------------------------- 建图

_MAPPING_STARTABLE = (
    MappingStatus.UNKNOWN,
    MappingStatus.PASSIVE,
    MappingStatus.MAPP_ERROR,
    MappingStatus.MAPPING_SAVE_END,
)


@dataclass
class MappingStateMachine:
    """建图功能状态,见 refs/nav-api §1.3。"""

    # 假设(待真机验证): 以下三个延时参数是建图流程各阶段的预估耗时。
    # 文档未指定这些时间;在实际机器人上需验证:
    # 1. sensor_delay_s=0.2 s: 传感器初始化等待耗时(启动到传感器就绪)
    # 2. ready_delay_s=0.2 s: 建图准备阶段耗时(就绪到实际开始建图)
    # 3. save_delay_s=0.3 s: 地图保存耗时(停止到保存完成)
    sensor_delay_s: float = 0.2
    ready_delay_s: float = 0.2
    save_delay_s: float = 0.3
    #: 保存完成时回调一次,仿真器用它落地图并推送 notify_stop_mapping_status
    on_saved: Callable[[], None] | None = None

    status: MappingStatus = MappingStatus.PASSIVE

    _schedule: _Schedule = field(default_factory=_Schedule, repr=False)

    def start(self) -> None:
        if self.status not in _MAPPING_STARTABLE:
            raise SimRejected(f"当前 {self.status.value} 不能开始建图")
        self._schedule.clear()
        self.status = MappingStatus.INIT_WAIT_SENSOR
        self._schedule.push(self.sensor_delay_s, MappingStatus.MAPPING_READY)
        self._schedule.push(self.ready_delay_s, MappingStatus.MAPPING_RUNNING)

    def stop(self) -> None:
        if self.status is not MappingStatus.MAPPING_RUNNING:
            raise SimRejected(f"当前 {self.status.value} 没有建图任务可停止")
        self._schedule.clear()
        self.status = MappingStatus.MAPPING_SAVE_BEGIN
        self._schedule.push(self.save_delay_s, MappingStatus.MAPPING_SAVE_END)

    def step(self, dt: float) -> None:
        for value, _ in self._schedule.tick(dt):
            self.status = value
            if value is MappingStatus.MAPPING_SAVE_END and self.on_saved is not None:
                self.on_saved()
