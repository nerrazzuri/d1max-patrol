"""引擎测试的公共零件。

假后端是**手写的小对象**,不是 mock 库:测试要能直接改 ``fake_nav.loc``
这样的字段,而 mock 的自动 spec 在这里只会碍事。
"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.mission import Action, Mission, MissionWaypoint, Policy
from d1max_patrol.protocol.nav_types import Pose


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
