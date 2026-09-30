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


def test_只收紧():
    old = _set([_nogo("a"), _slow("s", 0.5)])
    assert tightens(old, _set([_nogo("a"), _slow("s", 0.5)], rev=2))
    assert tightens(old, _set([_nogo("a"), _slow("s", 0.3), _nogo("b")], rev=2))
    assert tightens(old, _set([_nogo("a"), _slow("s", 0.5), _slow("t", 1.0)], rev=2))
    assert not tightens(old, _set([_slow("s", 0.5)], rev=2))                      # 删禁行
    assert not tightens(old, _set([_nogo("a")], rev=2))                           # 删限速
    assert not tightens(old, _set([_nogo("a"), _slow("s", 0.6)], rev=2))          # 升限速
    moved = [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert not tightens(old, _set([_nogo("a", poly=moved), _slow("s", 0.5)], rev=2))
    assert not tightens(old, _set([_nogo("a"), _slow("s", 0.5, poly=moved)], rev=2))
    assert not tightens(old, _set([{**_slow("a", 0.1)}, _slow("s", 0.5)], rev=2))  # 换种类
    assert not tightens(old, _set([_nogo("a"), _slow("s", 0.5)], rev=2, ver="v2"))
