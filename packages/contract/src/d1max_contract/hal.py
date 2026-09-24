"""RobotHAL:品牌适配器要实现的接口(总设计 §2)。**方法名在这里定死。**

职责边界(§2.1):适配器不持有巡逻/派遣等任务状态;但**必须**维护设备连接、厂商
控制权、硬件操作状态与底层安全互锁(SDK 断连即停、速度命令 ttl 到期即停、硬件
急停只读、运动模式切换互锁)。任务策略属于代理;设备安全限制不可被上层绕过。

十一类原语一个不缺地列在 :class:`RobotHAL` 里。适配器对自己没有的能力抛
:class:`HalUnsupported`,**并且**在 :meth:`RobotHAL.hal_capabilities` 里报 ``false``
—— 测试钉「声明了就得有入口,没声明就得抛」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from d1max_contract.errors import ContractError
from d1max_contract.wire import as_bool, as_dict, as_float, as_str


class HalUnsupported(Exception):
    """这台机器/这个适配器没有这项能力。调用方先看 ``hal_capabilities()``。"""


class MotionStatus(str, Enum):
    UNKNOWN = "unknown"
    LYING = "lying"
    STANDING = "standing"
    #: 能收速度命令。
    READY = "ready"


@dataclass(frozen=True)
class VelocityCommand:
    """带序号与有效期的速度命令。``ttl_ms`` 到期无新命令 → 适配器自停(§2.2)。"""

    seq: int
    ttl_ms: int
    frame: str
    vx: float
    vy: float
    wz: float

    def __post_init__(self) -> None:
        if self.ttl_ms <= 0:
            raise ContractError("VelocityCommand: ttl_ms 必须为正")


@dataclass(frozen=True)
class VelocityResult:
    """**真实应用值** + 夹/拒原因。做不到的速度明确拒绝或返回实际值,禁止静默改写。"""

    applied_vx: float
    applied_wz: float
    clamped: bool
    rejected: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if self.clamped and self.rejected:
            raise ContractError("VelocityResult: 不许既 clamped 又 rejected")
        if self.rejected and not self.reason:
            raise ContractError("VelocityResult: rejected 必须给 reason")


@dataclass(frozen=True)
class ControlStatus:
    held: bool
    #: 厂商侧是否允许释放;不支持释放的机型在能力里明示。
    releasable: bool
    detail: str = ""


@dataclass(frozen=True)
class Health:
    link_ok: bool
    control: bool
    estop: bool
    faults: tuple[str, ...]
    #: 0.0 = 定位不可用;1.0 = 好。
    loc_quality: float


@dataclass(frozen=True)
class Odometry:
    stamp_ms: int
    frame_id: str
    x: float
    y: float
    yaw: float
    vx: float
    wz: float
    valid: bool


@dataclass(frozen=True)
class Battery:
    percent: float
    charging: bool


@dataclass(frozen=True)
class Fault:
    code: str
    fatal: bool
    text: str


RECHARGE_MODES = ("vendor_dock", "none")
SENSING_KEYS = ("lidar", "depth", "thermal", "imu", "joint_effort", "foot_force")
ACTUATOR_KEYS = ("light", "siren", "speaker", "spotlight", "head")


@dataclass(frozen=True)
class HalCapabilities:
    """硬件层能力(§2.4):设备能提供什么。**不是站点看到的清单**——那是代理合成的。"""

    max_vx: float
    max_wz: float
    deadband_vx: float
    lateral: bool
    control_releasable: bool
    recharge_mode: str
    sensing: dict[str, bool]
    actuators: dict[str, bool]

    def __post_init__(self) -> None:
        if self.recharge_mode not in RECHARGE_MODES:
            raise ContractError(f"HalCapabilities: recharge_mode 只能是 {RECHARGE_MODES}")
        for k in SENSING_KEYS:
            if not isinstance(self.sensing.get(k), bool):
                raise ContractError(f"HalCapabilities: sensing.{k} 要是布尔且必填")
        for k in ACTUATOR_KEYS:
            if not isinstance(self.actuators.get(k), bool):
                raise ContractError(f"HalCapabilities: actuators.{k} 要是布尔且必填")

    def to_wire(self) -> dict[str, Any]:
        return {"max_vx": self.max_vx, "max_wz": self.max_wz, "deadband_vx": self.deadband_vx,
                "lateral": self.lateral, "control_releasable": self.control_releasable,
                "recharge_mode": self.recharge_mode, "sensing": dict(self.sensing),
                "actuators": dict(self.actuators)}

    @classmethod
    def from_wire(cls, d: Any) -> HalCapabilities:
        if not isinstance(d, dict):
            raise ContractError("HalCapabilities: 要是对象")
        w = "HalCapabilities"
        return cls(max_vx=as_float(d, "max_vx", w), max_wz=as_float(d, "max_wz", w),
                   deadband_vx=as_float(d, "deadband_vx", w), lateral=as_bool(d, "lateral", w),
                   control_releasable=as_bool(d, "control_releasable", w),
                   recharge_mode=as_str(d, "recharge_mode", w),
                   sensing=as_dict(d, "sensing", w), actuators=as_dict(d, "actuators", w))


class RobotHAL(Protocol):
    """总设计 §2.2 的全部原语。W00 的 adapter-sim 只实现运动/停止/急停/里程/电池/故障
    那一部分,其余抛 HalUnsupported 并在能力里报 false。"""

    # ---- 生命周期
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def health(self) -> Health: ...

    # ---- 设备控制权(站点租约 ≠ 厂商运动控制权)
    async def acquire_control(self) -> None: ...
    async def release_control(self) -> None: ...
    async def control_status(self) -> ControlStatus: ...

    # ---- 运动准备
    async def motion_status(self) -> MotionStatus: ...
    async def set_motion_mode(self, mode: str) -> None: ...

    # ---- 速度
    async def set_velocity(self, cmd: VelocityCommand) -> VelocityResult: ...

    # ---- 停止:请求与确认分开
    async def stop(self) -> None: ...
    async def stopped(self) -> bool: ...

    # ---- 急停:复位不自动恢复任务;硬件急停只读
    async def emergency_stop(self, on: bool) -> None: ...
    async def estop_status(self) -> bool: ...
    async def estop_reset(self) -> None: ...

    # ---- 感知流(统一 stamp/frame_id/valid/age)
    async def odometry(self) -> Odometry: ...
    async def imu(self) -> Any: ...
    async def lidar(self) -> Any: ...
    async def ultrasonic(self) -> Any: ...
    async def joints(self) -> Any: ...
    async def contacts(self) -> Any: ...

    # ---- 电池与故障
    async def battery(self) -> Battery: ...
    async def faults(self) -> tuple[Fault, ...]: ...

    # ---- 执行器(有类型;声光类必带最大持续时间)
    async def light(self, channel: str, on: bool) -> None: ...
    async def strobe(self, channel: str, pattern: str, max_s: float) -> None: ...
    async def sound(self, clip_or_tts: str, max_s: float) -> None: ...
    async def spotlight(self, on: bool, max_s: float) -> None: ...
    async def head(self, pan: float, tilt: float) -> None: ...

    # ---- 媒体
    async def camera_sources(self) -> tuple[str, ...]: ...
    async def snapshot(self, source: str) -> bytes: ...
    async def stream_url(self, source: str) -> str: ...
    async def depth(self) -> Any: ...
    async def thermal(self) -> Any: ...
    async def audio_session(self) -> Any: ...

    # ---- 回充(可选;模式在能力里明示)
    async def recharge_start(self) -> None: ...
    async def recharge_stop(self) -> None: ...
    async def undock(self) -> None: ...
    async def recharge_status(self) -> str: ...

    # ---- 能力
    def hal_capabilities(self) -> HalCapabilities: ...
