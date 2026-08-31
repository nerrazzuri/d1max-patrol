"""仿真机器人的平面运动学。

刻意做得简单而确定:没有加速度、没有噪声、没有 IO。
它的职责是让"从 A 走到 B 需要一段可控的时间"这件事可被上层复现,
而不是逼真地模拟四足步态。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from d1max_patrol.protocol.nav_types import Pose


def wrap_angle(a: float) -> float:
    """把角度归一化到 (-π, π]。"""
    wrapped = math.remainder(a, 2 * math.pi)
    # remainder 在恰好 -π 时返回 -π,统一到 +π
    return math.pi if wrapped == -math.pi else wrapped


@dataclass
class Planar2DModel:
    """平面上的位姿与朝向目标的趋近。"""

    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0

    # 假设(待真机验证): 以下五个运动参数是为四足机器人选定的代表性值,未在实物上测量过。
    # 在真机验证前,这些数字是推测性的。上层导航状态机的测试已针对这些具体数值标定;
    # 真机测量后若发现差异,需评估是否影响路径跟踪精度或超时行为。
    # 具体要验证的项:
    # 1. linear_speed=0.6 m/s: 实测直线行走速度能否稳定在此数值,加减速过程如何
    # 2. angular_speed=1.2 rad/s: 实测原地转身速度能否达到此数值
    # 3. position_tolerance=0.08 m: 实测到达判定误差范围(≈8 cm),是否充分安全
    # 4. yaw_tolerance=0.10 rad: 实测朝向对齐精度(≈5.7°),是否满足下游路径跟踪要求
    # 5. bearing_threshold=0.20 rad: 实测朝向偏差阈值(≈11.5°),超过此值是否真的应禁止前进

    #: 名义线速度 m/s
    linear_speed: float = 0.6
    #: 名义角速度 rad/s
    angular_speed: float = 1.2
    #: 位置到达容差 m
    position_tolerance: float = 0.08
    #: 朝向到达容差 rad
    yaw_tolerance: float = 0.10
    #: 朝向误差超过此值时只转不走
    bearing_threshold: float = 0.20

    #: 故障注入 slow: 线速度与角速度的统一缩放
    speed_scale: float = 1.0
    #: 故障注入 stuck: 状态照常但完全不动
    frozen: bool = False

    _goal: Pose | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ 目标

    def set_goal(self, pose: Pose) -> None:
        self._goal = pose

    def clear_goal(self) -> None:
        self._goal = None

    def teleport(self, pose: Pose) -> None:
        """直接改变位姿(用于设置初始位置),同时放弃当前目标。"""
        self.x = pose.position.x
        self.y = pose.position.y
        self.yaw = pose.yaw
        self._goal = None

    @property
    def has_goal(self) -> bool:
        return self._goal is not None

    @property
    def goal_distance(self) -> float:
        if self._goal is None:
            return math.inf
        return math.hypot(self._goal.position.x - self.x, self._goal.position.y - self.y)

    @property
    def arrived(self) -> bool:
        if self._goal is None:
            return False
        if self.goal_distance > self.position_tolerance:
            return False
        return abs(wrap_angle(self._goal.yaw - self.yaw)) <= self.yaw_tolerance

    # ------------------------------------------------------------------ 步进

    def pose(self) -> Pose:
        return Pose.from_xy_yaw(self.x, self.y, self.yaw)

    def step(self, dt: float) -> None:
        goal = self._goal
        if goal is None or self.frozen or self.arrived or dt <= 0.0:
            return

        max_turn = self.angular_speed * self.speed_scale * dt
        max_move = self.linear_speed * self.speed_scale * dt

        dx = goal.position.x - self.x
        dy = goal.position.y - self.y
        distance = math.hypot(dx, dy)

        if distance > self.position_tolerance:
            bearing_error = wrap_angle(math.atan2(dy, dx) - self.yaw)
            self.yaw = wrap_angle(self.yaw + _clamp(bearing_error, max_turn))
            if abs(bearing_error) <= self.bearing_threshold:
                # 朝向已经够准,边走边微调
                advance = min(distance, max_move)
                self.x += advance * math.cos(self.yaw)
                self.y += advance * math.sin(self.yaw)
            return

        # 位置到位,只剩终点朝向
        yaw_error = wrap_angle(goal.yaw - self.yaw)
        self.yaw = wrap_angle(self.yaw + _clamp(yaw_error, max_turn))


def _clamp(value: float, limit: float) -> float:
    """把 value 限制在 [-limit, limit]。"""
    return max(-limit, min(limit, value))
