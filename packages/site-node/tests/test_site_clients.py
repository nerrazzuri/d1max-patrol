"""App 版本号上报(C-B4):登录时报自己是哪一版,记在会话上;管理员在版本一览里看。"""
from __future__ import annotations

import json
import sqlite3

import pytest
from test_site_api import PW, 站

from d1max_site.accounts import Accounts, clean_client
from d1max_site.db import SCHEMA_VERSION, SiteDB

APP = {"kind": "app", "version": "1.0.0", "api_level": 1, "platform": "android"}


@pytest.mark.parametrize("obj,want", [
    (APP, APP),
    ({"kind": "web"}, {"kind": "web"}),
    ({"kind": "app", "version": "1.2.3+45", "extra": "x", "token": "secret"},
     {"kind": "app", "version": "1.2.3+45"}),
    ({"kind": "app", "version": "a" * 32}, {"kind": "app", "version": "a" * 32}),
    ({"kind": "app", "version": "a" * 33, "platform": "iOS"}, {"kind": "app", "platform": "iOS"}),
    ({"kind": "app", "version": "1.0 <b>", "platform": "", "api_level": "1"}, {"kind": "app"}),
    ({"kind": "app", "version": ".hidden", "api_level": True}, {"kind": "app"}),
    ({"kind": "app", "version": 5, "api_level": -1}, {"kind": "app"}),
    ({"kind": "app", "api_level": 0}, {"kind": "app", "api_level": 0}),
    ({"kind": "app", "api_level": 9999}, {"kind": "app", "api_level": 9999}),
    ({"kind": "app", "api_level": 10000}, {"kind": "app"}),
    ({"kind": "app", "api_level": 1.0}, {"kind": "app"}),
])
def test_报的程序信息_只留认得的几样(obj, want):
    assert json.loads(clean_client(obj)) == want


@pytest.mark.parametrize("obj", [None, "app", 5, [], {}, {"version": "1"}, {"kind": "robot"},
                                 {"kind": ["app"]}, {"kind": "APP"}])
def test_看不懂的_不记(obj):
    assert clean_client(obj) == ""


@pytest.fixture
def 账(tmp_path):
    now = [1_800_000_000_000]
    db = SiteDB(tmp_path / "s.db")
    acc = Accounts(db, now_ms=lambda: now[0])
    acc.add("alice", PW, role="admin")
    acc.add("gina", PW, role="guard")
    yield acc, now, db
    db.close()


def test_登录记下是哪一版_最近用过的在前_没报的值守令牌的不列(账):
    acc, now, _ = 账
    acc.login("alice", PW, APP)
    now[0] += 1000
    t2 = acc.login("gina", PW, {**APP, "version": "0.9.0", "platform": "ios"})
    now[0] += 1000
    acc.login("gina", PW)                               # 老 App:不报
    acc.login("alice", PW, {"bogus": 1})                # 看不懂:照样登录,不记
    acc.issue_watch_token("gina")
    got = acc.clients()
    assert [(c["name"], c["version"], c["platform"]) for c in got] == [
        ("gina", "0.9.0", "ios"), ("alice", "1.0.0", "android")]
    assert got[0]["kind"] == "app" and got[0]["api_level"] == 1
    assert got[0]["created_at"] == got[0]["last_used"] == 1_800_000_001_000
    now[0] += 5000
    assert acc.check(t2) is not None                    # 用了一下:挪到最近
    assert acc.clients()[0]["last_used"] == now[0]


def test_退出了_过期了_账号停用了_都不再列(账):
    acc, now, _ = 账
    t1 = acc.login("alice", PW, APP)
    acc.login("gina", PW, APP)
    acc.logout(t1)
    assert [c["name"] for c in acc.clients()] == ["gina"]
    acc.set_disabled("gina", True)
    assert acc.clients() == []
    acc.set_disabled("gina", False)
    acc.login("gina", PW, APP)
    now[0] += acc.idle_ms
    assert len(acc.clients()) == 1, "正好到闲置期:还算"
    now[0] += 1
    assert acc.clients() == []


def test_库里那一格被弄坏了_跳过_不炸(账):
    acc, _, db = 账
    acc.login("alice", PW, APP)
    acc.login("gina", PW, APP)
    db.query("UPDATE sessions SET client='{not json' WHERE name='alice'")
    assert [c["name"] for c in acc.clients()] == ["gina"]


def test_老库升级_补上那一列_老会话照样能用(tmp_path):
    p = tmp_path / "s.db"
    db = SiteDB(p)
    acc = Accounts(db, now_ms=lambda: 1_800_000_000_000)
    acc.add("alice", PW, role="admin")
    tok = acc.login("alice", PW)
    db.close()
    c = sqlite3.connect(p)
    c.execute("ALTER TABLE sessions DROP COLUMN client")
    c.execute("UPDATE meta SET value='53' WHERE key='schema'")
    c.commit()
    c.close()
    db = SiteDB(p)
    try:
        assert "client" in {r[1] for r in db.query("PRAGMA table_info(sessions)")}
        assert db.query("SELECT value FROM meta WHERE key='schema'")[0]["value"] == str(
            SCHEMA_VERSION) == "55"
        acc = Accounts(db, now_ms=lambda: 1_800_000_000_000)
        assert acc.check(tok) is not None and acc.clients() == []
    finally:
        db.close()


def test_接口_App登录报版本_管理员在版本一览里看得到_别人看不到名单(tmp_path):
    s = 站(tmp_path)
    try:
        s.accounts.add("gina", PW, role="guard")
        code, d = s.req("POST", "/api/login", {"name": "gina", "password": PW, "client": APP})
        assert code == 200
        gina = d["token"]
        # 冒充网页的不记;乱报的照样登录
        assert s.req("POST", "/api/login", {"name": "gina", "password": PW,
                                            "client": {"kind": "web"}})[0] == 200
        assert s.req("POST", "/api/login", {"name": "gina", "password": PW,
                                            "client": "<script>"})[0] == 200
        code, d = s.req("POST", "/api/login", {"name": "alice", "password": PW})
        admin = d["token"]
        code, v = s.req("GET", "/api/versions", token=admin)
        assert code == 200
        assert [(c["name"], c["kind"], c["version"], c["api_level"], c["platform"])
                for c in v["clients"]] == [("gina", "app", "1.0.0", 1, "android")]
        code, v = s.req("GET", "/api/versions", token=gina)
        assert code == 200 and "clients" not in v and "site" in v
    finally:
        s.close()
