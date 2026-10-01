"""W11 局部避障:滚动记忆、守卫(扫过区、原地转、实测速度、看不见当挡、机身自己不查)、规划后端被挡
(等、绕、放弃、不发 13330)、感知断了(短暂等、超过 2 s 发 13331)、外参没过拒 goto、第二层许可门
单独也能停住、带链路延迟 / 起步切步态 / 刹车上限的运动模型。仿真里每拍查机身压没压上真值障碍。"""

from __future__ import annotations

import asyncio
import math

import pytest
from obs_world import 世界, 假感知

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.bridges.planned_nav import PlannedNavBackend
from d1max_agent.obstacles import FRESH_S, ObstacleGuard, ObstacleView
from d1max_agent.planning.planner import Planner
from d1max_contract.obsbridge import Grid, pack_bits
from d1max_patrol.backends.base import AlgErrorEvent, NavRequestError, NavStatusEvent
from d1max_patrol.protocol.nav_types import ALG_LIDAR_DISCONNECTED, ALG_NAV_BLOCKED, NavStatus, Pose


class 钟:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000

    def __call__(self) -> int:
        return self.ms

    def s(self) -> float:
        return self.ms / 1000.0


# ------------------------------------------------------------ 记忆与守卫(单元)

def _grid(cells_occ=(), cells_known=None, size=20, res=0.1, seq=1, check="ok"):
    n = size
    occ = [False] * (n * n)
    known = [cells_known is None] * (n * n)
    for r, c in cells_occ:
        occ[r * n + c] = True
        known[r * n + c] = True
    for r, c in (cells_known or ()):
        known[r * n + c] = True
    return Grid(seq=seq, stamp_ns=0, res=res, size=n, occ=pack_bits(occ, n),
                known=pack_bits(known, n), check=check)


def test_记忆_按里程挪_从新到旧_都没看见是未知():
    t = [0.0]
    v = ObstacleView(monotonic=lambda: t[0])
    assert v.state() == "lost"
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_grid(cells_occ=[(15, 10)]))           # 狗身系 x = 0.55、y = 0.05 有东西
    assert v.state() == "ok"
    hit, unk = v.lookup([(0.55, 0.05), (0.15, 0.15)], (0.0, 0.0, 0.0))
    assert hit == [0] and unk == []
    # 往前走了 1.2 m:那个东西在狗身后 0.65 m;新的一帧只看见前面(后面没看见)
    t[0] = 0.2
    v.note_odom(1.2, 0.0, 0.0)
    front_only = [(r, c) for r in range(10, 20) for c in range(20)]
    v.on_grid(_grid(cells_known=front_only, seq=2))
    hit, unk = v.lookup([(-0.65, 0.05), (0.55, 0.05), (-2.5, 0.0)], (1.2, 0.0, 0.0))
    assert hit == [0], "身后的格子靠记忆:旧的那帧看见过它是挡"
    assert unk == [2], "哪一帧都没看见过:未知"
    # 新的一帧把那格看成空的:新的说了算
    t[0] = 0.3
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_grid(seq=3))
    hit, _ = v.lookup([(0.55, 0.05)], (0.0, 0.0, 0.0))
    assert hit == [], "新的一帧说空就空(东西挪走了)"
    t[0] = 0.3 + FRESH_S + 0.01
    assert v.state() == "stale"
    t[0] = 3.0
    assert v.state() == "lost"
    t[0] = 11.0
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_grid(seq=4))
    assert len(v.frames) == 1, "10 s 以前的帧丢掉"


def test_记忆_自检没过不用_状态说清楚():
    v = ObstacleView(monotonic=lambda: 0.0)
    v.note_odom(0, 0, 0)
    v.on_grid(_grid(check="extrinsic_bad"))
    assert v.state() == "extrinsic_bad" and not v.frames
    v2 = ObstacleView(monotonic=lambda: 0.0)
    v2.on_connect()
    v2.on_grid(_grid(check="initializing"))
    assert v2.state() == "initializing"
    v3 = ObstacleView(monotonic=lambda: 0.0)
    v3.on_grid(_grid())                               # 没有里程:配不上
    assert not v3.frames


def test_里程插值():
    t = [0.0]
    v = ObstacleView(monotonic=lambda: t[0])
    v.note_odom(0, 0, 0)
    t[0] = 1.0
    v.note_odom(2, 0, 1.0)
    assert v.pose_at(0.5) == pytest.approx((1.0, 0.0, 0.5))
    assert v.pose_at(-1) == (0.0, 0.0, 0.0) and v.pose_at(9) == (2.0, 0.0, 1.0)


def _view_with(world_cells_occ, size=40):
    v = ObstacleView(monotonic=lambda: 0.0)
    v.note_odom(0, 0, 0)
    v.on_grid(_grid(cells_occ=world_cells_occ, size=size))
    return v


