"""导航状态枚举、故障码与位姿类型。

状态取值逐字照抄 refs/nav-api/自主导航_WEBSOCKET_API.md
§1.3 / §3.6 / §4.3 的状态值说明,以及 §9 的算法故障码。
"""

from __future__ import annotations

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


# 几何类型(Position/Orientation/Pose 与两个换算)W00c2a 起住在契约包里;这里转手同一个类。
from d1max_contract.geometry import (  # noqa: E402,F401 - 转手导出
    Orientation,
    Pose,
    Position,
    _num,
    orientation_to_yaw,
    yaw_to_orientation,
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
