"""SimRobot:RobotHAL 的假实现(总设计 §5 adapter-sim)。

刻意简单而确定:没有噪声、没有 IO、时钟注入。它的职责是让代理与站点的测试能
复现「速度命令 → 位姿变化 → 停止确认」这条链,以及 HAL 的几条安全互锁:
没有控制权拒速度、急停拒速度、``ttl`` 到期自停、停止请求与确认分开。

W00 只实现运动/停止/急停/里程/电池/故障/控制权;其余原语抛 :class:`HalUnsupported`
且在 :meth:`hal_capabilities` 里报 ``false``——契约测试钉「声明了就得有入口,没声明就得抛」。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from d1max_contract.hal import (
    Battery,
    ControlStatus,
    Fault,
    HalCapabilities,
    HalUnsupported,
    Health,
    MotionStatus,
    Odometry,
    VelocityCommand,
    VelocityResult,
)

ADAPTER_ID = "sim/0.1.0"


def wrap_angle(a: float) -> float:
    wrapped = math.remainder(a, 2 * math.pi)
    return math.pi if wrapped == -math.pi else wrapped


class SimRobot:
    """``now_ms`` 是注入的钟;``tick(dt_s)`` 推进一步。两者由调用方保持一致。"""

    #: 代理合成能力时写进 ``adapter`` 字段。
    adapter_id = ADAPTER_ID

    def __init__(self, *, now_ms: Callable[[], int], max_vx: float = 1.0, max_wz: float = 1.5,
                 deadband_vx: float = 0.05, stop_latency_s: float = 0.2,
                 battery_drain_pct_per_h: float = 8.0, frame_id: str = "odom") -> None:
        self._now = now_ms
        self.max_vx, self.max_wz, self.deadband_vx = max_vx, max_wz, deadband_vx
        self._stop_latency_s = stop_latency_s
        self._frame = frame_id
        self._connected = False
        self._control = False
        self._estop = False
        self._loc_lost = False
        self._faults: list[Fault] = []
        # 位姿与速度
        self.x = self.y = self.yaw = 0.0
        self._vx = self._wz = 0.0                 # 当前实际速度
        self._cmd_vx = self._cmd_wz = 0.0         # 最近一条有效命令
        self._cmd_deadline_ms: int | None = None
        self._stopping_left_s: float | None = None
        # 电池
        self._battery_pct = 100.0
        self._battery_drain = battery_drain_pct_per_h
        self._battery_t0 = now_ms()

    # ------------------------------------------------------------ 注入口(测试用)

    def inject_control_lost(self) -> None:
        self._control = False
        self._zero()

    def inject_loc_lost(self, lost: bool) -> None:
        self._loc_lost = lost

    def inject_battery(self, pct: float) -> None:
        self._battery_pct = pct
        self._battery_t0 = self._now()

    def inject_fault(self, code: str, fatal: bool, text: str = "") -> None:
        self._faults.append(Fault(code=code, fatal=fatal, text=text))

    def teleport(self, x: float, y: float, yaw: float) -> None:
        self.x, self.y, self.yaw = x, y, yaw

    # ------------------------------------------------------------ 时间推进

    def tick(self, dt_s: float) -> None:
        """推进 dt:先看互锁(急停/控制权/ttl),再制动或按命令走,最后积分位姿。"""
        if dt_s <= 0:
            return
        now = self._now()
        if self._estop or not self._control:
            self._zero()
        elif self._cmd_deadline_ms is not None and now >= self._cmd_deadline_ms:
            self._zero()                          # ttl 到期无新命令:自停
        elif self._stopping_left_s is not None:
            self._stopping_left_s -= dt_s
            if self._stopping_left_s <= 1e-9:
                self._stopping_left_s = None
                self._vx = self._wz = 0.0
                self._cmd_vx = self._cmd_wz = 0.0
                self._cmd_deadline_ms = None
        else:
            self._vx, self._wz = self._cmd_vx, self._cmd_wz
        self.yaw = wrap_angle(self.yaw + self._wz * dt_s)
        self.x += self._vx * math.cos(self.yaw) * dt_s
        self.y += self._vx * math.sin(self.yaw) * dt_s

    def _zero(self) -> None:
        self._vx = self._wz = 0.0
        self._cmd_vx = self._cmd_wz = 0.0
        self._cmd_deadline_ms = None
        self._stopping_left_s = None

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        # SDK 断连即停止输出(总设计 §2.1):控制权也随链路一起没了。
        self._connected = False
        self._control = False
        self._zero()

    async def health(self) -> Health:
        return Health(link_ok=self._connected, control=self._control, estop=self._estop,
                      faults=tuple(f.code for f in self._faults),
                      loc_quality=0.0 if self._loc_lost else 1.0)

    # ------------------------------------------------------------ 控制权

    async def acquire_control(self) -> None:
        if not self._connected:
            raise RuntimeError("还没 connect()")
        self._control = True

    async def release_control(self) -> None:
        self._control = False
        self._zero()

    async def control_status(self) -> ControlStatus:
        return ControlStatus(held=self._control, releasable=True)

    # ------------------------------------------------------------ 运动

    async def motion_status(self) -> MotionStatus:
        if not self._connected:
            return MotionStatus.UNKNOWN
        return MotionStatus.READY if self._control and not self._estop else MotionStatus.STANDING

    async def set_motion_mode(self, mode: str) -> None:
        raise HalUnsupported("sim 只有一种运动模式")

    async def set_velocity(self, cmd: VelocityCommand) -> VelocityResult:
        if not self._control:
            return VelocityResult(0.0, 0.0, clamped=False, rejected=True, reason="no_control")
        if self._estop:
            return VelocityResult(0.0, 0.0, clamped=False, rejected=True, reason="estop")
        if abs(cmd.vy) > 1e-9:
            return VelocityResult(0.0, 0.0, clamped=False, rejected=True, reason="no_lateral")
        if 0.0 < abs(cmd.vx) < self.deadband_vx:
            return VelocityResult(0.0, 0.0, clamped=False, rejected=True, reason="deadband")
        vx = max(-self.max_vx, min(self.max_vx, cmd.vx))
        wz = max(-self.max_wz, min(self.max_wz, cmd.wz))
        clamped = (vx != cmd.vx) or (wz != cmd.wz)
        self._cmd_vx, self._cmd_wz = vx, wz
        self._cmd_deadline_ms = self._now() + cmd.ttl_ms
        self._stopping_left_s = None
        return VelocityResult(vx, wz, clamped=clamped, rejected=False)

    # ------------------------------------------------------------ 停止与急停

    async def stop(self) -> None:
        """停止**请求**。确认要等 ``stop_latency_s`` 的 tick 走完 —— 停止时间是测出来的参数。"""
        if self._vx == 0.0 and self._wz == 0.0 and self._cmd_vx == 0.0 and self._cmd_wz == 0.0:
            self._stopping_left_s = None
            return
        self._stopping_left_s = self._stop_latency_s

    async def stopped(self) -> bool:
        return self._vx == 0.0 and self._wz == 0.0 and self._stopping_left_s is None

    async def emergency_stop(self, on: bool) -> None:
        self._estop = on
        if on:
            self._zero()

    async def estop_status(self) -> bool:
        return self._estop

    async def estop_reset(self) -> None:
        self._estop = False                      # 不恢复任务:速度仍为零,等新命令

    # ------------------------------------------------------------ 感知

    async def odometry(self) -> Odometry:
        return Odometry(stamp_ms=self._now(), frame_id=self._frame, x=self.x, y=self.y,
                        yaw=self.yaw, vx=self._vx, wz=self._wz, valid=not self._loc_lost)

    async def imu(self) -> Any:
        raise HalUnsupported("sim 没有 imu")

    async def lidar(self) -> Any:
        raise HalUnsupported("sim 没有 lidar")

    async def ultrasonic(self) -> Any:
        raise HalUnsupported("sim 没有超声")

    async def joints(self) -> Any:
        raise HalUnsupported("sim 没有关节流")

    async def contacts(self) -> Any:
        raise HalUnsupported("sim 没有触地")

    # ------------------------------------------------------------ 电池与故障

    async def battery(self) -> Battery:
        hours = max(0, self._now() - self._battery_t0) / 3_600_000
        pct = max(0.0, self._battery_pct - hours * self._battery_drain)
        return Battery(percent=pct, charging=False)

    async def faults(self) -> tuple[Fault, ...]:
        return tuple(self._faults)

    # ------------------------------------------------------------ 执行器与媒体(都没有)

    async def light(self, channel: str, on: bool) -> None:
        raise HalUnsupported("sim 没有灯")

    async def strobe(self, channel: str, pattern: str, max_s: float) -> None:
        raise HalUnsupported("sim 没有灯")

    async def sound(self, clip_or_tts: str, max_s: float) -> None:
        raise HalUnsupported("sim 没有喇叭")

    async def spotlight(self, on: bool, max_s: float) -> None:
        raise HalUnsupported("sim 没有探照灯")

    async def head(self, pan: float, tilt: float) -> None:
        raise HalUnsupported("sim 没有云台")

    async def camera_sources(self) -> tuple[str, ...]:
        return ()

    async def snapshot(self, source: str) -> bytes:
        raise HalUnsupported("sim 没有相机")

    async def stream_url(self, source: str) -> str:
        raise HalUnsupported("sim 没有相机")

    async def depth(self) -> Any:
        raise HalUnsupported("sim 没有深度相机")

    async def thermal(self) -> Any:
        raise HalUnsupported("sim 没有热像")

    async def audio_session(self) -> Any:
        raise HalUnsupported("sim 没有音频")

    # ------------------------------------------------------------ 回充(没有)

    async def recharge_start(self) -> None:
        raise HalUnsupported("sim 不回充")

    async def recharge_stop(self) -> None:
        raise HalUnsupported("sim 不回充")

    async def undock(self) -> None:
        raise HalUnsupported("sim 不回充")

    async def recharge_status(self) -> str:
        raise HalUnsupported("sim 不回充")

    # ------------------------------------------------------------ 能力

    def hal_capabilities(self) -> HalCapabilities:
        return HalCapabilities(
            max_vx=self.max_vx, max_wz=self.max_wz, deadband_vx=self.deadband_vx, lateral=False,
            control_releasable=True, recharge_mode="none",
            sensing={"lidar": False, "depth": False, "thermal": False, "imu": False,
                     "joint_effort": False, "foot_force": False},
            actuators={"light": False, "siren": False, "speaker": False, "spotlight": False,
                       "head": False})
