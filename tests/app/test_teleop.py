"""手动遥控:脉冲、死区、守死人开关,以及和任务引擎的互斥。

这里的假设备是**本地写的**,不是从 ``tests/engine`` 借的:那边的假件不认识
``walk``,而 ``walk`` 恰恰是这个模块的全部。
"""

from __future__ import annotations

import asyncio
import time
from unittest import mock

import pytest

from d1max_patrol.app import teleop as teleop_mod
from d1max_patrol.app.teleop import (
    DEFAULT_PULSE_S,
    HEARTBEAT_TIMEOUT_S,
    MAX_PULSE_S,
    MIN_FWD,
    MIN_YAW,
    PROFILES,
    ROAM,
    SCAN,
    Teleop,
    TeleopBusy,
    snap_to_axis,
)
from d1max_patrol.backends.base import Event, EventEmitter, NavStatusEvent
from d1max_patrol.engine.homing import HomePoint
from d1max_patrol.engine.machine import EngineBusy, MissionEngine, RunState
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose
from tests.app.conftest import post, request
from tests.conftest import NoDisks
from tests.engine.conftest import make_mission

#: 看门狗周期。测试里拨的是假表,真表上要等的就只有这一小段。
TICK = 0.01

#: 起飞门槛把原点当成前置条件。这里测的是遥控和引擎的互斥,不是原点本身,
#: 给个跟 ``make_mission`` 同一张图的原点,免得每个用例都要单独传。
_HOME = HomePoint(map_id="map_test", pose=Pose.from_xy_yaw(0.0, 0.0),
                  marked_at_ms=1_757_000_000_000)


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
    # removable 给假探针,免得默认的 DEFAULT_PROBE 去扫真机上的 /media、/mnt。
    return MissionEngine(fake_nav, fake_device, {}, tmp_path / "runs",
                         removable=NoDisks())


@pytest.fixture
async def teleop(fake_device, engine, fake_clock):
    """看门狗周期压到 10ms:测试拨的是假表,不该真等 0.2 秒一拍。

    ``video_gate=lambda: ""``:这份夹具喂给的是这个文件里其它组的用例
    (吸附、一拍、两档节奏、守死人、互斥、急停、HTTP),它们测的不是 §5.9
    那道闸,给个永远放行的闸。§5.9 自己那组用例(见下面"视频闸"一节)
    各自现造带闸的 ``Teleop``,不用这份夹具。
    """
    t = Teleop(fake_device, engine, clock=fake_clock, watch_period_s=TICK,
              video_gate=lambda: "")
    yield t
    await t.aclose()
    await engine.aclose()


@pytest.fixture
async def 跑着的engine(engine, sample_mission):
    """真被内核跑着的引擎:借 ``NavStub`` 默认 ``arrives=False``,卡在第一个
    点的 ``goto`` 上不动 —— 跟 ``test_任务在跑的时候遥控被拒`` 是同一招。
    """
    await engine.start(sample_mission, home=_HOME)
    await engine.wait_state(RunState.RUNNING)
    yield engine
    await engine.aclose()


@pytest.fixture
async def 挂起的engine(跑着的engine):
    """§5.10:引擎让开腿。用 Task 5 的 ``suspend()`` 真的把它推到
    ``SUSPENDED``,不在这儿复刻状态表 —— ``yielding`` 是引擎自己的词汇。
    """
    eng = 跑着的engine
    await eng.suspend("测试:人来开")
    await eng.wait_state(RunState.SUSPENDED)
    return eng


@pytest.fixture
def sample_mission():
    return make_mission()


async def 等到(pred, timeout: float = 3.0) -> None:
    """带截止时间的轮询,不许用固定 ``sleep(余量)`` 空等一个后台协程的结果。"""
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("等条件超时")
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------ 单轴吸附


def test_斜着推只留大的那一轴():
    """规格 §7.7:左摇杆吸附到单轴。

    **这不是手感,是正确性。** 底下的控制量是百分比不是速度,死区又大
    (清单 #37/#38),所以 ``_clamp_above_deadband`` 是逐轴把非零值顶到死区
    之上的。两个轴一起顶,``(0.5, 0.2)`` 会变成 ``(0.5, 0.3)`` —— 人指的是
    「基本朝前、稍微偏一点」,狗走的是「四十度斜着」。
    """
    assert snap_to_axis(0.5, 0.2) == (0.5, 0.0)
    assert snap_to_axis(0.2, -0.5) == (0.0, -0.5)


def test_一样大的时候留前进():
    """**平局判给前进。**

    正推四十五度是「我想往前,手抖了」的概率,远大于「我想横着走,手也抖了」;
    而且前进是这台机器唯一一个在真机上量过死区的轴(清单 #37/#38),侧移那个
    数至今是借来的。平局倒向量过的那一头。
    """
    assert snap_to_axis(0.4, 0.4) == (0.4, 0.0)
    assert snap_to_axis(-0.4, 0.4) == (-0.4, 0.0)


