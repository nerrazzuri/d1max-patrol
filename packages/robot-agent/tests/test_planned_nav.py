"""W10 规划后端 + 仿真狗 + 栅格世界:绕墙、绕禁行区、限速区、到点对朝向、热更收紧、在禁行区里、
规划期间叫停、偏离重规划、丢定位、回家。每拍查狗离墙、离禁行区够不够远(不是只看终点)。"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.bridges.planned_nav import PlannedNavBackend
from d1max_agent.planning.planner import Planner
from d1max_contract.zones import ZoneSet, distance_to_polygon
from d1max_patrol.backends.base import NavRequestError, NavStatusEvent
from d1max_patrol.protocol.nav_types import NavStatus, Pose

RES = 0.05
W_M, H_M = 12.0, 8.0


class 钟:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))


def 画世界(tmp_path, walls=()):
    """12 × 8 m 的院子,四周是墙;``walls``:地图系里的矩形 (x0, y0, x1, y1)。
    返回真值占用(行从下往上)。"""
    h, w = int(H_M / RES), int(W_M / RES)
    occ = np.zeros((h, w), dtype=bool)
    occ[:2, :] = occ[-2:, :] = True
    occ[:, :2] = occ[:, -2:] = True
    for x0, y0, x1, y1 in walls:
        occ[int(y0 / RES):int(math.ceil(y1 / RES)), int(x0 / RES):int(math.ceil(x1 / RES))] = True
    img = np.where(np.flipud(occ), 0, 254).astype(np.uint8)
    (tmp_path / "floor.pgm").write_bytes(b"P5\n%d %d\n255\n" % (w, h) + img.tobytes())
    (tmp_path / "floor.yaml").write_text(
        f"image: floor.pgm\nresolution: {RES}\norigin: [0.0, 0.0, 0.0]\nnegate: 0\n"
        "occupied_thresh: 0.65\nfree_thresh: 0.196\n")
    return occ


def 离墙(occ, x, y):
    ys, xs = np.nonzero(occ)
    return float(np.min(np.hypot((xs + 0.5) * RES - x, (ys + 0.5) * RES - y)))


def 区域(*zs, rev=1):
    return ZoneSet.from_wire({"map_id": "m", "map_version": "v", "revision": rev,
                              "zones": list(zs)})


class 台子:
    def __init__(self, tmp_path, walls=(), planner=None, radius=0.52):
        self.c = 钟()
        self.occ = 画世界(tmp_path, walls)
        self.r = SimRobot(now_ms=self.c, max_vx=0.6, max_wz=1.5, stop_latency_s=0.2)
        self.nav = PlannedNavBackend(self.r, now_ms=self.c, map_id="m",
                                     planner=planner or Planner(in_process=True),
                                     robot_radius_m=radius)
        self.nav.load_grid(tmp_path)
        self.events: list = []
        self.nav.on_event = lambda k, d: self.events.append((k, d))
        self.status: list[NavStatus] = []
        emit = self.nav.emit

        def 记(e):
            if isinstance(e, NavStatusEvent):
                self.status.append(e.status)
            emit(e)
        self.nav.emit = 记
        self.min_clear = math.inf
        self.min_zone = math.inf
        self.speeds: list[tuple[float, float, float]] = []

    async def start(self, x, y, yaw=0.0):
        await self.r.connect()
        await self.r.acquire_control()
        await self.nav.connect()
        self.r.teleport(x, y, yaw)
        await self.nav.step(0.1)

    async def 一拍(self, dt=0.1):
        for _ in range(2000):              # 规划在线程里按真实时间算:算完之前不推仿真时钟
            busy = self.nav._planning or (self.nav._replan is not None
                                          and not self.nav._replan.done())
            if not busy:
                break
            await asyncio.sleep(0.005)
        await self.nav.step(dt)
        self.r.tick(dt)
        self.c.advance(dt)
        for _ in range(3):
            await asyncio.sleep(0)
        o = await self.r.odometry()
        self.min_clear = min(self.min_clear, 离墙(self.occ, o.x, o.y))
        for z in (self.nav.zones.nogo() if self.nav.zones else ()):
            self.min_zone = min(self.min_zone, distance_to_polygon(o.x, o.y, z.polygon))
        self.speeds.append((o.x, o.y, abs(o.vx)))

    async def 走到头(self, limit_s=120.0):
        for _ in range(int(limit_s / 0.1)):
            await self.一拍()
            if self.nav._status is NavStatus.STANDBY and self.status and self.status[-1] in (
                    NavStatus.SUCCEED, NavStatus.FAILED, NavStatus.CANCELLED, NavStatus.STANDBY):
                return
        raise AssertionError(f"{limit_s} s 没走完;状态 {self.status[-5:]}")

    async def 在(self):
        return await self.r.odometry()


async def test_绕墙到点_对朝向_一路不贴墙(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])       # 中间一堵墙,上面留 2 m 口子
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 2.0, math.pi / 2))
    assert t.nav.planned_length_m > 12.0
    await t.走到头()
    o = await t.在()
    assert NavStatus.SUCCEED in t.status
    assert math.hypot(o.x - 10.0, o.y - 2.0) <= 0.2
    assert abs(math.remainder(o.yaw - math.pi / 2, 2 * math.pi)) <= 0.2
    assert t.min_clear >= 0.4, t.min_clear


async def test_绕禁行区_不进(tmp_path):
    t = 台子(tmp_path)
    await t.nav.set_zones(区域({"id": "pond", "kind": "nogo", "label": "池子",
                              "polygon": [[5, 1], [7, 1], [7, 6], [5, 6]]}))
    await t.start(2.0, 3.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 3.0))
    await t.走到头()
    o = await t.在()
    assert math.hypot(o.x - 10.0, o.y - 3.0) <= 0.2
    assert t.min_zone >= 0.4, t.min_zone


async def test_终点在禁行区_没图_没定位_都同步拒_狗不动(tmp_path):
    t = 台子(tmp_path)
    await t.nav.set_zones(区域({"id": "pond", "kind": "nogo",
                              "polygon": [[5, 1], [7, 1], [7, 6], [5, 6]]}))
    await t.start(2.0, 3.0)
    with pytest.raises(NavRequestError, match="规划失败"):
        await t.nav.goto(Pose.from_xy_yaw(6.0, 3.0))
    with pytest.raises(NavRequestError, match="规划失败"):
        await t.nav.goto(Pose.from_xy_yaw(20.0, 3.0))
    t.nav.load_grid(None)
    assert not t.nav.plan_ok
    with pytest.raises(NavRequestError, match="没有规划栅格"):
        await t.nav.goto(Pose.from_xy_yaw(3.0, 3.0))
    t.nav.load_grid(tmp_path / "没有")
    assert "载不了" in t.nav.plan_problem
    t.nav.load_grid(tmp_path)
    t.r.inject_loc_lost(True)
    await t.一拍()
    with pytest.raises(NavRequestError, match="定位"):
        await t.nav.goto(Pose.from_xy_yaw(3.0, 3.0))
    for _ in range(10):
        await t.一拍()
    o = await t.在()
    assert (round(o.x, 3), round(o.y, 3)) == (2.0, 3.0)
    assert t.status == []


async def test_限速区里不超速(tmp_path):
    t = 台子(tmp_path)
    await t.nav.set_zones(区域({"id": "slow", "kind": "slow", "max_speed_mps": 0.2,
                              "polygon": [[4, 0], [8, 0], [8, 8], [4, 8]]}))
    await t.start(1.0, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(11.0, 4.0))
    await t.走到头()
    inside = [v for x, _, v in t.speeds if 4.6 <= x <= 7.4]
    outside = [v for x, _, v in t.speeds if 1.5 <= x <= 3.0]
    assert inside and max(inside) <= 0.2 + 1e-6
    assert max(outside) > 0.4


async def test_热更收紧_挡住正在走的路_重规划绕开(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.5, 4.0))
    for _ in range(20):
        await t.一拍()
    await t.nav.set_zones(区域({"id": "new", "kind": "nogo",
                              "polygon": [[6, 2], [7, 2], [7, 6], [6, 6]]}))
    await t.走到头()
    o = await t.在()
    assert NavStatus.SUCCEED in t.status
    assert math.hypot(o.x - 10.5, o.y - 4.0) <= 0.2
    assert t.min_zone >= 0.4


async def test_狗在新禁行区里_原地停_失败_发事件_不往外走(tmp_path):
    t = 台子(tmp_path)
    await t.start(2.0, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    for _ in range(30):
        await t.一拍()
    o = await t.在()
    await t.nav.set_zones(区域({"id": "here", "kind": "nogo", "label": "花坛",
                              "polygon": [[o.x - 1, 2], [o.x + 1, 2], [o.x + 1, 6], [o.x - 1, 6]]}))
    assert t.status[-1] is NavStatus.FAILED
    assert t.events and t.events[0][0] == "inside_nogo" and t.events[0][1]["zone"] == "here"
    for _ in range(20):
        await t.一拍()
    o2 = await t.在()
    assert math.hypot(o2.x - o.x, o2.y - o.y) < 0.25            # 只有刹车那一段
    with pytest.raises(NavRequestError, match="禁行区"):
        await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))


class 慢规划器(Planner):
    def __init__(self):
        super().__init__(in_process=True)
        self.go = asyncio.Event()

    async def plan(self, cm, start, goal):
        await self.go.wait()
        return await super().plan(cm, start, goal)


async def test_规划期间叫停_晚到的结果作废_狗不动(tmp_path):
    pl = 慢规划器()
    t = 台子(tmp_path, planner=pl)
    await t.start(2.0, 4.0)
    task = asyncio.create_task(t.nav.goto(Pose.from_xy_yaw(10.0, 4.0)))
    for _ in range(3):
        await asyncio.sleep(0)
    with pytest.raises(NavRequestError, match="正在规划"):
        await t.nav.goto(Pose.from_xy_yaw(3.0, 4.0))
    await t.nav.stop()
    pl.go.set()
    with pytest.raises(NavRequestError, match="被叫停"):
        await task
    for _ in range(20):
        await t.一拍()
    o = await t.在()
    assert (round(o.x, 3), round(o.y, 3)) == (2.0, 4.0) and t.status == []


async def test_被推开_偏离路径_重规划接着走(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 2.0))
    for _ in range(40):
        await t.一拍()
    t.r.teleport(2.0, 6.5, 0.0)                 # 被人抱走了
    await t.走到头()
    o = await t.在()
    assert NavStatus.SUCCEED in t.status and t.nav._replans >= 1
    assert math.hypot(o.x - 10.0, o.y - 2.0) <= 0.2


async def test_重规划不出来_失败(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 2.0))
    for _ in range(20):
        await t.一拍()
    # 口子被新禁行区封死:收紧,没有别的路
    await t.nav.set_zones(区域({"id": "gap", "kind": "nogo",
                              "polygon": [[5.5, 5.9], [6.5, 5.9], [6.5, 8], [5.5, 8]]}))
    await t.走到头()
    assert t.status[-2:] == [NavStatus.FAILED, NavStatus.STANDBY]


async def test_丢定位自己停(tmp_path):
    t = 台子(tmp_path)
    await t.start(2.0, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    for _ in range(10):
        await t.一拍()
    t.r.inject_loc_lost(True)
    await t.走到头()
    assert NavStatus.FAILED in t.status and NavStatus.SUCCEED not in t.status


async def test_回家_规划回去(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])
    await t.start(10.0, 2.0)
    with pytest.raises(NavRequestError, match="return_to"):
        await t.nav.return_home()
    await t.nav.return_to(Pose.from_xy_yaw(2.0, 2.0))
    await t.走到头()
    o = await t.在()
    assert math.hypot(o.x - 2.0, o.y - 2.0) <= 0.2 and t.min_clear >= 0.4


async def test_路长估计_先给None_后台算完再给(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])
    await t.start(2.0, 2.0)
    a, b = Pose.from_xy_yaw(2.0, 2.0), Pose.from_xy_yaw(10.0, 2.0)
    assert t.nav.path_length_hint(a, b) is None
    for _ in range(50):
        await asyncio.sleep(0.01)
        if t.nav.path_length_hint(a, b) is not None:
            break
    assert t.nav.path_length_hint(a, b) > 12.0
    await t.nav.set_zones(区域())
    assert t.nav.path_length_hint(a, b) is None         # 换区域清空
