"""W48 路网,机器人这一头:落盘、规划后端按路网分段规划(途经点只是路过)、走不通退回直接规划。"""

from __future__ import annotations

import math

from test_planned_nav import 台子

from d1max_agent.routebook import RouteBook
from d1max_contract.routenet import RouteNet
from d1max_patrol.protocol.nav_types import NavStatus, Pose


def _网(nodes, edges, *, version="v", rev=1):
    return RouteNet.from_wire({"map_id": "m", "map_version": version, "revision": rev,
                               "nodes": [{"id": i, "x": x, "y": y} for i, x, y in nodes],
                               "edges": [[a, b] for a, b in edges]})


# 现场 12 × 8 m:一条 L 形路 A(2,2) — B(2,6) — C(10,6)
L = _网([("A", 2, 2), ("B", 2, 6), ("C", 10, 6)], [("A", "B"), ("B", "C")])


def test_路网落盘_按地图版本取_别的版本不给(tmp_path):
    book = RouteBook(tmp_path / "routenets.json")
    assert book.get("m", "v") is None
    book.put(L)
    assert book.get("m", "v") == L
    assert book.get("m", "other") is None
    (tmp_path / "routenets.json").write_text("坏了")
    assert book.get("m", "v") is None, "文件坏了当没有"


async def test_有路网_沿路走到拐角再过去_不斜穿(tmp_path):
    t = 台子(tmp_path)
    t.nav.set_route_net(L)
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 5.0))
    pts = t.nav.current_path()
    assert any(x < 3.0 and y > 5.5 for x, y in pts), "要经过拐角 B(2,6)"
    assert t.nav.planned_length_m > 11.0
    await t.走到头()
    assert NavStatus.SUCCEED in t.status and NavStatus.FAILED not in t.status
    o = await t.在()
    assert math.hypot(o.x - 10.0, o.y - 5.0) < 0.4


async def test_没路网_照旧直接规划(tmp_path):
    t = 台子(tmp_path)
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 5.0))
    pts = t.nav.current_path()
    assert not any(x < 3.0 and y > 5.5 for x, y in pts)
    assert t.nav.planned_length_m < 10.0


async def test_路网上有一段规划不出来_这一次直接走_发事件(tmp_path):
    t = 台子(tmp_path, walls=[(1.5, 5.5, 2.5, 6.5)])        # 拐角 B 被墙盖住
    t.nav.set_route_net(L)
    await t.start(2.0, 2.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 5.0))
    assert t.nav.planned_length_m < 10.0
    assert [k for k, _ in t.events].count("route_net_bypassed") == 1
    await t.走到头()
    assert NavStatus.SUCCEED in t.status and NavStatus.FAILED not in t.status