def test_守卫_停车距离按实测速度_原地转按外接圆_不动就放行():
    g = ObstacleGuard()
    # 狗身系 x = 1.25 有一排挡(40 格、0.1 m:行 32)
    v = _view_with([(32, c) for c in range(15, 25)])
    assert g.check(0.0, 0.0, 0.0, v, (0, 0, 0)).ok
    slow = g.check(0.1, 0.0, 0.1, v, (0, 0, 0))
    assert slow.ok, "慢:刹停 + 余量够不着 1.2 m"
    fast = g.check(0.1, 0.0, 0.6, v, (0, 0, 0))
    assert not fast.ok and fast.hits, "实测快(命令慢也不算):扫过区够着了"
    assert not g.check(0.6, 0.0, 0.0, v, (0, 0, 0)).ok, "命令快也算"
    # 原地转:外接圆 0.57 m 以内有东西才挡
    near = _view_with([(25, 20)])                    # x = 0.55
    assert not g.check(0.0, 0.5, 0.0, near, (0, 0, 0)).ok
    far = _view_with([(27, 20)])                     # x = 0.75
    assert g.check(0.0, 0.5, 0.0, far, (0, 0, 0)).ok
    assert g.reach(0.6) == pytest.approx(0.6 * 0.2 + 0.36 + 0.3)


def test_守卫_看不见当挡_机身底下不查_数据不新鲜当挡():
    g = ObstacleGuard()
    n = 40
    only_body_unknown = [(r, c) for r in range(n) for c in range(n)
                         if not (abs((r - n / 2 + 0.5) * 0.1) <= 0.515
                                 and abs((c - n / 2 + 0.5) * 0.1) <= 0.29)]
    v = ObstacleView(monotonic=lambda: 0.0)
    v.note_odom(0, 0, 0)
    v.on_grid(_grid(cells_known=only_body_unknown, size=n))
    assert g.check(0.3, 0.0, 0.3, v, (0, 0, 0)).ok, "机身底下看不见:不查"
    assert g.check(0.0, 0.8, 0.0, v, (0, 0, 0)).ok
    v2 = ObstacleView(monotonic=lambda: 0.0)
    v2.note_odom(0, 0, 0)
    v2.on_grid(_grid(cells_known=[], size=n))
    bad = g.check(0.3, 0.0, 0.0, v2, (0, 0, 0))
    assert not bad.ok and bad.unknown > 0 and not bad.hits, "看不见当挡,但不记成障碍"
    t = [0.0]
    v3 = ObstacleView(monotonic=lambda: t[0])
    v3.note_odom(0, 0, 0)
    v3.on_grid(_grid(size=n))
    t[0] = 1.0
    assert not g.check(0.3, 0.0, 0.0, v3, (0, 0, 0)).ok


# ------------------------------------------------------------ 规划后端 + 仿真世界

class 台子:
    def __init__(self, tmp_path, *, walls=(), rear=True, guard=True, latency=0.0, gait=0.0,
                 decel=math.inf, clearance=False, max_vx=0.6):
        self.c = 钟()
        self.world = 世界(walls=walls)
        self.world.地图(tmp_path)
        self.r = SimRobot(now_ms=self.c, max_vx=max_vx, max_wz=1.5, stop_latency_s=0.2,
                          latency_s=latency, gait_start_s=gait, max_decel=decel,
                          require_clearance=clearance)
        self.nav = PlannedNavBackend(self.r, now_ms=self.c, map_id="m",
                                     planner=Planner(in_process=True))
        self.nav.load_grid(tmp_path)
        self.view = ObstacleView(monotonic=self.c.s)
        self.nav.obstacles = self.view
        self.nav.guard = ObstacleGuard() if guard else _放行()
        self.per = 假感知(self.world, rear=rear)
        self.events: list = []
        self.nav.on_event = lambda k, d: self.events.append((k, d))
        self.status: list = []
        self.algs: list = []
        emit = self.nav.emit

        def 记(e):
            if isinstance(e, NavStatusEvent):
                self.status.append(e.status)
            if isinstance(e, AlgErrorEvent):
                self.algs += [i.code for i in e.items]
            emit(e)
        self.nav.emit = 记
        self.perceive = True
        self.crashed = False

    async def start(self, x, y, yaw=0.0):
        await self.r.connect()
        await self.r.acquire_control()
        await self.nav.connect()
        self.r.teleport(x, y, yaw)
        await self.nav.step(0.1)
        self._feed()

    def _feed(self):
        if not self.perceive:
            return
        g = self.per.grid(self.r.x, self.r.y, self.r.yaw)
        self.view.on_grid(g)
        if self.r.require_clearance:
            from d1max_localizer.obstacles import Config, permit
            ms = permit(self.per.净空(g), Config())
            if ms:
                self.r.clear(ms)

    async def 一拍(self, dt=0.1):
        for _ in range(2000):
            busy = self.nav._planning or (self.nav._replan is not None
                                          and not self.nav._replan.done())
            if not busy:
                break
            await asyncio.sleep(0.005)
        await self.nav.step(dt)
        self.r.tick(dt)
        self.c.ms += int(dt * 1000)
        self._feed()
        for _ in range(3):
            await asyncio.sleep(0)
        if self.world.撞没撞(self.r.x, self.r.y, self.r.yaw):
            self.crashed = True

    async def 跑(self, seconds):
        for _ in range(int(seconds / 0.1)):
            await self.一拍()
            if self.status and self.status[-1] is NavStatus.STANDBY:
                return


