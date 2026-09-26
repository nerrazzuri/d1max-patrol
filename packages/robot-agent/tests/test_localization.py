"""里程锚定(W00c6e,W08 决定 4 的过渡实现):纯的那一层。"""

from __future__ import annotations

import math

import pytest

from d1max_agent.localization import (
    JUMP_M,
    SIGMA0_XY_M,
    SIGMA_LOST_M,
    OdomAnchor,
    compose,
    inverse,
)

M = ("estate-1", "7")


def test_合成与求逆():
    a = (1.0, 2.0, math.pi / 2)
    b = (3.0, 0.0, 0.0)
    assert compose(a, b) == pytest.approx((1.0, 5.0, math.pi / 2))
    back = compose(a, inverse(a))
    assert back == pytest.approx((0.0, 0.0, 0.0), abs=1e-12)


def test_没锚过_不可信_没有位姿():
    a = OdomAnchor()
    a.on_map(M)
    assert not a.anchored and a.estimate((0, 0, 0)) is None
    assert not a.ok(True) and "设位置" in a.why_not(True)
    assert a.quality(True) == 0.0


def test_锚了之后_地图位姿按里程推_带转角():
    """人说狗在地图 (10, 5)、朝北;那一刻里程是 (2, 0)、朝东。之后里程往前走 1 m(朝东),地图上是往北
    1 m。"""
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (10.0, 5.0, math.pi / 2), (2.0, 0.0, 0.0))
    e = a.estimate((2.0, 0.0, 0.0))
    assert (e.x, e.y, e.yaw) == pytest.approx((10.0, 5.0, math.pi / 2))
    a.update((3.0, 0.0, 0.0))
    e = a.estimate((3.0, 0.0, 0.0))
    assert (e.x, e.y) == pytest.approx((10.0, 6.0)) and e.yaw == pytest.approx(math.pi / 2)
    assert (e.map_id, e.map_version, e.source) == ("estate-1", "7", "odom_anchor")


def test_走得越远越不可信_过线就丢():
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    assert a.sigma_xy == pytest.approx(SIGMA0_XY_M) and a.ok(True)
    x = 0.0
    while a.ok(True) and x < 100.0:                       # 有上限:不漂的话别死循环
        x += 0.5
        a.update((x, 0.0, 0.0))
    assert 7.5 <= x <= 8.5, "按 9 m 漂 2 m、线在 2 m:约走 8 m"
    assert a.sigma_xy > SIGMA_LOST_M and "重新设位置" in a.why_not(True)
    assert a.estimate((x, 0.0, 0.0)) is not None, "位姿照样给,可信不可信调用方看"
    q = OdomAnchor()
    q.on_map(M)
    q.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    for i in range(1, 9):
        q.update((i * 0.5, 0.0, 0.0))                     # 一步 0.5 m,走 4 m(不算跳)
    assert 0.0 < q.quality(True) < 1.0


def test_里程一拍跳了_锚定作废():
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (5.0, 5.0, 0.0), (10.0, 0.0, 0.0))
    a.update((10.0 - JUMP_M - 0.5, 0.0, 0.0))
    assert not a.anchored and "跳" in a.why_not(True)


def test_里程不新鲜_不可信():
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    assert not a.ok(False) and "里程" in a.why_not(False)


def test_换了地图_锚定作废_同一张图重报不作废():
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    a.on_map(M)
    assert a.anchored
    a.on_map(("estate-1", "8"))
    assert not a.anchored and "换了地图" in a.why_not(True)


def test_重新设位置_σ从头算():
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    a.update((0.9, 0.0, 0.0))
    a.update((1.8, 0.0, 0.0))
    assert a.sigma_xy > SIGMA0_XY_M
    a.anchor(M, (1.8, 0.0, 0.0), (1.8, 0.0, 0.0))
    assert a.sigma_xy == pytest.approx(SIGMA0_XY_M)


