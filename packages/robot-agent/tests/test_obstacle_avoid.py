"""W11 局部避障:滚动记忆、守卫(扫过区、原地转、实测速度、看不见当挡、机身自己不查)、规划后端被挡
(等、绕、放弃、不发 13330)、感知断了(短暂等、超过 2 s 发 13331)、外参没过拒 goto、第二层许可门
单独也能停住、带链路延迟 / 起步切步态 / 刹车上限的运动模型。仿真里每拍查机身压没压上真值障碍。"""

from __future__ import annotations

import asyncio
import math

import pytest
from obs_world import 世界, 假感知

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.bridges.planned_nav import CREEP_M, PlannedNavBackend
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
    v = ObstacleView(monotonic=lambda: t[0], latency_s=0.0)
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


def _现场视野(size=80, res=0.1, occ=()):
    """照 C40221 实测(obs-3f4):前后各一个往外变宽的视野锥,两侧看不见。"""
    c = [(i - size / 2 + 0.5) * res for i in range(size)]
    known = [(r, k) for r in range(size) for k in range(size)
             if (abs(c[r]) > 0.465 and abs(c[k]) <= 0.6 + 0.9 * (abs(c[r]) - 0.465))]
    return _grid(cells_occ=occ, cells_known=known, size=size, res=res)


def test_W34_现场视野_直行起得了步_转向扫进两侧盲带_前面真有挡照样拦():
    g = ObstacleGuard()
    v = ObstacleView(monotonic=lambda: 0.0)
    v.note_odom(0, 0, 0)
    v.on_grid(_现场视野())
    assert g.check(0.3, 0.0, 0.3, v, (0, 0, 0)).ok, "直行:扫过区都在前面看得见的锥里"
    turn = g.check(0.0, 0.4, 0.0, v, (0, 0, 0))
    assert not turn.ok and not turn.hits and turn.unknown > 100, "原地转:机身角甩进盲带"
    arc = g.check(0.3, 0.3, 0.3, v, (0, 0, 0))
    assert not arc.ok and not arc.hits and arc.unknown, "弧线也是"
    blocked = ObstacleView(monotonic=lambda: 0.0)
    blocked.note_odom(0, 0, 0)
    blocked.on_grid(_现场视野(occ=[(46, k) for k in range(37, 43)]))    # 正前 0.65 m 一排
    hit = g.check(0.3, 0.0, 0.3, blocked, (0, 0, 0))
    assert not hit.ok and hit.hits, "身外真有挡:照拦"


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
                 decel=math.inf, clearance=False, max_vx=0.6, rear_cal=None, head="head",
                 cone=False):
        self.c = 钟()
        self.world = 世界(walls=walls)
        self.world.地图(tmp_path)
        self.r = SimRobot(now_ms=self.c, max_vx=max_vx, max_wz=1.5, stop_latency_s=0.2,
                          latency_s=latency, gait_start_s=gait, max_decel=decel,
                          require_clearance=clearance)
        self.r.inject_head(head)
        self.nav = PlannedNavBackend(self.r, now_ms=self.c, map_id="m",
                                     planner=Planner(in_process=True))
        self.nav.load_grid(tmp_path)
        self.view = ObstacleView(monotonic=self.c.s)
        self.nav.obstacles = self.view
        self.nav.guard = ObstacleGuard() if guard else _放行()
        self.per = 假感知(self.world, rear=rear, rear_cal=rear_cal, cone=cone)
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
        self._q: list = []

    async def start(self, x, y, yaw=0.0):
        await self.r.connect()
        await self.r.acquire_control()
        await self.nav.connect()
        self.r.teleport(x, y, yaw)
        await self.nav.step(0.1)
        for _ in range(3):                       # 第一帧路上要 0.15 s:先等它送到
            self._feed()
            self.c.ms += 100
            await self.nav.step(0.1)
        self._feed()

    def _feed(self):
        """假感知按此刻的真实位姿出栅格,**晚 0.15 s 才送到**(跟代理按「收到 − 延迟」配位姿对上;
        内审应修 4:原来没延迟,测不出配错位姿)。"""
        if not self.perceive:
            self._q.clear()
            return
        g = self.per.grid(self.r.x, self.r.y, self.r.yaw)
        self._q.append((self.c.s() + 0.15, g))
        while self._q and self._q[0][0] <= self.c.s() + 1e-9:
            self.view.on_grid(self._q.pop(0)[1])
        if self.r.require_clearance:
            from d1max_localizer.obstacles import Config, permit
            for end in ("head", "tail") if self.per.rear_cal else ("head",):
                ms = permit(self.per.净空(g, end), Config())
                if ms:
                    self.r.clear(ms, end)

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
    # W34:任务失败原因写清楚是被挡(几处挡、几处看不见),不是笼统的「导航回了 Failed」
    assert t.nav.fail_reason.startswith("被挡 ") and "扫过区有挡" in t.nav.fail_reason, \
        t.nav.fail_reason


