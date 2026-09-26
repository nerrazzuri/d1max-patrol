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
    near_b = min(math.hypot(x - 3.0, y - 3.0) for x, y in track)
    assert near_b < 0.3, "回程先回 B(内审:以前跳过 B 直奔 A 也是绿的)"
    near_a = min(math.hypot(x - 3.0, y) for x, y in track)
    assert near_a < 0.3, "回程经过了 A(沿来路)"
    # 直线回家会经过 L 的内角(返航起点与原点连线的中点附近)。
    mid = (returned_at[0] / 2, returned_at[1] / 2)
    assert min(math.hypot(x - mid[0], y - mid[1]) for x, y in track) > 1.0, "没有斜穿内角"


def test_能力里报导航走哪种路_直线桥报straight():
    from d1max_agent.bridges.hal_nav import HalNavBackend
    assert HalNavBackend.PATH_KIND == "straight"
    r = SimRobot(now_ms=lambda: 0)
    caps = compose_capabilities(robot_id="A", hal_caps=r.hal_capabilities(), adapter_id="sim",
                                loaded_map=("m", "1"), nav_path=HalNavBackend.PATH_KIND,
                                autonomy="autonomous")
    assert caps.tasks["goto"]["path"] == "straight"

def _L(**pol) -> Mission:
    wps = tuple(MissionWaypoint(name=n, pose=Pose.from_xy_yaw(x, y))
                for n, x, y in (("A", 3.0, 0.0), ("B", 3.0, 3.0), ("C", 0.0, 3.0)))
    pol.setdefault("battery_return_pct", 40.0)
    pol.setdefault("battery_abort_pct", 15.0)
    return Mission(mission="L", map_id="m", waypoints=wps, policy=Policy(**pol))


async def test_直线桥报得出狗在哪(台子):
    c, r, parts = 台子
    r.teleport(1.5, -2.0, 0.3)
    p = await parts.nav.current_pose()
    assert (round(p.position.x, 3), round(p.position.y, 3)) == (1.5, -2.0)


async def test_半路电量返航的巡检_报失败带电量_不报跑完(台子, tmp_path):
    """内审阻断 1:引擎返航回到原点落 DONE,代理照样报 ``task_done`` —— 站点当成「跑完了、狗停在
    最后一个点」,派回程巡检让狗先直线奔从没到过的最后一点。半路返航就是没跑完。"""
    from d1max_agent.events import EventBook
    from d1max_agent.tasks.patrol import PatrolTask
    from d1max_contract.messages import TaskState
    c, r, parts = 台子
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    t = PatrolTask(task_id="t1", mission=_L(), parts=parts, events=book, now_ms=c)
    await t.start()
    injected = False
    for _ in range(2000):
        await t.step(0.1)
        await _一拍(c, r, parts)
        if not injected and parts.engine.snapshot.waypoint_index == 2:
            r.inject_battery(35.0)
            injected = True
        if t.done:
            break
    assert injected and RunState.RETURNING in parts.engine._seen
    assert t.state is TaskState.FAILED, (t.state, t.detail)
    assert "电量" in t.detail["reason"], t.detail


async def test_半路电量返航的goto_也报失败(台子, tmp_path):
    from d1max_agent.events import EventBook
    from d1max_agent.tasks.engine_goto import EngineGotoTask
    from d1max_contract.messages import TaskState
    c, r, parts = 台子
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    from d1max_contract.messages import MapPose
    t = EngineGotoTask(task_id="g1", target=MapPose(map_id="m", map_version="1", frame_id="map",
                                                    x=4.0, y=0.0, yaw=0.0),
                       max_speed_mps=None, parts=parts, events=book, now_ms=c)
    await t.start()
    injected = False
    for _ in range(2000):
        await t.step(0.1)
        await _一拍(c, r, parts)
        o = await r.odometry()
        if not injected and o.x > 1.5:
            r.inject_battery(27.0)                     # goto 的默认:中止线 25,返航线 ≥ 25 + 3
            injected = True
        if t.done:
            break
    assert injected and t.state is TaskState.FAILED, (t.state, t.detail)
    assert "电量" in t.detail["reason"], t.detail


async def test_出发点不是原点_沿来路回到出发点就停(台子):
    """内审阻断 2:狗从 (0,-2) 出发(上一趟停在那儿),原点在 (0,0)。回到出发点之后,出发点到原点
    那一段没走过 —— 原地停、按返航失败收尾,原因里带电量(站点按电量告警)。"""
    c, r, parts = 台子
    r.teleport(0.0, -2.0, 0.0)
    await parts.engine.start(_L(), home=parts.home)
    injected = False
    for _ in range(2500):
        await _一拍(c, r, parts)
        if not injected and parts.engine.snapshot.waypoint_index == 2:
            r.inject_battery(35.0)
            injected = True
        if parts.engine.state in FINAL_STATES:
            break
    assert injected
    assert parts.engine.state is RunState.ABORTED, parts.engine.snapshot.reason
    assert "出发点" in parts.engine.snapshot.reason and "电量" in parts.engine.snapshot.reason
    for _ in range(20):
        await _一拍(c, r, parts)
    o = await r.odometry()
    assert math.hypot(o.x - 0.0, o.y + 2.0) < 0.3, (o.x, o.y)



async def test_引擎落DONE但点位结果少了_也报失败不报跑完(台子, tmp_path):
    """防御那一道(W00c6b 内审阻断 1):不管什么原因,DONE 却没跑满「点数 × 圈数」就不是跑完。正常路径
    只有半路返航会这样(上面那条);这里直接把引擎的结果截掉一条,钉住核条数那一句。"""
    import dataclasses

    from d1max_agent.events import EventBook
    from d1max_agent.tasks.patrol import PatrolTask
    from d1max_contract.messages import TaskState
    c, r, parts = 台子
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    m = Mission(mission="two", map_id="m", policy=Policy(),
                waypoints=tuple(MissionWaypoint(name=n, pose=Pose.from_xy_yaw(x, 0.0))
                                for n, x in (("A", 0.5), ("B", 1.0))))
    t = PatrolTask(task_id="t1", mission=m, parts=parts, events=book, now_ms=c)
    await t.start()
    for _ in range(1500):
        await _一拍(c, r, parts)
        if parts.engine.state in FINAL_STATES:
            break
    assert parts.engine.state is RunState.DONE and len(parts.engine.snapshot.results) == 2
    e = parts.engine
    e._snapshot = dataclasses.replace(e._snapshot, results=e._snapshot.results[:1])
    for _ in range(100):
        await t.step(0.1)
        await _一拍(c, r, parts)
        if t.done:
            break
    assert t.state is TaskState.FAILED and "1/2" in t.detail["reason"], (t.state, t.detail)