def test_零还是零():
    """全零是「停」,吸附不许把它变成别的。"""
    assert snap_to_axis(0.0, 0.0) == (0.0, 0.0)
    assert snap_to_axis(0.0, 0.7) == (0.0, 0.7)


async def test_侧移用自己的死区不再借前进的(fake_device, engine):
    """``MIN_LAT`` 是独立常量。**今天两个数一样,这条测试照样测得到东西** ——
    它盯的是「用的是哪一个常量」,不是「值是多少」。真机标定那天改 ``MIN_LAT``,
    这条会跟着变;而在此之前,有人把 ``MIN_LAT`` 删掉改回借用,这条会红。
    """
    tel = Teleop(fake_device, engine, video_gate=lambda: "")
    try:
        with mock.patch.object(teleop_mod, "MIN_LAT", 0.66):
            await tel.pulse(0.0, 0.05, 0.0)
        assert fake_device.walk_calls[-1][2] == pytest.approx(0.66)
    finally:
        await tel.aclose()


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


# ------------------------------------------------------------------ 两档节奏


async def test_扫图档的转向是把一拍缩短_不是把量压低(fake_device, engine):
    """**这一条是这个任务的全部要点。**

    控制量是百分比不是速度,死区在 0.3 附近(清单 #37/#38)。把扫图档的
    「转向压低」实现成「乘一个小于 1 的系数」,结果是控制量掉进死区 ——
    狗原地不动,而且不报错,页面上看起来像掉线。规格 §7.7 要的「压低」在
    这台机器上只有一种正确实现:**把一拍缩短**。

    所以断言有两半,缺一不可:时长变短了 **而且** 控制量仍然在死区之上。
    """
    tel = Teleop(fake_device, engine, video_gate=lambda: "")
    try:
        await tel.pulse(0.0, 0.0, 1.0, profile=SCAN)
        seconds, _fwd, _lat, yaw = fake_device.walk_calls[-1]
        assert seconds == pytest.approx(SCAN.yaw_seconds)
        assert seconds < ROAM.yaw_seconds
        assert abs(yaw) >= MIN_YAW
    finally:
        await tel.aclose()


async def test_扫图档的平移用平移那个时长(fake_device, engine):
    """转向和平移是**两个**时长。扫图时人要的是转向一点一点来,
    平移倒不必碎成一样。混成一个数就没法分别调。
    """
    tel = Teleop(fake_device, engine, video_gate=lambda: "")
    try:
        await tel.pulse(1.0, 0.0, 0.0, profile=SCAN)
        assert fake_device.walk_calls[-1][0] == pytest.approx(SCAN.fwd_seconds)
    finally:
        await tel.aclose()


async def test_漫游档是默认的(fake_device, engine):
    """不传 profile 就是漫游 —— 网页版 app 那些老调用方一行不用改。"""
    tel = Teleop(fake_device, engine, video_gate=lambda: "")
    try:
        await tel.pulse(1.0, 0.0, 0.0)
        assert fake_device.walk_calls[-1][0] == pytest.approx(ROAM.fwd_seconds)
    finally:
        await tel.aclose()


async def test_同时有平移和转向时按长的那个时长走(fake_device, engine):
    """一拍只有一个时长,得挑一个。

    **挑长的那个。** 挑短的话,人推着摇杆前进兼转向,前进会被截成扫图那么短
    的一小步 —— 明明推的是「往前走并且拐个弯」,走出来的是「原地拐了一下」。
    """
    tel = Teleop(fake_device, engine, video_gate=lambda: "")
    try:
        await tel.pulse(1.0, 0.0, 1.0, profile=SCAN)
        assert fake_device.walk_calls[-1][0] == pytest.approx(
            max(SCAN.fwd_seconds, SCAN.yaw_seconds))
    finally:
        await tel.aclose()


async def test_显式给了秒数就听人的(fake_device, engine):
    """``seconds`` 仍然压过 profile —— 网页版 app 现在传的就是它。"""
    tel = Teleop(fake_device, engine, video_gate=lambda: "")
    try:
        await tel.pulse(1.0, 0.0, 0.0, seconds=1.5, profile=SCAN)
        assert fake_device.walk_calls[-1][0] == pytest.approx(1.5)
    finally:
        await tel.aclose()


def test_两档都在名录里_而且名字就是上线的那个词():
    """``PROFILES`` 的键是 HTTP 上直接收的字符串,不许跟 ``name`` 不一致 ——
    不一致的话,路由认得的词和 ``to_wire`` 报出去的词会是两套。
    """
    assert set(PROFILES) == {"roam", "scan"}
    assert all(k == v.name for k, v in PROFILES.items())