async def test_挡的东西挪走了_接着走原来的路(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.world.dyn["person"] = (4.0, 3.6, 4.4, 4.4)
    for _ in range(50):                                      # 近障降速(W12):慢慢靠过去才被挡
        await t.一拍()
        if t.nav._blocked_since is not None:
            break
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


async def test_要掉头_身后看不见就不转_没后雷达先往前挪_有后雷达往终点那头退(tmp_path):
    """内审测试漏洞:原来只跑 3 s。身后、身侧没看过:不原地转,先挪,再转、走到。
    W35:原来挪 1.0 m,没后雷达时转身那一圈最靠后的一条还没看过,挪了也转不过去、等满 20 s 放弃;
    现在挪 1.3 m,转身那一圈全在前雷达刚看过的范围里,没后雷达也转得过去(每一步照样过守卫)。
    W41:终点在身后、后雷达标过 → 往后退(朝着终点),不再背着终点往前挪。"""
    blind = 台子(tmp_path, rear=False)
    await blind.start(5.0, 4.0, yaw=0.0)
    await blind.nav.goto(Pose.from_xy_yaw(2.0, 4.0))            # 目标在身后
    await blind.跑(3.0)
    assert abs(blind.r.yaw) < 0.05, "身后没看过:不原地转"
    bx = []
    for _ in range(600):
        await blind.一拍()
        bx.append(blind.r.x)
        if blind.status and blind.status[-1] is NavStatus.STANDBY:
            break
    assert not blind.crashed and NavStatus.SUCCEED in blind.status, blind.nav.fail_reason
    assert 5.0 + CREEP_M - 0.2 < max(bx) < 5.0 + CREEP_M + 0.1, "往前挪一次,不多挪"
    seeing = 台子(tmp_path, rear=True)
    await seeing.start(5.0, 4.0, yaw=0.0)
    await seeing.nav.goto(Pose.from_xy_yaw(2.0, 4.0))
    xs = []
    for _ in range(600):
        await seeing.一拍()
        xs.append(seeing.r.x)
        if seeing.status and seeing.status[-1] is NavStatus.STANDBY:
            break
    assert NavStatus.SUCCEED in seeing.status and not seeing.crashed
    assert max(xs) < 5.0 + 0.05, "不背着终点往前挪"
    assert min(xs) < 5.0 - CREEP_M + 0.2, "往终点那头退了,再转"


async def test_W34_现场视野_起步是弧线_只被两侧看不见挡住_先直着挪再走到(tmp_path):
    """2026-10-08 C40221:前后雷达都标过,连直行 1.6 m 都起不了步 —— 起步那条命令带转向,机身角
    甩进两侧盲带(约 150 格看不见)。以前只有纯原地转才「先往前挪」,弧线被挡就原地等到放弃。"""
    t = 台子(tmp_path, rear=True, cone=True)
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 4.6))               # 斜前方:起步是弧线
    for _ in range(600):
        await t.一拍()
        if t.status and t.status[-1] is NavStatus.STANDBY:
            break
    assert NavStatus.SUCCEED in t.status and not t.crashed, (t.status, t.nav.fail_reason)


async def test_W34_现场视野_弧线被挡_前面真有东西_不往前挪(tmp_path):
    """身外真有挡(正前方看得见的那块里):不往前挪,不撞。W41:后雷达标过就往后退一个机身长,再绕过去;
    没后雷达就原地等、到时候放弃。"""
    t = 台子(tmp_path, rear=True, cone=True, walls=[(3.85, 3.7, 3.95, 4.3)])
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 4.6))
    xs = []
    for _ in range(900):
        await t.一拍()
        xs.append(t.r.x)
        if t.status and t.status[-1] is NavStatus.STANDBY:
            break
    assert not t.crashed and NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason)
    assert min(xs) < 3.0 - 1.0, "先往后退了"
    blind = 台子(tmp_path, rear=False, cone=True, walls=[(3.85, 3.7, 3.95, 4.3)])
    await blind.start(3.0, 4.0, yaw=0.0)
    await blind.nav.goto(Pose.from_xy_yaw(5.0, 4.6))
    await blind.跑(40.0)
    assert not blind.crashed and blind.r.x < 3.0 + 0.1, "前面挡着、后面看不见:不挪"
    assert not blind.nav._crept


