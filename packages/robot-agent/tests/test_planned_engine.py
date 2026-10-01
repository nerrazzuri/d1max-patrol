"""W10 引擎 + 规划后端 + 仿真狗:点位被规划拒 → 这个点失败(不再整趟中止);电量返航规划回原点、
绕墙不沿来路;回家规划不出来 → 原地停、按返航失败中止;返航超时按路长;电量按规划路长。"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.assembly import build_engine
from d1max_agent.engine.machine import FINAL_STATES, RETURN_TIMEOUT_S, RunState
from d1max_agent.engine.mission import Mission, MissionWaypoint, Policy
from d1max_agent.planning.planner import Planner
from d1max_contract.zones import ZoneSet
from d1max_patrol.protocol.nav_types import Pose

RES = 0.05


class 钟:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000
        self.mono = 100.0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))
        self.mono += dt_s


def 画图(d, walls=()):
    h, w = int(8 / RES), int(12 / RES)
    occ = np.zeros((h, w), dtype=bool)
    occ[:2, :] = occ[-2:, :] = occ[:, :2] = occ[:, -2:] = True
    for x0, y0, x1, y1 in walls:
        occ[int(y0 / RES):int(math.ceil(y1 / RES)), int(x0 / RES):int(math.ceil(x1 / RES))] = True
    img = np.where(np.flipud(occ), 0, 254).astype(np.uint8)
    (d / "floor.pgm").write_bytes(b"P5\n%d %d\n255\n" % (w, h) + img.tobytes())
    (d / "floor.yaml").write_text(f"resolution: {RES}\norigin: [0.0, 0.0, 0.0]\nnegate: 0\n")


@pytest.fixture
async def 台子(tmp_path):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=0.6, max_wz=1.5, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    画图(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])
    parts = build_engine(r, runs_root=tmp_path / "runs", now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=Pose.from_xy_yaw(2.0, 2.0), nav_kind="planned",
                         planner=Planner(in_process=True))
    parts.nav.load_grid(tmp_path)
    await parts.nav.connect()
    r.teleport(2.0, 2.0, 0.0)
    yield c, r, parts
    await parts.engine.aclose()


async def 规划完(nav):
    """规划在线程里按真实时间算:算完之前不推仿真时钟(不然机器一忙,仿真几百秒过去了路还没出来)。"""
    for _ in range(2000):
        busy = nav._planning or (nav._replan is not None and not nav._replan.done())
        if not busy:
            return
        await asyncio.sleep(0.005)


async def _一拍(c, r, parts, dt=0.1):
    await 规划完(parts.nav)
    await parts.nav.step(dt)
    await parts.device.step(dt)
    r.tick(dt)
    c.advance(dt)
    for _ in range(4):
        await asyncio.sleep(0)


async def _跑完(c, r, parts, limit=4000, each=None):
    for _ in range(limit):
        await _一拍(c, r, parts)
        if each is not None:
            await each()
        if parts.engine.state in FINAL_STATES:
            return
    o = await r.odometry()
    raise AssertionError(f"没跑完:{parts.engine.state} 狗在 ({o.x:.2f}, {o.y:.2f}) "
                         f"导航 {parts.nav._status} 路 {parts.nav._path} "
                         f"区域 {parts.nav.zones} 电 {r._battery_pct}")


def _任务(pts, **pol):
    wps = tuple(MissionWaypoint(name=n, pose=Pose.from_xy_yaw(x, y)) for n, x, y in pts)
    return Mission(mission="t", map_id="m", waypoints=wps, policy=Policy(**pol))


def test_默认还是直线桥_不认识的拒(tmp_path):
    from d1max_agent.bridges.hal_nav import HalNavBackend
    from d1max_agent.bridges.planned_nav import PlannedNavBackend
    r = SimRobot(now_ms=lambda: 0)
    p = build_engine(r, runs_root=tmp_path, now_ms=lambda: 0, map_id="m", home=None)
    assert type(p.nav) is HalNavBackend
    p2 = build_engine(r, runs_root=tmp_path, now_ms=lambda: 0, map_id="m", home=None,
                      nav_kind="planned", robot_radius_m=0.4)
    assert isinstance(p2.nav, PlannedNavBackend) and p2.nav.robot_radius_m == 0.4
    assert p2.nav.PATH_KIND == "planned"
    with pytest.raises(ValueError):
        build_engine(r, runs_root=tmp_path, now_ms=lambda: 0, map_id="m", home=None,
                     nav_kind="nav2")


async def test_点位在禁行区里_这个点失败_其余照走(台子):
    c, r, parts = 台子
    await parts.nav.set_zones(ZoneSet.from_wire({
        "map_id": "m", "map_version": "v", "revision": 1,
        "zones": [{"id": "pond", "kind": "nogo", "polygon": [[3, 4], [5, 4], [5, 6], [3, 6]]}]}))
    await parts.engine.start(_任务([("坏", 4.0, 5.0), ("好", 3.5, 2.0)],
                                   on_waypoint_failed="skip"), home=parts.home)
    await _跑完(c, r, parts)
    assert parts.engine.state is RunState.DONE, parts.engine.snapshot.reason
    res = parts.engine._live.results
    assert [x.ok for x in res] == [False, True]
    assert "导航拒了" in res[0].note and "规划失败" in res[0].note


async def test_电量返航_规划回原点_绕墙_不沿来路(台子):
    c, r, parts = 台子
    await parts.engine.start(_任务([("A", 10.0, 2.0), ("B", 10.0, 5.0)],
                                   battery_return_pct=40.0, battery_abort_pct=15.0),
                             home=parts.home)
    hit = {"done": False}
    track = []

    async def each():
        o = await r.odometry()
        track.append((o.x, o.y))
        if not hit["done"] and parts.engine.snapshot.waypoint_index == 1 and o.y > 3.0:
            r.inject_battery(35.0)
            hit["done"] = True
            hit["at"] = len(track)
    await _跑完(c, r, parts, each=each)
    assert hit["done"]
    assert RunState.RETURNING in parts.engine._seen
    assert parts.engine.state is RunState.DONE, parts.engine.snapshot.reason
    o = await r.odometry()
    assert math.hypot(o.x - 2.0, o.y - 2.0) < 0.3
    # 一路没穿墙
    assert not [(x, y) for x, y in track if 5.3 < x < 6.7 and y < 6.0]
    # 规划回家,不沿来路(沿来路要先回到 A 再回出发点)
    back = track[hit["at"]:]
    assert min(math.hypot(x - 10.0, y - 2.0) for x, y in back) > 0.8


async def test_回家规划不出来_原地停_按返航失败中止(台子):
    c, r, parts = 台子
    await parts.engine.start(_任务([("A", 10.0, 2.0), ("B", 10.0, 5.0)], battery_return_pct=40.0,
                                   battery_abort_pct=15.0), home=parts.home)
    for _ in range(3000):
        await _一拍(c, r, parts)
        if parts.engine._live.results:
            break
    # 到了 A 之后:口子封死、再掉电 → 回家规划不出来
    await parts.nav.set_zones(ZoneSet.from_wire({
        "map_id": "m", "map_version": "v", "revision": 1,
        "zones": [{"id": "gap", "kind": "nogo",
                   "polygon": [[5.5, 5.9], [6.5, 5.9], [6.5, 8], [5.5, 8]]}]}))
    r.inject_battery(35.0)
    o0 = await r.odometry()
    await _跑完(c, r, parts)
    assert parts.engine.state is RunState.ABORTED, (parts.engine.snapshot.reason,
                                                    parts.engine._seen, o0)
    assert "返航失败" in parts.engine.snapshot.reason
    o = await r.odometry()
    assert math.hypot(o.x - o0.x, o.y - o0.y) < 0.3


async def test_返航超时按路长(台子):
    c, r, parts = 台子
    eng = parts.engine
    parts.nav.planned_length_m = None
    assert eng._return_budget_s() == RETURN_TIMEOUT_S
    parts.nav.planned_length_m = 20.0
    assert eng._return_budget_s() == pytest.approx(20.0 / 0.4 * 2 + 60)
    parts.nav.planned_length_m = float("nan")
    assert eng._return_budget_s() == RETURN_TIMEOUT_S


async def test_返航电量按规划路长(台子):
    from d1max_agent.engine.homing import ReturnParams
    c, r, parts = 台子
    eng = parts.engine
    # 让路长决定电量(不被 3% 下限盖住)
    eng._return_params = ReturnParams(drain_pct_per_hour=20000.0)
    await eng.start(_任务([("A", 10.0, 2.0)]), home=parts.home)
    straight = eng._return_cost_pct()                # 还没算出路长:直线 × 1.4
    for _ in range(100):
        await asyncio.sleep(0.01)
        if parts.nav.path_length_hint(Pose.from_xy_yaw(10.0, 2.0), parts.home.pose):
            break
    planned = eng._return_cost_pct()
    L = parts.nav.path_length_hint(Pose.from_xy_yaw(10.0, 2.0), parts.home.pose)
    assert L > 12.0
    from d1max_agent.engine.homing import estimate_cost_pct
    p = eng._return_params
    assert straight == pytest.approx(estimate_cost_pct(8.0, p))
    assert planned == pytest.approx(estimate_cost_pct(L / 1.4, p))
    assert planned > straight
    await eng.stop() if hasattr(eng, "stop") else None


class 慢规划器(Planner):
    def __init__(self):
        super().__init__(in_process=True)
        self.go = asyncio.Event()
        self.calls = 0

    async def plan(self, cm, start, goal):
        self.calls += 1
        await self.go.wait()
        return await super().plan(cm, start, goal)


async def test_规划期间被叫停_引擎不接着派下一个点(tmp_path):
    """内审应修 3:叫停让 goto 抛出来,以前按点位失败走 skip、接着规划下一个点。"""
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=0.6, max_wz=1.5, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    画图(tmp_path)
    pl = 慢规划器()
    parts = build_engine(r, runs_root=tmp_path / "runs", now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=Pose.from_xy_yaw(2.0, 2.0), nav_kind="planned",
                         planner=pl)
    parts.nav.load_grid(tmp_path)
    await parts.nav.connect()
    r.teleport(2.0, 2.0, 0.0)
    try:
        await parts.engine.start(_任务([("A", 5.0, 2.0), ("B", 5.0, 5.0)],
                                       on_waypoint_failed="skip"), home=parts.home)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if parts.nav._planning:
                break
        assert parts.nav._planning
        await parts.nav.stop()                           # 叫停:导航先停
        await parts.engine.abort("人叫停")               # 引擎的中止跟着进队列
        pl.go.set()
        for _ in range(300):
            await asyncio.sleep(0.01)
            if parts.engine.state in FINAL_STATES:
                break
        assert parts.engine.state is RunState.ABORTED, parts.engine.snapshot
        assert pl.calls == 1, "没接着去规划下一个点"
    finally:
        await parts.engine.aclose()


async def test_规划时没定位_走定位恢复_不把后面的点跑空(台子):
    """W10 外审 3:skip 策略下,第二个点规划时没有可信定位 —— 以前当场记失败、接着第三个、第四个……
    转眼跑空。现在停车、重定位、等恢复,然后重试当前点(不算一次重试)。"""
    from d1max_patrol.backends.base import LocStatusEvent, NavNotLocalizedError
    from d1max_patrol.protocol.nav_types import LocStatus
    c, r, parts = 台子
    nav = parts.nav
    real = nav.goto
    state = {"n": 0, "refused": False}

    async def goto(pose):
        state["n"] += 1
        if state["n"] == 2:                               # 第二个点:这会儿没定位
            state["refused"] = True
            raise NavNotLocalizedError("goto", "没有可信定位,不规划")
        await real(pose)
    nav.goto = goto
    await parts.engine.start(_任务([("A", 3.5, 2.0), ("B", 3.5, 4.0), ("C", 2.0, 4.0)],
                                   on_waypoint_failed="skip"), home=parts.home)
    for _ in range(2000):
        await _一拍(c, r, parts)
        if state["refused"]:
            break
    for _ in range(30):
        await _一拍(c, r, parts)
    assert parts.engine.state is RunState.PAUSED, parts.engine.snapshot
    assert len(parts.engine._live.results) == 1, "B、C 没被当场记成失败"
    nav.emit(LocStatusEvent(LocStatus.CONTINUOUS_LOC, LocStatus.LOC_LOST))   # 定位回来了
    await _跑完(c, r, parts)
    assert parts.engine.state is RunState.DONE, parts.engine.snapshot.reason
    assert [x.ok for x in parts.engine._live.results] == [True, True, True]
