"""真狗返航沿来路(W00c6b,核查 A)。

以前:直线桥 ``HalNavBackend`` 登记了原点就 ``goto(原点)`` —— 直线、不规划、不避障;
引擎的 W04 沿来路回(``_retrace_home``)只在后端**同步拒绝** ``return_home`` 时才走,
所以真链路上根本走不到。
"""

from __future__ import annotations

import asyncio
import math

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.assembly import build_engine
from d1max_agent.engine.machine import FINAL_STATES, RunState
from d1max_agent.engine.mission import Mission, MissionWaypoint, Policy
from d1max_agent.status import compose_capabilities
from d1max_patrol.backends.base import NavRequestError
from d1max_patrol.protocol.nav_types import Pose


class 钟:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000
        self.mono = 100.0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))
        self.mono += dt_s


@pytest.fixture
async def 台子(tmp_path):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    parts = build_engine(r, runs_root=tmp_path / "runs", now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=Pose.from_xy_yaw(0.0, 0.0))
    yield c, r, parts
    await parts.engine.aclose()


async def _一拍(c, r, parts, dt=0.1):
    await parts.nav.step(dt)
    await parts.device.step(dt)
    r.tick(dt)
    c.advance(dt)
    for _ in range(4):
        await asyncio.sleep(0)


async def test_直线桥不认return_home_登记了原点也拒_狗不动(台子):
    c, r, parts = 台子
    r.teleport(3.0, 0.0, 0.0)
    with pytest.raises(NavRequestError, match="沿来路"):
        await parts.nav.return_home()
    for _ in range(20):
        await _一拍(c, r, parts)
    o = await r.odometry()
    assert abs(o.x - 3.0) < 0.01 and abs(o.vx) < 0.01


async def test_电量到线_引擎沿来路回原点_不走直线(台子):
    """L 形巡检:(3,0) → (3,3) → (0,3)。往第三个点走到一半时电量到线:直线回原点会斜穿 L 的内角;
    沿来路回是 (3,3) → (3,0) → 原点。"""
    c, r, parts = 台子
    wps = tuple(MissionWaypoint(name=n, pose=Pose.from_xy_yaw(x, y))
                for n, x, y in (("A", 3.0, 0.0), ("B", 3.0, 3.0), ("C", 0.0, 3.0)))
    mission = Mission(mission="L", map_id="m", waypoints=wps,
                      policy=Policy(battery_return_pct=40.0, battery_abort_pct=15.0))
    await parts.engine.start(mission, home=parts.home)
    returned_at = None
    track: list[tuple[float, float]] = []
    for _ in range(1500):
        await _一拍(c, r, parts)
        o = await r.odometry()
        if returned_at is None and parts.engine.snapshot.waypoint_index == 2 and o.x < 1.8:
            r.inject_battery(35.0)                   # 在返航线与中止线之间
            returned_at = (o.x, o.y)
        if returned_at is not None:
            track.append((o.x, o.y))
        if parts.engine.state in FINAL_STATES:
            break
    assert returned_at is not None, "前提:走到了往第三个点的路上"
    assert RunState.RETURNING in parts.engine._seen
    assert parts.engine.state is RunState.DONE, parts.engine.snapshot.reason
    o = await r.odometry()
    assert math.hypot(o.x, o.y) < 0.3, "回到了原点"
    near_a = min(math.hypot(x - 3.0, y) for x, y in track)
    assert near_a < 0.3, "回程经过了 A(沿来路)"
    # 直线回家会经过 L 的内角(返航起点与原点连线的中点附近)。
    mid = (returned_at[0] / 2, returned_at[1] / 2)
    assert min(math.hypot(x - mid[0], y - mid[1]) for x, y in track) > 1.0, "没有斜穿内角"


def test_能力里报导航走哪种路_直线桥报straight():
    r = SimRobot(now_ms=lambda: 0)
    caps = compose_capabilities(robot_id="A", hal_caps=r.hal_capabilities(), adapter_id="sim",
                                loaded_map=("m", "1"), autonomy="autonomous")
    assert caps.tasks["goto"]["path"] == "straight"
