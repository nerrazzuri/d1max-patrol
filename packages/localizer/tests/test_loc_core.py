"""定位核心(W09b 决定 3):MOLA 的一帧估计 → 给代理的 ``pose``/``status``。纯 Python、手拨的钟。

坐标用单位 ``Frames``(MOLA 系就是地图平面系、雷达 Z 朝上 X 朝前),估计直接按平面位姿造。"""

from __future__ import annotations

import math

import pytest
from d1max_localizer.core import (
    AUTO_RELOC_SIGMA_M,
    JUMP_LOST_N,
    NO_SCAN_S,
    RELOC_SETTLE_S,
    SIGMA_JUMPED_M,
    STALL_S,
    START_TIMEOUT_S,
    Estimate,
    LocalizerCore,
)
from d1max_localizer.frames import Frames, mat_to_quat

from d1max_contract.locbridge import Pose, State

FLAT = Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
              sensor_height=0.0)
M = ("estate-1", "7")


class 钟:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _est(x, y, yaw=0.0, *, stamp, q=0.95):
    c, s = math.cos(yaw), math.sin(yaw)
    R = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
    return Estimate(stamp=stamp, p=(x, y, 0.0), q=mat_to_quat(R), quality=q)


class 台:
    """核心 + 手拨的钟;``walk`` 按 10 Hz 喂点云与估计,钟跟着走。"""

    def __init__(self, *, init=True):
        self.c = 钟()
        self.core = LocalizerCore(monotonic=self.c)
        self.stamp = 5000.0
        self.core.prior_loaded(M, FLAT)
        self.core.backend_started()
        if init:
            assert self.core.relocalized(req=1, x=0.0, y=0.0, yaw=0.0, sigma=0.5, human=True)
        self.out: list = []

    def step(self, x, y, yaw=0.0, *, q=0.95, dt=0.1, scan=True, est=True):
        self.c.t += dt
        self.stamp += dt
        if scan:
            self.core.on_scan()
        if est:
            self.core.on_estimate(_est(x, y, yaw, stamp=self.stamp, q=q))
        self.core.tick()
        got = self.core.drain()
        self.out.extend(got)
        return got

    def poses(self):
        return [m for m in self.out if isinstance(m, Pose)]

    def states(self):
        return [(m.state, m.reason) for m in self.out if isinstance(m, State)]

    def walk(self, n, *, v=0.5, x0=0.0, **kw):
        for i in range(1, n + 1):
            self.step(x0 + v * 0.1 * i, 0.0, **kw)
        return x0 + v * 0.1 * n


def test_先验没载好_没给初值_都在初始化_不出位姿():
    c = 钟()
    core = LocalizerCore(monotonic=c)
    core.tick()
    assert core.drain() == [State(seq=1, state="initializing", reason="还没有先验")]
    core.prior_loading(M)
    core.tick()
    assert [m.reason for m in core.drain()] == ["在载入先验"]
    core.prior_loaded(M, FLAT)
    core.backend_started()
    core.tick()
    assert [m.reason for m in core.drain()] == ["等人给初始位置"]
    core.on_scan()
    core.on_estimate(_est(1.0, 1.0, stamp=1.0))           # MOLA 按默认初值瞎给的,不发
    core.tick()
    assert core.drain() == []


def test_给了初值_第一帧带跳变与请求号_之后正常跟():
    t = 台()
    t.step(0.05, 0.0)
    [p] = t.poses()
    assert p.jump and p.reloc_id == 1 and (p.map_id, p.map_version) == M
    assert (round(p.x, 3), round(p.y, 3)) == (0.05, 0.0) and p.source == "scan_match"
    assert p.stamp_ns == round(t.stamp * 1e9) and p.meas_age_ms == 0
    assert t.states()[-1] == ("tracking", "")
    t.walk(5, x0=0.05)
    later = t.poses()[1:]
    assert not any(m.jump or m.reloc_id for m in later)
    seqs = [m.seq for m in t.out]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "序号一直涨"


def test_σ随质量_刚重定位的时候不小于给的初值_慢慢降下来():
    t = 台()
    t.step(0.0, 0.0, q=0.95)
    s0 = t.poses()[-1].sigma_xy
    assert s0 == pytest.approx(0.5, abs=0.02), "刚重定位:σ 从给的初值开始"
    t.c.t += RELOC_SETTLE_S
    t.step(0.0, 0.0, q=0.95)
    assert t.poses()[-1].sigma_xy == pytest.approx(0.1, abs=0.01), "跟住 30 s:按质量给"
    t.step(0.0, 0.0, q=0.65)
    mid = t.poses()[-1].sigma_xy
    t.step(0.0, 0.0, q=0.5)
    low = t.poses()[-1].sigma_xy
    assert 0.1 < mid < low and low >= 1.0, (mid, low)


