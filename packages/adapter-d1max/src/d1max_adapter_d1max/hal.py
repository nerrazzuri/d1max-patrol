"""D1MaxHal:真狗 D1 Max 的 RobotHAL(W00d 设计稿 ``2026-09-25-W00d-adapter-d1max-design.md``)。

运动走**常驻旁路进程** ``motion/patrol_agent.cpp``(握着厂商 SDK 会话),这里经 TCP 行协议接上去,
复用根包的 :class:`SidecarDeviceBackend`(连接、回执、状态与里程帧、「故意不释放控制权」都在那)。

三件事是这一层自己的:

- **单位**(决定三 A):HAL 说 m/s 与 rad/s,SDK 的 ``Move`` 收比例值。``mps_per_unit`` /
  ``radps_per_unit`` 是比例 1.0 对应的速度,``max_fraction`` 是比例上限,``deadband_mps`` 以下拒。
  **这几个数都没实测过**(``docs/庄园场景待真机测试.md``),默认值取保守的,实测后改配置。
- **控制权**(决定二 A):``control_releasable = false``,``release_control()`` 抛
  :class:`HalUnsupported`。放一次 SDK 控制权就得重启整台 RK3588。
- **停没停**:看旁路进程报上来的里程速度;刚发出的速度还在有效期里、或里程不新鲜,都不算停。
"""

from __future__ import annotations

import asyncio
import math
import time
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
from d1max_patrol.backends.base import DeviceBackendError
from d1max_patrol.backends.sidecar_device import (
    DEFAULT_AGENT_HOST,
    DEFAULT_AGENT_PORT,
    MAX_WALK_SPEED,
    VEL_TTL_MAX_MS,
    VEL_TTL_MIN_MS,
    SidecarDeviceBackend,
)
from d1max_patrol.protocol.agent_frames import EmergencyStatus
from d1max_patrol.protocol.agent_frames import MotionStatus as SdkMotion

ADAPTER_ID = "d1max/0.1.0"

#: 能收速度命令的 SDK 运动状态。**保守**:只认站着待命(``General``)与在走(``Gait``);其余
#: 站姿(``InPlace``/``Stair``/…)报 STANDING,不派速度。真狗待命时到底报哪个,探针会看。
_READY_MOTIONS = frozenset({SdkMotion.GENERAL, SdkMotion.GAIT})
#: 算「趴着」的。``Locked``(关节锁死)也算:那时候不能走。
_LYING_MOTIONS = frozenset({SdkMotion.LIE_DOWN, SdkMotion.LOCKED})

#: 里程多久没来算不新鲜(秒)。旁路进程 50 Hz 转发 ``OnMcData``,1 s 没来就是链路出事了。
ODOM_STALE_S = 1.0
#: 里程速度绝对值低于这个算停了(m/s、rad/s)。**待测**:四足站着不动时里程速度的噪声没量过,
#: 探针会记下观察窗口里的最大值;太小的话 ``stopped()`` 永远不成立(代理那头有兜底超时)。
STOPPED_EPS = 0.02
#: ``vel`` 等回执的上限(秒)。速度环 10 Hz,回执卡住就把整个代理的一拍冻住;超时按拒绝算,
#: 狗那头最多再走一个 ``ttl`` 就自己停。
VEL_ACK_TIMEOUT_S = 0.5
#: 一条故障多久没再报就算消了(秒)。**待真机**:SDK 报故障的节奏没量过。
FAULT_FRESH_S = 10.0
#: 连上之后等第一帧状态与里程多久。等不到就是旁路进程不对劲,不接。
FIRST_FRAME_TIMEOUT_S = 3.0
#: 站起/趴下之后等状态帧跟上的上限。站起实测约 6 s(清单 #36)。
POSTURE_TIMEOUT_S = 10.0


def _clamp(v: float, lim: float) -> float:
    return max(-lim, min(lim, v))


def wall_ms() -> int:
    return int(time.time() * 1000)