async def test_W34_转向被挡_有实打实的挡就不往前挪_只有看不见才挪(tmp_path):
    """挡住转向的是看得见的东西(在转身扫过的那一圈里),不是盲带:往前挪解决不了,照旧记临时障碍、绕。"""
    from d1max_agent.obstacles import Verdict
    for hits, crept in (((( -0.52, 0.05),), False), ((), True)):
        t = 台子(tmp_path, rear=True, cone=True)
        await t.start(3.0, 4.0, yaw=0.0)
        await t.nav.goto(Pose.from_xy_yaw(1.0, 4.0))
        await t.跑(0.5)
        from types import SimpleNamespace
        here = SimpleNamespace(x=t.r.x, y=t.r.y, yaw=t.r.yaw)
        v = Verdict(False, "扫过区有挡", hits=hits, unknown=50)
        for _ in range(30):                                  # 3 s:过了「先等一会儿再绕」
            t.c.ms += 100
            t._feed()                                        # 感知照常来(正前方看得清是空的)
            await t.nav._blocked(v, here, 0.1, 0.0, 0.4)
        assert t.nav._crept is crept, hits


async def test_往前挪_前面是禁行区或墙就不挪_对朝向的时候不挪(tmp_path):
    """内审阻断 1:挪之前看地图(致命格、禁行区),不只看守卫;到点对朝向时不挪(挪了就不在点上了)。"""
    t = 台子(tmp_path, rear=True)
    await t.nav.set_zones(_区域({"id": "pond", "kind": "nogo", "label": "池子",
                               "polygon": [[6.0, 3.0], [7.0, 3.0], [7.0, 5.0], [6.0, 5.0]]}))
    await t.start(5.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(2.0, 4.0))
    await t.跑(30.0)
    assert t.r.x < 5.0 + 0.05, "前面 1 m 加机身前沿碰着禁行区:不挪"
    assert not t.nav._crept
    t2 = 台子(tmp_path, rear=True)
    await t2.start(5.0, 4.0, yaw=0.0)
    await t2.nav.goto(Pose.from_xy_yaw(5.05, 4.0, math.pi))    # 已经在点上,只差掉头
    await t2.跑(30.0)
    assert abs(t2.r.x - 5.0) < 0.2, "对朝向的时候不往前挪"


async def test_临时障碍过期_任务结束清掉(tmp_path):
    """内审阻断 2:绕障记下的临时障碍 30 s 过期、到终态清空,不会让以后的 goto 一直绕开。"""
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.world.dyn["box"] = (5.0, 3.5, 5.6, 4.5)
    for _ in range(60):                                       # 被挡 2 s 之后才记
        await t.一拍()
    assert t.nav._temp, "被挡久了记下了临时障碍"
    exp = max(t.nav._temp.values())
    t.c.ms = int((exp + 1) * 1000)
    t.nav._expire_temp()
    assert not t.nav._temp, "过期就忘"
    t.nav._temp = {(1, 1): t.c.s() + 99}
    await t.nav._enter_terminal(NavStatus.CANCELLED)
    assert not t.nav._temp, "到终态清空"


def _区域(*zs):
    from d1max_contract.zones import ZoneSet
    return ZoneSet.from_wire({"map_id": "m", "map_version": "v", "revision": 1, "zones": list(zs)})


def test_守卫_急转弯扫掠不漏格():
    """内审应修 3:步数按机身角点走过的弧长;转弯半径小于对角线时并上外接圆。"""
    g = ObstacleGuard()
    for vx, wz in ((0.1, 0.8), (0.15, 1.0), (0.3, 0.5)):
        pts = g.swept(vx, wz, 0.0)
        cells = {(math.floor(x / 0.1), math.floor(y / 0.1)) for x, y in pts}
        # 逐点精确算一遍机身(不带余量)在这段里压过的格
        dist = g.reach(vx)
        n = 400
        truth = set()
        for i in range(n + 1):
            d = dist * i / n
            rad = vx / wz
            th = d / rad
            px, py = rad * math.sin(th), rad * (1 - math.cos(th))
            c, s = math.cos(th), math.sin(th)
            for u in [k * 0.02 - 0.465 for k in range(47)]:
                for v in [k * 0.02 - 0.24 for k in range(25)]:
                    x, y = px + c * u - s * v, py + s * u + c * v
                    # 守卫的分辨率:机身(含 5 cm 余量)外不到 2 cm 的窄条可以漏(落在余量里)
                    if abs(x) > 0.535 or abs(y) > 0.31:
                        truth.add((math.floor(x / 0.1), math.floor(y / 0.1)))
        assert truth <= cells, (vx, wz, sorted(truth - cells)[:5])


