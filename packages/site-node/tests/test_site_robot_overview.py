"""W44 第二期:机器人概览(「机器人」页的卡片、「家里的状况」的小卡片)、起名、解绑要再输密码。"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_site_api import PW, 站

from d1max_site.db import SiteDB
from d1max_site.registry import Registry, RegistryError
from d1max_site.robot_overview import overview

KL = ZoneInfo("Asia/Kuala_Lumpur")


def kl(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=KL).timestamp() * 1000)


NOW = kl(2026, 10, 27, 12)


class 假派遣:
    def __init__(self, db) -> None:
        self.registry = Registry(db, site_id="site1")
        self.clients: dict = {}
        self.fresh: set[str] = set()
        self.devices: dict = {}

    def _fresh(self, c) -> bool:
        return c is not None and c.rid in self.fresh

    def devices_view(self, rid):
        return self.devices.get(rid)


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    d = 假派遣(db)
    d.registry.enroll("A", fingerprint="sha256:a", issued_at=0, expires_at=NOW + 10**9)
    d.clients["A"] = SimpleNamespace(rid="A", telemetry=SimpleNamespace(battery_pct=76.4),
                                     capabilities=SimpleNamespace(agent="0.9.1", tasks={}),
                                     status=SimpleNamespace(online=True))
    d.fresh.add("A")
    yield d, db
    db.close()


def _cmd(db, i, at, kind="patrol", ack="accepted", robot="A"):
    with db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, ack_result) VALUES (?,?,?,?,?,?,?,?)",
                  (f"c{i}", f"t{i}", robot, kind, "{}", "alice", at, ack))


def _alert(db, key, kind, first, resolved=None, robot="A"):
    with db.tx() as c:
        c.execute("INSERT INTO alerts(key, level, kind, robot, title, detail, first_ms, last_ms, "
                  "count, acked_by, resolved_by, resolved_ms, escalated) "
                  "VALUES (?,?,?,?,?,?,?,?,1,'','',?,0)",
                  (key, "P1", kind, robot, kind, "", first, first, resolved))


def _one(d, db, **kw):
    return next(r for r in overview(d, db, now_ms=NOW, tz=KL, **kw) if r["robot_id"] == "A")


def test_概览_今天巡了几趟(台):
    d, db = 台
    _cmd(db, 1, kl(2026, 10, 26, 23))
    _cmd(db, 2, kl(2026, 10, 27, 8))
    _cmd(db, 3, kl(2026, 10, 27, 9), ack="rejected")
    _cmd(db, 4, kl(2026, 10, 27, 10), kind="goto")
    _cmd(db, 5, kl(2026, 10, 27, 11), ack=None)
    assert _one(d, db)["patrols_today"] == 2


def test_概览_今天按站点时区(台):
    d, db = 台
    _cmd(db, 1, kl(2026, 10, 27, 0, 30))          # 吉隆坡今天 00:30 = UTC 昨天 16:30
    assert _one(d, db)["patrols_today"] == 1
    got = next(r for r in overview(d, db, now_ms=NOW, tz=ZoneInfo("Asia/Tokyo"))
               if r["robot_id"] == "A")
    assert got["patrols_today"] == 1
    got = next(r for r in overview(d, db, now_ms=NOW, tz=ZoneInfo("America/New_York"))
               if r["robot_id"] == "A")
    assert got["patrols_today"] == 0                # 纽约那边 10-27 00:00 之后才算


def test_概览_毛病(台):
    d, db = 台
    _alert(db, "A/stuck#1", "stuck", NOW - 3_600_000)
    _alert(db, "A/fallen#1", "fallen", NOW - 7_200_000, resolved=NOW - 3_000_000)
    d.devices["A"] = {"items": [{"id": "front_camera", "state": "offline"},
                                {"id": "lidar", "state": "online"}], "stale": False}
    r = _one(d, db)
    assert [p["kind"] for p in r["problems"] if "kind" in p] == ["stuck"]
    assert {"device": "front_camera", "state": "offline"} in r["problems"]
    assert [p["kind"] for p in r["recent_problems"]] == ["stuck", "fallen"]


def test_概览_技术告警不算毛病(台):
    d, db = 台
    _alert(db, "A/cert#1", "cert_expiring", NOW - 1000)
    _alert(db, "A/old#1", "stuck", NOW - 31 * 86_400_000, resolved=NOW - 30 * 86_400_000 - 1)
    r = _one(d, db)
    assert r["problems"] == [] and r["recent_problems"] == []


def test_概览_电量和在线(台):
    d, db = 台
    r = _one(d, db)
    assert r["online"] is True and r["battery_pct"] == 76 and r["agent"] == "0.9.1"
    d.fresh.clear()
    r = _one(d, db)
    assert r["online"] is False and r["battery_pct"] is None


def test_概览_只能手动派(台):
    d, db = 台
    assert _one(d, db)["manual_only"] is False
    d.registry.set_manual_only("A", True, by="alice", now_ms=NOW)
    assert _one(d, db)["manual_only"] is True


def test_起名(台):
    d, db = 台
    d.registry.set_name("A", "  前院  ")
    assert d.registry.names() == {"A": "前院"} and _one(d, db)["name"] == "前院"
    d.registry.set_name("A", "")
    assert d.registry.names() == {} and _one(d, db)["name"] == ""


@pytest.mark.parametrize("bad", ["字" * 33, "a\nb", 3, None])
def test_起名_不合规矩(台, bad):
    with pytest.raises(RegistryError):
        台[0].registry.set_name("A", bad)


def test_起名_没这台(台):
    with pytest.raises(RegistryError):
        台[0].registry.set_name("Z", "前院")


# ------------------------------------------------------------ 接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def test_概览权限(站点):
    s = 站点
    assert s.req("GET", "/api/robots/overview", token=_登(s, "gina"))[0] == 403
    code, d = s.req("GET", "/api/robots/overview", token=_登(s, "alice"))
    assert code == 200 and [r["robot_id"] for r in d["robots"]] == ["A"]


def test_接口起名_robots回包带name(站点):
    s = 站点
    alice = _登(s, "alice")
    assert s.req("POST", "/api/admin/robots/A/name", {"name": "前院"}, token=_登(s, "gina"))[0] \
        == 403
    code, d = s.req("POST", "/api/admin/robots/A/name", {"name": "前院"}, token=alice)
    assert code == 200, d
    robots = s.req("GET", "/api/robots", token=alice)[1]["robots"]
    assert robots[0]["name"] == "前院"
    assert s.req("POST", "/api/admin/robots/A/name", {"name": "字" * 33}, token=alice)[0] == 400
    assert s.req("POST", "/api/admin/robots/Z/name", {"name": "x"}, token=alice)[0] == 404


def test_解绑要再输密码(站点, tmp_path):
    s = 站点
    alice = _登(s, "alice")
    s.api.home = tmp_path / "home"              # 没站点目录也先过密码这一关
    code, d = s.req("POST", "/api/admin/robots/A/revoke", {}, token=alice)
    assert code == 403 and "密码" in d["error"]
    code, d = s.req("POST", "/api/admin/robots/A/revoke", {"confirm_password": "nope-nope-nope"},
                    token=alice)
    assert code == 403
    audit = s.req("GET", "/api/audit", token=alice)[1]["audit"]
    assert json.dumps(audit).count("revoke") >= 2
