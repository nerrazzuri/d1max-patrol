"""导航状态枚举、故障码与位姿类型。

状态取值逐字照抄 refs/nav-api/自主导航_WEBSOCKET_API.md
§1.3 / §3.6 / §4.3 的状态值说明,以及 §9 的算法故障码。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar


class NavStatus(str, Enum):
    """导航功能状态,见 §3.6。"""

    STANDBY = "StandBy"              # 就绪 —— 仅在此状态下可启动导航
    INITIALIZING = "Initializing"
    ACTIVE = "Active"
    PAUSE = "Pause"
    CANCELLED = "Cancelled"
    SUCCEED = "Succeed"
    FAILED = "Failed"


class LocStatus(str, Enum):
    """定位功能状态,见 §4.3。"""

    INIT = "Init"
    MAP_LOADING = "MapLoading"
    INIT_LOCALIZATION = "InitLocalization"
    CONTINUOUS_LOC = "ContinuousLoc"
    ERROR = "Error"
    DYNAMIC_INIT_LOC = "DynamicInitLoc"
    LOC_LOST = "LocLost"


class MappingStatus(str, Enum):
    """建图功能状态,见 §1.3。"""

    UNKNOWN = "Unknown"
    PASSIVE = "Passive"
    INIT_WAIT_SENSOR = "InitWaitSensor"
    MAPPING_READY = "MappingReady"
    MAPPING_RUNNING = "MappingRunning"
    MAPP_ERROR = "MappError"          # 文档原文如此,不是笔误
    MAPPING_SAVE_BEGIN = "MappingSaveBegin"
    MAPPING_SAVE_END = "MappingSaveEnd"


NAV_TERMINAL_SUCCESS = frozenset({NavStatus.SUCCEED})
NAV_TERMINAL_FAILURE = frozenset({NavStatus.FAILED, NavStatus.CANCELLED})
#: 一次 start_nav 结束的判定集合
NAV_TERMINAL = NAV_TERMINAL_SUCCESS | NAV_TERMINAL_FAILURE

#: 只有持续定位才允许继续行走
LOC_HEALTHY = frozenset({LocStatus.CONTINUOUS_LOC})
#: 设计文档 §6.4 要求 LocLost 时停车;ERROR 状态亦应停车确保安全
LOC_UNUSABLE = frozenset({LocStatus.LOC_LOST, LocStatus.ERROR})

#: 算法故障码,见 §9
ALG_NAV_BLOCKED = 13330
ALG_LIDAR_DISCONNECTED = 13331

_E = TypeVar("_E", bound=Enum)


def parse_enum(cls: type[_E], value: Any) -> _E | None:
    """把线上字符串转成枚举;不认识的取值返回 None 而不是抛异常。

    固件升级可能引入本代码未知的状态值,整条链路不应因此崩溃;
    调用方负责按"未知状态"处理并记日志。
    """
    if not isinstance(value, str):
        return None
    try:
        return cls(value)
    except ValueError:
        return None


def _num(container: dict[str, Any], key: str, where: str, default: float | None = None
         ) -> float:
    if key not in container:
        if default is None:
            raise ValueError(f"{where} 缺少字段 {key}")
        return default
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}.{key} 应为数字,实际为 {value!r}")
    return float(value)


@dataclass(frozen=True)
class Position:
    x: float
    y: float
    z: float = 0.0

    def to_wire(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}

    @classmethod
    def from_wire(cls, raw: Any) -> Position:
        if not isinstance(raw, dict):
            raise ValueError(f"position 应为映射,实际为 {raw!r}")
        return cls(
            x=_num(raw, "x", "position"),
            y=_num(raw, "y", "position"),
            z=_num(raw, "z", "position", default=0.0),
        )


@dataclass(frozen=True)
class Orientation:
    x: float
    y: float
    z: float
    w: float

    def to_wire(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z, "w": self.w}

    @classmethod
    def from_wire(cls, raw: Any) -> Orientation:
        if not isinstance(raw, dict):
            raise ValueError(f"orientation 应为映射,实际为 {raw!r}")
        return cls(
            x=_num(raw, "x", "orientation"),
            y=_num(raw, "y", "orientation"),
            z=_num(raw, "z", "orientation"),
            w=_num(raw, "w", "orientation"),
        )


def yaw_to_orientation(yaw: float) -> Orientation:
    """绕 z 轴的偏航角转四元数。机器人在平面上运动,roll/pitch 恒为 0。"""
    half = yaw / 2.0
    return Orientation(x=0.0, y=0.0, z=math.sin(half), w=math.cos(half))


def orientation_to_yaw(o: Orientation) -> float:
    """四元数取偏航角,结果落在 (-π, π]。"""
    siny_cosp = 2.0 * (o.w * o.z + o.x * o.y)
    cosy_cosp = 1.0 - 2.0 * (o.y * o.y + o.z * o.z)
    return math.atan2(siny_cosp, cosy_cosp)


@dataclass(frozen=True)
class Pose:
    position: Position
    orientation: Orientation

    @classmethod
    def from_xy_yaw(cls, x: float, y: float, yaw: float = 0.0) -> Pose:
        return cls(Position(x, y, 0.0), yaw_to_orientation(yaw))

    @property
    def yaw(self) -> float:
        return orientation_to_yaw(self.orientation)

    def distance_to(self, other: Pose) -> float:
        """平面欧氏距离,忽略 z。"""
        return math.hypot(
            self.position.x - other.position.x,
            self.position.y - other.position.y,
        )

    def to_wire(self) -> dict[str, dict[str, float]]:
        return {
            "position": self.position.to_wire(),
            "orientation": self.orientation.to_wire(),
        }

    @classmethod
    def from_wire(cls, raw: Any) -> Pose:
        if not isinstance(raw, dict):
            raise ValueError(f"pose 应为映射,实际为 {raw!r}")
        if "position" not in raw:
            raise ValueError("pose 缺少 position")
        if "orientation" not in raw:
            raise ValueError("pose 缺少 orientation")
        return cls(
            position=Position.from_wire(raw["position"]),
            orientation=Orientation.from_wire(raw["orientation"]),
        )


@dataclass(frozen=True)
class Waypoint:
    """路径点。线格式是 ["点名", {pose}] 这样的二元列表,见 §2.1。"""

    name: str
    pose: Pose

    def to_wire(self) -> list[Any]:
        return [self.name, self.pose.to_wire()]

    @classmethod
    def from_wire(cls, raw: Any) -> Waypoint:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError(f"路径点应为 [名称, pose] 二元列表,实际为 {raw!r}")
        name, pose_raw = raw
        if not isinstance(name, str):
            raise ValueError(f"路径点名称应为字符串,实际为 {name!r}")
        return cls(name=name, pose=Pose.from_wire(pose_raw))