def test_仿真按原样_不漂_瞬移不作废():
    a = OdomAnchor(identity=True)
    a.on_map(M)
    assert a.anchored and a.ok(True) and a.quality(True) == 1.0
    e = a.estimate((3.0, 4.0, 0.5))
    assert (e.x, e.y, e.yaw) == pytest.approx((3.0, 4.0, 0.5)) and e.source == "odom_identity"
    assert e.sigma_xy_m == 0.0
    for x in range(0, 100, 5):
        a.update((float(x), 0.0, 0.0))
    assert a.ok(True) and a.sigma_xy == 0.0
    a.on_map(("other", "1"))
    assert a.anchored and a.estimate((0, 0, 0)).map_id == "other"


def test_遥测里的loc块():
    a = OdomAnchor()
    a.on_map(M)
    w = a.to_wire(True)
    assert w["source"] == "odom_anchor" and w["anchored"] is False and w["reason"]
    a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    w = a.to_wire(True)
    assert w["anchored"] and w["sigma_m"] == pytest.approx(SIGMA0_XY_M) and w["reason"] == ""



def test_重设位置_回的是修正量_没锚过回None():
    a = OdomAnchor()
    a.on_map(M)
    assert a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)) is None
    d = a.anchor(M, (1.0, 2.0, 0.0), (0.0, 0.0, 0.0))
    assert d == pytest.approx((1.0, 2.0, 0.0))


def test_里程不新鲜就作废_仿真不管():
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    a.update((0.0, 0.0, 0.0), odom_ok=False)
    assert not a.anchored and "里程" in a.reason
    s = OdomAnchor(identity=True)
    s.on_map(M)
    s.update((0.0, 0.0, 0.0), odom_ok=False)
    assert s.anchored


def test_仿真设过位置_来源不再说按原样():
    a = OdomAnchor(identity=True)
    a.on_map(M)
    a.anchor(M, (5.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    assert a.source == "odom_anchor"


def test_修正量带转角_顺序对():
    """Δ = T_new ∘ T_old⁻¹:同一个里程下,旧地图位姿经 Δ 就是新的。"""
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (1.0, 0.0, 0.0), (0.0, 0.0, 0.0))              # T_old = (1, 0, 0)
    old = a.estimate((2.0, 0.0, 0.0))
    d = a.anchor(M, (0.0, 0.0, math.pi / 2), (0.0, 0.0, 0.0))  # T_new = (0, 0, π/2)
    new = a.estimate((2.0, 0.0, 0.0))
    moved = compose(d, (old.x, old.y, old.yaw))
    assert moved == pytest.approx((new.x, new.y, new.yaw))


def test_导航桥_真狗默认不按原样_等人的时限():
    from d1max_adapter_sim.robot import SimRobot
    from d1max_agent.bridges.hal_nav import HUMAN_RELOCALIZE_WAIT_S, HalNavBackend
    sim = HalNavBackend(SimRobot(now_ms=lambda: 0), now_ms=lambda: 0, map_id="m")
    assert sim.anchor.identity and sim.RELOCALIZE_WAIT_S is None
    real = SimRobot(now_ms=lambda: 0)
    real.adapter_id = "d1max/0.1.0"
    bridge = HalNavBackend(real, now_ms=lambda: 0, map_id="m")
    assert not bridge.anchor.identity and bridge.RELOCALIZE_WAIT_S == HUMAN_RELOCALIZE_WAIT_S



def test_合成的朝向回绕到正负π之间():
    a = compose((0.0, 0.0, 3.0), (0.0, 0.0, 3.0))
    assert -math.pi <= a[2] <= math.pi and a[2] == pytest.approx(6.0 - 2 * math.pi)


def test_质量是一减偏差比线():
    a = OdomAnchor()
    a.on_map(M)
    a.anchor(M, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    for i in range(1, 10):
        a.update((i * 0.5, 0.0, 0.0))
    assert a.quality(True) == pytest.approx(1.0 - a.sigma_xy / SIGMA_LOST_M)
