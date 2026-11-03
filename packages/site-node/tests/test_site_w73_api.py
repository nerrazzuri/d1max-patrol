"""W73 第一期的接口:路线按类别列、防区带查看点 / 值守位、生成防区路线、每个点的预计时间。"""

from __future__ import annotations

import pytest
from test_site_api import PW, 站
from test_site_maps import _files
from test_site_w73_guard_plan import EAST, WEST, _两间

from d1max_site.areas import AreaBook


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    src = _files(tmp_path / "mapsrc")
    _两间(src)
    s.maps.import_dir(src, map_id="shop", version="1")
    s.db.query("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
               "is_default) VALUES ('A', 'dock', 'shop', '1', 1.0, 1.0, 0, 1)")
    s.api.areas = AreaBook(s.db, now_ms=lambda: 1000)
    s.api.areas.set("shop", "1", "东区", EAST, by="a")
    s.api.areas.set("shop", "1", "西区", WEST, by="a")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _路线(**kw):
    return {"map_id": "shop", "map_version": "1", "base_revision": 0,
            "waypoints": [{"name": "A", "x": 2.0, "y": 2.0, "yaw": 0.0},
                          {"name": "B", "x": 6.0, "y": 2.0, "yaw": 0.0}], **kw}


def test_路线列表默认只给巡逻路线_要防区路线得说(站点):
    s = 站点
    alice = _登(s, "alice")
    assert s.req("POST", "/api/routes/night", _路线(), token=alice)[0] == 200
    code, d = s.req("POST", "/api/routes/east", _路线(kind="guard"), token=alice)
    assert code == 200 and d["kind"] == "guard", d
    ids = lambda q: [r["id"] for r in s.req("GET", "/api/routes" + q, token=alice)[1]["routes"]]  # noqa: E731
    assert ids("") == ["night"], "老手机、老网页只看得到巡逻路线"
    assert ids("?kind=guard") == ["east"]
    assert ids("?kind=all") == ["east", "night"]
    assert s.req("GET", "/api/routes?kind=bogus", token=alice)[0] == 400