def test_守卫_往后退往后扫_是往前扫的中心对称():
    """W09i:狗尾为前时往后退。(−v, w) 扫过的 = (v, w) 扫过的绕机身中心转 180°(机身前后左右对称)。"""
    g = ObstacleGuard()
    for vx, wz, vm in ((0.3, 0.0, 0.0), (0.3, 0.5, 0.0), (0.15, 1.0, 0.0), (0.2, 0.0, 0.6)):
        fwd = {(round(-x, 3), round(-y, 3)) for x, y in g.swept(vx, wz, vm)}
        back = set(g.swept(-vx, wz, -vm))
        assert fwd == back, (vx, wz)
    behind = _view_with([(7, c) for c in range(15, 25)])          # 狗身系 x = −1.25
    assert g.check(-0.1, 0.0, -0.1, behind, (0, 0, 0)).ok, "慢:够不着"
    v = g.check(-0.1, 0.0, -0.6, behind, (0, 0, 0))
    assert not v.ok and all(x < 0 for x, _ in v.hits), "实测快:往后扫够着了"
    assert g.check(0.6, 0.0, 0.6, behind, (0, 0, 0)).ok, "往前走不管身后"


def test_记忆_按收到时刻减延迟配位姿():
    t = [0.0]
    v = ObstacleView(monotonic=lambda: t[0], latency_s=0.2)
    v.note_odom(0.0, 0.0, 0.0)
    t[0] = 0.2
    v.note_odom(0.2, 0.0, 0.0)
    v.on_grid(_grid(cells_occ=[(15, 10)]))           # 这一帧是 0.0 那一刻扫的:x = 0.55
    hit, _ = v.lookup([(0.35, 0.05)], (0.2, 0.0, 0.0))
    assert hit == [0], "挡在里程 0.55 处,狗现在在 0.2:离狗 0.35"


def test_记忆_新的说空_旧的说挡_还有别的点没查完也不被旧的盖掉():
    t = [0.0]
    v = ObstacleView(monotonic=lambda: t[0], latency_s=0.0)
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_grid(cells_occ=[(15, 10)]))           # 旧:x = 0.55 挡
    t[0] = 0.1
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_grid(cells_known=[(15, 10)], seq=2))  # 新:那一格看见了,空;别的都没看见
    hit, unk = v.lookup([(0.55, 0.05), (0.85, 0.35)], (0.0, 0.0, 0.0))
    assert hit == [], "新的说了算(旧的那帧还要查第二个点,不许把第一个点盖回挡)"


def test_记忆_狗自己3秒内站过的地方算空_再久不算():
    t = [0.0]
    v = ObstacleView(monotonic=lambda: t[0], latency_s=0.0)
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_grid(cells_known=[]))                 # 什么都没看见
    t[0] = 1.0
    v.note_odom(1.0, 0.0, 0.0)
    _, unk = v.lookup([(-0.8, 0.0), (-0.8, 0.6)], (1.0, 0.0, 0.0))
    assert unk == [1], "身后 0.8 m 是 1 s 前机身占着的地方:算空;旁边那点没站过:未知"
    t[0] = 5.0
    v.note_odom(1.0, 0.0, 0.0)
    _, unk = v.lookup([(-0.8, 0.0)], (1.0, 0.0, 0.0))
    assert unk == [0], "超过 3 s:不算了"


