"""布防模式(W20):布防、在家、访客;撤防的防区来了入侵只记账、不派狗、不报告警;谁能切。"""

from __future__ import annotations

import json

import pytest
from test_site_api import PW, 站
from test_site_dispatcher import 台子
from test_site_incidents import _gotos, _报

from d1max_site.db import SiteDB
from d1max_site.incidents import IncidentDesk
from d1max_site.modes import ArmingDesk, ModeError


class 钟:
    def __init__(self) -> None:
        self.t = 1_000_000

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    clock = 钟()
    pushed: list[dict] = []
    desk = ArmingDesk(db, now_ms=clock, publish=pushed.append)
    desk.clock, desk.pushed = clock, pushed
    yield desk
    db.close()


def test_新站点默认布防_所有防区都布防(台):
    assert 台.view()["mode"] == "armed"
    assert 台.armed("front-yard") == (True, "armed")
    台.set_zone_home("garden", False)
    assert 台.armed("garden") == (True, "armed"), "布防时在家撤防的防区也布防"


def test_在家_标了在家撤防的撤_没配过的防区照样布防(台):
    台.set_zone_home("garden", False)
    台.set_mode("home", by="olga")
    assert 台.armed("garden") == (False, "home")
    assert 台.armed("front-gate") == (True, "home"), "没配过的防区一律布防"
    台.set_zone_home("garden", True)
    assert 台.armed("garden") == (True, "home")


def test_访客_在家的基础上再撤指定防区_到点退回原来的模式(台):
    台.set_zone_home("garden", False)
    台.set_mode("home", by="olga")
    台.set_mode("visitor", by="olga", zones=["drive"], minutes=60)
    v = 台.view()
    assert v["mode"] == "visitor" and v["prev_mode"] == "home"
    assert v["until_ms"] == 台.clock.t + 3_600_000
    assert 台.armed("drive")[0] is False and 台.armed("garden")[0] is False
    assert 台.armed("front-gate")[0] is True
    台.clock.t += 3_600_000
    # 还没 tick:过了点就按退回算,不等那一拍
    assert 台.armed("drive") == (True, "home")
    assert 台.tick() is True
    v = 台.view()
    assert v["mode"] == "home" and v["visitor_zones"] == [] and v["until_ms"] is None
    assert v["set_by"] == "site:visitor_expired"
    assert 台.tick() is False
    assert 台.pushed[-1] == {"kind": "mode", "mode": v}


def test_访客叠访客_退回的还是开访客之前的模式(台):
    台.set_mode("visitor", by="olga", zones=["drive"], minutes=30)
    台.set_mode("visitor", by="olga", zones=["drive", "garden"], minutes=90)
    assert 台.view()["prev_mode"] == "armed"
    台.clock.t += 90 * 60_000
    台.tick()
    assert 台.view()["mode"] == "armed"


def test_访客不填时长默认4小时_时长和防区不合规矩都拒(台):
    台.set_mode("visitor", by="olga")
    assert 台.view()["until_ms"] == 台.clock.t + 4 * 3_600_000
    for bad in (0, 24 * 60 + 1, True, 1.5, "60"):
        with pytest.raises(ModeError):
            台.set_mode("visitor", by="olga", minutes=bad)
    with pytest.raises(ModeError):
        台.set_mode("visitor", by="olga", zones="drive")
    with pytest.raises(ModeError):
        台.set_mode("home", by="olga", minutes=30)
    with pytest.raises(ModeError):
        台.set_mode("off", by="olga")
    with pytest.raises(ModeError):
        台.set_zone_home("garden", "no")


