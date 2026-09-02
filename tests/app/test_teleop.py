"""手动遥控:脉冲、死区、守死人开关,以及和任务引擎的互斥。

这里的假设备是**本地写的**,不是从 ``tests/engine`` 借的:那边的假件不认识
``walk``,而 ``walk`` 恰恰是这个模块的全部。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from d1max_patrol.app.teleop import (
    DEFAULT_PULSE_S,
    HEARTBEAT_TIMEOUT_S,
    MAX_PULSE_S,
    MIN_FWD,
    MIN_YAW,
    Teleop,
    TeleopBusy,
)
from d1max_patrol.backends.base import Event, EventEmitter, NavStatusEvent
from d1max_patrol.engine.machine import EngineBusy, MissionEngine, RunState
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus
from tests.app.conftest import post
from tests.engine.conftest import make_mission

#: 看门狗周期。测试里拨的是假表,真表上要等的就只有这一小段。
TICK = 0.01


class NavStub(EventEmitter[Event]):
    """假导航。``arrives`` 为假时任务会一直停在"走着"上,正好用来占着引擎。"""

    def __init__(self) -> None:
        super().__init__()
        self.arrives = False

    async def nav_status(self) -> NavStatus:
        return NavStatus.STANDBY

    async def loc_status(self) -> LocStatus:
        return LocStatus.CONTINUOUS_LOC

    async def goto(self, pose) -> None:
        if self.arrives:
            self.emit(NavStatusEvent(NavStatus.SUCCEED))

    async def stop(self) -> None: ...

    async def reset_localization(self) -> None: ...

    async def return_home(self) -> None:
        self.emit(NavStatusEvent(NavStatus.SUCCEED))


class DeviceStub(EventEmitter[Event]):
    """假本体。``walk_calls`` 是这一组测试的主要观测点。"""

    def __init__(self) -> None:
        super().__init__()
        self.estop = False
        self.batt = 88.0
        self.control = True
        self.walk_calls: list[tuple[float, float, float, float]] = []
        self.stop_calls = 0

    async def battery(self) -> float:
        return self.batt

    async def emergency(self) -> bool:
        return self.estop

    async def has_control(self) -> bool:
        return self.control

    async def walk(self, seconds: float, forward: float,
                   lateral: float = 0.0, yaw: float = 0.0) -> None:
        self.walk_calls.append((seconds, forward, lateral, yaw))
        if (forward, lateral, yaw) == (0.0, 0.0, 0.0):
            self.stop_calls += 1

    async def stand(self) -> None: ...

    async def lie(self) -> None: ...

    async def set_light(self, on: bool) -> None: ...

    async def set_gimbal(self, pitch: float, yaw: float) -> None: ...


class FakeClock:
    """能往前拨的表。底下垫真 monotonic,免得和 asyncio 的超时脱节。"""

    def __init__(self) -> None:
        self.offset = 0.0

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def __call__(self) -> float:
        return time.monotonic() + self.offset


@pytest.fixture
def fake_device() -> DeviceStub:
    return DeviceStub()


@pytest.fixture
def fake_nav() -> NavStub:
    return NavStub()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def engine(fake_nav, fake_device, tmp_path) -> MissionEngine:
    return MissionEngine(fake_nav, fake_device, {}, tmp_path / "runs")


@pytest.fixture
async def teleop(fake_device, engine, fake_clock):
    """看门狗周期压到 10ms:测试拨的是假表,不该真等 0.2 秒一拍。"""
    t = Teleop(fake_device, engine, clock=fake_clock, watch_period_s=TICK)
    yield t
    await t.aclose()
    await engine.aclose()


@pytest.fixture
def sample_mission():
    return make_mission()


async def _until(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("等条件超时")
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------ 一拍


async def test_一拍就是一次walk(teleop, fake_device):
    await teleop.pulse(1.0, 0.0, 0.0)
    assert len(fake_device.walk_calls) == 1
    seconds, fwd, lat, yaw = fake_device.walk_calls[0]
    assert seconds == DEFAULT_PULSE_S
    assert fwd >= MIN_FWD
    assert (lat, yaw) == (0.0, 0.0), "没按的轴要原样是零,不是死区值"


async def test_控制量永远压在死区之上(teleop, fake_device):
    """#37/#38:Move 是百分比且有死区,低于死区就是原地不动还不报错。"""
    await teleop.pulse(0.05, 0.0, 0.0)
    fwd = fake_device.walk_calls[-1][1]
    assert abs(fwd) >= MIN_FWD


async def test_想走慢是缩短时长不是压低控制量(teleop, fake_device):
    await teleop.pulse(1.0, 0.0, 0.0, seconds=0.1)
    d, fwd, _, _ = fake_device.walk_calls[-1]
    assert d == pytest.approx(0.1)
    assert abs(fwd) >= MIN_FWD


async def test_倒着走也顶到死区之上(teleop, fake_device):
    """符号要留住 —— 顶成正的就是该后退时往前冲。"""
    await teleop.pulse(-0.05, 0.0, 0.0)
    assert fake_device.walk_calls[-1][1] <= -MIN_FWD


async def test_转向有自己的死区(teleop, fake_device):
    await teleop.pulse(0.0, 0.0, 0.01)
    assert abs(fake_device.walk_calls[-1][3]) >= MIN_YAW


async def test_控制量不会超过满量程(teleop, fake_device):
    await teleop.pulse(1.0, 1.0, 1.0)
    _, fwd, lat, yaw = fake_device.walk_calls[-1]
    assert max(fwd, lat, yaw) <= 1.0


async def test_零输入就是停不是发一个死区值(teleop, fake_device):
    await teleop.pulse(0.0, 0.0, 0.0)
    assert fake_device.walk_calls[-1][1] == 0.0
    assert not teleop.active


