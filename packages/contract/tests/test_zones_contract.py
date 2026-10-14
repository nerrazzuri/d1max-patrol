"""W10 区域格式:解析、校验、只收紧判定、几何小工具。"""

from __future__ import annotations

import math

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.zones import (
    ZoneSet,
    distance_to_polygon,
    is_simple,
    point_in_polygon,
    polygon_area,
    tightens,
)

SQ = [[0, 0], [2, 0], [2, 2], [0, 2]]


def _set(zones, rev=1, ver="v1"):
    return ZoneSet.from_wire({"map_id": "m", "map_version": ver, "revision": rev, "zones": zones})


def _nogo(zid="a", poly=SQ):
    return {"id": zid, "kind": "nogo", "label": "池子", "polygon": poly}


def _slow(zid="s", speed=0.3, poly=SQ):
    return {"id": zid, "kind": "slow", "polygon": poly, "max_speed_mps": speed}


def test_来回一致():
    zs = _set([_nogo(), _slow()])
    assert ZoneSet.from_wire(zs.to_wire()) == zs
    assert [z.id for z in zs.nogo()] == ["a"] and [z.id for z in zs.slow()] == ["s"]
    assert zs.slow()[0].max_speed_mps == 0.3
    assert "max_speed_mps" not in zs.nogo()[0].to_wire()


@pytest.mark.parametrize("bad", [
    {"id": "", "kind": "nogo", "polygon": SQ},
    {"id": "a/b", "kind": "nogo", "polygon": SQ},
    {"id": "a", "kind": "wall", "polygon": SQ},
    {"id": "a", "kind": "nogo", "polygon": SQ[:2]},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0]] * 65},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [1, 0], [math.nan, 1]]},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [1, 0], [math.inf, 1]]},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [1, 0], [True, 1]]},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [1, 0], ["1", 1]]},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [1, 0], [1, 1, 1]]},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [1, 0], [2e5, 1]]},
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [1, 0], [2, 0]]},              # 面积 0
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [0.05, 0], [0.05, 0.05]]},     # 太小
    {"id": "a", "kind": "nogo", "polygon": [[0, 0], [2, 2], [2, 0], [0, 2]]},      # 8 字
    {"id": "a", "kind": "nogo", "polygon": SQ, "max_speed_mps": 0.3},
    {"id": "a", "kind": "nogo", "polygon": SQ, "label": 3},
    {"id": "a", "kind": "nogo", "polygon": SQ, "label": "x" * 65},
    {"id": "s", "kind": "slow", "polygon": SQ},
    {"id": "s", "kind": "slow", "polygon": SQ, "max_speed_mps": 0},
    {"id": "s", "kind": "slow", "polygon": SQ, "max_speed_mps": -1},
    {"id": "s", "kind": "slow", "polygon": SQ, "max_speed_mps": 6},
    {"id": "s", "kind": "slow", "polygon": SQ, "max_speed_mps": math.nan},
    {"id": "s", "kind": "slow", "polygon": SQ, "max_speed_mps": True},
    "zone",
])
def test_坏区域拒(bad):
    with pytest.raises(ContractError):
        _set([bad])


def test_区域集本身的校验():
    with pytest.raises(ContractError):
        _set([_nogo("a"), _nogo("a")])
    with pytest.raises(ContractError):
        _set([_nogo(f"z{i}") for i in range(201)])
    for rev in (-1, True, 1.0, "1"):
        with pytest.raises(ContractError):
            _set([], rev=rev)
    with pytest.raises(ContractError):
        ZoneSet.from_wire({"map_id": "../x", "map_version": "v", "revision": 1, "zones": []})
    with pytest.raises(ContractError):
        ZoneSet.from_wire({"map_id": "m", "map_version": "v", "revision": 1, "zones": {}})
    with pytest.raises(ContractError):
        ZoneSet.from_wire([])
    assert _set([_nogo(f"z{i}") for i in range(200)]).revision == 1
    assert _set([], rev=0).zones == ()


