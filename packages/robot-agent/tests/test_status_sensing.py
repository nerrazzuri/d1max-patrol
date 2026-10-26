"""W41:能力里的 ``sensing.imu_hz`` 照 HAL 报的填(原来写死 0,现场看着像没有 IMU)。"""

from __future__ import annotations

import math

from d1max_agent.status import compose_capabilities
from d1max_contract.hal import ACTUATOR_KEYS, HalCapabilities

BASE = {"lidar": False, "depth": False, "thermal": False, "imu": True, "joint_effort": False,
        "foot_force": False}


def _caps(sensing):
    sensing = BASE | sensing
    hal = HalCapabilities(max_vx=0.6, max_wz=1.0, deadband_vx=0.0, lateral=False,
                          control_releasable=False, recharge_mode="none", sensing=sensing,
                          actuators=dict.fromkeys(ACTUATOR_KEYS, False))
    return compose_capabilities(robot_id="A", hal_caps=hal, adapter_id="sim", loaded_map=None,
                                nav_path="planned", autonomy="supervised").sensing


def test_W41_imu_hz_照HAL报的_坏值是0():
    assert _caps({"imu": True, "imu_hz": 20})["imu_hz"] == 20
    assert _caps({"imu": True})["imu_hz"] == 0, "老 HAL 不报:0"
    for bad in (True, "20", -1, math.nan, math.inf, None):
        assert _caps({"imu_hz": bad})["imu_hz"] == 0, bad


def test_W41_真狗HAL报20Hz():
    from d1max_adapter_d1max.hal import IMU_HZ
    assert IMU_HZ == 20
