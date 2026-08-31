"""平面运动学。确定性:同样的初值和步长序列必然给出同样的轨迹。"""

import math

import pytest

from d1max_patrol.protocol.nav_types import Pose
from d1max_sim.kinematics import Planar2DModel, wrap_angle

DT = 0.05


def _run(model: Planar2DModel, max_steps: int = 2000) -> int:
    """步进直到到达,返回用掉的步数;未到达返回 max_steps。"""
    for i in range(max_steps):
        if model.arrived:
            return i
        model.step(DT)
    return max_steps


@pytest.mark.parametrize(
    "value, expected",
    [(0.0, 0.0), (math.pi, math.pi), (-math.pi, math.pi),
     (3 * math.pi / 2, -math.pi / 2), (-3 * math.pi / 2, math.pi / 2)],
)
def test_角度归一化(value, expected):
    assert wrap_angle(value) == pytest.approx(expected)


def test_没有目标时不动也不算到达():
    m = Planar2DModel()
    assert m.has_goal is False
    assert m.arrived is False
    assert m.goal_distance == math.inf
    m.step(DT)
    assert (m.x, m.y, m.yaw) == (0.0, 0.0, 0.0)


def test_直线前进能到达目标():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(2.0, 0.0, 0.0))
    steps = _run(m)
    assert 0 < steps < 200
    assert m.arrived is True
    assert m.x == pytest.approx(2.0, abs=m.position_tolerance)
    assert m.y == pytest.approx(0.0, abs=m.position_tolerance)


def test_需要先转身再前进也能到达():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(0.0, 2.0, math.pi / 2))
    steps = _run(m)
    assert m.arrived is True
    assert steps < 300
    assert m.y == pytest.approx(2.0, abs=m.position_tolerance)
    assert wrap_angle(m.yaw - math.pi / 2) == pytest.approx(0.0, abs=m.yaw_tolerance)


def test_到位后还会转到目标朝向():
    m = Planar2DModel(x=1.0, y=1.0, yaw=0.0)
    m.set_goal(Pose.from_xy_yaw(1.0, 1.0, math.pi))
    assert m.arrived is False   # 位置到了但朝向没到
    _run(m)
    assert m.arrived is True
    assert abs(wrap_angle(m.yaw - math.pi)) <= m.yaw_tolerance


def test_目标已在容差内则立即算到达():
    m = Planar2DModel(x=1.0, y=1.0, yaw=0.0)
    m.set_goal(Pose.from_xy_yaw(1.02, 1.02, 0.0))
    assert m.arrived is True


def test_冻结后永不到达():
    """故障注入 stuck: 机器人报告状态正常但就是不动。"""
    m = Planar2DModel(frozen=True)
    m.set_goal(Pose.from_xy_yaw(2.0, 0.0))
    assert _run(m, max_steps=400) == 400
    assert (m.x, m.y) == (0.0, 0.0)
    assert m.arrived is False


def test_减速后步数显著增加():
    """故障注入 slow: 用于制造导航超时。"""
    fast = Planar2DModel()
    fast.set_goal(Pose.from_xy_yaw(2.0, 0.0))
    fast_steps = _run(fast)

    slow = Planar2DModel(speed_scale=0.25)
    slow.set_goal(Pose.from_xy_yaw(2.0, 0.0))
    slow_steps = _run(slow)

    assert slow_steps > fast_steps * 3


def test_清除目标后不再算到达():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(0.0, 0.0, 0.0))
    assert m.arrived is True
    m.clear_goal()
    assert m.has_goal is False
    assert m.arrived is False


def test_瞬移直接改变位姿并清除目标():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(5.0, 5.0))
    m.teleport(Pose.from_xy_yaw(1.0, 2.0, math.pi / 4))
    assert (m.x, m.y) == (1.0, 2.0)
    assert m.yaw == pytest.approx(math.pi / 4)
    assert m.has_goal is False


def test_pose_读数与内部状态一致():
    m = Planar2DModel(x=1.5, y=-2.5, yaw=1.0)
    pose = m.pose()
    assert pose.position.x == 1.5
    assert pose.position.y == -2.5
    assert pose.yaw == pytest.approx(1.0)


def test_轨迹可复现():
    def trace() -> list[tuple[float, float, float]]:
        m = Planar2DModel()
        m.set_goal(Pose.from_xy_yaw(1.0, 1.0, 0.5))
        out = []
        for _ in range(60):
            m.step(DT)
            out.append((m.x, m.y, m.yaw))
        return out

    assert trace() == trace()


def test_目标距离随前进单调下降():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(3.0, 0.0))
    prev = m.goal_distance
    for _ in range(50):
        m.step(DT)
        assert m.goal_distance <= prev + 1e-9
        prev = m.goal_distance
