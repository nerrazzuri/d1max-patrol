"""W44:保安的临时路线(设计 §4.3,用户选的):在地图上按顺序点几个点,机器人走一趟,不进路线库。
另:授权列表每条带「此刻生效没有」(地图页防区标签用;星期、跨午夜、时区只在站点算一处)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站

from d1max_site.authz import AuthzBook
from d1max_site.db import SiteDB


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _图(s, tmp_path, tok):
    """导入一个任务包,拿它那条路线的图号、版本和点(这些点在模拟机器人的图上走得通)。"""
    from test_site_schedule import 打包
    _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    code, d = s.req("POST", "/api/bundles", {"path": str(打包(tmp_path, 1))}, token=tok)
    assert code == 200, d
    r = s.req("GET", "/api/routes", token=tok)[1]["routes"]
    loop = next(x for x in r if x["id"] == "loop")
    pts = [{"x": w["x"], "y": w["y"]} for w in loop["waypoints"]]
    return loop["map_id"], loop["map_version"], pts


def test_临时路线_派出去不进路线库(站点, tmp_path):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    mid, ver, pts = _图(s, tmp_path, alice)
    body = {"points": pts[:2], "map_id": mid, "map_version": ver}
    code, d = s.req("POST", "/api/robots/A/patrol", body, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    ids = [x["id"] for x in s.req("GET", "/api/routes", token=alice)[1]["routes"]]
    assert ids == ["loop"], "临时路线不存"
    v = s.req("GET", "/api/robots/A", token=alice)[1]
    cmd = v["commands"][0]
    assert cmd["kind"] == "patrol" and cmd["issued_by"] == "gina"
    audit = s.req("GET", "/api/audit", token=alice)[1]["audit"]
    rows = [r for r in audit if r["actor"] == "gina" and "/patrol" in r["action"]]
    assert rows, audit[:3]
    assert rows[0]["detail"]["temp"] is True and rows[0]["detail"]["points"] == 2


def test_临时路线_点数0或21个400(站点, tmp_path):
    s = 站点
    gina = _登(s, "gina")
    for pts in ([], [{"x": 0, "y": 0}] * 21):
        body = {"points": pts, "map_id": "m", "map_version": "1"}
        code, d = s.req("POST", "/api/robots/A/patrol", body, token=gina)
        assert code == 400, d


def test_临时路线_点不成形400带第几个(站点):
    s = 站点
    gina = _登(s, "gina")
    code, d = s.req("POST", "/api/robots/A/patrol",
                    {"points": [{"x": 0, "y": 0}, {"x": "a", "y": 0}], "map_id": "m",
                     "map_version": "1"},
                    token=gina)
    assert code == 400 and d["errors"][0]["index"] == 1


def test_临时路线_只看账号403_经理能派(站点):
    s = 站点
    s.accounts.add("vic", PW, role="viewer")
    body = {"points": [{"x": 0, "y": 0}], "map_id": "m", "map_version": "1"}
    assert s.req("POST", "/api/robots/A/patrol", body, token=_登(s, "vic"))[0] == 403
    # W53:经理能派(过了权限这一关;后面被别的拦下不是 403)
    assert s.req("POST", "/api/robots/A/patrol", body, token=_登(s, "olga"))[0] != 403


def test_授权此刻生效没有(tmp_path):
    import time
    db = SiteDB(tmp_path / "s.db")
    now = int(time.time() * 1000)
    book = AuthzBook(db, now_ms=lambda: now)
    lt = time.localtime(now / 1000)
    m = lt.tm_hour * 60 + lt.tm_min
    hh = lambda x: f"{(x // 60) % 24:02d}:{x % 60:02d}"  # noqa: E731
    book.add({"name": "维修工", "zones": ["北区"], "days": 127, "start": hh(max(0, m - 1)),
              "end": hh(min(1439, m + 2))}, by="alice")
    book.add({"name": "保洁", "zones": ["主楼"], "days": 127, "start": hh((m + 120) % 1440),
              "end": hh((m + 180) % 1440)}, by="alice")
    got = {e["name"]: e["active_now"] for e in book.list(with_active=True)}
    assert got == {"维修工": True, "保洁": False}


def test_W53_接口_派之前先看_派出去记成临时巡逻_今天的巡逻里有_能取消排队的(站点, tmp_path):
    s = 站点
    alice, gina, olga = _登(s, "alice"), _登(s, "gina"), _登(s, "olga")
    _图(s, tmp_path, alice)
    code, p = s.req("GET", "/api/robots/A/patrol-plan?mission_id=loop", token=olga)
    assert code == 200 and p["estimate_s"] > 0 and p["label"] == "loop", p
    assert s.req("GET", "/api/robots/A/patrol-plan?mission_id=nope", token=gina)[0] == 404
    s.accounts.add("vic", PW, role="viewer")
    assert s.req("GET", "/api/robots/A/patrol-plan?mission_id=loop", token=_登(s, "vic"))[0] == 403
    code, d = s.req("POST", "/api/robots/A/patrol", {"mission_id": "loop"}, token=gina)
    assert code == 200 and d["queued"] is False and d["ack"]["result"] == "accepted", d
    today = s.req("GET", "/api/patrols/today", token=olga)[1]["items"]
    mine = [x for x in today if x["kind"] == "adhoc"]
    assert mine and mine[0]["by"] == "gina" and mine[0]["label"] == "loop", today
    assert s.req("POST", f"/api/adhoc/{mine[0]['adhoc_id']}/cancel", {}, token=gina)[0] == 409, \
        "开始跑了的不能取消(用叫停)"
