"""W53:开站清单(站点按数据判断每一项)、上手向导的记录(按账号存)。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_site_api import PW, _等, 站

from d1max_site.db import SiteDB
from d1max_site.setup import GuideError, read_guide, setup_view, write_guide


class 假派遣器:
    def __init__(self, robots: dict[str, tuple[str, str] | None]) -> None:
        self.registry = SimpleNamespace(list=lambda: [SimpleNamespace(robot_id=r, revoked=False)
                                                      for r in robots])
        self.clients = {r: SimpleNamespace(m=m) for r, m in robots.items()}
        self.zones = SimpleNamespace(confirmed_current=lambda m, v: (m, v) in self.ok)
        self.ok: set[tuple[str, str]] = set()

    @staticmethod
    def _loaded(c):
        return c.m if c is not None else None


def _项(v):
    return {i["id"]: i for i in v["items"]}


def test_空站点什么都没做_做一样亮一样(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    v = setup_view(db, 假派遣器({}))
    assert v["done"] == 0 and v["total"] == 9
    assert [i["id"] for i in v["items"]][:3] == ["robots", "map", "zones"]
    d = 假派遣器({"A": ("m1", "3"), "B": ("m1", "3")})
    items = _项(setup_view(db, d))
    assert items["robots"]["done"] and items["robots"]["count"] == 2
    assert items["map"]["done"] and not items["zones"]["done"]
    assert items["zones"]["maps"] == ["m1:3"]
    assert items["standby"]["robots"] == ["A", "B"]
    d.ok.add(("m1", "3"))
    with db.tx() as c:
        c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                  "is_default) VALUES ('A','s','m1','3',0,0,0,1)")
    items = _项(setup_view(db, d))
    assert items["zones"]["done"] and items["standby"]["robots"] == ["B"]
    db.close()


def test_标成不用了的项不算没做(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    v = setup_view(db, 假派遣器({}), skip={"arming", "push"})
    it = _项(v)
    assert it["arming"]["skipped"] and it["push"]["skipped"] and v["done"] == 2
    db.close()


def test_向导记录_按账号_合并写_不认的键拒(tmp_path):
    from d1max_site.accounts import Accounts
    db = SiteDB(tmp_path / "s.db")
    acc = Accounts(db, now_ms=lambda: 0)
    acc.add("g", "pass-word-123", role="guard")
    assert read_guide(acc, "g") == {"tour": 0, "web_tour": 0, "setup_hidden": False, "skip": []}
    write_guide(acc, "g", {"tour": 1})
    assert write_guide(acc, "g", {"skip": ["arming"], "web_tour": 1}) == {
        "tour": 1, "web_tour": 1, "setup_hidden": False, "skip": ["arming"]}
    for bad in ({}, {"x": 1}, {"tour": "1"}, {"tour": True}, {"skip": ["nope"]},
                {"tour": -1}, {"setup_hidden": 1}):
        with pytest.raises(GuideError):
            write_guide(acc, "g", bad)
    db.close()


def test_接口_我的向导记录谁都能写_清单只给管理员(tmp_path):
    s = 站(tmp_path)
    try:
        s.accounts.add("vic", PW, role="viewer")
        vic = s.req("POST", "/api/login", {"name": "vic", "password": PW})[1]["token"]
        assert s.req("GET", "/api/me", token=vic)[1]["guide"]["tour"] == 0
        assert s.req("POST", "/api/me/guide", {"tour": 2}, token=vic)[0] == 200
        assert s.req("GET", "/api/me", token=vic)[1]["guide"]["tour"] == 2
        assert s.req("POST", "/api/me/guide", {"bad": 1}, token=vic)[0] == 400
        assert s.req("GET", "/api/setup", token=vic)[0] == 403
        alice = s.login()
        _等(lambda: s.req("GET", "/api/robots/A", token=alice)[1].get("fresh"))
        code, d = s.req("GET", "/api/setup", token=alice)
        assert code == 200 and _项(d)["robots"]["done"] and _项(d)["accounts"]["done"]
    finally:
        s.close()