def test_重启之后模式还在(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    ArmingDesk(db, now_ms=钟()).set_mode("home", by="olga")
    db.close()
    db = SiteDB(tmp_path / "site.db")
    assert ArmingDesk(db, now_ms=钟()).view()["mode"] == "home"
    db.close()


def test_手机看的防区列表_带每个防区现在布不布防(台):
    台.set_zone_home("garden", False)
    台.set_mode("home", by="olga")
    zs = {z["zone"]: z for z in 台.view()["zones"]}
    assert zs["garden"] == {"zone": "garden", "home_armed": False, "armed": False}


# ------------------------------------------------------------ 入侵派遣


@pytest.fixture
async def 事件台(tmp_path):
    """同 ``test_site_incidents`` 的台子。"""
    t = 台子(tmp_path)
    await t.start()
    t.desk = IncidentDesk(t.db, t.site, now_ms=t.clock)
    t.secret = t.desk.add_source("nvr-1")
    t.desk.set_intercept("gate", map_id="estate-1", map_version="7", x=1.0, y=0.0, yaw=0.0)
    t.desk.map_zone("front-yard", "gate")
    yield t
    await t.close()


async def test_撤防的防区来了入侵_只记账_不派狗_不报告警(事件台):
    t = 事件台
    told = []
    t.desk.on_outcome = told.append
    t.desk.arming = ArmingDesk(t.db, now_ms=t.clock)
    t.desk.arming.set_zone_home("front-yard", False)
    t.desk.arming.set_mode("home", by="olga")
    await t.run(12)
    r = await _报(t, "e1")
    assert r["outcome"] == "disarmed" and "在家" in r["note"]
    assert not _gotos(t) and told == []
    assert t.desk.retell() == 0, "撤防的不补报"
    t.desk.arming.set_mode("armed", by="gina")
    r = await _报(t, "e2")
    assert r["outcome"] == "dispatched" and len(_gotos(t)) == 1
    assert [x["outcome"] for x in told] == ["dispatched"]


async def test_访客撤的防区不派_别的防区照派(事件台):
    t = 事件台
    t.desk.map_zone("back", "gate")
    t.desk.arming = ArmingDesk(t.db, now_ms=t.clock)
    t.desk.arming.set_mode("visitor", by="olga", zones=["front-yard"], minutes=60)
    await t.run(12)
    assert (await _报(t, "e1"))["outcome"] == "disarmed"
    assert (await _报(t, "e2", zone="back"))["outcome"] == "dispatched"


# ------------------------------------------------------------ 接口与角色


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard", display_name="王保安")
    s.accounts.add("olga", PW, role="owner")
    s.api.arming = ArmingDesk(s.db, now_ms=s.api._now, publish=s.disp.feed.publish)
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"], d


def test_保安只能切到布防_业主随便切_防区配置只有管理员(站点):
    s = 站点
    (gina, _), (olga, _), (alice, _) = _登(s, "gina"), _登(s, "olga"), _登(s, "alice")
    assert s.req("GET", "/api/mode", token=gina) == (200, s.api.arming.view())
    assert s.req("POST", "/api/mode", {"mode": "home"}, token=gina)[0] == 403
    assert s.req("POST", "/api/mode", {"mode": "visitor", "minutes": 60}, token=gina)[0] == 403
    code, d = s.req("POST", "/api/mode", {"mode": "visitor", "zones": ["drive"], "minutes": 60},
                    token=olga)
    assert code == 200 and d["mode"] == "visitor" and d["set_by"] == "olga"
    assert s.req("POST", "/api/mode", {"mode": "armed"}, token=gina)[1]["mode"] == "armed"
    assert s.req("POST", "/api/mode", {"mode": "visitor", "minutes": 0}, token=olga)[0] == 400
    assert s.req("POST", "/api/mode/zones", {"zone": "garden", "home_armed": False},
                 token=olga)[0] == 403
    code, d = s.req("POST", "/api/mode/zones", {"zone": "garden", "home_armed": False},
                    token=alice)
    assert code == 200 and {"zone": "garden", "home_armed": False, "armed": True} in d["zones"]
    actions = [(a["actor"], a["action"], a["target"]) for a in s.api.audit.list()]
    assert ("olga", "POST /api/mode", "visitor") in actions
    assert ("gina", "POST /api/mode", "armed") in actions


def test_没开布防模式的站点_接口404(站点):
    s = 站点
    s.api.arming = None
    assert s.req("GET", "/api/mode", token=_登(s, "alice")[0])[0] == 404


def test_业主能确认告警_保安照旧(站点):
    from d1max_site.permissions import HANDLE_ALERTS, SET_MODE, allowed
    assert allowed("owner", HANDLE_ALERTS) and allowed("guard", HANDLE_ALERTS)
    assert not allowed("guard", SET_MODE)


def test_显示名_登录回包带_改显示名不吊销会话_改角色照样吊销(站点):
    s = 站点
    gina, d = _登(s, "gina")
    assert d["display_name"] == "王保安"
    assert _登(s, "olga")[1]["display_name"] == "olga", "没设就是账号名"
    alice, _ = _登(s, "alice")
    code, d = s.req("POST", "/api/accounts/gina", {"display_name": "王大保安"}, token=alice)
    assert code == 200 and {"name": "gina", "role": "guard", "disabled": False,
                            "display_name": "王大保安"} in d["accounts"]
    assert s.req("GET", "/api/robots", token=gina)[0] == 200, "只改显示名不吊销"
    assert s.req("POST", "/api/accounts/gina", {"display_name": "x\n"}, token=alice)[0] == 400
    assert s.req("POST", "/api/accounts/gina", {"role": "owner"}, token=alice)[0] == 200
    assert s.req("GET", "/api/robots", token=gina)[0] == 401
    code, d = s.req("POST", "/api/accounts", {"name": "ben", "password": PW, "role": "guard",
                                             "display_name": "李四"}, token=alice)
    assert code == 200 and any(a["display_name"] == "李四" for a in d["accounts"])


def test_切模式推给手机(站点):
    s = 站点
    sub = s.disp.feed.subscribe()
    s.req("POST", "/api/mode", {"mode": "home"}, token=_登(s, "olga")[0])
    frames = []
    while (f := sub.get(0.5)) is not None:
        frames.append(f)
    assert any(f["kind"] == "mode" and f["mode"]["mode"] == "home" for f in frames)
    json.dumps(frames)                                       # 推出去的能变成 JSON
