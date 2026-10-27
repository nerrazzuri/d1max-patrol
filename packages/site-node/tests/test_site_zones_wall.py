"""虚拟墙(用户 2026-10-15):客户端给墙线,站点算禁行带;规则跟禁行区一样(加墙收紧免确认、删墙要确认、
可以标硬);经站点接口到仿真狗,狗上守的是那条带子。"""

from __future__ import annotations

import pytest
from test_site_api import MAP, PW, _等, 站

from d1max_contract.zones import point_in_polygon, wall_polygon
from d1max_site.db import SiteDB
from d1max_site.nav_zones import NavZones, ZonesError

LINE = [[1, 1], [4, 1], [4, 3]]


def _wall(**kw):
    return {"id": "w1", "kind": "nogo", "label": "虚拟墙", "wall": LINE} | kw


def _z(tmp_path):
    return NavZones(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1000)


def test_站点按墙线算多边形_客户端给的不信(tmp_path):
    z = _z(tmp_path)
    zs, tight = z.put("m", "1", [_wall(polygon=[[0, 0], [9, 0], [9, 9]])], base_revision=0, by="a")
    [w] = zs.zones
    assert w.polygon == wall_polygon(LINE)
    assert w.wall == tuple(tuple(map(float, p)) for p in LINE)
    assert point_in_polygon(2.5, 1.0, w.polygon) and not point_in_polygon(2.5, 2.0, w.polygon)
    assert tight


def test_不带多边形也行_只给墙线(tmp_path):
    z = _z(tmp_path)
    zs, _ = z.put("m", "1", [_wall()], base_revision=0, by="a")
    assert zs.zones[0].polygon == wall_polygon(LINE)


@pytest.mark.parametrize("bad, why", [
    (_wall(wall=[[0, 0]]), "两个点"),
    (_wall(wall=[[0, 0], [2, 0], [0, 0.1]]), "拐得太急"),
    (_wall(kind="slow", max_speed_mps=0.3), "禁行区"),
])
def test_画不成的墙_说原因_不存(tmp_path, bad, why):
    z = _z(tmp_path)
    with pytest.raises(ZonesError, match=why):
        z.put("m", "1", [bad], base_revision=0, by="a")
    assert z.current("m", "1").revision == 0


def test_加墙收紧免确认_删墙要确认_没确认前照守(tmp_path):
    z = _z(tmp_path)
    z.put("m", "1", [], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a", )
    z.put("m", "1", [_wall(hard=True)], base_revision=1, by="a")
    assert z.confirmed_current("m", "1"), "加墙是收紧:当场生效"
    z.put("m", "1", [], base_revision=2, by="a")
    assert not z.confirmed_current("m", "1"), "删墙是放宽:要确认"
    eff = z.effective("m", "1")
    assert len(eff.hard()) == 1 and eff.hard()[0].wall is not None, "没确认前机器人上照旧守着这堵墙"


def test_挪墙_重新存墙线_多边形跟着变(tmp_path):
    z = _z(tmp_path)
    z.put("m", "1", [_wall()], base_revision=0, by="a")
    old_poly = [list(p) for p in wall_polygon(LINE)]
    zs, _ = z.put("m", "1", [_wall(wall=[[1, 1], [5, 1]], polygon=old_poly)], base_revision=1,
                  by="a")
    assert zs.zones[0].polygon == wall_polygon([[1, 1], [5, 1]])


# ------------------------------------------------------------ 经站点接口到仿真狗

ZP = f"/api/maps/{MAP[0]}/{MAP[1]}/zones"


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    d = tmp_path / "cur"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"x")
    s.maps.import_dir(d, map_id=MAP[0], version=MAP[1])
    yield s
    s.close()


def test_接口_画墙_狗上守的是那条带子_看的时候墙线还在(站点):
    s = 站点
    alice = s.req("POST", "/api/login", {"name": "alice", "password": PW})[1]["token"]

    def rev():
        c = s.disp.clients["A"].capabilities
        z = None if c is None else c.tasks.get("zones_set")
        return None if z is None else z.get("rev")
    _等(lambda: rev() is not None)
    code, d = s.req("POST", ZP, {"zones": [_wall(hard=True)], "base_revision": 0}, token=alice)
    assert code == 200, d
    assert d["zones"][0]["wall"] == LINE
    assert [list(p) for p in wall_polygon(LINE)] == d["zones"][0]["polygon"]
    _等(lambda: rev() == 1, timeout=8)
    [w] = s.agent._zones.zones
    assert w.polygon == wall_polygon(LINE) and w.hard
    assert len(s.agent._hard_zones()) == 1, "标了硬的墙,遥控也守"
    code, d = s.req("POST", ZP, {"zones": [_wall(wall=[[0, 0], [0, 0.05]])], "base_revision": 1},
                    token=alice)
    assert code == 400 and "太近" in d["error"], d
