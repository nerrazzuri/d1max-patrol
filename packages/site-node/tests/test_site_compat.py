"""版本兼容(商业化 A7):狗的代理级别配不配站点(太老、太新各报 P2,配上了解决)、版本一览、登录回复
带接口级别。"""

from __future__ import annotations

from types import SimpleNamespace

from test_site_api import PW, 站

from d1max_contract.compat import (
    AGENT_LEVEL,
    SITE_MAX_AGENT_LEVEL,
    SITE_MIN_AGENT_LEVEL,
    agent_verdict,
)
from d1max_site.alert_store import AlertDesk
from d1max_site.compat import CompatWatch, versions
from d1max_site.db import SiteDB


def _狗(level=None, *, caps=True):
    tasks = {} if level is None else {"compat": {"level": level, "sidecar_proto": 8}}
    return SimpleNamespace(status=SimpleNamespace(online=True),
                           capabilities=SimpleNamespace(tasks=tasks, agent="0.9", adapter="sim")
                           if caps else None)


def test_级别规矩_不报的算1():
    assert agent_verdict(None) == ("ok" if SITE_MIN_AGENT_LEVEL <= 1 else "agent_too_old")
    assert agent_verdict(AGENT_LEVEL) == "ok"
    assert agent_verdict(SITE_MAX_AGENT_LEVEL + 1) == "site_too_old"


def test_对账_老代理报狗太老_太新报站点太老_升好了解决_没报能力不动(tmp_path):
    desk = AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1)
    d = SimpleNamespace(clients={"A": _狗(None), "B": _狗(SITE_MAX_AGENT_LEVEL + 1),
                                 "C": _狗(AGENT_LEVEL), "D": _狗(caps=False)})
    w = CompatWatch(d)
    w.alerts = desk
    w.tick()
    w.tick()
    got = sorted((a.robot, a.kind, a.level.name) for a in desk.book.open())
    assert got == [("A", "agent_too_old", "P2"), ("B", "site_too_old", "P2")]
    d.clients["A"] = _狗(caps=False)                        # 重连了、还没报能力:说不清,告警留着
    w.tick()
    assert ("A", "agent_too_old") in {(a.robot, a.kind) for a in desk.book.open()}
    d.clients["A"] = _狗(AGENT_LEVEL)
    d.clients["B"] = _狗(AGENT_LEVEL)
    w.tick()
    assert not desk.book.open()


def test_版本一览():
    v = versions(SimpleNamespace(clients={"A": _狗(AGENT_LEVEL), "B": _狗(None)}))
    assert v["site"]["agent_level_min"] == SITE_MIN_AGENT_LEVEL
    rows = {r["robot_id"]: r for r in v["robots"]}
    assert rows["A"]["verdict"] == "ok" and rows["A"]["sidecar_proto"] == 8
    assert rows["B"]["level"] == 1


def test_登录回复带接口级别_版本接口能看就能看(tmp_path):
    s = 站(tmp_path, alerts=True)
    try:
        code, d = s.req("POST", "/api/login", {"name": "alice", "password": PW})
        assert code == 200 and d["api_level"] >= 1 and d["min_app_level"] >= 1
        code, v = s.req("GET", "/api/versions", token=d["token"])
        assert code == 200 and "site" in v and "robots" in v
    finally:
        s.close()


def test_站点库比程序新_老程序不开_也不改库(tmp_path):
    import sqlite3

    import pytest

    from d1max_site.db import SCHEMA_VERSION, SchemaTooNew
    SiteDB(tmp_path / "s.db").close()
    c = sqlite3.connect(tmp_path / "s.db")
    c.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION + 1),))
    c.commit()
    c.close()
    with pytest.raises(SchemaTooNew, match="新版程序写的"):
        SiteDB(tmp_path / "s.db")
    c = sqlite3.connect(tmp_path / "s.db")
    assert c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == \
        str(SCHEMA_VERSION + 1)
    c.close()
    SiteDB(tmp_path / "s2.db").close()                      # 新库、同版本的库照常开
    SiteDB(tmp_path / "s2.db").close()
