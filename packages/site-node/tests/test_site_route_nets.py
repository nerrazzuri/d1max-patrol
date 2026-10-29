"""W48 路网,站点这一头:存(修订号、别人改过拒、不许进禁行区)、接口(看、改、权限)、下发和补发。"""

from __future__ import annotations

import pytest
from test_site_api import MAP, PW, _等, 站

from d1max_contract.zones import Zone
from d1max_site.db import SiteDB
from d1max_site.route_nets import RouteNetConflict, RouteNetError, RouteNets
from d1max_site.temporal import SAFE, temporal_class


def _body(nodes, edges, base=0):
    return {"nodes": [{"id": i, "x": x, "y": y} for i, x, y in nodes],
            "edges": [list(e) for e in edges], "base_revision": base}


POND = Zone("pond", "nogo", ((3.0, 1.0), (4.0, 1.0), (4.0, 2.0), (3.0, 2.0)), "池子")


def test_存_修订号_别人改过拒_空的是第0版(tmp_path):
    rn = RouteNets(SiteDB(tmp_path / "s.db"), now_ms=lambda: 5)
    assert rn.get("m", "1").revision == 0 and rn.get("m", "1").nodes == ()
    net = rn.put("m", "1", _body([("A", 0, 0), ("B", 5, 0)], [("A", "B")]), by="olga")
    assert net.revision == 1 and len(net.edges) == 1
    assert rn.get("m", "1") == net
    with pytest.raises(RouteNetConflict):
        rn.put("m", "1", _body([], [], base=0), by="olga")
    assert rn.put("m", "1", _body([], [], base=1), by="olga").revision == 2
    assert rn.view("m", "1")["revision"] == 2 and rn.view("m", "1")["updated_by"] == "olga"


def test_点不许放进禁行区_线不许穿过禁行区_说清是哪个(tmp_path):
    rn = RouteNets(SiteDB(tmp_path / "s.db"), now_ms=lambda: 5)
    with pytest.raises(RouteNetError, match="点 P"):
        rn.put("m", "1", _body([("P", 3.5, 1.5)], []), by="o", keep_out=(POND,))
    with pytest.raises(RouteNetError, match="A–B"):
        rn.put("m", "1", _body([("A", 2, 1.5), ("B", 5, 1.5)], [("A", "B")]), by="o",
               keep_out=(POND,))
    with pytest.raises(RouteNetError):
        rn.put("m", "1", {"nodes": "x", "base_revision": 0}, by="o")
    assert rn.put("m", "1", _body([("A", 2, 3), ("B", 5, 3)], [("A", "B")]), by="o",
                  keep_out=(POND,)).revision == 1


def test_时间权威_路网一律照发():
    assert temporal_class("route_net_set", {}) is SAFE


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.accounts.add("gina", PW, role="guard")
    d = tmp_path / "cur"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"x")
    s.maps.import_dir(d, map_id=MAP[0], version=MAP[1])
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


RP = f"/api/maps/{MAP[0]}/{MAP[1]}/route_net"


def test_接口_看改_权限_404_别人改过409_写错400(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    code, d = s.req("GET", RP, token=gina)
    assert code == 200 and d["revision"] == 0 and d["nodes"] == []
    body = _body([("A", 0, 0), ("B", 5, 0)], [("A", "B")])
    assert s.req("POST", RP, body, token=gina)[0] == 403
    assert s.req("GET", f"/api/maps/{MAP[0]}/99/route_net", token=gina)[0] == 404
    code, d = s.req("POST", RP, body, token=alice)
    assert code == 200 and d["revision"] == 1 and "pushed" in d, d
    assert s.req("POST", RP, body, token=alice)[0] == 409
    assert s.req("POST", RP, _body([("A", 0, 0)], [("A", "Z")], base=1), token=alice)[0] == 400


def test_补发_机器人报的修订旧了就发_同一版30秒内不重发_对上了不发(站点):
    s = 站点
    _等(lambda: s.disp.clients["A"].capabilities is not None)
    caps = s.disp.clients["A"].capabilities.tasks
    caps["route_net_set"] = {"rev": 0, "nodes": 0}          # 当它是规划后端的狗
    assert s.loop.call(lambda: s.disp.sync_route_nets()) == [], "都是第 0 版:不发"
    s.disp.route_nets.put(MAP[0], MAP[1], _body([("A", 0, 0), ("B", 5, 0)], [("A", "B")]),
                          by="alice")
    assert s.loop.call(lambda: s.disp.sync_route_nets()) == ["A"]
    assert s.loop.call(lambda: s.disp.sync_route_nets()) == [], "同一版 30 s 内不重发"
    caps["route_net_set"] = {"rev": 1, "nodes": 2}
    assert s.loop.call(lambda: s.disp.sync_route_nets()) == [], "对上了不发"
    del caps["route_net_set"]                               # 直线桥的狗不认路网:不发
    s.disp.route_nets.put(MAP[0], MAP[1], _body([], [], base=1), by="alice")
    assert s.loop.call(lambda: s.disp.sync_route_nets()) == []
