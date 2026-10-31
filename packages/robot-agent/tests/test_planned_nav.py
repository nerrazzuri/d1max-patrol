"""W10 规划后端 + 仿真狗 + 栅格世界:绕墙、绕禁行区、限速区、到点对朝向、热更收紧、在禁行区里、
规划期间叫停、偏离重规划、丢定位、回原点。每拍查狗离墙、离禁行区够不够远(不是只看终点)。"""

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
    """12 × 8 m 的现场,四周是墙;``walls``:地图系里的矩形 (x0, y0, x1, y1)。
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
    """到墙的实体(每个占用格是一整块)的最近距离(W10 外审 1:以前量到格心,量出来偏大)。"""
    ys, xs = np.nonzero(occ)
    dx = np.maximum(np.abs(x - (xs + 0.5) * RES) - RES / 2, 0.0)
    dy = np.maximum(np.abs(y - (ys + 0.5) * RES) - RES / 2, 0.0)
    return float(np.min(np.hypot(dx, dy)))


def 区域(*zs, rev=1):
    return ZoneSet.from_wire({"map_id": "m", "map_version": "v", "revision": rev,
                              "zones": list(zs)})


class 台子:
    def __init__(self, tmp_path, walls=(), planner=None, radius=0.52, **robot):
        self.c = 钟()
        self.occ = 画世界(tmp_path, walls)
        self.r = SimRobot(now_ms=self.c, max_vx=0.6, max_wz=1.5, stop_latency_s=0.2, **robot)
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
        raise AssertionError(f"{limit_s} s 未走完;状态 {self.status[-5:]}")

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
    assert t.min_clear >= 0.52, t.min_clear      # 机身外接圆不碰墙(路径本身 ≥ 0.57)


async def test_绕禁行区_不进(tmp_path):
    t = 台子(tmp_path)
    await t.nav.set_zones(区域({"id": "pond", "kind": "nogo", "label": "池子",
                              "polygon": [[5, 1], [7, 1], [7, 6], [5, 6]]}))
    await t.start(2.0, 3.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 3.0))
    await t.走到头()
    o = await t.在()
    assert math.hypot(o.x - 10.0, o.y - 3.0) <= 0.2
    assert t.min_zone >= 0.52, t.min_zone


async def test_抹掉区_规划当禁行绕开_终点在里面拒_狗走进去每拍查到就停(tmp_path):
    """地图台账 44:管理员在图上抹掉的一块(``erase``),狗永远不去 —— 规划绕开、离它跟离禁行区一样远,
    终点在里面同步拒;狗被推进去,每拍回退检查就停、发 inside_nogo。"""
    t = 台子(tmp_path)
    await t.nav.set_zones(区域({"id": "junk", "kind": "erase", "label": "抹掉",
                              "polygon": [[5, 1], [7, 1], [7, 6], [5, 6]]}))
    await t.start(2.0, 3.0)
    with pytest.raises(NavRequestError, match="规划失败"):
        await t.nav.goto(Pose.from_xy_yaw(6.0, 3.0))
    assert t.nav.nogo_at(6.0, 3.0) is not None and t.nav.nogo_at(6.0, 3.0).id == "junk"
    await t.nav.goto(Pose.from_xy_yaw(10.0, 3.0))
    await t.走到头()
    o = await t.在()
    assert math.hypot(o.x - 10.0, o.y - 3.0) <= 0.2
    assert t.min_zone >= 0.52, t.min_zone
    await t.nav.goto(Pose.from_xy_yaw(2.0, 7.0))
    for _ in range(5):
        await t.一拍()
    t.r.teleport(6.0, 5.5, 0.0)
    await t.一拍()
    await t.一拍()
    assert t.status[-1] is NavStatus.FAILED
    assert [k for k, _ in t.events][-1:] == ["inside_nogo"]


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
    assert "无法加载" in t.nav.plan_problem
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
    assert t.min_zone >= 0.52


async def test_狗在新禁行区里_原地停_失败_发事件_不往外走(tmp_path):
    t = 台子(tmp_path)
    await t.start(2.0, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    for _ in range(30):
        await t.一拍()
    o = await t.在()
    await t.nav.set_zones(区域({"id": "here", "kind": "nogo", "label": "高楼楼梯",
                              "polygon": [[o.x - 1, 2], [o.x + 1, 2], [o.x + 1, 6], [o.x - 1, 6]]}))
    assert t.status[-1] is NavStatus.FAILED
    assert t.events and t.events[0][0] == "inside_nogo" and t.events[0][1]["zone"] == "here"
    for _ in range(20):
        await t.一拍()
    o2 = await t.在()
    assert math.hypot(o2.x - o.x, o2.y - o.y) < 0.25            # 只有刹车那一段
    with pytest.raises(NavRequestError, match="不自己往外走"):
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


async def test_回原点_规划回去(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])
    await t.start(10.0, 2.0)
    with pytest.raises(NavRequestError, match="return_to"):
        await t.nav.return_home()
    await t.nav.return_to(Pose.from_xy_yaw(2.0, 2.0))
    await t.走到头()
    o = await t.在()
    assert math.hypot(o.x - 2.0, o.y - 2.0) <= 0.2 and t.min_clear >= 0.52


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


async def test_走着走着进了禁行区_每拍都查_停(tmp_path):
    """定位漂、被推:狗自己走进了已有的禁行区(不是新画的)—— 下一拍就停、发事件。"""
    t = 台子(tmp_path)
    await t.nav.set_zones(区域({"id": "pond", "kind": "nogo", "label": "池子",
                              "polygon": [[5, 6], [7, 6], [7, 7.5], [5, 7.5]]}))
    await t.start(2.0, 3.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 3.0))
    for _ in range(10):
        await t.一拍()
    t.r.teleport(6.0, 6.8, 0.0)
    await t.一拍()
    await t.一拍()
    assert NavStatus.FAILED in t.status
    assert [k for k, _ in t.events] == ["inside_nogo"]


async def test_朝向差大先原地转_不边走边甩(tmp_path):
    t = 台子(tmp_path)
    await t.start(3.0, 4.0, yaw=math.pi)                 # 背对目标
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    seen = []
    for _ in range(40):
        await t.一拍()
        o = await t.在()
        err = abs(math.remainder(0.0 - o.yaw, 2 * math.pi))
        seen.append((err, abs(o.x - 3.0)))
    turning = [dx for err, dx in seen if err > 0.8]
    assert turning and max(turning) < 0.05, seen[:10]


async def test_规划期间区域换了_按新的重算(tmp_path):
    pl = 慢规划器()
    t = 台子(tmp_path, planner=pl)
    await t.start(2.0, 4.0)
    task = asyncio.create_task(t.nav.goto(Pose.from_xy_yaw(10.0, 4.0)))
    for _ in range(5):
        await asyncio.sleep(0.01)
    await t.nav.set_zones(区域({"id": "wall", "kind": "nogo",
                              "polygon": [[5.5, 2], [6.5, 2], [6.5, 6], [5.5, 6]]}))
    pl.go.set()
    await task
    await t.走到头()
    assert NavStatus.SUCCEED in t.status
    assert t.min_zone >= 0.52, t.min_zone


def test_定位不确定度加宽禁行区(tmp_path):
    from d1max_agent.localization import OdomAnchor

    class E:
        def __init__(self, s, x=0.0, y=0.0):
            self.sigma_xy_m, self.x, self.y = s, x, y
    t = 台子(tmp_path)
    m = t.nav._margin
    assert m(None) == 0.0 and m(E(0.3)) == pytest.approx(0.3) and m(E(-1)) == 0.0
    assert m(E(0.9)) == 0.5 and m(E(float("nan"))) == 0.5 and m(E(float("inf"))) == 0.5
    goal = Pose.from_xy_yaw(1.0, 0.0)
    assert m(E(0.1), goal) == pytest.approx(0.1), "仿真按原样:σ 不涨"
    t.nav.use_anchor(OdomAnchor(identity=False))
    # 里程锚定(内审应修 4):按走到终点时的 σ 算,每米涨 2/9
    assert m(E(0.1), goal) == pytest.approx(0.1 + 2.0 / 9.0)
    assert m(E(0.1), Pose.from_xy_yaw(9.0, 0.0)) == 0.5


async def test_路长估计_算到一半区域换了_不记旧的(tmp_path):
    pl = 慢规划器()
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)], planner=pl)
    await t.start(2.0, 2.0)
    a, b = Pose.from_xy_yaw(2.0, 2.0), Pose.from_xy_yaw(10.0, 2.0)
    assert t.nav.path_length_hint(a, b) is None
    for _ in range(5):
        await asyncio.sleep(0.01)
    await t.nav.set_zones(区域({"id": "x", "kind": "nogo",
                              "polygon": [[3, 6.5], [4, 6.5], [4, 7.5], [3, 7.5]]}))
    pl.go.set()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if t.nav._len_task.done():
            break
    assert t.nav._len_cache == {}, "换了区域之后算完的旧路长不许记下"


async def test_热更收紧_核对新区域期间原地等_不沿旧路走进去(tmp_path):
    """内审阻断 2:算新代价图要时间(Orin 上大图一两秒);期间以前沿旧路全速走,机身先压进去。"""
    import time as _time
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.5, 4.0))
    for _ in range(15):
        await t.一拍()
    o = await t.在()
    slow = t.nav._costmap

    def 慢(m):
        _time.sleep(0.3)
        return slow(m)
    t.nav._costmap = 慢
    task = asyncio.create_task(t.nav.set_zones(区域({"id": "new", "kind": "nogo", "polygon": [
        [o.x + 0.9, 2], [o.x + 1.9, 2], [o.x + 1.9, 6], [o.x + 0.9, 6]]})))
    moved = []
    while not task.done():
        await t.nav.step(0.1)
        t.r.tick(0.1)
        t.c.advance(0.1)
        await asyncio.sleep(0.02)
        moved.append((await t.在()).x)
    await task
    assert moved and max(moved) - o.x < 0.25, "核对期间只有刹车那一段"
    t.nav._costmap = slow
    await t.走到头()
    assert NavStatus.SUCCEED in t.status and t.min_zone >= 0.52


async def test_核对新区域期间走到头或被叫停_不炸(tmp_path):
    import time as _time
    t = 台子(tmp_path)
    await t.start(2.0, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    for _ in range(5):
        await t.一拍()
    slow = t.nav._costmap

    def 慢(m):
        _time.sleep(0.2)
        return slow(m)
    t.nav._costmap = 慢
    task = asyncio.create_task(t.nav.set_zones(区域({"id": "far", "kind": "nogo",
                                                  "polygon": [[1, 7], [2, 7], [2, 7.5]]})))
    await asyncio.sleep(0.05)
    await t.nav.stop()
    await task                                         # 以前这里 AssertionError
    assert not t.nav._hold


async def test_空闲的狗被新禁行区圈在里面_也发事件(tmp_path):
    t = 台子(tmp_path)
    await t.start(3.0, 3.0)
    await t.nav.set_zones(区域({"id": "bed", "kind": "nogo", "label": "高楼楼梯",
                              "polygon": [[2, 2], [4, 2], [4, 4], [2, 4]]}))
    assert t.events and t.events[0][0] == "inside_nogo" and t.events[0][1]["idle"] is True
    assert t.status == []
    for _ in range(3):
        await t.一拍()
    assert [k for k, _ in t.events] == ["inside_nogo"], "空闲每拍的检查不再报一遍(工单 16)"


async def test_限速低于死区的限速区当禁行绕开(tmp_path):
    t = 台子(tmp_path)
    await t.nav.set_zones(区域({"id": "crawl", "kind": "slow", "max_speed_mps": 0.03,
                              "polygon": [[5, 1], [7, 1], [7, 6], [5, 6]]}))
    await t.start(2.0, 3.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 3.0))
    await t.走到头()
    assert NavStatus.SUCCEED in t.status
    assert not [x for x, y, _ in t.speeds if 5 <= x <= 7 and 1 <= y <= 6], "守不住的限速区不进"


async def test_走进障碍格就停(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 6.0)])
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 2.0))
    for _ in range(5):
        await t.一拍()
    t.r.teleport(6.0, 3.0, 0.0)          # 定位跳进了墙里:偏离 → 重规划 → 起点在墙里
    for _ in range(5):
        await t.一拍()
    assert NavStatus.FAILED in t.status


async def test_重规划的结果是旧区域上的_不装(tmp_path):
    t = 台子(tmp_path)
    await t.start(2.0, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    for _ in range(6):                                  # 过了起步延时,在跟路径了
        await t.一拍()
    assert t.nav._status is NavStatus.ACTIVE
    old = t.nav._path
    from d1max_agent.planning.planner import PlannedPath
    bogus = PlannedPath(((2.0, 4.0), (2.0, 7.9)))
    t.nav._replan_out = ("ok", (bogus, t.nav._cm), t.nav._epoch - 1)
    await t.一拍()
    assert t.nav._path is not bogus
    assert t.nav._path is old or t.nav._replans >= 1


async def test_新禁行区贴着机身画_没压中心也停(tmp_path):
    t = 台子(tmp_path)
    await t.start(2.0, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    for _ in range(10):
        await t.一拍()
    o = await t.在()
    await t.nav.set_zones(区域({"id": "edge", "kind": "nogo", "polygon": [
        [o.x - 1, o.y + 0.15], [o.x + 1, o.y + 0.15], [o.x + 1, o.y + 1], [o.x - 1, o.y + 1]]}))
    assert t.status[-1] is NavStatus.FAILED and t.events[-1][0] == "inside_nogo"


async def test_窄限速带_两端都在外面也减速_进带之前已经降下来(tmp_path):
    """W10 外审 4:狗和 0.6 m 前视点都在一条 0.3 m 宽的限速带外面、中间穿过它;
    剪枝后是一整条长线段。"""
    t = 台子(tmp_path)
    await t.nav.set_zones(区域(
        {"id": "band", "kind": "slow", "max_speed_mps": 0.2,
         "polygon": [[6.0, 0.2], [6.3, 0.2], [6.3, 7.8], [6.0, 7.8]]},
        {"id": "inner", "kind": "slow", "max_speed_mps": 0.1,
         "polygon": [[6.1, 3.5], [6.2, 3.5], [6.2, 4.5], [6.1, 4.5]]}))
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.5, 4.0))
    assert len(t.nav._path.points) == 2, "一整条长线段(剪枝未被限速带打断)"
    await t.走到头()
    assert NavStatus.SUCCEED in t.status
    in_band = [v for x, _, v in t.speeds if 6.0 <= x <= 6.3]
    assert in_band and max(in_band) <= 0.1 + 1e-6, "叠着取最低"
    before = [v for x, _, v in t.speeds if 5.9 <= x < 6.0]
    assert before and max(before) <= 0.1 + 1e-6, "进带之前已降到上限"
    assert max(v for x, _, v in t.speeds if 2.5 <= x <= 4.0) > 0.4, "远处照常走"


async def test_到了拐点就换下一段_不追着拐点本身走(tmp_path):
    from d1max_agent.planning.planner import PlannedPath
    t = 台子(tmp_path)
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 5.0))
    for _ in range(4):
        await t.一拍()
    t.nav._path = PlannedPath(((2.0, 2.0), (4.0, 2.0), (4.0, 5.0), (5.0, 5.0)))
    t.nav._seg = 0
    t.r.teleport(3.95, 2.0, 0.0)                         # 离第一个拐点 5 cm
    await t.一拍()
    assert t.nav._seg == 1, "到了拐点就换下一段"


# ------------------------------------------------------------ S7(2026-10-14):每拍禁行区回退检查
# 按机身矩形 + 定位 σ + 此刻速度下的刹车距离;限速低到守不住的限速区一样当禁行。


def _在(x, y, yaw=0.0, sigma=0.0):
    from types import SimpleNamespace
    return SimpleNamespace(x=x, y=y, yaw=yaw, sigma_xy_m=sigma)


def _区(*zs):
    return 区域(*zs)


async def test_S7_回退检查_机身侧面加σ_不是看中心(tmp_path):
    """狗身中心离禁行区 0.27 m(原来 0.25 m 的线外,照走),机身侧面只差 3 cm:σ 0.05 就该刹。"""
    t = 台子(tmp_path)
    await t.nav.set_zones(_区({"id": "bed", "kind": "nogo", "label": "高楼楼梯",
                              "polygon": [[0, 4.27], [12, 4.27], [12, 6], [0, 6]]}))
    t.nav._v_meas = 0.3                                   # 顺着高楼楼梯边往前走
    assert t.nav._backstop(_在(5.0, 4.0, sigma=0.0)) == ("", None)
    v, z = t.nav._backstop(_在(5.0, 4.0, sigma=0.05))
    assert v == "near" and z.id == "bed"
    t.nav._v_meas = 0.0
    assert t.nav._backstop(_在(5.0, 4.0, sigma=0.05)) == ("", None), "站着不动不刹(原地转归规划管)"
    v, _ = t.nav._backstop(_在(5.0, 4.1))                  # 侧面压上 7 cm
    assert v == "inside"


async def test_S7_回退检查_刹车距离_快了就刹_慢了不刹_往外走不刹(tmp_path):
    """禁行区在机头前 0.3 m:0.6 m/s 再走一拍、再刹停要走 0.54 m,进去了;0.1 m/s 只要 4 cm。"""
    t = 台子(tmp_path)
    await t.nav.set_zones(_区({"id": "pond", "kind": "nogo",
                              "polygon": [[5.765, 0], [8, 0], [8, 8], [5.765, 8]]}))
    for v, want in ((0.6, "near"), (0.4, "near"), (0.1, "")):
        t.nav._v_meas = v
        assert t.nav._backstop(_在(5.0, 4.0))[0] == want, v
    t.nav._v_meas = 0.6
    assert t.nav._backstop(_在(5.0, 4.0, yaw=math.pi))[0] == "", "往外走(背着池子)不刹"
    t.nav._dir = -1                                        # 狗尾为前:往狗尾那头走
    assert t.nav._backstop(_在(5.0, 4.0, yaw=math.pi))[0] == "near"
    t.nav._dir = 0
    # 限速:离池子越近越慢;离得远不限
    cap = t.nav._zone_cap(_在(5.0, 4.0))
    assert 0 < cap < 0.4, cap
    assert t.nav._zone_cap(_在(4.0, 4.0)) > cap
    assert t.nav._zone_cap(_在(1.0, 4.0)) == math.inf
    assert t.nav._zone_cap(_在(5.0, 4.0, yaw=math.pi)) == math.inf, "往外走不限"


async def test_S7_限速低到守不住的限速区_回退检查和保持距离都当禁行(tmp_path):
    """能力对账附录 A 说明 3:规划、「先挪」当禁行,每拍回退检查和保持距离的 ``nogo_near`` 原来只看
    原始禁行区。"""
    t = 台子(tmp_path)
    await t.nav.set_zones(_区({"id": "crawl", "kind": "slow", "max_speed_mps": 0.03,
                              "polygon": [[4, 3.5], [6, 3.5], [6, 4.5], [4, 4.5]]},
                             {"id": "ok", "kind": "slow", "max_speed_mps": 0.3,
                              "polygon": [[8, 3.5], [10, 3.5], [10, 4.5], [8, 4.5]]}))
    v, z = t.nav._backstop(_在(5.0, 4.0))
    assert v == "inside" and z.id == "crawl"
    assert t.nav._backstop(_在(9.0, 4.0))[0] == "", "守得住的限速区不当禁行"
    assert t.nav.nogo_near(5.0, 3.0, 0.6).id == "crawl", "保持距离那头也认"
    assert t.nav.nogo_near(9.0, 3.0, 0.6) is None


class σ锚:
    """仿真里程就是位姿,σ 固定(测正常走会不会被回退检查误停)。"""

    def __new__(cls, sigma):
        from d1max_agent.localization import OdomAnchor

        class _A(OdomAnchor):
            @property
            def sigma_xy(self):
                return sigma
        return _A(identity=True)


@pytest.mark.parametrize("sigma", [0.0, 0.2, 0.4])
@pytest.mark.parametrize("case", ["around", "corridor", "zigzag", "headon", "elbow"])
async def test_S7_正常规划的路_几种σ_不被回退检查误停(tmp_path, sigma, case):
    """规划出的路离禁行区 0.57 m + σ:绕池子;穿过两个禁行区夹出来的、刚好过得去的通道;在通道口急拐
    (拐点原地转,机身角甩向禁行区的角);正对着禁行区走到膨胀边外的终点;沿着一条通道全速冲向正对着
    禁行区的拐点再拐上去(按刹车距离提前减速,不然要刹)—— 都不刹、不停、机身一下都不压上。"""
    t = 台子(tmp_path)
    t.nav.use_anchor(σ锚(sigma))
    m = math.ceil(sigma / 0.1 - 1e-9) * 0.1                # 规划按 σ 加宽禁行区(按 0.1 m 取整)
    half = 0.57 + m + 0.3                    # 通道半宽:刚好过得去(栅格 0.1 m、按半格对角线往保守取)
    lo = {"id": "lo", "kind": "nogo", "polygon": [[4, 0.2], [8, 0.2], [8, 4 - half], [4, 4 - half]]}
    hi = {"id": "hi", "kind": "nogo", "polygon": [[4, 4 + half], [8, 4 + half], [8, 7.8], [4, 7.8]]}
    x_end = 7.6 + 2 * (0.57 + m) + 0.4                     # 拐角那条竖通道的右边:正对着来路
    pond = {"id": "pond", "kind": "nogo", "polygon": [[5, 2], [7, 2], [7, 5], [5, 5]]}
    zones = {"around": [pond], "corridor": [lo, hi], "zigzag": [lo, hi],
             "headon": [{"id": "wall", "kind": "nogo",
                         "polygon": [[9, 0.2], [11.8, 0.2], [11.8, 7.8], [9, 7.8]]}],
             "elbow": [{"id": "blk", "kind": "nogo",
                        "polygon": [[0.2, 2.6], [7.6, 2.6], [7.6, 7.8], [0.2, 7.8]]},
                       {"id": "end", "kind": "nogo",
                        "polygon": [[x_end, 0.2], [11.8, 0.2], [11.8, 7.8], [x_end, 7.8]]}]}[case]
    start, goal = {"around": ((2.0, 3.5), (10.0, 3.5)), "corridor": ((1.5, 4.0), (10.5, 4.0)),
                   "zigzag": ((2.5, 1.2), (9.5, 6.8)),
                   "headon": ((2.0, 4.0), (9.0 - 0.57 - m - 0.15, 4.0)),
                   "elbow": ((1.5, 1.15), ((7.6 + x_end) / 2, 6.8))}[case]
    await t.nav.set_zones(区域(*zones))
    near = []
    real = t.nav._near_nogo

    async def 记(z, here, dt):
        near.append((z.id, round(here.x, 2), round(here.y, 2)))
        await real(z, here, dt)
    t.nav._near_nogo = 记
    worst = [math.inf]
    step = t.一拍

    async def 量(dt=0.1):
        await step(dt)
        o = await t.在()
        from d1max_agent.zone_guard import footprint, rect_poly_distance
        for z in t.nav.zones.nogo():
            worst[0] = min(worst[0], rect_poly_distance(footprint(o.x, o.y, o.yaw), z.polygon))
    t.一拍 = 量
    await t.start(*start)
    await t.nav.goto(Pose.from_xy_yaw(*goal))
    await t.走到头()
    assert NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason, near)
    assert near == [], near
    assert not [e for e in t.events if e[0] == "inside_nogo"]
    assert worst[0] > 0.0, worst
    assert max(v for _, _, v in t.speeds) > 0.55, "离得远的那几段照常快走"


async def test_S7_路不知道的禁行区_正对着冲过去_刹得住_机身一下都没进去(tmp_path):
    """定位漂、规划栅格画漏了:路直直穿过一个禁行区(这里直接塞给回退检查,不换路)。仿真狗有 0.2 s
    链路延迟、0.5 m/s² 刹车减速度 —— 原来「中心离 0.25 m」的线,机头要先进去 0.2 m 再加刹车那一段。
    现在按刹车距离提前刹、重规划(路还是穿过去 → 刹 → 用光次数 → FAILED),机身没进去。"""
    t = 台子(tmp_path, latency_s=0.2, max_decel=0.5)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.5, 4.0))
    for _ in range(20):
        await t.一拍()
    assert (await t.在()).vx > 0.5
    t.nav.zones = 区域({"id": "ditch", "kind": "nogo", "label": "沟",
                       "polygon": [[7.0, 0.2], [8.0, 0.2], [8.0, 7.8], [7.0, 7.8]]})
    t.nav._rebox()                                        # 只给回退检查,不重算代价图(路不知道)
    nose = 0.0
    for _ in range(400):
        await t.一拍()
        o = await t.在()
        nose = max(nose, o.x + 0.465)
        if t.status and t.status[-1] in (NavStatus.FAILED, NavStatus.STANDBY):
            break
    assert nose < 7.0, f"机头进了沟({nose:.3f})"
    assert NavStatus.FAILED in t.status and NavStatus.SUCCEED not in t.status


async def test_工单16_空闲时被遥控器开进禁行区_也报一次_出来再进去再报(tmp_path):
    """2026-10-28 D3:旁路直接把狗开回 Z1 里,20 s 没有 P1;代理只在区域变了、或者自己跑任务时查。
    手持遥控器同样绕开代理。空闲时也每拍查(有可信定位才查)。"""
    t = 台子(tmp_path)
    await t.start(6.0, 3.0)
    await t.nav.set_zones(区域({"id": "bed", "kind": "nogo", "label": "高楼楼梯",
                              "polygon": [[2, 2], [4, 2], [4, 4], [2, 4]]}))
    assert not t.events
    for _ in range(3):
        await t.一拍()
    t.r.teleport(3.0, 3.0, 0.0)                             # 遥控器开进去(不经代理)
    for _ in range(5):
        await t.一拍()
    got = [d for k, d in t.events if k == "inside_nogo"]
    assert len(got) == 1 and got[0]["idle"] is True and got[0]["zone"] == "bed", t.events
    assert t.status == [], "空闲的不发导航状态"
    for _ in range(5):
        await t.一拍()
    assert len([1 for k, _ in t.events if k == "inside_nogo"]) == 1, "在里面待着不重复报"
    t.r.teleport(6.0, 3.0, 0.0)
    for _ in range(3):
        await t.一拍()
    t.r.teleport(3.0, 3.0, 0.0)
    for _ in range(3):
        await t.一拍()
    assert len([1 for k, _ in t.events if k == "inside_nogo"]) == 2, "出来再进去:再报"