def test_简单多边形():
    assert is_simple(((0, 0), (2, 0), (2, 2), (0, 2)))
    assert is_simple(((0, 0), (4, 0), (4, 4), (2, 1), (0, 4)))        # 凹的
    assert not is_simple(((0, 0), (2, 2), (2, 0), (0, 2)))
    assert not is_simple(((0, 0), (2, 0), (2, 0), (0, 2)))            # 重复顶点
    assert not is_simple(((0, 0), (2, 0), (1, 0), (1, 2)))            # 共线折回
    assert not is_simple(((0, 0), (4, 0), (4, 4), (0, 4), (2, 0)))     # 顶点压在别的边上


def test_点在里面_边上算里面():
    sq = ((0, 0), (2, 0), (2, 2), (0, 2))
    assert point_in_polygon(1, 1, sq)
    assert point_in_polygon(2, 1, sq) and point_in_polygon(0, 0, sq)
    assert not point_in_polygon(3, 1, sq) and not point_in_polygon(-0.01, 1, sq)
    concave = ((0, 0), (4, 0), (4, 4), (2, 1), (0, 4))
    assert not point_in_polygon(2, 3, concave) and point_in_polygon(1, 1, concave)
    assert distance_to_polygon(1, 1, sq) == 0
    assert distance_to_polygon(3, 1, sq) == pytest.approx(1.0)
    assert distance_to_polygon(3, 3, sq) == pytest.approx(math.sqrt(2))
    assert polygon_area(sq) == 4


FAR = [[5, 5], [7, 5], [7, 7], [5, 7]]


