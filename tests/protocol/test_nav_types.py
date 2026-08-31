"""状态枚举与位姿类型。取值全部照抄 refs/nav-api 的状态值说明。"""

import math

import pytest

from d1max_patrol.protocol.nav_types import (
    ALG_LIDAR_DISCONNECTED,
    ALG_NAV_BLOCKED,
    LOC_HEALTHY,
    LOC_UNUSABLE,
    NAV_TERMINAL,
    NAV_TERMINAL_FAILURE,
    NAV_TERMINAL_SUCCESS,
    LocStatus,
    MappingStatus,
    NavStatus,
    Orientation,
    Pose,
    Position,
    Waypoint,
    orientation_to_yaw,
    parse_enum,
    yaw_to_orientation,
)


def test_导航状态取值与文档一致():
    assert [s.value for s in NavStatus] == [
        "StandBy", "Initializing", "Active", "Pause", "Cancelled", "Succeed", "Failed",
    ]


def test_定位状态取值与文档一致():
    assert [s.value for s in LocStatus] == [
        "Init", "MapLoading", "InitLocalization", "ContinuousLoc",
        "Error", "DynamicInitLoc", "LocLost",
    ]


def test_建图状态取值与文档一致():
    assert [s.value for s in MappingStatus] == [
        "Unknown", "Passive", "InitWaitSensor", "MappingReady",
        "MappingRunning", "MappError", "MappingSaveBegin", "MappingSaveEnd",
    ]


def test_终态集合():
    assert NAV_TERMINAL_SUCCESS == frozenset({NavStatus.SUCCEED})
    assert NAV_TERMINAL_FAILURE == frozenset({NavStatus.FAILED, NavStatus.CANCELLED})
    assert NAV_TERMINAL == NAV_TERMINAL_SUCCESS | NAV_TERMINAL_FAILURE
    assert NavStatus.ACTIVE not in NAV_TERMINAL


def test_只有持续定位算健康():
    assert LOC_HEALTHY == frozenset({LocStatus.CONTINUOUS_LOC})


def test_定位可用性集合():
    assert LOC_UNUSABLE == frozenset({LocStatus.LOC_LOST, LocStatus.ERROR})
    # 健康和不可用状态不应有交集（安全不变量）
    assert LOC_HEALTHY & LOC_UNUSABLE == frozenset()


def test_故障码常量():
    assert (ALG_NAV_BLOCKED, ALG_LIDAR_DISCONNECTED) == (13330, 13331)


def test_parse_enum_已知取值():
    assert parse_enum(NavStatus, "Active") is NavStatus.ACTIVE
    assert parse_enum(LocStatus, "LocLost") is LocStatus.LOC_LOST


@pytest.mark.parametrize("value", ["Wandering", "", None, 3, "active"])
def test_parse_enum_未知取值返回_None_而不抛异常(value):
    """固件可能新增状态值,不能因为不认识就崩掉整条链路。"""
    assert parse_enum(NavStatus, value) is None


def test_pose_线格式与文档一致():
    pose = Pose(Position(1.0, 2.0, 0.0), Orientation(0.0, 0.0, 0.0, 1.0))
    assert pose.to_wire() == {
        "position": {"x": 1.0, "y": 2.0, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }


def test_pose_从线格式还原():
    wire = {
        "position": {"x": 1.5, "y": -2.5, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.7071, "w": 0.7071},
    }
    pose = Pose.from_wire(wire)
    assert pose.position == Position(1.5, -2.5, 0.0)
    assert pose.orientation.z == pytest.approx(0.7071)


def test_pose_从线格式缺少_z_时补零():
    pose = Pose.from_wire({"position": {"x": 1.0, "y": 2.0},
                           "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}})
    assert pose.position.z == 0.0


def test_pose_从线格式字段缺失报错():
    with pytest.raises(ValueError, match="position"):
        Pose.from_wire({"orientation": {"x": 0, "y": 0, "z": 0, "w": 1}})


@pytest.mark.parametrize("yaw", [0.0, 0.5, -0.5, 1.57, -1.57, 3.0, -3.0])
def test_yaw_四元数往返(yaw):
    assert orientation_to_yaw(yaw_to_orientation(yaw)) == pytest.approx(yaw, abs=1e-9)


def test_yaw_归一化到正负_pi():
    """输入 3π/2 应等价于 -π/2。"""
    o = yaw_to_orientation(3 * math.pi / 2)
    assert orientation_to_yaw(o) == pytest.approx(-math.pi / 2, abs=1e-9)


def test_pose_from_xy_yaw_与_yaw_属性():
    pose = Pose.from_xy_yaw(3.0, 4.0, math.pi / 2)
    assert pose.position == Position(3.0, 4.0, 0.0)
    assert pose.yaw == pytest.approx(math.pi / 2)


def test_pose_平面距离():
    a = Pose.from_xy_yaw(0.0, 0.0)
    b = Pose.from_xy_yaw(3.0, 4.0)
    assert a.distance_to(b) == pytest.approx(5.0)


def test_waypoint_线格式是二元列表():
    wp = Waypoint("P1_变压器", Pose.from_xy_yaw(1.0, 2.0))
    wire = wp.to_wire()
    assert wire[0] == "P1_变压器"
    assert wire[1]["position"]["x"] == 1.0
    assert Waypoint.from_wire(wire) == wp


def test_waypoint_从线格式格式错误报错():
    with pytest.raises(ValueError, match="路径点"):
        Waypoint.from_wire(["only_name"])