async def test_超范围的量直接拒(teleop, fake_device):
    for bad in ((5.0, 0.0, 0.0), (0.0, -2.0, 0.0), (0.0, 0.0, 1.5)):
        with pytest.raises(ValueError, match=r"\[-1, 1\]"):
            await teleop.pulse(*bad)
    assert fake_device.walk_calls == [], "拒掉的指令不该有半条发出去"


async def test_过长的一拍直接拒(teleop):
    """一拍最多两秒 —— 更长的等于把守死人开关架空了。"""
    with pytest.raises(ValueError):
        await teleop.pulse(1.0, 0.0, 0.0, seconds=MAX_PULSE_S + 0.1)
    with pytest.raises(ValueError):
        await teleop.pulse(1.0, 0.0, 0.0, seconds=0.0)


# ------------------------------------------------------------------ 守死人


async def test_心跳停了六百毫秒内停车(teleop, fake_device, fake_clock):
    await teleop.pulse(1.0, 0.0, 0.0)
    fake_clock.advance(HEARTBEAT_TIMEOUT_S + 0.01)
    await _until(lambda: fake_device.stop_calls >= 1)
    assert not teleop.active


async def test_心跳一直有就不会被停(teleop, fake_device, fake_clock):
    await teleop.pulse(1.0, 0.0, 0.0)
    for _ in range(5):
        fake_clock.advance(0.2)
        teleop.heartbeat()
        await asyncio.sleep(TICK * 2)
    assert fake_device.stop_calls == 0
    assert teleop.active


async def test_没人遥控的时候看门狗不会自己发停车(teleop, fake_device, fake_clock):
    """页面开着没人碰方向键,不该每隔半秒往狗身上发一条指令。"""
    fake_clock.advance(10.0)
    await asyncio.sleep(TICK * 3)
    assert fake_device.walk_calls == []


async def test_停过之后不会再被守死人补一刀(teleop, fake_device, fake_clock):
    await teleop.pulse(1.0, 0.0, 0.0)
    await teleop.stop()
    before = fake_device.stop_calls
    fake_clock.advance(HEARTBEAT_TIMEOUT_S + 1.0)
    await asyncio.sleep(TICK * 3)
    assert fake_device.stop_calls == before


async def test_收尾不会给狗发指令(teleop, fake_device):
    """关服务不该让狗动一下 —— 它可能正稳稳站着。"""
    await teleop.pulse(1.0, 0.0, 0.0)
    before = len(fake_device.walk_calls)
    await teleop.aclose()
    assert len(fake_device.walk_calls) == before
    assert not teleop.active


# ------------------------------------------------------------------ 互斥


async def test_任务在跑的时候遥控被拒(teleop, engine, sample_mission):
    await engine.start(sample_mission)
    await engine.wait_state(RunState.RUNNING)
    with pytest.raises(TeleopBusy, match="任务"):
        await teleop.pulse(1.0, 0.0, 0.0)


async def test_遥控在动的时候起任务被拒(teleop, engine, sample_mission):
    await teleop.pulse(1.0, 0.0, 0.0)
    with pytest.raises(EngineBusy, match="遥控"):
        await engine.start(sample_mission)


async def test_松手之后任务又能起了(teleop, engine, sample_mission):
    """互斥不能变成"遥控过一次就再也起不了任务"。"""
    await teleop.pulse(1.0, 0.0, 0.0)
    await teleop.stop()
    await engine.start(sample_mission)
    assert engine.running


# ------------------------------------------------------------------ 急停


async def test_急停按下去遥控直接不动(teleop, fake_device):
    fake_device.estop = True
    with pytest.raises(TeleopBusy, match="急停"):
        await teleop.pulse(1.0, 0.0, 0.0)
    assert fake_device.walk_calls == []


async def test_急停会同时打断正在跑的任务(engine, teleop, sample_mission):
    """遥控和任务是两条各自独立的动腿路径,只停一条等于没停。"""
    await engine.start(sample_mission)
    await engine.wait_state(RunState.RUNNING)
    await teleop.emergency_stop("按了急停")
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert teleop._device.stop_calls >= 1


async def test_没任务在跑也能按急停(teleop, fake_device):
    """红按钮不该因为"现在没任务"就报错 —— 按它的人正处在慌张里。"""
    await teleop.emergency_stop()
    assert fake_device.stop_calls >= 1


# ------------------------------------------------------------------ HTTP


def test_遥控接口拒绝超范围的量(server):
    assert post(server, "/api/teleop", {"fwd": 5.0}) == 400


def test_遥控接口拒绝过长的一拍(server):
    """一拍最多 2 秒 —— 更长的等于把守死人开关架空了。"""
    assert post(server, "/api/teleop", {"fwd": 1.0, "seconds": 30}) == 400


def test_遥控接口拒绝不是数的量(server):
    assert post(server, "/api/teleop", {"fwd": "快点"}) == 400
    assert post(server, "/api/teleop", {"fwd": True}) == 400


def test_遥控接口走得通(server, ctx):
    assert post(server, "/api/teleop", {"fwd": 1.0}) == 200
    assert ctx.device.walk_calls[-1][1] >= MIN_FWD


def test_心跳接口走得通(server):
    assert post(server, "/api/teleop/heartbeat") == 200


def test_急停接口走得通(server, ctx):
    assert post(server, "/api/estop") == 200
    assert ctx.device.stop_calls >= 1


def test_急停按着的时候遥控接口给409(server, ctx):
    ctx.device.estop = True
    assert post(server, "/api/teleop", {"fwd": 1.0}) == 409


def test_遥控接口只收POST(server):
    from tests.app.conftest import status
    assert status(server, "/api/teleop") == 405
