"""虚拟墙(用户 2026-10-15):画一条折线,存成约 0.3 m 宽的禁行带。

- 区域照旧是 ``nogo`` 多边形(狗、老狗、规划、每拍检查、遥控守卫、收紧判定都不用改);多一个可选字段
  ``wall``:画的那条折线(显示成线、以后改墙改的是线)。
- 多边形由站点按线算(:func:`wall_polygon`):线往两边各让出半宽,两头各伸出半宽,拐角斜接。
- 内容摘要不算 ``wall``(那是显示用的):还不认这个字段的狗回报的摘要照样对得上。
"""

from __future__ import annotations

import math

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.zones import (
    WALL_WIDTH_M,
    Zone,
    ZoneSet,
    is_simple,
    point_in_polygon,
    polygon_area,
    signed_distance,
    wall_polygon,
    zones_digest,
)


def _wire(line, **kw):
    return {"id": "w1", "kind": "nogo", "label": "墙",
            "polygon": [list(p) for p in wall_polygon(line)],
            "wall": [list(p) for p in line]} | kw


def test_直墙_宽0点3_两头各伸出半宽():
    poly = wall_polygon([(0, 0), (4, 0)])
    assert WALL_WIDTH_M == 0.3
    assert is_simple(poly)
    assert polygon_area(poly) == pytest.approx(4.3 * 0.3)
    assert point_in_polygon(2, 0.1, poly) and not point_in_polygon(2, 0.2, poly)
    assert point_in_polygon(-0.1, 0, poly) and not point_in_polygon(-0.2, 0, poly)


def test_拐弯的墙_线上每一点都在带子里_离线0点15以外都不在():
    line = [(0, 0), (3, 0), (3, 2), (5, 4)]
    poly = wall_polygon(line)
    assert is_simple(poly) and len(poly) == 2 * len(line)
    for (ax, ay), (bx, by) in zip(line, line[1:], strict=False):
        for k in range(11):
            x, y = ax + (bx - ax) * k / 10, ay + (by - ay) * k / 10
            assert signed_distance(x, y, poly) <= -0.14, (x, y)       # 线在带子里,离边至少半宽
    assert not point_in_polygon(1.5, 0.3, poly) and not point_in_polygon(1.5, -0.3, poly)


@pytest.mark.parametrize("line, why", [
    ([(0, 0)], "两个点"),
    ([(0, 0), (0, 0)], "太近"),
    ([(0, 0), (1, 0), (1.01, 0)], "太近"),
    ([(0, 0), (2, 0), (0, 0.1)], "拐得太急"),
    ([(0, 0), (4, 0), (4, 3), (2, 3), (2, -1)], "交叉"),
    ([(i, i % 2) for i in range(40)], "最多"),
])
def test_画不成墙的说清楚(line, why):
    with pytest.raises(ContractError, match=why):
        wall_polygon(line)


def test_区域带墙线_来回一字不差_只许禁行区():
    line = [(0, 0), (3, 0), (3, 2)]
    z = Zone.from_wire(_wire(line))
    assert z.wall == ((0.0, 0.0), (3.0, 0.0), (3.0, 2.0))
    assert z.keep_out and z.kind == "nogo"
    assert Zone.from_wire(z.to_wire()) == z
    plain = Zone.from_wire({"id": "p", "kind": "nogo", "polygon": [[0, 0], [1, 0], [1, 1]]})
    assert "wall" not in plain.to_wire()
    with pytest.raises(ContractError, match="禁行区"):
        Zone.from_wire(_wire(line, kind="slow", max_speed_mps=0.3))
    with pytest.raises(ContractError, match="wall"):
        Zone.from_wire(_wire(line) | {"wall": [[0, 0]]})
    with pytest.raises(ContractError, match="wall"):
        Zone.from_wire(_wire(line) | {"wall": "x"})
    h = Zone.from_wire(_wire(line, hard=True))
    assert h.hard and h.wall is not None


def test_摘要不算墙线_老狗回报的对得上():
    line = [(0, 0), (3, 0)]
    new = ZoneSet.from_wire({"map_id": "m", "map_version": "1", "revision": 2,
                             "zones": [_wire(line)]})
    old_wire = new.to_wire()
    old_wire["zones"] = [{k: v for k, v in z.items() if k != "wall"} for z in old_wire["zones"]]
    old = ZoneSet.from_wire(old_wire)
    assert old.zones[0].wall is None
    assert zones_digest(new) == zones_digest(old)


def test_墙线跟多边形对不上_契约不管_站点每次按线重算():
    """契约只核墙线本身;多边形是站点算的(:mod:`d1max_site.nav_zones`),狗只按多边形守。"""
    z = Zone.from_wire(_wire([(0, 0), (3, 0)]) | {"polygon": [[0, 0], [1, 0], [1, 1]]})
    assert z.wall is not None and len(z.polygon) == 3


def test_拐角斜接_带子不会因为拐角变细():
    poly = wall_polygon([(0, 0), (4, 0), (4, 4)])
    assert signed_distance(4, 0, poly) <= -0.149
    assert math.isclose(max(x for x, _ in poly), 4.15, abs_tol=1e-6)
