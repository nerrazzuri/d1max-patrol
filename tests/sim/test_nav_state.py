"""三套状态机的迁移规则。"""

import math

import pytest

from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus, NavStatus, Pose
from d1max_sim.kinematics import Planar2DModel
from d1max_sim.nav_state import (
    LocStateMachine,
    MappingStateMachine,
    NavStateMachine,
    SimRejected,
)

DT = 0.05


def _advance(sm, seconds: float) -> None:
    for _ in range(int(round(seconds / DT))):
        sm.step(DT)


def _run_until(sm, predicate, max_seconds: float = 60.0) -> bool:
    for _ in range(int(max_seconds / DT)):
        if predicate():
            return True
        sm.step(DT)
    return predicate()


# ------------------------------------------------------------------ 导航状态机

def _nav() -> NavStateMachine:
    return NavStateMachine(model=Planar2DModel())


def test_初始状态是就绪():
    assert _nav().status is NavStatus.STANDBY


def test_启动后先初始化再激活():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(2.0, 0.0))
    assert sm.status is NavStatus.INITIALIZING
    _advance(sm, 0.35)
    assert sm.status is NavStatus.ACTIVE


def test_到点后成功再回到就绪():
    """真机上 StandBy 是启动导航的唯一前提,因此终态必须自动回落。"""
    sm = _nav()
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)


def test_非就绪状态下启动被拒绝():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(3.0, 0.0))
    with pytest.raises(SimRejected, match="StandBy"):
        sm.start(Pose.from_xy_yaw(1.0, 0.0))


def test_停止导航进入取消再回就绪():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(5.0, 0.0))
    _advance(sm, 0.5)
    sm.stop()
    assert sm.status is NavStatus.CANCELLED
    assert sm.model.has_goal is False
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)


def test_就绪状态下停止被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.stop()


def test_暂停期间不再前进():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(5.0, 0.0))
    _advance(sm, 0.5)
    sm.pause()
    assert sm.status is NavStatus.PAUSE
    x_before = sm.model.x
    _advance(sm, 1.0)
    assert sm.model.x == pytest.approx(x_before)


def test_继续后恢复前进并最终到达():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    _advance(sm, 0.5)
    sm.pause()
    sm.resume()
    assert sm.status is NavStatus.ACTIVE
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)


def test_未暂停时继续被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.resume()


def test_非激活状态下暂停被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.pause()


def test_无导航时注入失败被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.fail("没有导航在跑")


def test_注入失败进入_failed_再回就绪():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(5.0, 0.0))
    _advance(sm, 0.5)
    sm.fail("注入")
    assert sm.status is NavStatus.FAILED
    assert sm.model.has_goal is False
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)


def test_预约下一次导航失败():
    """fail_next_start 用于制造"某个航点走不到"的场景。"""
    sm = _nav()
    sm.fail_next_start = True
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    assert _run_until(sm, lambda: sm.status is NavStatus.FAILED)
    assert sm.fail_next_start is False   # 一次性
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)


def test_机器人卡住时状态停在_active():
    """stuck 注入:导航状态正常但永远到不了,用于触发上层航点超时。"""
    sm = NavStateMachine(model=Planar2DModel(frozen=True))
    sm.start(Pose.from_xy_yaw(3.0, 0.0))
    _advance(sm, 10.0)
    assert sm.status is NavStatus.ACTIVE


# ------------------------------------------------------------------ 定位状态机

def test_定位初始状态():
    assert LocStateMachine().status is LocStatus.INIT


def test_加载地图走完三段迁移():
    sm = LocStateMachine()
    sm.load_map("m1")
    assert sm.status is LocStatus.MAP_LOADING
    assert sm.loaded_map_id == "m1"
    _advance(sm, 0.25)
    assert sm.status is LocStatus.INIT_LOCALIZATION
    _advance(sm, 0.35)
    assert sm.status is LocStatus.CONTINUOUS_LOC
    assert sm.healthy is True


def test_定位丢失后不再健康():
    sm = LocStateMachine()
    sm.load_map("m1")
    _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)
    sm.lose()
    assert sm.status is LocStatus.LOC_LOST
    assert sm.healthy is False
    _advance(sm, 2.0)
    assert sm.status is LocStatus.LOC_LOST   # 不会自愈


def test_定位丢失后可恢复():
    sm = LocStateMachine()
    sm.load_map("m1")
    _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)
    sm.lose()
    sm.recover()
    assert _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)


def test_重置定位重新走初始化():
    sm = LocStateMachine()
    sm.load_map("m1")
    _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)
    sm.reset()
    assert sm.status is LocStatus.INIT_LOCALIZATION
    assert _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)


# ------------------------------------------------------------------ 建图状态机

def test_建图初始状态是抑制():
    assert MappingStateMachine().status is MappingStatus.PASSIVE


def test_建图走完启动三段迁移():
    sm = MappingStateMachine()
    sm.start()
    assert sm.status is MappingStatus.INIT_WAIT_SENSOR
    _advance(sm, 0.25)
    assert sm.status is MappingStatus.MAPPING_READY
    _advance(sm, 0.25)
    assert sm.status is MappingStatus.MAPPING_RUNNING


def test_停止建图触发保存并回调一次():
    fired: list[int] = []
    sm = MappingStateMachine(on_saved=lambda: fired.append(1))
    sm.start()
    _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_RUNNING)
    sm.stop()
    assert sm.status is MappingStatus.MAPPING_SAVE_BEGIN
    assert _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_SAVE_END)
    _advance(sm, 2.0)
    assert fired == [1]


def test_未建图时停止被拒绝():
    sm = MappingStateMachine()
    with pytest.raises(SimRejected):
        sm.stop()


def test_建图中重复启动被拒绝():
    sm = MappingStateMachine()
    sm.start()
    with pytest.raises(SimRejected):
        sm.start()


def test_保存完成后可再次建图():
    sm = MappingStateMachine()
    sm.start()
    _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_RUNNING)
    sm.stop()
    _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_SAVE_END)
    sm.start()
    assert sm.status is MappingStatus.INIT_WAIT_SENSOR


def test_零角度目标也能收敛():
    """回归:目标朝向为 π 时角度归一化不能来回抖。"""
    sm = NavStateMachine(model=Planar2DModel())
    sm.start(Pose.from_xy_yaw(1.0, 0.0, math.pi))
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)


def test_跨初始化边界的大步长不多算行走时间():
    """回归:一次大 step 里,初始化占掉的那部分时间不能算进行走距离。"""
    big = _nav()
    big.start(Pose.from_xy_yaw(5.0, 0.0))
    big.step(0.31)                       # 0.30s 初始化 + 0.01s 行走
    small = _nav()
    small.start(Pose.from_xy_yaw(5.0, 0.0))
    for _ in range(31):
        small.step(0.01)
    assert big.status is NavStatus.ACTIVE
    assert small.status is NavStatus.ACTIVE
    assert big.model.x == pytest.approx(small.model.x, abs=1e-6)
