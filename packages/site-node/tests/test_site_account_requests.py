"""W52:App 上申请账号,管理员批准时选权限(默认只看)。真 HTTP(夹具同 ``test_site_api.py``)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, 站

from d1max_site import account_requests as ar


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name, pw=PW):
    return s.req("POST", "/api/login", {"name": name, "password": pw})


def _申请(s, name="wangxm", display="王小明", pw="pass-word-123"):
    return s.req("POST", "/api/account-requests", {"name": name, "display": display,
                                                    "password": pw})


def test_申请_不用登录_批准时选权限默认只看_用自己的密码登录(站点):
    s = 站点
    code, d = _申请(s)
    assert code == 200 and d["request_id"], d
    code, d = _登(s, "wangxm", "pass-word-123")
    assert code == 401 and "等管理员批准" in d["error"], d
    alice = s.login()
    code, d = s.req("GET", "/api/account-requests", token=alice)
    [r] = d["requests"]
    assert (r["name"], r["display"]) == ("wangxm", "王小明") and "pw_hash" not in r
    assert s.req("POST", f"/api/account-requests/{r['id']}/approve", {}, token=alice)[0] == 403, \
        "要再输一次密码"
    code, d = s.req("POST", f"/api/account-requests/{r['id']}/approve",
                    {"confirm_password": PW}, token=alice)
    assert code == 200 and d == {"name": "wangxm", "role": "viewer"}, d
    code, d = _登(s, "wangxm", "pass-word-123")
    assert code == 200 and d["role"] == "viewer", d
    assert s.req("GET", "/api/account-requests", token=alice)[1]["requests"] == []
    accts = s.req("GET", "/api/accounts", token=alice)[1]["accounts"]
    assert any(a["name"] == "wangxm" and a["display_name"] == "王小明" for a in accts)


def test_批准时能选保安_不能选工程师_拒了登录说被拒(站点):
    s = 站点
    alice = s.login()
    _申请(s, "a1")
    _申请(s, "a2")
    rs = {r["name"]: r["id"] for r in s.req("GET", "/api/account-requests", token=alice)[1][
        "requests"]}
    code, d = s.req("POST", f"/api/account-requests/{rs['a1']}/approve",
                    {"confirm_password": PW, "role": "engineer"}, token=alice)
    assert code == 400
    code, d = s.req("POST", f"/api/account-requests/{rs['a1']}/approve",
                    {"confirm_password": PW, "role": "guard"}, token=alice)
    assert code == 200 and d["role"] == "guard"
    assert s.req("POST", f"/api/account-requests/{rs['a2']}/reject", {}, token=alice)[0] == 200
    code, d = _登(s, "a2", "pass-word-123")
    assert code == 401 and "拒" in d["error"]
    assert s.req("POST", f"/api/account-requests/{rs['a2']}/approve",
                 {"confirm_password": PW}, token=alice)[0] == 409, "处理过的不能再批"


def test_只有管理员能看能批(站点):
    s = 站点
    gina = _登(s, "gina")[1]["token"]
    assert s.req("GET", "/api/account-requests", token=gina)[0] == 403


def test_名字已有_已在申请_密码太短_名字不合规_都拒(站点):
    s = 站点
    assert _申请(s, "gina")[0] == 409
    assert _申请(s, "bob")[0] == 200
    assert _申请(s, "bob")[0] == 409
    assert _申请(s, "c1", pw="short")[0] == 400
    assert _申请(s, "bad name!")[0] == 400
    assert _申请(s, "engineer")[0] == 400
    assert _申请(s, "eng-20270101-0800")[0] == 400
    assert _申请(s, "d1", display="  ")[0] == 400


def test_同一地址每分钟三次_最多挂二十条(站点, monkeypatch):
    s = 站点
    for i in range(3):
        assert _申请(s, f"u{i}")[0] == 200
    assert _申请(s, "u3")[0] == 429
    monkeypatch.setattr(ar, "RATE_PER_MIN", 100)
    for i in range(3, 20):
        assert _申请(s, f"u{i}")[0] == 200
    assert _申请(s, "u20")[0] == 429, "最多挂 20 条"


def test_七天没人批作废_登录说过期了_有提醒批完解决(tmp_path):
    from d1max_site.db import SiteDB

    class 告警:
        def __init__(self):
            self.raised, self.resolved = [], []

        def raise_alert(self, **kw):
            self.raised.append(kw)

        def resolve_all(self, robot, kind, *, who):
            self.resolved.append((robot, kind, who))

    from d1max_site.accounts import Accounts
    db = SiteDB(tmp_path / "s.db")
    clock = [1_000_000]
    a = 告警()
    desk = ar.AccountRequests(Accounts(db, now_ms=lambda: clock[0]), now_ms=lambda: clock[0],
                              alerts=a)
    desk.submit({"name": "x1", "display": "甲", "password": "pass-word-123"}, source="1.2.3.4")
    assert a.raised[0]["kind"] == "account_request" and "甲" in a.raised[0]["title"]
    clock[0] += ar.TTL_MS + 1
    assert desk.pending() == [] and "过期" in desk.login_hint("x1")
    desk.submit({"name": "x2", "display": "乙", "password": "pass-word-123"}, source="1.2.3.4")
    [r] = desk.pending()
    desk.reject(r["id"], by="alice")
    assert a.resolved == [("site", "account_request", "alice")]
    db.close()
