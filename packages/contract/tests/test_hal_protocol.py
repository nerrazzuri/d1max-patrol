"""RobotHAL:总设计 §2.2 十一类原语一个都不许缺,方法名在这里定死。"""

from __future__ import annotations

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.hal import (
    Battery,
    Fault,
    HalCapabilities,
    HalUnsupported,
    Health,
    MotionStatus,
    Odometry,
    RobotHAL,
    VelocityCommand,
    VelocityResult,
)

#: 总设计 §2.2 的方法名单,按类别。改这里 = 改契约。
METHODS = {
    "生命周期": ("connect", "close", "health"),
    "设备控制权": ("acquire_control", "release_control", "control_status"),
    "运动准备": ("motion_status", "set_motion_mode"),
    "速度": ("set_velocity",),
    "停止": ("stop", "stopped"),
    "急停": ("emergency_stop", "estop_status", "estop_reset"),
    "感知流": ("odometry", "imu", "lidar", "ultrasonic", "joints", "contacts"),
    "电池与故障": ("battery", "faults"),
    "执行器": ("light", "strobe", "sound", "spotlight", "head"),
    "媒体": ("camera_sources", "snapshot", "stream_url", "depth", "thermal", "audio_session"),
    "回充": ("recharge_start", "recharge_stop", "undock", "recharge_status"),
    "能力": ("hal_capabilities",),
}


def test_十一类原语一个不缺():
    for 类别, names in METHODS.items():
        for n in names:
            assert hasattr(RobotHAL, n), f"{类别}: RobotHAL 缺 {n}()"


def test_VelocityResult_不许既夹又拒():
    with pytest.raises(ContractError):
        VelocityResult(applied_vx=0.0, applied_wz=0.0, clamped=True, rejected=True, reason="x")
    with pytest.raises(ContractError, match="reason"):
        VelocityResult(applied_vx=0.0, applied_wz=0.0, clamped=False, rejected=True)
    ok = VelocityResult(applied_vx=0.3, applied_wz=0.0, clamped=True, rejected=False)
    assert ok.applied_vx == 0.3


def test_VelocityCommand_ttl必须正():
    with pytest.raises(ContractError, match="ttl_ms"):
        VelocityCommand(seq=1, ttl_ms=0, frame="base", vx=0.1, vy=0.0, wz=0.0)


def test_能力往返():
    caps = HalCapabilities(max_vx=1.0, max_wz=1.5, deadband_vx=0.05, lateral=False,
                           control_releasable=True, recharge_mode="none",
                           sensing={"lidar": False, "depth": False, "thermal": False, "imu": False,
                                    "joint_effort": False, "foot_force": False},
                           actuators={"light": False, "siren": False, "speaker": False,
                                      "spotlight": False, "head": False})
    assert HalCapabilities.from_wire(caps.to_wire()) == caps
    with pytest.raises(ContractError, match="recharge_mode"):
        HalCapabilities.from_wire({**caps.to_wire(), "recharge_mode": "magic"})


def test_数据类存在():
    assert MotionStatus.READY.value == "ready"
    Health(link_ok=True, control=True, estop=False, faults=(), loc_quality=1.0)
    Odometry(stamp_ms=1, frame_id="odom", x=0, y=0, yaw=0, vx=0, wz=0, valid=True)
    Battery(percent=50.0, charging=False)
    Fault(code="E1", fatal=False, text="x")
    assert issubclass(HalUnsupported, Exception)
