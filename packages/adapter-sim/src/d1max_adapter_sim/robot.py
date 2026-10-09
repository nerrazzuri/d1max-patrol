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
    ImuSample,
    MotionStatus,
    Odometry,
    VelocityCommand,
    VelocityResult,
)

ADAPTER_ID = "sim/0.1.0"


def wrap_angle(a: float) -> float:
    wrapped = math.remainder(a, 2 * math.pi)
    return math.pi if wrapped == -math.pi else wrapped


#: 假充电桩(W13):对桩、出桩各要多久(秒,仿真的钟)。
DOCK_S = 5.0
UNDOCK_S = 3.0


class SimRobot:
    """``now_ms`` 是注入的钟;``tick(dt_s)`` 推进一步。两者由调用方保持一致。"""

    #: 代理合成能力时写进 ``adapter`` 字段。
    adapter_id = ADAPTER_ID

    def __init__(self, *, now_ms: Callable[[], int], max_vx: float = 1.0, max_wz: float = 1.5,
                 deadband_vx: float = 0.05, stop_latency_s: float = 0.2,
                 battery_drain_pct_per_h: float = 8.0, frame_id: str = "odom",
                 latency_s: float = 0.0, gait_start_s: float = 0.0,
                 max_decel: float = math.inf, require_clearance: bool = False,
                 payload: bool = False,
                 charger: tuple[float, float, float] | None = None,
                 charge_pct_per_h: float = 60.0, imu: bool = False) -> None:
        self._now = now_ms
        #: 假 IMU(W26):给了才有(``imu()``、能力里报);姿态、撞击、腿上承重用 :meth:`inject_imu` 改。
        self._imu_on = imu
        self._roll = self._pitch = 0.0
        self._shock = 0.0
        self._load: float | None = 100.0
        #: 假充电桩(W13):桩前对准点的位姿(x, y, yaw)。给了就会对桩:站在对准点 0.4 m、20° 内调
        #: ``recharge_start``,过 :data:`DOCK_S` 秒上桩、充电;``undock`` 过 :data:`UNDOCK_S` 秒出桩。
        #: 没站准就一直上不了桩(跟厂家回充一样不报失败)。
        self.charger = charger
        self._charge_rate = charge_pct_per_h
        self.docked = False
        self._dock_at: int | None = None
        self._undock_at: int | None = None
        self.undock_calls = 0
        #: 上装(W21):开了就有警灯、警笛、聚光灯、喇叭。每一路记「开到几点」(按仿真的钟),到点算关。
        self.payload = payload
        self.deter_until: dict[str, int] = {}
        self.deter_clip = ""
        self.clips: tuple[str, ...] = ("warn-zh", "warn-en") if payload else ()
        self.max_vx, self.max_wz, self.deadband_vx = max_vx, max_wz, deadband_vx
        #: W11 运动模型(W08 决定 10):链路延迟(命令晚这么久生效)、起步切步态(从站着到走要等这么久)、
        #: 刹车减速度上限;旁路进程的净空许可门(``require_clearance``:没有有效许可,前进分量置零、
        #: 转向照常)。
        self.latency_s, self.gait_start_s, self.max_decel = latency_s, gait_start_s, max_decel
        self.require_clearance = require_clearance
        #: 净空许可分两头(W09i):往前走向的那一头(狗头为前是狗头、狗尾为前是狗尾)要那一头的许可。
        self._clear_until_ms = {"head": -1, "tail": -1}
        self._pending: list[tuple[int, float, float, int]] = []    # (生效时刻, vx, wz, 截止)
        self._gait_left_s: float | None = None
        self._stop_latency_s = stop_latency_s
        self._frame = frame_id
        self._connected = False
        self._control = False
        self._estop = False
        self._loc_lost = False
        #: 头尾方向(W11a):仿真默认狗头为前;测试用 :meth:`inject_head` 调。
        self.head_direction = "head"
        self._faults: list[Fault] = []
        #: W00c5d 第二部分:载入的图、注入的载入失败(非空 = 失败原因)。
        self.loaded_map: tuple[str, str, str] | None = None
        self.fail_load = ""
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

    def inject_head(self, head: str) -> None:
        self.head_direction = head

    def inject_imu(self, *, roll: float | None = None, pitch: float | None = None,
                   shock_g: float | None = None, load: float | None | str = "keep") -> None:
        """W26:改姿态(弧度)、来一下撞击(下一次 ``imu()`` 读走就清)、腿上承重(``None`` = 读不到)。"""
        if roll is not None:
            self._roll = roll
        if pitch is not None:
            self._pitch = pitch
        if shock_g is not None:
            self._shock = max(self._shock, shock_g)
        if load != "keep":
            self._load = load  # type: ignore[assignment]

    def inject_loc_lost(self, lost: bool) -> None:
        self._loc_lost = lost

    def inject_battery(self, pct: float) -> None:
        self._battery_pct = pct
        self._battery_t0 = self._now()

    def _battery_now(self) -> float:
        hours = max(0, self._now() - self._battery_t0) / 3_600_000
        rate = self._charge_rate if self.docked else -self._battery_drain
        return max(0.0, min(100.0, self._battery_pct + hours * rate))

    def _rebase_battery(self) -> None:
        """充没充电变了:电量从这一刻接着算(充是涨、不充是掉)。"""
        self._battery_pct, self._battery_t0 = self._battery_now(), self._now()

    def _dock_tick(self) -> None:
        now = self._now()
        if self._dock_at is not None and now >= self._dock_at:
            self._dock_at = None
            self._rebase_battery()
            self.docked = True
        if self._undock_at is not None and now >= self._undock_at:
            self._undock_at = None
            self._rebase_battery()
            self.docked = False

    def inject_fault(self, code: str, fatal: bool, text: str = "") -> None:
        self._faults.append(Fault(code=code, fatal=fatal, text=text))

    def clear_faults(self) -> None:
        self._faults.clear()

    def teleport(self, x: float, y: float, yaw: float) -> None:
        self.x, self.y, self.yaw = x, y, yaw

    # ------------------------------------------------------------ 时间推进

    def tick(self, dt_s: float) -> None:
        """推进 dt:先看互锁(急停/控制权/ttl),再制动或按命令走,最后积分位姿。"""
        if dt_s <= 0:
            return
        now = self._now()
        while self._pending and self._pending[0][0] <= now:      # 链路延迟到了的命令生效
            _, vx, wz, deadline = self._pending.pop(0)
            self._cmd_vx, self._cmd_wz, self._cmd_deadline_ms = vx, wz, deadline
            self._stopping_left_s = None
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
            want_vx, want_wz = self._cmd_vx, self._cmd_wz
            lead = -1.0 if self.head_direction == "tail" else 1.0   # 往前那头(机身系)
            end = "tail" if lead < 0 else "head"
            if self.require_clearance and want_vx * lead > 0 and (
                    self.head_direction not in ("head", "tail")
                    or now >= self._clear_until_ms[end]):
                want_vx = 0.0                                    # 往前那头没有净空许可:不许往前
            standing = self._vx == 0.0 and self._wz == 0.0
            if standing and (want_vx or want_wz) and self.gait_start_s > 0:
                if self._gait_left_s is None:
                    self._gait_left_s = self.gait_start_s        # 从站着起步:先切步态
                self._gait_left_s -= dt_s
                if self._gait_left_s > 1e-9:
                    want_vx = want_wz = 0.0
                else:
                    self._gait_left_s = None
            if abs(want_vx) < abs(self._vx) and math.isfinite(self.max_decel):
                step = self.max_decel * dt_s                     # 减速有上限(刹车距离)
                want_vx = (max(want_vx, self._vx - step) if self._vx > 0
                           else min(want_vx, self._vx + step))
            self._vx, self._wz = want_vx, want_wz
        self.yaw = wrap_angle(self.yaw + self._wz * dt_s)
        self.x += self._vx * math.cos(self.yaw) * dt_s
        self.y += self._vx * math.sin(self.yaw) * dt_s

    def _zero(self) -> None:
        self._pending.clear()                     # 路上的命令也作废(急停、丢控制权)
        self._gait_left_s = None
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
                      loc_quality=0.0 if self._loc_lost else 1.0, head=self.head_direction)

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
        if self.latency_s > 0:
            at = self._now() + int(self.latency_s * 1000)
            self._pending.append((at, vx, wz, at + cmd.ttl_ms))
            return VelocityResult(vx, wz, clamped=clamped, rejected=False)
        self._cmd_vx, self._cmd_wz = vx, wz
        self._cmd_deadline_ms = self._now() + cmd.ttl_ms
        self._stopping_left_s = None
        return VelocityResult(vx, wz, clamped=clamped, rejected=False)

    def clear(self, ms: int, end: str = "head") -> None:
        """旁路进程的净空许可(W11 第二层):从现在起 ``ms`` 毫秒内 ``end`` 那一头是空的。"""
        self._clear_until_ms[end] = max(self._clear_until_ms[end], self._now() + int(ms))

    @property
    def speed(self) -> tuple[float, float]:
        """此刻真实的 (vx, wz)(测试看刹车、看许可门)。"""
        return self._vx, self._wz

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

    async def soft_estop_confirmed(self) -> bool:
        return self._estop

    async def load_map(self, map_id: str, version: str, path) -> None:
        """W00c5d 第二部分:载入站点下发的一张图(仿真里只记下来;``fail_load`` 注入失败)。"""
        if self.fail_load:
            raise RuntimeError(self.fail_load)
        self.loaded_map = (map_id, version, str(path))

    async def estop_reset(self) -> None:
        self._estop = False                      # 不恢复任务:速度仍为零,等新命令

    # ------------------------------------------------------------ 感知

    async def odometry(self) -> Odometry:
        return Odometry(stamp_ms=self._now(), frame_id=self._frame, x=self.x, y=self.y,
                        yaw=self.yaw, vx=self._vx, wz=self._wz, valid=not self._loc_lost)

    async def imu(self) -> Any:
        if not self._imu_on:
            raise HalUnsupported("sim 没开 imu(SimRobot(imu=True))")
        shock, self._shock = self._shock, 0.0             # 峰值:读走就清
        return ImuSample(stamp_ms=self._now(), roll=self._roll, pitch=self._pitch, shock_g=shock,
                         load=self._load, valid=True)

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
        self._dock_tick()
        return Battery(percent=self._battery_now(), charging=self.docked)

    async def faults(self) -> tuple[Fault, ...]:
        return tuple(self._faults)

    # ------------------------------------------------------------ 执行器与媒体(上装可选,W21)

    async def light(self, channel: str, on: bool) -> None:
        raise HalUnsupported("sim 没有灯")

    def _deter(self, output: str, on: bool, max_s: float) -> None:
        if not self.payload:
            raise HalUnsupported("sim 没装上装")
        self.deter_until[output] = self._now() + int(max_s * 1000) if on and max_s > 0 else 0

    def deter_on(self, output: str) -> bool:
        """这一路现在开着没有(到点就算关)。"""
        return self._now() < self.deter_until.get(output, 0)

    async def strobe(self, channel: str, pattern: str, max_s: float) -> None:
        self._deter("strobe", pattern != "off", max_s)

    async def siren(self, on: bool, max_s: float) -> None:
        self._deter("siren", on, max_s)

    async def sound(self, clip_or_tts: str, max_s: float) -> None:
        if self.payload and clip_or_tts and not clip_or_tts.startswith("tts:") \
                and clip_or_tts not in self.clips:
            raise ValueError(f"没有这段话术: {clip_or_tts}")
        self._deter("speaker", bool(clip_or_tts), max_s)
        self.deter_clip = clip_or_tts if clip_or_tts else ""

    def sound_clips(self) -> tuple[str, ...]:
        return self.clips

    async def spotlight(self, on: bool, max_s: float) -> None:
        self._deter("spotlight", on, max_s)

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

    # ------------------------------------------------------------ 回充(W13:假充电桩)

    async def recharge_start(self) -> None:
        if self.charger is None:
            raise HalUnsupported("sim 没配充电桩")
        cx, cy, cyaw = self.charger
        aligned = (math.hypot(self.x - cx, self.y - cy) <= 0.4
                   and abs(math.remainder(self.yaw - cyaw, 2 * math.pi)) <= math.radians(20))
        if aligned and not self.docked:
            self._dock_at = self._now() + int(DOCK_S * 1000)

    async def recharge_stop(self) -> None:
        if self.charger is None:
            raise HalUnsupported("sim 没配充电桩")
        self._dock_at = None

    async def undock(self) -> None:
        if self.charger is None:
            raise HalUnsupported("sim 没配充电桩")
        self.undock_calls += 1
        if self.docked:
            self._undock_at = self._now() + int(UNDOCK_S * 1000)

    async def recharge_status(self) -> str:
        if self.charger is None:
            raise HalUnsupported("sim 没配充电桩")
        self._dock_tick()
        return "docked" if self.docked else ("docking" if self._dock_at else "idle")

    # ------------------------------------------------------------ 能力

    def hal_capabilities(self) -> HalCapabilities:
        return HalCapabilities(
            max_vx=self.max_vx, max_wz=self.max_wz, deadband_vx=self.deadband_vx, lateral=False,
            control_releasable=True,
            recharge_mode="vendor_dock" if self.charger is not None else "none",
            sensing={"lidar": False, "depth": False, "thermal": False, "imu": self._imu_on,
                     "joint_effort": False, "foot_force": False},
            actuators={"light": False, "strobe": self.payload, "siren": self.payload,
                       "speaker": self.payload, "spotlight": self.payload, "head": False})
