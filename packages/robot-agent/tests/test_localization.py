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