class D1MaxHal:
    """``host:port`` 是旁路进程(默认只绑回环 127.0.0.1:8090)。"""

    adapter_id = ADAPTER_ID

    def __init__(self, host: str = DEFAULT_AGENT_HOST, port: int = DEFAULT_AGENT_PORT, *,
                 mps_per_unit: float = 0.4, radps_per_unit: float = 1.0,
                 deadband_mps: float = 0.05, max_fraction: float = 0.5,
                 invert_yaw: bool = False, stopped_eps: float = STOPPED_EPS,
                 frame_id: str = "odom",
                 now_ms: Callable[[], int] = wall_ms,
                 backend: SidecarDeviceBackend | None = None) -> None:
        for name, v in (("mps_per_unit", mps_per_unit), ("radps_per_unit", radps_per_unit)):
            if not (math.isfinite(v) and v > 0):
                raise ValueError(f"{name} 要是正数,收到 {v}")
        if not (math.isfinite(deadband_mps) and deadband_mps >= 0):
            raise ValueError(f"deadband_mps 不能是负数,收到 {deadband_mps}")
        if not (math.isfinite(max_fraction) and 0 < max_fraction <= MAX_WALK_SPEED):
            raise ValueError(f"max_fraction 要在 (0, {MAX_WALK_SPEED}],收到 {max_fraction}")
        self._b = backend or SidecarDeviceBackend(host, port)
        self._mps, self._radps = mps_per_unit, radps_per_unit
        self._deadband = deadband_mps
        self._frac = max_fraction
        self._yaw_sign = -1.0 if invert_yaw else 1.0
        if not (math.isfinite(stopped_eps) and stopped_eps > 0):
            raise ValueError(f"stopped_eps 要是正数,收到 {stopped_eps}")
        self._stopped_eps = stopped_eps
        self._frame = frame_id
        self._now = now_ms
        self._monotonic: Callable[[], float] = time.monotonic
        #: 最近一条被接受的速度到什么时候(monotonic)还有效。有效期里不算停。
        self._vel_until: float | None = None

    @property
    def max_vx(self) -> float:
        return self._frac * self._mps

    @property
    def max_wz(self) -> float:
        return self._frac * self._radps

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> None:
        await self._b.connect()
        deadline = self._monotonic() + FIRST_FRAME_TIMEOUT_S
        while self._b.last_state is None or self._b.last_odom is None:
            if self._monotonic() >= deadline:
                await self._b.close()
                raise DeviceBackendError(
                    f"连上旁路进程 {FIRST_FRAME_TIMEOUT_S}s 还没收到状态帧与里程帧")
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        """只断 Python 这头。SDK 会话与控制权留在旁路进程里(清单 #47)。"""
        self._vel_until = None
        await self._b.close()

    async def health(self) -> Health:
        return Health(link_ok=self._b.connected, control=await self._b.has_control(),
                      estop=self._estop_now(),
                      faults=tuple(str(f.code) for f in self._b.current_faults(FAULT_FRESH_S)),
                      loc_quality=1.0 if self._odom_fresh() else 0.0)

    # ------------------------------------------------------------ 控制权

    async def acquire_control(self) -> None:
        """只**核对**旁路进程握着控制权(它开机时抢)。拿不到就抛。"""
        await self._b.acquire_control()

    async def release_control(self) -> None:
        raise HalUnsupported("D1 Max 的 SDK 控制权放了就得重启 RK3588 才能再抢;不放")

    async def control_status(self) -> ControlStatus:
        return ControlStatus(held=await self._b.has_control(), releasable=False,
                             detail="旁路进程常驻握着 SDK 控制权")

    # ------------------------------------------------------------ 运动

    async def motion_status(self) -> MotionStatus:
        st = self._b.last_state
        if not self._b.connected or st is None or st.motion is SdkMotion.UNKNOWN:
            return MotionStatus.UNKNOWN
        if st.motion in _LYING_MOTIONS:
            return MotionStatus.LYING
        if st.motion in _READY_MOTIONS and await self._b.has_control() and not st.emergency:
            return MotionStatus.READY
        return MotionStatus.STANDING

    async def set_motion_mode(self, mode: str) -> None:
        """``stand`` / ``lie``。旁路进程做完才回执,这里再等状态帧跟上。"""
        if mode == "stand":
            await self._b.stand()
            await self._wait_motion(_READY_MOTIONS)
        elif mode == "lie":
            await self._b.lie()
            await self._wait_motion(frozenset({SdkMotion.LIE_DOWN}))
        else:
            raise HalUnsupported(f"D1 Max 适配器只做 stand/lie,不认 {mode!r}")

    async def _wait_motion(self, want: frozenset[SdkMotion]) -> None:
        deadline = self._monotonic() + POSTURE_TIMEOUT_S
        while True:
            st = self._b.last_state
            if st is not None and st.motion in want:
                return
            if self._monotonic() >= deadline:
                now = st.motion.value if st is not None else "(无状态帧)"
                raise DeviceBackendError(f"等姿态超过 {POSTURE_TIMEOUT_S}s,当前 {now}")
            await asyncio.sleep(0.02)

    async def set_velocity(self, cmd: VelocityCommand) -> VelocityResult:
        def _reject(reason: str) -> VelocityResult:
            return VelocityResult(0.0, 0.0, clamped=False, rejected=True, reason=reason)

        if not await self._b.has_control():
            return _reject("no_control")
        if self._estop_now():
            return _reject("estop")
        if await self.motion_status() is not MotionStatus.READY:
            return _reject("not_ready")          # 趴着、锁死、姿态未知、非待命站姿:不下发
        if not all(math.isfinite(v) for v in (cmd.vx, cmd.vy, cmd.wz)):
            return _reject("not_finite")
        if abs(cmd.vy) > 1e-9:
            return _reject("no_lateral")
        if 0.0 < abs(cmd.vx) < self._deadband:
            return _reject("deadband")
        vx, wz = _clamp(cmd.vx, self.max_vx), _clamp(cmd.wz, self.max_wz)
        clamped = (vx != cmd.vx) or (wz != cmd.wz)
        # 换算后再夹一次:浮点误差也不许越过旁路进程的上限(它越界就拒)。
        fwd = _clamp(vx / self._mps, self._frac)
        yaw = _clamp(self._yaw_sign * wz / self._radps, self._frac)
        ttl = min(max(int(cmd.ttl_ms), VEL_TTL_MIN_MS), VEL_TTL_MAX_MS)
        try:
            await self._b.vel(fwd, 0.0, yaw, ttl, timeout_s=VEL_ACK_TIMEOUT_S)
        except DeviceBackendError as exc:
            return _reject(f"sidecar: {exc}")
        self._vel_until = self._monotonic() + ttl / 1000.0
        return VelocityResult(vx, wz, clamped=clamped, rejected=False)

    # ------------------------------------------------------------ 停止与急停

    async def stop(self) -> None:
        """停止**请求**:``halt`` 插队,作废有效期里的速度。确认看 :meth:`stopped`。"""
        self._vel_until = None
        await self._b.halt()

    async def stopped(self) -> bool:
        if self._vel_until is not None and self._monotonic() < self._vel_until:
            return False                              # 刚发的速度还在有效期里,可能正在起步
        o = self._b.last_odom
        if o is None or not self._odom_fresh():
            return False                              # 不知道,就不说停了
        return max(abs(o.vx), abs(o.vy), abs(o.vyaw)) < self._stopped_eps

    async def emergency_stop(self, on: bool) -> None:
        if on:
            self._vel_until = None
        await self._b.emergency_stop(on)

    async def estop_status(self) -> bool:
        return self._estop_now()

    async def estop_reset(self) -> None:
        await self._b.emergency_stop(False)

    def _estop_now(self) -> bool:
        """软、硬两路**都**报「已解除」才算没急停。没状态帧、任一路 ``Unknown``(旁路进程在
        客户端刚连上时会先补一帧全零的占位状态)都按急停算:不知道,放行等于赌运气。"""
        st = self._b.last_state
        if st is None:
            return True
        return not (st.estop_software is EmergencyStatus.RECOVER
                    and st.estop_hardware is EmergencyStatus.RECOVER)

    # ------------------------------------------------------------ 感知

    def _odom_fresh(self) -> bool:
        at = self._b.last_odom_at
        return self._b.connected and at is not None and self._monotonic() - at <= ODOM_STALE_S

    async def odometry(self) -> Odometry:
        """运控自己的里程(``OnMcData``),**不是**地图上的定位。"""
        o = self._b.last_odom
        if o is None:
            return Odometry(stamp_ms=self._now(), frame_id=self._frame, x=0.0, y=0.0, yaw=0.0,
                            vx=0.0, wz=0.0, valid=False)
        return Odometry(stamp_ms=self._now(), frame_id=self._frame, x=o.x, y=o.y, yaw=o.yaw,
                        vx=o.vx, wz=o.vyaw, valid=self._odom_fresh())

    async def imu(self) -> Any:
        raise HalUnsupported("W00d 不接 imu")

    async def lidar(self) -> Any:
        raise HalUnsupported("W00d 不接 lidar(走 ROS,归后面的工单)")

    async def ultrasonic(self) -> Any:
        raise HalUnsupported("W00d 不接超声")

    async def joints(self) -> Any:
        raise HalUnsupported("W00d 不接关节流")

    async def contacts(self) -> Any:
        raise HalUnsupported("W00d 不接触地")

    # ------------------------------------------------------------ 电池与故障

    async def battery(self) -> Battery:
        """两块电池里低的那块。充没充电旁路进程不报,按没充。"""
        st = self._b.last_state
        if st is None:
            raise DeviceBackendError("还没收到过状态帧,读不到电量")
        return Battery(percent=float(st.battery), charging=False)

    async def faults(self) -> tuple[Fault, ...]:
        """**当前**故障:最近 ``FAULT_FRESH_S`` 秒内旁路进程还报过的(见旁路客户端
        ``current_faults``)。不是历史 —— 历史里跌倒过一次就永远挂着,站点认不出下一次。"""
        # 厂商没说哪些 level 算致命;取 level>=2 是假设(待真机验证),同旁路客户端。
        return tuple(Fault(code=str(f.code), fatal=f.level >= 2, text=f.message)
                     for f in self._b.current_faults(FAULT_FRESH_S))

    # ------------------------------------------------------------ 执行器与媒体

    async def light(self, channel: str, on: bool) -> None:
        setter = {"front": self._b.set_front_light, "back": self._b.set_back_light,
                  "both": self._b.set_light}.get(channel)
        if setter is None:
            raise ValueError(f"灯只有 front/back/both,收到 {channel!r}")
        await setter(on)

    async def strobe(self, channel: str, pattern: str, max_s: float) -> None:
        raise HalUnsupported("W00d 不做爆闪")

    async def sound(self, clip_or_tts: str, max_s: float) -> None:
        raise HalUnsupported("W00d 不接喇叭")

    async def spotlight(self, on: bool, max_s: float) -> None:
        raise HalUnsupported("D1 Max 没有探照灯")

    async def head(self, pan: float, tilt: float) -> None:
        raise HalUnsupported("ControlHead 的单位与方向没验过,先不开")

    async def camera_sources(self) -> tuple[str, ...]:
        return ()                                     # RTSP 归媒体那一层,不在 HAL 里

    async def snapshot(self, source: str) -> bytes:
        raise HalUnsupported("SDK 没有拍照接口,取图走 RTSP")

    async def stream_url(self, source: str) -> str:
        raise HalUnsupported("W00d 不报视频地址")

    async def depth(self) -> Any:
        raise HalUnsupported("W00d 不接深度相机")

    async def thermal(self) -> Any:
        raise HalUnsupported("D1 Max 没有热像")

    async def audio_session(self) -> Any:
        raise HalUnsupported("W00d 不接音频")

    # ------------------------------------------------------------ 回充(没有)

    async def recharge_start(self) -> None:
        raise HalUnsupported("W00d 不做回充")

    async def recharge_stop(self) -> None:
        raise HalUnsupported("W00d 不做回充")

    async def undock(self) -> None:
        raise HalUnsupported("W00d 不做回充")

    async def recharge_status(self) -> str:
        raise HalUnsupported("W00d 不做回充")

    # ------------------------------------------------------------ 能力

    def hal_capabilities(self) -> HalCapabilities:
        return HalCapabilities(
            max_vx=self.max_vx, max_wz=self.max_wz, deadband_vx=self._deadband, lateral=False,
            control_releasable=False, recharge_mode="none",
            sensing={"lidar": False, "depth": False, "thermal": False, "imu": False,
                     "joint_effort": False, "foot_force": False},
            actuators={"light": True, "siren": False, "speaker": False, "spotlight": False,
                       "head": False})