async def test_临时障碍过期了不进代价图(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    cell = (40, 80)                                  # (y = 4.0, x = 8.0) 那一格
    t.nav._temp = {cell: t.c.s() - 1.0}
    t.nav._invalidate()
    assert not t.nav._costmap(0.0).hard[cell], "过期了:不算挡"
    t.nav._temp = {cell: t.c.s() + 5.0}
    t.nav._invalidate()
    assert t.nav._costmap(0.0).hard[cell]


async def test_绕不过去_规划失败_接着等_不当场放弃(tmp_path):
    """内审阻断 1 附带:绕障规划不出来(通道整个被堵)时原地等、5 s 后再试,满 20 s 才放弃。"""
    t = 台子(tmp_path, walls=[(0.0, 0.0, 12.0, 3.3), (0.0, 4.7, 12.0, 8.0)])   # 1.4 m 宽的通道
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.world.dyn["box"] = (5.0, 3.3, 5.6, 4.7)
    start = t.c.s()
    failed_at = None
    for _ in range(400):
        await t.一拍()
        if failed_at is None and NavStatus.FAILED in t.status:
            failed_at = t.c.s()
            break
    assert failed_at is not None and failed_at - start >= 19.0, failed_at and failed_at - start
    assert not t.crashed


# ------------------------------------------------------------ 狗尾为前(W09i)

async def test_狗尾为前_后雷达标过_狗尾对着路倒着走_路上箱子照样停_绕过去(tmp_path):
    t = 台子(tmp_path, head="tail")
    await t.start(1.5, 4.0, yaw=math.pi)                     # 狗尾朝 +x
    assert t.nav.travel() == -1
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0, math.pi))
    vxs = []
    for _ in range(20):
        await t.一拍()
        vxs.append(t.r.speed[0])
    assert min(vxs) < -0.1 and max(vxs) <= 1e-9, "倒着走(机身系往后)"
    assert abs(math.remainder(t.r.yaw - math.pi, 2 * math.pi)) < 0.2, "不掉头:狗尾对着路"
    t.world.dyn["box"] = (5.0, 3.5, 5.6, 4.5)
    await t.跑(80.0)
    assert not t.crashed
    assert NavStatus.SUCCEED in t.status, (t.status, t.events)
    assert [k for k, _ in t.events if k == "nav_unblocked"], t.events
    assert abs(math.remainder(t.r.yaw - math.pi, 2 * math.pi)) < 0.2, "到点对的是机身朝向"


async def test_狗尾为前_后雷达没标定或没配避障_不许自己走(tmp_path):
    t = 台子(tmp_path, head="tail", rear=True, rear_cal=False)
    await t.start(1.5, 4.0, yaw=math.pi)
    assert t.nav.travel() == 0 and "后雷达" in t.nav.head_block()
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0, math.pi))
    await t.跑(3.0)
    assert NavStatus.FAILED in t.status and abs(t.r.x - 1.5) < 0.05
    bare = 台子(tmp_path, head="tail")
    bare.nav.obstacles = None
    assert bare.nav.travel() == 0, "没配避障:身后没人看"


async def test_狗尾为前_第二层单独也停得住_狗尾那头的许可(tmp_path):
    t = 台子(tmp_path, head="tail", guard=False, clearance=True)
    await t.start(1.5, 4.0, yaw=math.pi)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0, math.pi))
    await t.跑(2.0)
    assert t.r.x > 1.6, "有狗尾的许可:走得动"
    t.world.dyn["box"] = (5.0, 3.0, 5.6, 5.0)
    await t.跑(15.0)
    assert not t.crashed, "第一层关了,狗尾那头的许可门照样挡"
    assert t.r.x < 5.0 - 0.465