class _放行:
    def check(self, vx, wz, v_meas, view, pose):
        from d1max_agent.obstacles import Verdict
        return Verdict(True)


async def test_路上突然有箱子_停在前面_等一会儿_绕过去(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.world.dyn["box"] = (5.0, 3.5, 5.6, 4.5)
    await t.跑(80.0)
    assert not t.crashed
    assert NavStatus.SUCCEED in t.status, (t.status, t.events)
    assert [k for k, _ in t.events if k == "nav_unblocked"], t.events
    assert ALG_NAV_BLOCKED not in t.algs, "不发 13330(W08 决定 8)"


async def test_通道整个堵死_等满20秒放弃_中间报一次被挡(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 5.0)])           # 只剩上面一个口子
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 2.0))
    await t.跑(10.0)
    t.world.dyn["gate"] = (5.2, 5.0, 6.8, 7.95)                # 口子堵死
    await t.跑(60.0)
    assert not t.crashed
    assert t.status[-2:] == [NavStatus.FAILED, NavStatus.STANDBY], t.status
    assert [k for k, _ in t.events].count("nav_blocked") == 1
    assert ALG_NAV_BLOCKED not in t.algs


async def test_挡的东西挪走了_接着走原来的路(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.world.dyn["person"] = (4.0, 3.6, 4.4, 4.4)
    for _ in range(12):
        await t.一拍()
    assert t.nav._blocked_since is not None
    del t.world.dyn["person"]
    await t.跑(60.0)
    assert NavStatus.SUCCEED in t.status and not t.crashed


async def test_感知短暂断_原地等_接上接着走_断太久发13331(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.perceive = False
    for _ in range(10):                                         # 1 s:不新鲜,原地等
        await t.一拍()
    assert abs(t.r.speed[0]) < 1e-9 and NavStatus.FAILED not in t.status
    t.perceive = True
    await t.跑(3.0)
    assert abs(t.r.speed[0]) > 0.1, "接上了接着走"
    t.perceive = False
    await t.跑(5.0)
    assert ALG_LIDAR_DISCONNECTED in t.algs and NavStatus.FAILED in t.status


async def test_外参自检没过_不受理goto(tmp_path):
    t = 台子(tmp_path)
    t.per.check = "extrinsic_bad"
    await t.start(1.5, 4.0)
    with pytest.raises(NavRequestError, match="外参自检没过"):
        await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    t2 = 台子(tmp_path)
    t2.perceive = False
    await t2.start(1.5, 4.0)
    with pytest.raises(NavRequestError, match="断了"):
        await t2.nav.goto(Pose.from_xy_yaw(10.0, 4.0))


async def test_第二层单独也停得住_第一层关掉_许可门挡前进(tmp_path):
    t = 台子(tmp_path, guard=False, clearance=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.world.dyn["box"] = (5.0, 3.0, 5.6, 5.0)
    await t.跑(15.0)
    assert not t.crashed, "第一层关了,旁路进程的许可门照样不放前进"
    assert t.r.x < 5.0 - 0.465


async def test_带链路延迟_起步切步态_刹车上限_也不撞(tmp_path):
    t = 台子(tmp_path, latency=0.2, gait=1.0, decel=0.5)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(4.0)
    t.world.dyn["box"] = (t.r.x + 1.6, 3.5, t.r.x + 2.0, 4.5)
    await t.跑(80.0)
    assert not t.crashed
    assert NavStatus.SUCCEED in t.status, t.status


async def test_要掉头_身后看不见就不转_后雷达开着就转(tmp_path):
    blind = 台子(tmp_path, rear=False)
    await blind.start(5.0, 4.0, yaw=0.0)
    await blind.nav.goto(Pose.from_xy_yaw(2.0, 4.0))            # 目标在身后
    await blind.跑(3.0)
    assert abs(blind.r.yaw) < 0.05, "身后没看过:不原地转"
    seeing = 台子(tmp_path, rear=True)
    await seeing.start(5.0, 4.0, yaw=0.0)
    await seeing.nav.goto(Pose.from_xy_yaw(2.0, 4.0))
    await seeing.跑(40.0)
    assert NavStatus.SUCCEED in seeing.status and not seeing.crashed