def test_只收紧():
    # 决策 66 之前这里的限速区跟禁行区是同一个多边形;按几何判收紧之后「删一个整个在禁行区里的
    # 限速区」什么都没放宽(见下面最后一条),所以限速区挪到别处,「删限速 = 放宽」照旧要成立。
    old = _set([_nogo("a"), _slow("s", 0.5, poly=FAR)])
    assert tightens(old, _set([_nogo("a"), _slow("s", 0.5, poly=FAR)], rev=2))
    assert tightens(old, _set([_nogo("a"), _slow("s", 0.3, poly=FAR), _nogo("b")], rev=2))
    assert tightens(old, _set([_nogo("a"), _slow("s", 0.5, poly=FAR), _slow("t", 1.0)], rev=2))
    assert not tightens(old, _set([_slow("s", 0.5, poly=FAR)], rev=2))            # 删禁行
    assert not tightens(old, _set([_nogo("a")], rev=2))                           # 删限速
    assert not tightens(old, _set([_nogo("a"), _slow("s", 0.6, poly=FAR)], rev=2))  # 升限速
    moved = [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert not tightens(old, _set([_nogo("a", poly=moved), _slow("s", 0.5, poly=FAR)], rev=2))
    assert not tightens(old, _set([_nogo("a"), _slow("s", 0.5, poly=moved)], rev=2))
    assert not tightens(old, _set([{**_slow("a", 0.1)}, _slow("s", 0.5, poly=FAR)],
                                  rev=2))                                         # 换种类
    assert not tightens(old, _set([_nogo("a"), _slow("s", 0.5, poly=FAR)], rev=2, ver="v2"))
    # 整个落在禁行区里的限速区删掉:那块地方还是禁行,没放宽
    inner = _set([_nogo("a"), _slow("s", 0.5)])
    assert tightens(inner, _set([_nogo("a")], rev=2))


# ------------------------------------------ 决策 65、66(2026-10-14):硬禁区、到期、按几何判收紧

def test_硬禁区与到期_来回一致_不用就不上线():
    """新字段只在用到时才上线:老的区域集线上一字不差(老狗、老站点照旧认)。"""
    plain = _set([_nogo(), _slow()])
    w0 = plain.to_wire()["zones"][0]
    assert "hard" not in w0 and "until_ms" not in w0
    zs = _set([_nogo("a") | {"hard": True}, _slow("s") | {"until_ms": 1_800_000_000_000},
               _nogo("b") | {"hard": False}])
    assert ZoneSet.from_wire(zs.to_wire()) == zs
    a, s, b = zs.zones
    assert a.hard and a.to_wire()["hard"] is True and a.until_ms is None
    assert s.until_ms == 1_800_000_000_000 and s.to_wire()["until_ms"] == 1_800_000_000_000
    assert not b.hard and "hard" not in b.to_wire(), "hard: false 跟没写一样"
    assert [z.id for z in zs.hard()] == ["a"]


def test_老格式的区域集照样认_线上一字不差():
    """向后兼容:老站点发的(没有新字段)照样解析,再发出去一字不差。"""
    old_wire = {"map_id": "m", "map_version": "v1", "revision": 3,
                "zones": [{"id": "a", "kind": "nogo", "label": "", "polygon": SQ}]}
    zs = ZoneSet.from_wire(old_wire)
    assert not zs.zones[0].hard and zs.zones[0].until_ms is None
    assert zs.to_wire() == old_wire


@pytest.mark.parametrize("bad", [
    {"id": "a", "kind": "nogo", "polygon": SQ, "hard": 1},
    {"id": "a", "kind": "nogo", "polygon": SQ, "hard": "yes"},
    {"id": "s", "kind": "slow", "polygon": SQ, "max_speed_mps": 0.3, "hard": True},
    {"id": "a", "kind": "nogo", "polygon": SQ, "until_ms": 0},
    {"id": "a", "kind": "nogo", "polygon": SQ, "until_ms": -5},
    {"id": "a", "kind": "nogo", "polygon": SQ, "until_ms": 1.5e12},
    {"id": "a", "kind": "nogo", "polygon": SQ, "until_ms": True},
    {"id": "a", "kind": "nogo", "polygon": SQ, "until_ms": "1"},
    {"id": "a", "kind": "nogo", "polygon": SQ, "until_ms": 2 ** 60},
])
def test_硬禁区与到期_坏的拒(bad):
    with pytest.raises(ContractError):
        _set([bad])


BIG = [[-1, -1], [3, -1], [3, 3], [-1, 3]]


def test_收紧_按几何_放大禁行区算收紧_换id也算():
    old = _set([_nogo("a")])
    assert tightens(old, _set([_nogo("a", poly=BIG)], rev=2)), "原地放大"
    assert tightens(old, _set([_nogo("z9", poly=BIG)], rev=2)), "删了旧的、画了一个更大的盖住它"
    assert tightens(old, _set([_nogo("a"), _nogo("b", poly=BIG)], rev=2))
    assert not tightens(old, _set([_nogo("a", poly=[[0, 0], [1.9, 0], [1.9, 2], [0, 2]])],
                                  rev=2)), "缩小一点点也是放宽"
    assert not tightens(old, _set([_nogo("a", poly=[[0.5, 0], [2.5, 0], [2.5, 2], [0.5, 2]])],
                                  rev=2)), "挪了(面积一样)"
    # 分成两半拼起来:不按并集算,保守地当放宽(要确认)
    halves = [_nogo("l", poly=[[0, 0], [1, 0], [1, 2], [0, 2]]),
              _nogo("r", poly=[[1, 0], [2, 0], [2, 2], [1, 2]])]
    assert not tightens(old, _set(halves, rev=2))


def test_收紧_刁钻几何_凹多边形和夹缝():
    sq = _set([_nogo("a", poly=[[0, 0], [2, 0], [2, 2], [0, 2]])])
    # 凹的大多边形:旧的四个角都在里面,可一道缺口伸进旧的禁行区(只看顶点判不出来)
    notch = [[-1, -1], [3, -1], [3, 3], [1.2, 3], [1.2, 1.0], [0.8, 1.0], [0.8, 3], [-1, 3]]
    assert not tightens(sq, _set([_nogo("n", poly=notch)], rev=2))
    # 缺口没碰到旧的:是收紧
    notch_far = [[-1, -1], [3, -1], [3, 3], [1.2, 3], [1.2, 2.5], [0.8, 2.5], [0.8, 3], [-1, 3]]
    assert tightens(sq, _set([_nogo("n", poly=notch_far)], rev=2))
    # 缺口的底正好贴着旧的上边(共线):还是收紧(边上算里面)
    notch_touch = [[-1, -1], [3, -1], [3, 3], [1.2, 3], [1.2, 2.0], [0.8, 2.0], [0.8, 3], [-1, 3]]
    assert tightens(sq, _set([_nogo("n", poly=notch_touch)], rev=2))
    # 一条 1 cm 的细缝从左边插进来横穿旧的:缝的顶点都压在旧的边上(不在里面)、新旧的边没有一处
    # 真交叉、旧的四个角都在新的里面 —— 只看「顶点 + 交叉」判成盖住了,其实缝那一条不是禁行区
    slit = [[-1, -1], [3, -1], [3, 3], [-1, 3], [-1, 1.01], [0, 1.01], [2, 1.01], [2, 1.0],
            [0, 1.0], [-1, 1.0]]
    assert not tightens(sq, _set([_nogo("s", poly=slit)], rev=2))
    # 旧的凹、新的是盖住它的凸包:收紧
    cav = [[0, 0], [4, 0], [4, 4], [2, 1], [0, 4]]
    assert tightens(_set([_nogo("c", poly=cav)]),
                    _set([_nogo("c", poly=[[0, 0], [4, 0], [4, 4], [0, 4]])], rev=2))
    # 旧的三角落在新的凹口里(顶点都压在新的边上),三角本身不在新的里面:不算盖住
    tri = [[0, 4], [2, 1], [4, 4]]
    assert not tightens(_set([_nogo("t", poly=tri)]), _set([_nogo("c", poly=cav)], rev=2))


def test_收紧_限速区_硬禁区_到期():
    slow = _set([_slow("s", 0.5)])
    assert tightens(slow, _set([_slow("s", 0.5, poly=BIG)], rev=2))
    assert tightens(slow, _set([_slow("s2", 0.3, poly=BIG)], rev=2))
    assert tightens(slow, _set([_nogo("n", poly=BIG)], rev=2)), "限速改成禁行:更严"
    assert not tightens(slow, _set([_slow("s", 0.6, poly=BIG)], rev=2)), "范围大了,限速升了"
    assert not tightens(_set([_nogo("a")]), _set([_slow("a", 0.1, poly=BIG)], rev=2)), \
        "禁行改成限速:放宽"
    hard = _set([_nogo("a") | {"hard": True}])
    assert not tightens(hard, _set([_nogo("a")], rev=2)), "去掉硬禁区标记:放宽"
    assert not tightens(hard, _set([_nogo("a", poly=BIG)], rev=2)), "放大了但不是硬的:放宽"
    assert tightens(hard, _set([_nogo("a", poly=BIG) | {"hard": True}], rev=2))
    assert tightens(_set([_nogo("a")]), _set([_nogo("a") | {"hard": True}], rev=2)), "加硬禁区标记"
    # 到期:加上到期时间、提前 = 放宽;推后、去掉 = 收紧
    t = 1_800_000_000_000
    temp = _set([_nogo("a") | {"until_ms": t}])
    assert not tightens(_set([_nogo("a")]), temp), "永久的改成临时的:放宽"
    assert not tightens(temp, _set([_nogo("a") | {"until_ms": t - 1}], rev=2))
    assert tightens(temp, _set([_nogo("a") | {"until_ms": t + 1}], rev=2))
    assert tightens(temp, _set([_nogo("a")], rev=2))
    # 已经到期的旧区域不用再盖住(站点会删它)
    assert not tightens(temp, _set([], rev=2))
    assert tightens(temp, _set([], rev=2), now_ms=t)
    assert not tightens(temp, _set([], rev=2), now_ms=t - 1)


def test_多边形包含():
    from d1max_contract.zones import polygon_contains
    sq = ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0))
    assert polygon_contains(sq, sq)
    assert polygon_contains(tuple(map(tuple, BIG)), sq)
    assert not polygon_contains(sq, tuple(map(tuple, BIG)))
    far = ((5.0, 5.0), (6.0, 5.0), (6.0, 6.0))
    assert not polygon_contains(sq, far) and not polygon_contains(far, sq)
