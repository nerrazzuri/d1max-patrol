"""防区多边形与名单(商业化 B1c):防区画在地图上、按位置算防区;时段 / 人员授权(星期、时段、跨午夜、
有效期、全站或几个防区);授权生效时摄像头入侵只记录、狗看见人不报 P1 改记 P3;接口权限。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from test_site_api import PW, 站
from test_site_incidents import _gotos, _报
from test_site_modes import 事件台  # noqa: F401 - pytest 夹具
from test_site_sightings import 台  # noqa: F401 - pytest 夹具

from d1max_site.areas import AreaBook, AreaError, inside
from d1max_site.authz import ALL_DAYS, AuthzBook, AuthzError
from d1max_site.db import SiteDB
from d1max_site.modes import ArmingDesk

NOW = 1_800_000_000_000
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def _lt(wday, hh, mm):
    """假的本地时间:星期 wday(0 = 星期一)hh:mm。"""
    return lambda _s: time.struct_time((2026, 10, 12, hh, mm, 0, wday, 285, 0))


def test_防区多边形_校验_按位置算防区_改与删(tmp_path):
    b = AreaBook(SiteDB(tmp_path / "s.db"), now_ms=lambda: NOW)
    for bad_zone, pts in (("", SQUARE), ("a", [[0, 0], [1, 1]]), ("a", [[0, 0], [1, 1], [2, 2]]),
                          ("a", [[0, 0], [1, "x"], [2, 0]]),
                          ("a", [[0, 0], [1, float("nan")], [2, 0]])):
        with pytest.raises(AreaError):
            b.set("m", "1", bad_zone, pts, by="admin")
    b.set("m", "1", "Back garden", SQUARE, by="admin")
    b.set("m", "1", "Pool", [[20, 0], [30, 0], [30, 5]], by="admin")
    assert b.zone_at("m", "1", 5, 5) == "Back garden"
    assert b.zone_at("m", "1", 50, 50) is None
    assert b.zone_at("m", "2", 5, 5) is None, "别的版本没画"
    b.set("m", "1", "Back garden", [[100, 100], [110, 100], [110, 110]], by="admin")
    assert b.zone_at("m", "1", 5, 5) is None, "改了就按新的"
    assert b.remove("m", "1", "Pool") and not b.remove("m", "1", "Pool")
    assert inside(1, 1, SQUARE) and not inside(11, 1, SQUARE)


def test_授权_星期_时段_跨午夜_有效期_全站或防区(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    clock = [NOW]
    local = [_lt(1, 9, 30)]                                  # 星期二 09:30
    b = AuthzBook(db, now_ms=lambda: clock[0], localtime=lambda s: local[0](s))
    for bad in ({"name": "", "start": "09:00", "end": "11:00"},
                {"name": "x", "start": "9:00", "end": "11:00"},
                {"name": "x", "start": "09:00", "end": "09:00"},
                {"name": "x", "start": "09:00", "end": "11:00", "days": 0},
                {"name": "x", "start": "09:00", "end": "11:00", "zones": "Back"},
                {"name": "x", "start": "09:00", "end": "11:00", "until_ms": NOW - 1}):
        with pytest.raises(AuthzError):
            b.add(bad, by="admin")
    b.add({"name": "Gardener", "zones": ["Back garden"], "days": 0b0000010,
           "start": "09:00", "end": "11:00"}, by="admin")         # 只有星期二
    assert b.active("Back garden") == "Gardener"
    assert b.active("Front lawn") is None, "别的防区不算"
    assert b.active(None) is None, "说不清在哪个防区只认全站授权"
    local[0] = _lt(1, 11, 0)
    assert b.active("Back garden") is None, "11:00 结束(不含)"
    local[0] = _lt(2, 9, 30)
    assert b.active("Back garden") is None, "星期三不算"
    b.add({"name": "Night cleaner", "days": 0b0010000, "start": "22:00", "end": "06:00",
           "until_ms": NOW + 86400_000}, by="admin")              # 星期五晚上 → 星期六早上,全站
    local[0] = _lt(4, 23, 0)
    assert b.active(None) == "Night cleaner"
    local[0] = _lt(5, 5, 59)
    assert b.active("Anywhere") == "Night cleaner", "跨午夜的早上按开始那天的星期算"
    local[0] = _lt(5, 23, 0)
    assert b.active(None) is None, "星期六晚上不在授权里"
    clock[0] = NOW + 86400_000
    local[0] = _lt(4, 23, 0)
    assert b.active(None) is None, "过了有效期"
    [e] = [x for x in b.list() if x["name"] == "Gardener"]
    assert e["start"] == "09:00" and b.remove(e["id"]) and not b.remove(e["id"])
    assert ALL_DAYS == 127


async def test_摄像头入侵_防区有授权_只记录不派狗(事件台):  # noqa: F811
    t = 事件台
    t.desk.arming = ArmingDesk(t.db, now_ms=t.clock)
    t.desk.arming.authz = SimpleNamespace(
        active=lambda zone: "Gardener" if zone == "front-yard" else None)
    await t.run(12)
    r = await _报(t, "e1")
    assert r["outcome"] == "authorized" and "Gardener" in r["note"]
    assert not _gotos(t)
    t.desk.arming.authz = SimpleNamespace(active=lambda zone: None)
    assert (await _报(t, "e2"))["outcome"] == "dispatched"


def test_狗看见人_在授权防区里_不报P1记P3_离开授权防区照报P1(台):  # noqa: F811
    t = 台
    areas = AreaBook(t.db, now_ms=lambda: t.ms[0])
    areas.set("m", "1", "Back garden", SQUARE, by="admin")          # 狗在 (3, 4)
    t.w.areas = areas
    _可靠位置(t)
    t.arming.authz = SimpleNamespace(
        active=lambda zone: "Gardener" if zone == "Back garden" else None)
    t.d.persons("A", present=True, count=1, nearest_m=4.0)
    t.w.tick()
    [a] = t.w.alerts.raised
    assert a["kind"] == "authorized_person" and "Gardener" in a["title"]
    assert a["context"]["zone"] == "Back garden"
    assert [r["robot_id"] for r in t.db.query("SELECT robot_id FROM person_sightings")] == ["A"], \
        "这一回记下了(人走了才清)"
    t.w.tick()
    assert len(t.w.alerts.raised) == 1, "这一回只记一次"
    t.d.persons("A", present=False)
    t.w.tick()
    t.d.clients["A"].telemetry.pose.x = 50.0                        # 走出授权的防区
    t.d.persons("A", present=True, count=1, nearest_m=4.0)
    t.w.tick()
    assert t.w.alerts.raised[-1]["kind"] == "dog_sees_person"


def _可靠位置(t, *, loc_ok=True, age_ms=0):
    t.d.clients["A"].status.ready = SimpleNamespace(loc_ok=loc_ok)
    t.d.telemetry_at = {"A": t.ms[0] - age_ms}
    t.d.stale_ms = 5000


def _授权台(t, active):
    areas = AreaBook(t.db, now_ms=lambda: t.ms[0])
    areas.set("m", "1", "Back garden", SQUARE, by="admin")          # 狗在 (3, 4)
    t.w.areas = areas
    _可靠位置(t)
    t.arming.authz = SimpleNamespace(active=active)
    t.d.persons("A", present=True, count=1, nearest_m=4.0)
    t.w.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["authorized_person"]


@pytest.mark.parametrize("变化", ["到期或删除", "走出授权区", "定位丢了", "位置停更"])
def test_外审I1_I3_授权在场的这一回_人一直在_授权不适用了就补报一次P1(台, 变化):  # noqa: F811
    t = 台
    granted = {"on": True}
    _授权台(t, lambda zone: "Gardener" if granted["on"] and zone == "Back garden" else None)
    t.w.tick()
    assert len(t.w.alerts.raised) == 1, "授权还在:不重复记"
    if 变化 == "到期或删除":
        granted["on"] = False
    elif 变化 == "走出授权区":
        t.d.clients["A"].telemetry.pose.x = 50.0
    elif 变化 == "定位丢了":
        _可靠位置(t, loc_ok=False)
    else:
        _可靠位置(t, age_ms=60_000)
    t.w.tick()
    kinds = [a["kind"] for a in t.w.alerts.raised]
    assert kinds == ["authorized_person", "dog_sees_person"], kinds
    assert "授权已不适用" in t.w.alerts.raised[-1]["title"]
    t.w.tick()
    t.w.tick()
    assert len(t.w.alerts.raised) == 2, "补报只报一次"


def test_外审I1_补报写在库里_重启以后照样补_告警台报不出去下一拍再报(台, tmp_path):  # noqa: F811
    from d1max_site.sightings import PersonWatch
    t = 台
    granted = {"on": True}
    _授权台(t, lambda zone: "Gardener" if granted["on"] and zone == "Back garden" else None)
    granted["on"] = False
    w2 = PersonWatch(t.db, t.d, now_ms=lambda: t.ms[0], arming=t.arming, deterrence=t.det)
    w2.areas, w2.alerts = t.w.areas, None                  # 「重启」,告警台还没接上
    w2.tick()
    assert [r["kind"] for r in t.db.query("SELECT kind FROM person_sightings")] == ["p1"]
    w2.alerts = t.w.alerts
    w2.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["authorized_person", "dog_sees_person"]


def test_外审I3_位置说不清_只认全站授权(台):  # noqa: F811
    t = 台
    areas = AreaBook(t.db, now_ms=lambda: t.ms[0])
    areas.set("m", "1", "Back garden", SQUARE, by="admin")
    t.w.areas = areas
    _可靠位置(t, loc_ok=False)                               # 旧坐标还在花园里
    t.arming.authz = SimpleNamespace(
        active=lambda zone: "Gardener" if zone == "Back garden" else None)
    t.d.persons("A", present=True, count=1, nearest_m=4.0)
    t.w.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["dog_sees_person"], "旧坐标不能套授权"


def test_接口_看要登录_改要管理员_坏参数400(tmp_path):
    s = 站(tmp_path, alerts=True)
    try:
        s.api.areas = AreaBook(s.db, now_ms=lambda: NOW)
        s.api.authz = AuthzBook(s.db, now_ms=lambda: NOW)
        s.accounts.add("gina", PW, role="guard")
        admin = s.login()
        guard = s.req("POST", "/api/login", {"name": "gina", "password": PW})[1]["token"]
        code, d = s.req("POST", "/api/maps/m/1/areas", {"zone": "Back", "points": SQUARE},
                        token=admin)
        assert code == 200 and d["areas"][0]["zone"] == "Back"
        assert s.req("GET", "/api/maps/m/1/areas", token=guard)[0] == 200
        area = "/api/maps/m/1/areas"
        assert s.req("POST", area, {"zone": "X", "points": SQUARE}, token=guard)[0] == 403
        assert s.req("POST", area, {"zone": "X", "points": [[0, 0]]}, token=admin)[0] == 400
        assert s.req("POST", area, {"zone": "Nope", "remove": True}, token=admin)[0] == 404
        code, d = s.req("POST", "/api/lists/authorizations",
                        {"name": "Gardener", "zones": ["Back"], "start": "09:00", "end": "11:00"},
                        token=admin)
        assert code == 200 and d["authorizations"][0]["name"] == "Gardener"
        assert s.req("POST", "/api/lists/authorizations", {"name": "x", "start": "9", "end": "10"},
                     token=admin)[0] == 400
        assert s.req("POST", "/api/lists/authorizations", {"name": "y", "start": "09:00",
                                                           "end": "10:00"}, token=guard)[0] == 403
        eid = d["authorizations"][0]["id"]
        assert s.req("GET", "/api/lists/authorizations", token=guard)[0] == 200
        assert s.req("POST", f"/api/lists/authorizations/{eid}/remove", {}, token=admin)[0] == 200
        assert s.req("POST", f"/api/lists/authorizations/{eid}/remove", {}, token=admin)[0] == 404
    finally:
        s.close()


@pytest.fixture
async def 派单台(tmp_path):
    from test_site_dispatcher import 台子
    from test_site_incidents import IncidentDesk
    t = 台子(tmp_path)
    await t.start()
    t.desk = IncidentDesk(t.db, t.site, now_ms=t.clock)
    t.secret = t.desk.add_source("nvr-1")
    t.desk.set_intercept("gate", map_id="estate-1", map_version="7", x=1.0, y=0.0, yaw=0.0)
    t.desk.map_zone("front-yard", "gate")
    yield t
    await t.close()


async def test_外审I2_首条没派成_补派时防区有授权_不派不占狗(派单台, monkeypatch):
    import asyncio

    from test_site_incidents import _两台狗, _假派单, _签好
    t = 派单台
    await _两台狗(t)
    t.desk.arming = ArmingDesk(t.db, now_ms=t.clock)
    granted = {"on": False}
    t.desk.arming.authz = SimpleNamespace(
        active=lambda zone: "Gardener" if granted["on"] and zone == "front-yard" else None)
    fake = _假派单(results=("rejected", "accepted"))
    monkeypatch.setattr(t.site, "goto", fake)
    h1 = asyncio.ensure_future(t.desk.handle("nvr-1", _签好(t, "e1")))
    for _ in range(5):
        await asyncio.sleep(0)
    r2 = await asyncio.wait_for(t.desk.handle("nvr-1", _签好(t, "e2")), 2)
    r3 = await asyncio.wait_for(t.desk.handle("nvr-1", _签好(t, "e3")), 2)
    assert r2["outcome"] == r3["outcome"] == "merged"
    granted["on"] = True                                   # 首条还在等回执时加了授权
    fake.gate.set()
    r1 = await asyncio.wait_for(h1, 2)
    assert r1["outcome"] == "dispatch_failed"
    await asyncio.wait_for(t.desk.drain(), 2)
    rows = {r["event_id"]: r for r in t.desk.list()}
    assert rows["e2"]["outcome"] == rows["e3"]["outcome"] == "authorized"
    assert "Gardener" in rows["e2"]["note"]
    assert len(fake.calls) == 1, "补派没再叫狗"
