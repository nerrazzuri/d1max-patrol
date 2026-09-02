"""引擎测试的公共零件。

假后端是**手写的小对象**,不是 mock 库:测试要能直接改 ``fake_nav.loc``
这样的字段,而 mock 的自动 spec 在这里只会碍事。
"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.mission import Action, Mission, MissionWaypoint, Policy
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose


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