async def test_半路调了头_导航桥自己停(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    assert t.r.x > 2.0
    t.r.inject_head("tail")                                   # 后雷达标过,狗尾为前本来也能走
    await t.一拍()
    assert t.status[-1] is NavStatus.FAILED, "行进方向变了:这一趟不接着走"


# ------------------------------------------------------------ 近障降速(W12)

def test_近障_只看行进方向前面那条带子里的挡_看不见不算():
    from d1max_agent.obstacles import FRESH_S  # noqa: F401
    v = _view_with([(30, 20)])                              # 40 格、0.1 m:x = 1.05、y = 0.05
    assert v.near_ahead(1, 0.515, 0.79) == pytest.approx(1.05 - 0.515, abs=1e-6)
    assert v.near_ahead(-1, 0.515, 0.79) == math.inf, "身后没有"
    side = _view_with([(30, 35)])                           # y = 1.55:带子外
    assert side.near_ahead(1, 0.515, 0.79) == math.inf
    behind = _view_with([(9, 20)])                          # x = −1.05
    assert behind.near_ahead(-1, 0.515, 0.79) == pytest.approx(1.05 - 0.515, abs=1e-6)
    empty = ObstacleView(monotonic=lambda: 0.0)
    assert empty.near_ahead(1, 0.5, 0.8) == math.inf


async def test_近障降速_前面有人慢下来_离远了恢复(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(3.0)
    free = t.r.speed[0]
    assert free > 0.5, "前提:空的时候走全速"
    t.world.dyn["person"] = (t.r.x + 1.6, 4.6, t.r.x + 1.9, 4.9)  # 前面偏一点(不挡路、在带子里)
    vs = []
    for _ in range(15):
        await t.一拍()
        vs.append(t.r.speed[0])
    assert min(vs) < free * 0.7 and min(vs) >= 0.2, vs
    await t.跑(80.0)
    assert NavStatus.SUCCEED in t.status and not t.crashed


# ---------------------------------------------------------------- W35(2026-10-13 C40221 第二轮)


async def test_W35_现场视野_贴着墙_起步要转_照样先直着挪再走到(tmp_path):
    """路线一弯就起不了步、「先挪」也不触发:狗站在桌子边上(半米),「往前挪」那条线头几格在**膨胀过**
    的代价图里是致命格,不让挪 —— 其实机身直着往前走得出去(机身半宽 0.24 m),挪出去 1 m 就离开桌子、
    转得开了;守卫也现看着。"""
    t = 台子(tmp_path, rear=True, cone=True, walls=[(2.4, 3.3, 3.2, 3.5)])
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 5.2))
    for _ in range(900):
        await t.一拍()
        if t.status and t.status[-1] is NavStatus.STANDBY:
            break
    assert NavStatus.SUCCEED in t.status and not t.crashed, (t.status, t.nav.fail_reason)


async def test_W35_现场视野_人挡了十几秒走开_马上接着走_不等到放弃(tmp_path):
    """3f.6 后半的回归(现场视野下:人挡一阵、走开,狗接着走)。这条在改之前就过 —— 现场那次「等满
    20 s」台式机上没复现出来,留着防退化。"""
    t = 台子(tmp_path, rear=True, cone=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(2.0)
    t.world.dyn["person"] = (4.0, 3.6, 4.4, 4.4)
    for _ in range(80):
        await t.一拍()
        if t.nav._blocked_since is not None:
            break
    assert t.nav._blocked_since is not None
    await t.跑(12.0)                                            # 站了 12 s
    assert NavStatus.FAILED not in t.status
    x0 = t.r.x
    del t.world.dyn["person"]
    await t.跑(6.0)
    assert NavStatus.FAILED not in t.status, t.nav.fail_reason
    assert t.r.x > x0 + 0.5 or NavStatus.SUCCEED in t.status, "人走了 6 s 内接着走"
    await t.跑(60.0)
    assert NavStatus.SUCCEED in t.status and not t.crashed


async def test_W35_往前挪_机身要压上地图里的墙就不挪_狗身下本来压着的不算(tmp_path):
    """挪之前看的是**没膨胀**的地图上机身扫过的那一条:前面 1 m 内有墙不挪;狗现在站的地方本来就
    压着的格子(定位差几厘米、贴着桌子站)不算,不然哪儿都挪不了。"""
    from types import SimpleNamespace
    t = 台子(tmp_path, rear=True, cone=True, walls=[(4.2, 3.0, 4.4, 5.0)])
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(3.0, 6.0))
    await t.跑(0.3)
    assert not t.nav._creep_clear(SimpleNamespace(x=3.0, y=4.0, yaw=0.0)), "前面 1.2 m 是墙"
    assert t.nav._creep_clear(SimpleNamespace(x=3.0, y=4.0, yaw=math.pi)), "往另一头是空的"
    t2 = 台子(tmp_path, rear=True, cone=True, walls=[(2.6, 3.6, 2.8, 3.8)])   # 狗身后角压着的一小块
    await t2.start(3.0, 4.0, yaw=0.0)
    await t2.nav.goto(Pose.from_xy_yaw(3.0, 6.0))
    await t2.跑(0.3)
    assert t2.nav._creep_clear(SimpleNamespace(x=3.0, y=4.0, yaw=0.0))
    # 不在中线上,机身边会蹭到
    t3 = 台子(tmp_path, rear=True, cone=True, walls=[(3.8, 4.2, 4.0, 4.4)])
    await t3.start(3.0, 4.0, yaw=0.0)
    await t3.nav.goto(Pose.from_xy_yaw(3.0, 6.0))
    await t3.跑(0.3)
    assert not t3.nav._creep_clear(SimpleNamespace(x=3.0, y=4.0, yaw=0.0)), "按机身宽查,不是一条线"


