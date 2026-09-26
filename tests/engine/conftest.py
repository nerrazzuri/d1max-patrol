"""引擎测试的公共零件。

假后端是**手写的小对象**,不是 mock 库:测试要能直接改 ``fake_nav.loc``
这样的字段,而 mock 的自动 spec 在这里只会碍事。

``NavStub``/``DeviceStub``/``MediaStub``/``Clock`` 连同 ``nav``/``device``/
``media``/``clock``/``make_engine`` 这几个夹具、``until`` 这个轮询助手,原来
都长在 ``test_machine.py`` 里,``test_machine_suspend.py`` 靠跨模块 import
它们(外加一个专门压 ruff F401 的 ``__all__``)复用。搬到这儿之后两个测试
模块都不用 import 就能直接用这些夹具 —— pytest 本来就会把 conftest.py 里
的 ``@pytest.fixture`` 自动喂给同目录下的每个测试文件,不用再跨模块 import
一遍,这也是仓库里其他共享夹具(比如下面这个 ``sample_mission``)一直在用
的办法。``_HOME``/``NEVER``/``until`` 不是夹具,是普通的值和函数,该怎么
导入还怎么导入。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from d1max_agent.engine.homing import HomePoint
from d1max_agent.engine.machine import MissionEngine
from d1max_agent.engine.mission import Action, Mission, MissionWaypoint, Policy
from d1max_patrol.backends.base import (
    DeviceEvent,
    Event,
    EventEmitter,
    Frame,
    LocStatusEvent,
    MediaError,
    MediaSource,
    NavBackendError,
    NavStatusEvent,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

from ..conftest import NoDisks


def make_mission(**overrides) -> Mission:
    base = {
        "mission": "test_patrol",
        "map_id": "map_test",
        "waypoints": (
            MissionWaypoint(
                name="P1_transformer",
                pose=Pose.from_xy_yaw(1.0, 0.0, 0.0),
                check="配电柜门是否关闭",
                actions=(Action(type="photo", camera="front"),),
            ),
            MissionWaypoint(
                name="P2_panel",
                pose=Pose.from_xy_yaw(2.0, 1.0, 1.57),
                actions=(Action(type="photo", camera="back"),),
            ),
        ),
        "policy": Policy(),
    }
    base.update(overrides)
    return Mission(**base)  # type: ignore[arg-type]


@pytest.fixture
def sample_mission() -> Mission:
    return make_mission()


class FakeNav:
    """假导航后端。只实现引擎真正会问的那几件事。

    不继承 ``NavBackend``:那样得把十几个抽象方法全写成 stub,而测试只关心
    ``nav`` / ``loc`` 这两个字段。端口的完整性由 ``tests/backends`` 盯着。
    """

    def __init__(self) -> None:
        self.nav = NavStatus.STANDBY
        self.loc = LocStatus.CONTINUOUS_LOC

    async def nav_status(self) -> NavStatus:
        return self.nav

    async def loc_status(self) -> LocStatus:
        return self.loc


class FakeDevice:
    """假设备后端。``batt`` / ``estop`` / ``control`` 直接改。"""

    def __init__(self) -> None:
        self.batt = 88.0
        self.estop = False
        self.control = True

    async def battery(self) -> float:
        return self.batt

    async def emergency(self) -> bool:
        return self.estop

    async def has_control(self) -> bool:
        return self.control


@pytest.fixture
def fake_nav() -> FakeNav:
    return FakeNav()


@pytest.fixture
def fake_device() -> FakeDevice:
    return FakeDevice()


# ------------------------------------------------------- 状态机测试的假后端

#: 起飞门槛把原点当成前置条件(preflight §home)。这些测试关心的是状态机
#: 的行为,不是原点本身,给个跟 ``sample_mission`` 同一张图的原点,免得每个
#: 用例都要单独传。
_HOME = HomePoint(map_id="map_test", pose=Pose.from_xy_yaw(0.0, 0.0),
                  marked_at_ms=1_757_000_000_000)

ARRIVED = [NavStatusEvent(NavStatus.SUCCEED)]
NEVER = []

#: 下发流水里代表"回家"的那一笔。``return_home()`` 接口上就没有目标位姿这个
#: 参数(见 ``backends/base.py``),用一个哨兵占位,好让"最后一次下发的是回
#: 家"和"最后一次下发的是某个点位"能在同一条记录上比 —— 任务 13 要断的正是
#: 「人接管完之后又重新发了一次回家」,分成两条计数就比不出"最后一次是谁"。
原点 = "原点"


class NavStub(EventEmitter[Event]):
    """假导航。``on_goto`` 决定每次下发之后推什么事件回来。

    不继承 ``NavBackend``:那要补十几个抽象方法,而引擎只用得上这几个。
    端口的完整性由 ``tests/backends`` 盯着。
    """

    #: 它自己会 ``return_home``(W00c6b):回家的电按直线估。拒返航的用例要沿来路回的话自己改成
    #: ``straight``。
    PATH_KIND = "planned"

    def __init__(self) -> None:
        super().__init__()
        self.nav = NavStatus.STANDBY
        self.loc = LocStatus.CONTINUOUS_LOC
        self.on_goto: list[Event] = list(ARRIVED)
        self.goto_calls: list[Pose] = []
        self.stop_calls = 0
        self.reset_calls = 0
        self.home_calls = 0
        self.reset_recovers = True
        #: 每次下发之后,状态机在终态上驻留几次问询才回落 StandBy。
        #: 真设备就是这样的:上一段导航到了 Succeed,状态机要过一会儿才让位。
        self.hold_after_goto = 0
        #: 还剩几次问询回终态。归零之前 ``goto`` 会像真设备那样直接拒绝。
        self.terminal_holds = 0
        #: ``return_home()`` 之后推什么事件回来。跟 ``on_goto`` 一个道理:
        #: 默认那份立刻推一条 ``Succeed``,引擎一拍就跑完 DONE,根本没有
        #: 「返航途中」这段时间可以去 suspend。
        self.on_return_home: list[Event] = list(ARRIVED)
        #: 下发过的目标点。``goto`` 记位姿,``return_home`` 记 ``原点``,
        #: **两者同一条流水**——只有这样才问得出"最后一次下发的是什么"。
        #: 跟 ``goto_calls`` 并存而不是取代它:``goto_calls`` 是"这一趟一共
        #: 发过几次点位"的累计量,好些既有用例按它的长度断言;这条流水是可以
        #: 被 ``清空下发记录()`` 归零的窗口,两个问题不一样,合成一个就会互相
        #: 拆台。
        self.下发过的目标点: list[Pose | str] = []
        #: ``current_pose()`` 答什么(W00c6b)。``None`` = 报不出位姿,引擎按从原点出发算。
        self.pose_now: Pose | None = None

    async def current_pose(self) -> Pose | None:
        return self.pose_now

    async def nav_status(self) -> NavStatus:
        if self.terminal_holds > 0:
            self.terminal_holds -= 1
            return NavStatus.SUCCEED
        return self.nav

    async def loc_status(self) -> LocStatus:
        return self.loc

    def emit_loc(self, status: LocStatus) -> None:
        """喂一条定位状态事件,顺带把 ``loc_status()`` 会答的那个值也改了。

        人拍的板 2(2026-09-10)那组测试要模拟"人把狗开出了地图"/"狗还在
        地图里"——两件事都得两头一致:既要让引擎收到一条
        ``LocStatusEvent``,也要让之后任何轮询 ``loc_status()`` 的代码
        看到同一个答案,不然两条真相打起来。
        """
        self.loc = status
        self.emit(LocStatusEvent(status))

    def 清空下发记录(self) -> None:
        """把下发流水归零,开一个新窗口。**不动 ``goto_calls``**(理由见字段)。

        断"人接管完之后**又**发了一次回家"需要一个干净的起点:不清的话,
        挂起之前那一次 ``return_home`` 就已经躺在流水末尾了,``[-1] == 原点``
        在引擎什么都没做的情况下也成立 —— 那是一句恒真的空话。
        """
        self.下发过的目标点.clear()

    async def goto(self, pose: Pose) -> None:
        if self.terminal_holds > 0:
            raise NavBackendError("导航只能在 StandBy 下启动,当前 Succeed")
        self.goto_calls.append(pose)
        self.下发过的目标点.append(pose)
        for event in self.on_goto:
            self.emit(event)
        self.terminal_holds = self.hold_after_goto

    async def stop(self) -> None:
        self.stop_calls += 1

    async def reset_localization(self) -> None:
        self.reset_calls += 1
        if self.reset_recovers:
            self.emit(LocStatusEvent(LocStatus.CONTINUOUS_LOC))

    async def return_home(self) -> None:
        self.home_calls += 1
        self.下发过的目标点.append(原点)
        for event in self.on_return_home:
            self.emit(event)


class DeviceStub(EventEmitter[DeviceEvent]):
    def __init__(self) -> None:
        super().__init__()
        self.batt = 88.0
        self.estop = False
        self.control = True
        self.lights: list[bool] = []
        self.gimbals: list[tuple[float, float]] = []
        self.lie_calls = 0
        self.stand_calls = 0

    async def battery(self) -> float:
        return self.batt

    async def emergency(self) -> bool:
        return self.estop

    async def has_control(self) -> bool:
        return self.control

    async def set_light(self, on: bool) -> None:
        self.lights.append(on)

    async def set_gimbal(self, pitch: float, yaw: float) -> None:
        self.gimbals.append((pitch, yaw))

    async def lie(self) -> None:
        self.lie_calls += 1

    async def stand(self) -> None:
        self.stand_calls += 1


class MediaStub(MediaSource):
    def __init__(self) -> None:
        self.grabs = 0
        self.fail = False

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def grab(self) -> Frame:
        if self.fail:
            raise MediaError("取不到图")
        self.grabs += 1
        return Frame(data=b"\xff\xd8fake", mime="image/jpeg", captured_at_ms=1)

    async def healthy(self) -> bool:
        return not self.fail


class Clock:
    """可以往前拨的表。用真 monotonic 打底,免得和 asyncio 的超时脱节。"""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


@pytest.fixture
def nav() -> NavStub:
    return NavStub()


@pytest.fixture
def device() -> DeviceStub:
    return DeviceStub()


@pytest.fixture
def media() -> dict[str, MediaStub]:
    return {"front": MediaStub(), "back": MediaStub()}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_engine(nav, device, media, clock, tmp_path):
    """造引擎。每个用例自己负责 ``aclose`` —— 收尾本身就是被测行为之一。

    原点不在这里给:它是每趟开跑时由 ``start(mission, home=...)`` 换进去的。
    """
    def _make(**kwargs) -> MissionEngine:
        # removable 给个假探针,免得默认的 DEFAULT_PROBE 去扫真机上的
        # /media、/mnt(见 tests/conftest.py);setdefault 是为了让显式传了
        # removable= 的用例照样用自己那份。
        kwargs.setdefault("removable", NoDisks())
        return MissionEngine(nav, device, media, tmp_path / "runs",
                             clock=clock, **kwargs)

    return _make


async def until(pred, timeout: float = 3.0) -> None:
    """等到条件成立。轮询而不是固定 sleep —— 固定 sleep 要么慢要么脆。"""
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("等条件超时")
        await asyncio.sleep(0.005)