def test_一帧挪得比狗能跑的还远_算跳_σ涨_5秒里跳3次丢定位_不跳了才恢复():
    t = 台()
    t.c.t += RELOC_SETTLE_S
    x = t.walk(10)
    t.step(x + 2.0, 0.0)                                   # 0.1 s 挪 2 m
    p = t.poses()[-1]
    assert p.jump and p.sigma_xy >= SIGMA_JUMPED_M
    for k in range(JUMP_LOST_N - 1):                       # 来回跳
        t.step(x + (0.0 if k % 2 == 0 else 2.0), 0.0)
    assert t.states()[-1] == ("lost", "匹配在来回跳")
    t.walk(30, x0=x)                                       # 3 s 没跳:还没到 5 s
    assert t.states()[-1][0] == "lost"
    t.walk(25, x0=x + 1.5)
    assert t.states()[-1] == ("tracking", "")


def test_转得比能转的还快也算跳():
    t = 台()
    t.c.t += RELOC_SETTLE_S
    t.walk(5)
    t.step(0.25, 0.0, yaw=1.0)                             # 0.1 s 转 1 rad
    assert t.poses()[-1].jump


def test_质量太低的那帧是它推出来的_报离上次真匹配多久():
    t = 台()
    t.c.t += RELOC_SETTLE_S
    t.walk(3)
    for _ in range(4):
        t.step(0.15, 0.0, q=0.3)
    assert t.poses()[-1].meas_age_ms == 400
    t.step(0.15, 0.0, q=0.9)
    assert t.poses()[-1].meas_age_ms == 0


def test_时间戳不涨的估计丢掉():
    t = 台()
    t.step(0.05, 0.0)
    n = len(t.poses())
    t.core.on_estimate(_est(0.05, 0.0, stamp=t.stamp))    # 同一帧又来一遍
    t.core.tick()
    assert [m for m in t.core.drain() if isinstance(m, Pose)] == []
    assert len(t.poses()) == n


def test_雷达没数据_丢定位_来了恢复():
    t = 台()
    t.walk(5)
    for _ in range(int(NO_SCAN_S * 10) + 2):
        t.step(0.25, 0.0, scan=False, est=False)
    state, reason = t.states()[-1]
    assert state == "lost" and "雷达" in reason
    assert t.core.want_restart() is False, "雷达没数据不重启 MOLA(开盖子、雷达起来才有数据)"
    t.walk(3, x0=0.25)
    assert t.states()[-1] == ("tracking", "")


def test_点云在来_MOLA_不出位姿_要重启_重启后用最后可信的位置自己重定位():
    t = 台()
    t.c.t += RELOC_SETTLE_S
    x = t.walk(10)
    for _ in range(int(STALL_S * 10) + 2):
        t.step(x, 0.0, est=False)
    assert t.states()[-1] == ("lost", "定位程序卡住了,在重启")
    assert t.core.want_restart() is True
    t.core.backend_started()                               # 适配层重启了 MOLA
    assert t.core.want_restart() is False
    want = t.core.want_reloc()
    assert want is not None
    wx, wy, wyaw, sigma = want
    assert (round(wx, 3), round(wy, 3), sigma) == (round(x, 3), 0.0, AUTO_RELOC_SIGMA_M)
    assert t.core.relocalized(req=None, x=wx, y=wy, yaw=wyaw, sigma=sigma, human=False)
    assert t.core.want_reloc() is None
    t.step(x, 0.0)
    p = t.poses()[-1]
    assert p.jump and p.reloc_id is None and p.sigma_xy >= AUTO_RELOC_SIGMA_M - 0.01


def test_刚起来_载图要一阵_给足时间才算卡住():
    t = 台(init=False)
    t.core.relocalized(req=1, x=0.0, y=0.0, yaw=0.0, sigma=0.5, human=True)
    for _ in range(int(START_TIMEOUT_S * 10) - 5):         # 点云在来,MOLA 还在载图
        t.step(0.0, 0.0, est=False)
    assert t.core.want_restart() is False
    for _ in range(10):
        t.step(0.0, 0.0, est=False)
    assert t.core.want_restart() is True


def test_没有先验不收重定位_换先验之后要重新给初值():
    c = 钟()
    core = LocalizerCore(monotonic=c)
    assert core.relocalized(req=1, x=0.0, y=0.0, yaw=0.0, sigma=0.5, human=True) is False
    t = 台()
    t.walk(3)
    t.core.prior_loading(("estate-1", "8"))
    t.core.tick()
    assert [(m.state, m.reason) for m in t.core.drain() if isinstance(m, State)] == \
        [("initializing", "在载入先验")]
    t.core.prior_loaded(("estate-1", "8"), FLAT)
    t.core.backend_started()
    t.core.tick()
    assert [m.reason for m in t.core.drain() if isinstance(m, State)][-1] == "等人给初始位置"
    assert t.core.want_reloc() is None, "换了图:旧图上的位置不能拿来当初值"


def test_状态没变不重复发_连上时要能再发一遍():
    t = 台()
    t.walk(3)
    n = len(t.states())
    t.walk(3, x0=0.15)
    assert len(t.states()) == n
    [s] = [m for m in t.core.hello() if isinstance(m, State)]
    assert s.state == "tracking"