async def test_W35_弧线被拦_原地转也被拦_就不转(tmp_path):
    """弧线被拦时改成原地转,前提是原地转那条自己过得了守卫:刚起步两侧没看过、前面又有东西(挪不了),
    哪条都过不了 —— 原地不动(没后雷达;有后雷达的往后退见 W34 那条)。"""
    t = 台子(tmp_path, rear=False, cone=True, walls=[(3.85, 3.7, 3.95, 4.3)])
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 4.6))
    await t.跑(8.0)
    assert not t.crashed and abs(t.r.yaw) < 0.02 and abs(t.r.x - 3.0) < 0.05


# ---------------------------------------------------------------- W35 外审 I2:挪也要守区域的规矩


async def test_外审I2_前面是限速低到守不住的限速区_当禁行_不往里挪(tmp_path):
    """规划那边把「限速低于这台狗的前进死区」的限速区当禁行(再慢就不走了)。挪之前改查没膨胀的
    图以后只看了原始的禁行区,这一类漏了:雷达看不见人划的区,守卫补不上。"""
    from types import SimpleNamespace
    t = 台子(tmp_path, rear=True, cone=True)
    await t.nav.set_zones(_区域({
        "id": "lawn", "kind": "slow", "label": "草坪", "max_speed_mps": 0.02,
        "polygon": [[3.8, 3.0], [4.6, 3.0], [4.6, 5.0], [3.8, 5.0]]}))
    await t.start(3.0, 4.0, yaw=0.0)
    here = SimpleNamespace(x=3.0, y=4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(3.0, 6.0))
    await t.跑(0.3)
    assert not t.nav._creep_clear(here), "前面 0.8 m 起是当禁行的限速区"
    assert t.nav._creep_clear(SimpleNamespace(x=3.0, y=4.0, yaw=math.pi)), "往另一头没有"
    await t.跑(30.0)
    assert t.r.x < 3.0 + 0.05 and not t.nav._crept, "整趟也没往里挪"


async def test_S7_往前挪_按机身轮廓加σ查禁行区(tmp_path):
    """S7(2026-10-14):挪之前查禁行区跟每拍回退检查一个口径 —— 机身矩形挪完再刹停,离禁行区不到
    σ + 余量、越挪越近就不挪(原来是中心线离 0.25 m、不加 σ)。禁行区在机身侧前方,中心线碰不到。"""
    from types import SimpleNamespace
    t = 台子(tmp_path, rear=True, cone=True)
    # 狗在 (3, 4) 朝 +x;禁行区在 y ≥ 4.3、x ≥ 4 —— 机身侧面离它 6 cm,中心线离 0.3 m
    await t.nav.set_zones(_区域({"id": "bed", "kind": "nogo", "label": "花坛",
                               "polygon": [[4.0, 4.3], [6.0, 4.3], [6.0, 5.0], [4.0, 5.0]]}))
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(3.0, 6.0))
    await t.跑(0.3)
    here = SimpleNamespace(x=3.0, y=4.0, yaw=0.0, sigma_xy_m=0.0)
    assert t.nav._creep_clear(here), "σ 0:侧面还差 6 cm,挪得过去"
    here.sigma_xy_m = 0.1
    assert not t.nav._creep_clear(here), "σ 0.1:侧面可能已经蹭上了,不挪"
    here.y = 3.0
    assert t.nav._creep_clear(here), "离远了照挪"


async def test_外审I2_前面是过得去的限速区_挪的时候不超它的限速(tmp_path):
    t = 台子(tmp_path, rear=True, cone=True)
    await t.nav.set_zones(_区域({
        "id": "path", "kind": "slow", "label": "小路", "max_speed_mps": 0.1,
        "polygon": [[3.2, 3.0], [6.0, 3.0], [6.0, 5.0], [3.2, 5.0]]}))
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 4.6))
    fast = 0.0
    for _ in range(400):
        await t.一拍()
        if t.nav._creep_from is not None:
            fast = max(fast, abs(t.r.speed[0]))
        if t.status and t.status[-1] is NavStatus.STANDBY:
            break
    assert t.nav._crept or fast > 0, "这一趟确实挪了"
    assert 0 < fast <= 0.1 + 1e-6, f"挪的时候 {fast} m/s,限速 0.1"


# ---------------------------------------------------------------- W41(2026-10-26 C40221 第三轮)