def test_防区带查看点和值守位_管理员能挪_能重置_保安不能挪(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    code, d = s.req("GET", "/api/maps/shop/1/areas", token=gina)
    assert code == 200
    posts = {p["zone"]: p for p in d["posts"]}
    assert set(posts) == {"东区", "西区"} and posts["东区"]["auto"] is True
    move = {"zone": "东区", "posts": {"view": {"x": 2.0, "y": 2.0},
                                      "watch": {"x": 3.0, "y": 3.0, "yaw": 0.5}}}
    assert s.req("POST", "/api/maps/shop/1/areas", move, token=gina)[0] == 403
    code, d = s.req("POST", "/api/maps/shop/1/areas", move, token=alice)
    assert code == 200, d
    east = {p["zone"]: p for p in d["posts"]}["东区"]
    assert east["auto"] is False and east["view"] == {"x": 2.0, "y": 2.0}
    assert [a["zone"] for a in d["areas"]] == ["东区", "西区"], "挪点不动多边形"
    bad = {"zone": "东区", "posts": {"view": {"x": 15.0, "y": 2.0},
                                     "watch": {"x": 3.0, "y": 3.0, "yaw": 0.5}}}
    code, d = s.req("POST", "/api/maps/shop/1/areas", bad, token=alice)
    assert code == 400 and "防区外" in d["error"]
    code, d = s.req("POST", "/api/maps/shop/1/areas", {"zone": "东区", "posts": None},
                    token=alice)
    assert code == 200 and {p["zone"]: p for p in d["posts"]}["东区"]["auto"] is True


def test_生成防区路线_只是草稿_不存(站点):
    s = 站点
    alice, olga = _登(s, "alice"), _登(s, "olga")
    body = {"map_id": "shop", "map_version": "1",
            "zones": [{"zone": "东区", "laps": 2, "photos": 1}, {"zone": "西区"}]}
    assert s.req("POST", "/api/guard-routes/generate", body, token=olga)[0] == 403
    code, d = s.req("POST", "/api/guard-routes/generate", body, token=alice)
    assert code == 200, d
    assert d["problems"] == [] and len(d["waypoints"]) >= 4
    assert {z["zone"] for z in d["zones"]} == {"东区", "西区"}
    assert s.req("GET", "/api/routes?kind=all", token=alice)[1]["routes"] == [], "没存"
    code, d = s.req("POST", "/api/guard-routes/generate", {**body, "zones": []}, token=alice)
    assert code == 400 and "至少选一个" in d["error"]


def test_每个点的预计时间(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    assert s.req("POST", "/api/routes/night", _路线(), token=alice)[0] == 200
    code, d = s.req("GET", "/api/routes/night/eta", token=gina)
    assert code == 200, d
    assert [w["name"] for w in d["waypoints"]] == ["A", "B"]
    assert d["waypoints"][1]["travel_m"] == pytest.approx(4.0, abs=0.3)
    assert d["total_s"] == d["waypoints"][1]["arrive_s"] > 0
    assert s.req("GET", "/api/routes/ghost/eta", token=gina)[0] == 404


def test_历史版本退回删掉的防区路线_还是防区路线(站点):
    from test_site_layout_history import _hist, _restore
    s = 站点
    alice = _登(s, "alice")
    assert s.req("POST", "/api/routes/east", _路线(kind="guard"), token=alice)[0] == 200
    assert s.req("POST", "/api/routes/east/delete", {"base_revision": 1}, token=alice)[0] == 200
    rows = _hist(s, alice, "route", item="east", map_id="shop", version="1")
    code, d = _restore(s, alice, rows[1]["id"])                    # 删之前那一版
    assert code == 200, d
    assert s.req("GET", "/api/routes/east", token=alice)[1]["kind"] == "guard"


# ---------------------------------------------------------------- 第二期:值守方案、状态、值守记录

def _接值守(s):
    from d1max_site.api import zone_posts
    from d1max_site.guard_duty import GuardDuty
    from d1max_site.modes import ArmingDesk
    if getattr(s.api, "arming", None) is None:
        s.api.arming = ArmingDesk(s.db, now_ms=lambda: 1000)
    s.api.guard = GuardDuty(s.db, s.disp, routes=s.api.routes, areas=s.api.areas,
                            arming=s.api.arming,
                            posts=lambda m, v: {p["zone"]: p for p in zone_posts(s.api).view(m, v)},
                            now_ms=lambda: 1000)
    s.api.guard.reach = getattr(s.api.routes, "reach", None)
    return s.api.guard


def test_值守方案_看_改_带预报_保安不能改_版本冲突(站点):
    s = 站点
    _接值守(s)
    alice, gina = _登(s, "alice"), _登(s, "gina")
    assert s.req("POST", "/api/routes/east", _路线(kind="guard"), token=alice)[0] == 200
    code, d = s.req("GET", "/api/guard/plan", token=gina)
    assert code == 200 and d["revision"] == 0 and d["entries"] == []
    body = {"entries": [{"route": "east", "robot": "A", "gap_min": 30}], "base_revision": 0}
    assert s.req("POST", "/api/guard/plan", body, token=gina)[0] == 403
    code, d = s.req("POST", "/api/guard/plan", body, token=alice)
    assert code == 200, d
    assert d["revision"] == 1 and d["entries"][0]["gap_min"] == 30
    fc = d["forecast"]
    assert fc["entries"][0]["cycle_s"] == 1800 and [z["zone"] for z in fc["zones"]] == ["东区"]
    assert any("西区" in w for w in fc["warnings"]), "两个点都在东区:西区没人看要提示"
    assert s.req("POST", "/api/guard/plan", body, token=alice)[0] == 409, "base_revision 旧了"
    bad = {"entries": [{"route": "nope"}], "base_revision": 1}
    code, d = s.req("POST", "/api/guard/plan", bad, token=alice)
    assert code == 400 and "没有路线" in d["error"]
    code, d = s.req("POST", "/api/routes/east/delete", {"base_revision": 1}, token=alice)
    assert code in (400, 409) and "值守方案" in d["error"], d


def test_值守状态和记录_保安能看(站点):
    s = 站点
    g = _接值守(s)
    gina = _登(s, "gina")
    code, d = s.req("GET", "/api/guard/status", token=gina)
    assert code == 200 and d["duty"] is None and d["runs"] == []
    with s.db.tx() as c:
        c.execute("INSERT INTO guard_duties(started_ms, ended_ms) VALUES (100, 900)")
        c.execute("INSERT INTO guard_log(duty_id, at_ms, kind, zone, point) "
                  "VALUES (1, 200, 'point', '东区', 'A')")
    code, d = s.req("GET", "/api/guard/duties", token=gina)
    assert code == 200 and d["duties"][0]["zones"][0]["looks"] == 1
    code, d = s.req("GET", "/api/guard/duties/1", token=gina)
    assert code == 200 and d["log"][0]["point"] == "A"
    assert s.req("GET", "/api/guard/duties/99", token=gina)[0] == 404
    assert g is s.api.guard
