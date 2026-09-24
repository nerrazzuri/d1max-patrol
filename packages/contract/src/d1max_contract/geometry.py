"""平面几何类型:位置、朝向(四元数)、位姿。W00c2a 从根包 ``protocol/nav_types.py`` 原样搬来
—— 任务点位用它,站点要解析任务,而站点只依赖契约包。根包转手的是**同一个类**。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


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