async def test_W41_现场视野_正前方箱子不拿走_两侧没看过_退一步再绕过去_不等到放弃(tmp_path):
    """3f.16 / 3x.5(2026-10-26):正前方被挡,绕行要先转身,转身被「机身两侧没看过」拦住
    (现场「有挡 0 处、看不见 149 处」),「先往前挪」又被箱子挡住 → 原地等满 20 s 放弃。
    后雷达标过:往后退一个机身长(后雷达看着),两侧就看过了,再转、绕过去。"""
    t = 台子(tmp_path, rear=True, cone=True)
    t.world.dyn["box"] = (5.1, 3.5, 5.7, 4.5)
    await t.start(4.4, 4.0)                                   # 起步就被挡:两侧一格都没看过
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    xs = []
    for _ in range(900):
        await t.一拍()
        xs.append(t.r.x)
        if t.status and t.status[-1] is NavStatus.STANDBY:
            break
    assert not t.crashed
    assert NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason, t.events)
    assert min(xs) < 4.4 - 1.0, "先往后退了"


async def test_W41_现场视野_箱子拿走了_几秒内接着走_不等满20秒(tmp_path):
    """3x.6:拿走箱子后晚了约 19 s 才走(路已经换成绕行的,转身照样被两侧看不见拦住)。"""
    t = 台子(tmp_path, rear=True, cone=True)
    t.world.dyn["box"] = (5.1, 3.5, 5.7, 4.5)
    await t.start(4.4, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(4.0)
    del t.world.dyn["box"]
    await t.跑(6.0)
    assert NavStatus.FAILED not in t.status, t.nav.fail_reason
    assert t.nav._blocked_since is None, "6 s 内接着走了"
    await t.跑(60.0)
    assert NavStatus.SUCCEED in t.status and not t.crashed


async def test_W41_离点只剩03米_转向被看不见挡住_不往前冲过头(tmp_path):
    """3f.30:离点只剩 0.2、0.4 m 也往前挪满 1.3 m,冲过头 1 m 多,还记「到了」(狗停在 0.93 m 外)。
    现在:往前挪会冲过终点就不往前;后雷达标过往后退、再走回来;没后雷达就原地等(到时候放弃),不冲。
    记「到了」的时候狗一定在点上。"""
    goal = Pose.from_xy_yaw(5.3, 4.25)
    for rear in (True, False):
        t = 台子(tmp_path, rear=rear, cone=True)
        await t.start(5.0, 4.0, yaw=0.0)
        await t.nav.goto(goal)
        xs = []
        for _ in range(900):
            await t.一拍()
            xs.append(t.r.x)
            if t.status and t.status[-1] is NavStatus.STANDBY:
                break
        assert not t.crashed
        assert max(xs) < 5.3 + 0.3, (rear, max(xs))
        if rear:
            assert NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason)
            assert min(xs) < 5.0 - 1.0, "往后退了"
        if NavStatus.SUCCEED in t.status:
            assert math.hypot(t.r.x - 5.3, t.r.y - 4.25) < 0.3, "记到了就在点上"


async def test_W41_对朝向时被挪开_不算到了_重新走过去(tmp_path):
    """进了「对朝向」以后离点超过容差 + 0.15 m(往后退了、定位一跳),不再按「在点上」对完朝向就
    记到了。"""
    t = 台子(tmp_path, rear=True, cone=True)
    await t.start(3.0, 4.0, yaw=0.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 4.0, 0.5))
    for _ in range(300):
        await t.一拍()
        if t.nav._aligning:
            break
    assert t.nav._aligning
    t.r.teleport(t.r.x - 0.6, t.r.y, t.r.yaw)                 # 对朝向的时候被挪开 0.6 m
    for _ in range(600):
        await t.一拍()
        if t.status and t.status[-1] is NavStatus.STANDBY:
            break
    assert NavStatus.SUCCEED in t.status, t.nav.fail_reason
    assert math.hypot(t.r.x - 5.0, t.r.y - 4.0) < 0.3


async def test_W41_放弃时写清楚前面真有挡_不只写转身那次的有挡0处(tmp_path):
    t = 台子(tmp_path, rear=False, cone=True)
    t.world.dyn["box"] = (5.1, 3.5, 5.7, 4.5)
    await t.start(4.4, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    await t.跑(40.0)
    assert NavStatus.FAILED in t.status and not t.crashed
    why = t.nav.fail_reason
    assert why.startswith("被挡 ") and "扫过区有挡 0 处" not in why, why
    assert "两侧看不见" in why, why
    blocked = [d["reason"] for k, d in t.events if k == "nav_blocked"]
    assert blocked and "扫过区有挡 0 处" not in blocked[0], blocked