def test_每一档的时长都在合法范围里():
    """一拍不许超过 ``MAX_PULSE_S`` —— 超了等于把守死人开关架空:
    心跳断了以后还要再走这么久。
    """
    for prof in PROFILES.values():
        assert 0.0 < prof.fwd_seconds <= MAX_PULSE_S
        assert 0.0 < prof.yaw_seconds <= MAX_PULSE_S

    # §7.7 那张表:扫图两个时长都比漫游短。**拿常量互相比,不拿常量跟自己比** ——
    # 跟自己比的断言对"SCAN 整个缩水成 ROAM"这种变异是瞎的。
    assert SCAN.fwd_seconds < ROAM.fwd_seconds
    assert SCAN.yaw_seconds < ROAM.yaw_seconds


# ------------------------------------------------------------------ 守死人


async def test_心跳停了六百毫秒内停车(teleop, fake_device, fake_clock):
    await teleop.pulse(1.0, 0.0, 0.0)
    fake_clock.advance(HEARTBEAT_TIMEOUT_S + 0.01)
    await 等到(lambda: fake_device.stop_calls >= 1)
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
    await engine.start(sample_mission, home=_HOME)
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
    await engine.start(sample_mission, home=_HOME)
    assert engine.running


# ------------------------------------------------------------------ 急停


async def test_急停按下去遥控直接不动(teleop, fake_device):
    fake_device.estop = True
    with pytest.raises(TeleopBusy, match="急停"):
        await teleop.pulse(1.0, 0.0, 0.0)
    assert fake_device.walk_calls == []


async def test_急停会同时打断正在跑的任务(engine, teleop, sample_mission):
    """遥控和任务是两条各自独立的动腿路径,只停一条等于没停。"""
    await engine.start(sample_mission, home=_HOME)
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


def test_不认识的节奏档报400(server):
    """**不许静默退回默认档。** 客户端拼错了词却照走漫游档,人在扫图的时候
    会拿到走路的节奏 —— 而扫图那一档存在的理由,恰恰是走路的节奏建不出图。
    """
    code, body, _ = request(server, "/api/teleop", method="POST", payload={
        "fwd": 1.0, "lat": 0.0, "yaw": 0.0, "profile": "sacn"})
    assert code == 400
    assert "sacn" in body.decode("utf-8")


def test_不传节奏档就是漫游(server, ctx):
    assert post(server, "/api/teleop", {"fwd": 1.0, "lat": 0.0, "yaw": 0.0}) == 200
    assert ctx.device.walk_calls[-1][0] == pytest.approx(ROAM.fwd_seconds)


# ------------------------------------------------------------------ 视频闸


def 带闸(fake_device, engine, fake_clock, 理由: list[str]) -> Teleop:
    return Teleop(fake_device, engine, clock=fake_clock, watch_period_s=TICK,
                  video_gate=lambda: 理由[0])


async def test_视频不在线就不许动(fake_device, engine, fake_clock):
    """§5.9。**安全约束,不是体验约束。**

    没有画面还让人开着走,是在盲开:对人,盲开的狗会撞到人;对狗,它自己也
    会被撞坏、被开下台阶。
    """
    理由 = ["前相机没画面"]
    t = 带闸(fake_device, engine, fake_clock, 理由)
    with pytest.raises(TeleopBusy) as e:
        await t.pulse(0.5, 0.0, 0.0)
    assert "画面" in str(e.value)
    assert fake_device.walk_calls == []


async def test_没有确认后继续这条路(fake_device, engine, fake_clock):
    """**规格明写不设绕过口子。**

    这一条钉的是「``pulse`` 上不存在任何放行参数」—— 加一个 ``force=True``
    之类的旁路,这条测试就该红。要挪狗,就必须能看得见;网差到没有画面,
    那就走过去挪。
    """
    import inspect
    sig = inspect.signature(Teleop.pulse)
    assert not {"force", "confirm", "override", "bypass"} & set(sig.parameters)


async def test_停车永远不许被闸挡住(fake_device, engine, fake_clock):
    """**这是这个任务里唯一能造成实际伤害的错法。**

    一个「因为看不见所以不许停」的实现,会在视频掉线的那一刻把狗锁在最后
    一个动作上 —— 掉线恰恰是最需要它停下来的时候。
    """
    理由 = ["前相机没画面"]
    t = 带闸(fake_device, engine, fake_clock, 理由)
    await t.pulse(0.0, 0.0, 0.0)          # 三轴全零 = 停
    await t.stop()
    assert fake_device.walk_calls[-1] == (0.0, 0.0, 0.0, 0.0)


async def test_视频在线就照常走(fake_device, engine, fake_clock):
    t = 带闸(fake_device, engine, fake_clock, [""])
    try:
        await t.pulse(0.5, 0.0, 0.0)
        assert fake_device.walk_calls
    finally:
        await t.aclose()


