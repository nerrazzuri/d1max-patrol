"""W48 路网:格式、校验、找路。"""

from __future__ import annotations

import math

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.routenet import OFF_NET_FACTOR, RouteNet, route


def _网(nodes, edges, rev=1):
    return RouteNet.from_wire({"map_id": "m", "map_version": "1", "revision": rev,
                               "nodes": [{"id": i, "x": x, "y": y} for i, x, y in nodes],
                               "edges": [[a, b] for a, b in edges]})


# 一条 L 形路:A(0,0) — B(20,0) — C(20,20)
L = _网([("A", 0, 0), ("B", 20, 0), ("C", 20, 20)], [("A", "B"), ("B", "C")])


def test_来回不变样_摘要只看内容():
    w = L.to_wire()
    assert RouteNet.from_wire(w) == L
    assert L.digest() == RouteNet.from_wire(w).digest()
    assert L.digest() != _网([("A", 0, 0), ("B", 20, 0)], [("A", "B")]).digest()


@pytest.mark.parametrize("bad", [
    {"nodes": [{"id": "A", "x": 0, "y": 0}, {"id": "A", "x": 1, "y": 1}], "edges": []},  # 重名
    {"nodes": [{"id": "A", "x": 0, "y": 0}], "edges": [["A", "Z"]]},  # 连到没有的点
    {"nodes": [{"id": "A", "x": 0, "y": 0}], "edges": [["A", "A"]]},  # 自己连自己
    {"nodes": [{"id": "A", "x": float("nan"), "y": 0}], "edges": []},
    {"nodes": [{"id": "A", "x": 0, "y": 0}, {"id": "B", "x": 500, "y": 0}],
     "edges": [["A", "B"]]},  # 一条线太长
    {"nodes": [{"id": "坏 id", "x": 0, "y": 0}], "edges": []},
    {"nodes": [{"id": "A", "x": 0, "y": 0}, {"id": "B", "x": 1, "y": 0}],
     "edges": [["A", "B"], ["B", "A"]]},  # 重复的线
])
def test_写错就拒(bad):
    with pytest.raises(ContractError):
        RouteNet.from_wire({"map_id": "m", "map_version": "1", "revision": 1, **bad})


def test_空路网_不上路网():
    empty = _网([], [])
    assert route(empty, (0, 0), (30, 30)) == []


def test_L形_从路口附近去另一头_沿路走_不抄近道():
    got = route(L, (1, 1), (19, 21))
    assert got == [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0)] or got == [(20.0, 0.0), (20.0, 20.0)]
    assert (20.0, 0.0) in got, "要经过拐角 B,不斜穿"


def test_两个点挨着_直接走不上路网():
    assert route(L, (5, 5), (6, 6)) == []


def test_离路网太远_不上路网():
    assert route(L, (100, 100), (120, 120)) == []


def test_离起点终点不到半米的途经点去掉():
    got = route(L, (0.2, 0.1), (20.1, 19.8))
    assert got == [(20.0, 0.0)]


def test_代价_走下路网按直线三倍算():
    assert OFF_NET_FACTOR == 3.0
    # 一条直路 A(0,0)—B(40,0);起点 (10,5),终点 (30,5):直走 20 × 3 = 60;上路网 5×3 + 20 + 5×3 = 50
    straight = _网([("A", 0, 0), ("B", 40, 0), ("P", 10, 0), ("Q", 30, 0)],
                   [("A", "P"), ("P", "Q"), ("Q", "B")])
    assert route(straight, (10, 5), (30, 5)) == [(10.0, 0.0), (30.0, 0.0)]


def test_不连通的两块_各走各的就不上路网():
    two = _网([("A", 0, 0), ("B", 10, 0), ("C", 50, 0), ("D", 60, 0)],
              [("A", "B"), ("C", "D")])
    assert route(two, (1, 1), (59, 1)) == []


def test_路网的长度():
    assert math.isclose(L.length_m(), 40.0)
