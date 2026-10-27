"""W44 第二期:工程师临时账号、删账号、最后登录、敏感操作再输一次密码
(设计 §3、§5.1「用户」、§5.2 第 4 条)。

工程师 = 业主开的、4 小时或 24 小时到点自动失效的账号;权限等于业主,但不能管账号(不能给自己续命)。
"""

from __future__ import annotations

import pytest
from test_site_api import PW, 站

from d1max_site.accounts import Accounts, AuthError, LockedOut
from d1max_site.db import SiteDB
from d1max_site.permissions import MANAGE_ACCOUNTS, ROLES, allowed


class 钟:
    def __init__(self) -> None:
        self.ms = 1_800_000_000_000

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def acc(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    c = 钟()
    a = Accounts(db, now_ms=c)
    a.add("alice", PW, role="admin")
    yield a, c
    db.close()


def test_工程师的权限是业主减去管账号():
    assert ROLES["engineer"] == ROLES["admin"] - {MANAGE_ACCOUNTS}
    assert not allowed("engineer", MANAGE_ACCOUNTS)


def test_开工程师账号_口令只回一次(acc):
    a, c = acc
    name, pw, exp = a.add_engineer("李工", 4, by="alice")
    assert name.startswith("eng-") and exp == c.ms + 4 * 3_600_000
    row = next(r for r in a.list() if r["name"] == name)
    assert row["role"] == "engineer" and row["display_name"] == "李工"
    assert row["expires_ms"] == exp and row["expired"] is False
    assert a.check(a.login(name, pw)) == name


def test_工程师账号名重了加后缀(acc):
    a, _ = acc
    n1 = a.add_engineer("李工", 4, by="alice")[0]
    n2 = a.add_engineer("王工", 24, by="alice")[0]
    assert n1 != n2 and n2.startswith(n1)


@pytest.mark.parametrize("hours", [1, 48, "4", 4.0, True])
def test_工程师只收4或24小时(acc, hours):
    with pytest.raises(AuthError):
        acc[0].add_engineer("李工", hours, by="alice")


def test_工程师到点_令牌立刻失效_登录不了(acc):
    a, c = acc
    name, pw, exp = a.add_engineer("李工", 4, by="alice")
    c.ms = exp - 60_000                     # 快到点时登录(闲置期 30 分钟,不影响这里)
    tok = a.login(name, pw)
    assert a.check(tok) == name
    c.ms = exp
    assert a.check(tok) is None
    with pytest.raises(AuthError):
        a.login(name, pw)
    assert next(r for r in a.list() if r["name"] == name)["expired"] is True


def test_普通加账号_改角色都不收engineer(acc):
    a, _ = acc
    with pytest.raises(AuthError):
        a.add("bob", PW, role="engineer")
    a.add("gina", PW, role="guard")
    with pytest.raises(AuthError):
        a.update("gina", role="engineer")
    with pytest.raises(AuthError):
        a.set_role("gina", "engineer")


def test_工程师账号不能改角色_能提前关(acc):
    a, _ = acc
    name, pw, _ = a.add_engineer("李工", 4, by="alice")
    for bad in ({"role": "admin"}, {"password": "another-long-pw"}, {"disabled": False}):
        with pytest.raises(AuthError):
            a.update(name, **bad)
    tok = a.login(name, pw)
    a.update(name, disabled=True)
    assert a.check(tok) is None


def test_删账号(acc):
    a, _ = acc
    a.add("gina", PW, role="guard")
    tok = a.login("gina", PW)
    a.remove("gina")
    assert "gina" not in a.names()
    assert a.check(tok) is None
    with pytest.raises(AuthError):
        a.remove("gina")


def test_最后一个业主删不掉(acc):
    a, _ = acc
    with pytest.raises(AuthError, match="最后一个"):
        a.remove("alice")
    a.add("bob", PW, role="admin")
    a.remove("alice")
    assert a.names() == ["bob"]


def test_最后登录时间(acc):
    a, c = acc
    assert a.list()[0]["last_login_ms"] is None
    a.login("alice", PW)
    assert a.list()[0]["last_login_ms"] == c.ms


def test_再输一次密码_跟登录同一个计数(acc):
    a, _ = acc
    assert a.verify("alice", PW) is True
    for _ in range(5):              # 跟登录一样:第五次还是「不对」,第六次起锁
        assert a.verify("alice", "wrong-wrong-wrong") is False
    with pytest.raises(LockedOut):
        a.verify("alice", "wrong-wrong-wrong")
    with pytest.raises(LockedOut):
        a.login("alice", PW)


# ------------------------------------------------------------ 接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name, pw=PW):
    code, d = s.req("POST", "/api/login", {"name": name, "password": pw})
    assert code == 200, d
    return d["token"]


def _开(s, tok, **kw):
    body = {"display_name": "李工", "hours": 4, "confirm_password": PW} | kw
    return s.req("POST", "/api/accounts/engineer", body, token=tok)


def test_接口_开工程师账号(站点):
    s = 站点
    alice = _登(s, "alice")
    code, d = _开(s, alice)
    assert code == 200, d
    assert d["name"].startswith("eng-") and len(d["password"]) >= 12 and d["expires_ms"] > 0
    eng = _登(s, d["name"], d["password"])
    code, me = s.req("GET", "/api/me", token=eng)
    assert me["role"] == "engineer"
    audit = s.req("GET", "/api/audit", token=alice)[1]["audit"]
    row = next(r for r in audit if "/api/accounts/engineer" in r["action"])
    assert d["password"] not in str(row)


def test_接口_开工程师账号要再输密码(站点):
    s = 站点
    alice = _登(s, "alice")
    assert _开(s, alice, confirm_password=None)[0] == 403
    for _ in range(5):
        code, d = _开(s, alice, confirm_password="wrong-wrong-wrong")
        assert code == 403 and "密码不对" in d["error"]
    assert _开(s, alice, confirm_password="wrong-wrong-wrong")[0] == 429


def test_接口_工程师只收4或24小时(站点):
    alice = _登(站点, "alice")
    for h in (1, 48, "4"):
        assert _开(站点, alice, hours=h)[0] == 400


def test_接口_工程师到点_令牌立刻失效(站点):
    s = 站点
    alice = _登(s, "alice")
    d = _开(s, alice)[1]
    eng = _登(s, d["name"], d["password"])
    assert s.req("GET", "/api/robots", token=eng)[0] == 200
    real = s.accounts._now
    s.accounts._now = lambda: real() + 4 * 3_600_000 + 1000
    try:
        assert s.req("GET", "/api/robots", token=eng)[0] == 401
        assert s.req("POST", "/api/login", {"name": d["name"], "password": d["password"]})[0] \
            == 401
    finally:
        s.accounts._now = real


def test_接口_工程师能做业主的事_不能管账号(站点):
    s = 站点
    alice = _登(s, "alice")
    d = _开(s, alice)[1]
    eng = _登(s, d["name"], d["password"])
    assert s.req("POST", "/api/duty-settings", {"stale_hours": 3}, token=eng)[0] == 200
    assert s.req("GET", "/api/audit", token=eng)[0] == 200
    assert s.req("GET", "/api/accounts", token=eng)[0] == 403
    assert s.req("POST", "/api/accounts", {"name": "x", "password": PW, "role": "admin"},
                 token=eng)[0] == 403
    assert _开(s, eng)[0] == 403
    assert s.req("POST", f"/api/accounts/{d['name']}", {"disabled": False}, token=eng)[0] == 403


def test_接口_工程师账号不能改角色(站点):
    s = 站点
    alice = _登(s, "alice")
    d = _开(s, alice)[1]
    assert s.req("POST", f"/api/accounts/{d['name']}", {"role": "admin"}, token=alice)[0] == 400
    assert s.req("POST", f"/api/accounts/{d['name']}", {"disabled": True}, token=alice)[0] == 200
    assert s.req("POST", "/api/accounts", {"name": "bob", "password": PW, "role": "engineer"},
                 token=alice)[0] == 400


def test_接口_删账号(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    code, d = s.req("POST", "/api/accounts/gina/remove", {}, token=alice)
    assert code == 200 and all(a["name"] != "gina" for a in d["accounts"])
    assert s.req("GET", "/api/robots", token=gina)[0] == 401
    assert s.req("POST", "/api/accounts/gina/remove", {}, token=alice)[0] == 404


def test_接口_不能删自己_最后一个业主(站点):
    s = 站点
    alice = _登(s, "alice")
    code, d = s.req("POST", "/api/accounts/alice/remove", {}, token=alice)
    assert code == 400


def test_接口_列表带最后登录和到期(站点):
    s = 站点
    alice = _登(s, "alice")
    rows = s.req("GET", "/api/accounts", token=alice)[1]["accounts"]
    me = next(r for r in rows if r["name"] == "alice")
    assert me["last_login_ms"] and me["expires_ms"] is None and me["expired"] is False


def test_接口_审计按人查_标出工程师(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    s.req("POST", "/api/shift-notes", {"text": "灯坏了"}, token=gina)
    d = _开(s, alice)[1]
    eng = _登(s, d["name"], d["password"])
    s.req("POST", "/api/duty-settings", {"stale_hours": 3}, token=eng)
    rows = s.req("GET", "/api/audit?actor=gina", token=alice)[1]["audit"]
    assert rows and all(r["actor"] == "gina" for r in rows)
    rows = s.req("GET", f"/api/audit?actor={d['name']}", token=alice)[1]["audit"]
    assert rows and all(r["actor_role"] == "engineer" for r in rows)