async def test_开着走的时候视频掉了_看门狗停狗(fake_device, engine, fake_clock):
    """**掉线时没有人在调 ``pulse``。**

    人的手还压在摇杆上,页面还在发心跳,``pulse`` 每一拍都进得来 —— 但真正
    危险的那一刻是「画面刚黑掉、人还没反应过来」的那半秒。守死人那条协程
    是唯一一直在看的东西,所以这一条判断得放在它里面。
    """
    理由 = [""]
    t = 带闸(fake_device, engine, fake_clock, 理由)
    try:
        await t.pulse(0.5, 0.0, 0.0)
        理由[0] = "前相机没画面"
        # 超时给 0.2s,不给默认的 3.0s:``fake_clock`` 没被拨动,它底下垫的
        # 还是真 monotonic,真等上 HEARTBEAT_TIMEOUT_S(0.6s)一样会把
        # ``active`` 拨成 False —— 那是心跳超时那条路在起作用,不是这条视频闸
        # 在起作用。0.2s 远够视频闸(周期 TICK=0.01s)反应,又远不够心跳超时
        # 假装替它擦屁股。
        await 等到(lambda: not t.active, timeout=0.2)   # 用这个文件里既有的那个助手
        assert t.active is False
        assert fake_device.walk_calls[-1] == (0.0, 0.0, 0.0, 0.0)
    finally:
        await t.aclose()


async def test_引擎在跑就不许遥控(fake_device, 跑着的engine, fake_clock):
    t = 带闸(fake_device, 跑着的engine, fake_clock, [""])
    with pytest.raises(TeleopBusy):
        await t.pulse(0.5, 0.0, 0.0)


async def test_引擎让开腿的时候可以遥控(fake_device, 挂起的engine, fake_clock):
    """§5.10:持租约 + 引擎已挂起 → 放行。

    **租约那一半不在这儿。** ``app/control.py`` 的 ``CONTROLLED`` 已经把
    ``POST /api/teleop`` 整条路由钉在租约后面了 —— 在这儿再查一遍就是把
    同一条规矩存两份,而两份迟早不一样。这儿只管「引擎让不让位」。
    """
    t = 带闸(fake_device, 挂起的engine, fake_clock, [""])
    try:
        await t.pulse(0.5, 0.0, 0.0)
        assert fake_device.walk_calls
    finally:
        await t.aclose()


async def test_挂起了也一样要有画面(fake_device, 挂起的engine, fake_clock):
    """**让位是让引擎的位,不是让 §5.9 的位。** 挂起恰恰是人要亲自开的时候
    —— 那正是最需要看得见的时候。
    """
    t = 带闸(fake_device, 挂起的engine, fake_clock, ["前相机没画面"])
    with pytest.raises(TeleopBusy):
        await t.pulse(0.5, 0.0, 0.0)


async def test_急停按着仍然不许动(fake_device, engine, fake_clock):
    """闸多了一道,原来那两道不许弱。"""
    fake_device.estop = True
    t = 带闸(fake_device, engine, fake_clock, [""])
    with pytest.raises(TeleopBusy):
        await t.pulse(0.5, 0.0, 0.0)


# ------------------------------------------------------------ 现场档/远程档


def test_默认是现场档(fake_device, engine, fake_clock):
    t = 带闸(fake_device, engine, fake_clock, [""])
    assert t.mode("ab12") == "onsite"


def test_切到远程(fake_device, engine, fake_clock):
    t = 带闸(fake_device, engine, fake_clock, [""])
    t.set_mode("remote", "ab12")
    assert t.mode("ab12") == "remote"


def test_换个人就掉回现场档(fake_device, engine, fake_clock):
    """**档是「这个人现在在哪儿」,不是狗的属性。**

    甲切了远程然后掉线,乙接过控制权时必须从现场档起步 —— 否则乙会在一个
    自己从没确认过的档上开狗,而事后的留痕上写着甲的名字。
    """
    t = 带闸(fake_device, engine, fake_clock, [""])
    t.set_mode("remote", "ab12")
    assert t.mode("cd34") == "onsite"


def test_不认识的档直接拒(fake_device, engine, fake_clock):
    t = 带闸(fake_device, engine, fake_clock, [""])
    with pytest.raises(ValueError):
        t.set_mode("半远程", "ab12")


async def test_远程档不放宽视频那道闸(fake_device, engine, fake_clock):
    """§7.8 明写:**§5.9 不受本节影响,仍是无豁免的硬规则。**"""
    t = 带闸(fake_device, engine, fake_clock, ["前相机没画面"])
    t.set_mode("remote", "ab12")
    with pytest.raises(TeleopBusy):
        await t.pulse(0.5, 0.0, 0.0)
