"""W00c5d 第三部分:发布经站点,走站点 API 到仿真狗(假的发布操作)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站
from test_site_releases import NAME, 做包


class 假发布:
    def __init__(self):
        self.cur = "2026-09-20-aaaaaa"
        self.done = set()
        self.calls = []
        self.fail = ""

    def current(self):
        return self.cur

    def ready(self, name):
        return name in self.done

    def can_switch_to(self, name):
        return True

    def install(self, ref):
        if self.fail:
            raise RuntimeError(self.fail)
        self.calls.append(("install", ref.name, ref.sha256))
        self.done.add(ref.name)

    def activate(self, name):
        self.calls.append(("activate", name))
        return {"unit": "installed"}

    def rollback(self):
        return self.cur

    def commit_if_pending(self):
        return None

    def disk_ok(self, size):
        return True

    def can_roll_back(self):
        return True

    note = None

    def take_guard_note(self):
        note, self.note = self.note, None
        return note


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, releases={"ops": 假发布()})
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def test_管理员给狗装一版_切过去_保安只看(站点, tmp_path):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    s.rel_catalog.add(做包(tmp_path))
    _等(lambda: (s.req("GET", "/api/releases", token=gina)[1]["robots"].get("A")))
    d = s.req("GET", "/api/releases", token=gina)[1]
    assert [r["name"] for r in d["releases"]] == [NAME] and d["robots"]["A"] == "2026-09-20-aaaaaa"
    assert s.req("POST", "/api/robots/A/release", {"action": "install", "name": NAME},
                 token=gina)[0] == 403
    assert s.req("POST", "/api/robots/A/release",
                 {"action": "install", "name": "2026-01-01-000000"}, token=alice)[0] == 404
    assert s.req("POST", "/api/robots/A/release", {"action": "zap"}, token=alice)[0] == 400
    code, d = s.req("POST", "/api/robots/A/release", {"action": "install", "name": NAME},
                    token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    ops = s.agent.releases
    _等(lambda: ("install", NAME, s.rel_catalog.get(NAME).sha256) in ops.calls)
    code, d = s.req("POST", "/api/robots/A/release", {"action": "activate", "name": NAME},
                    token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: ("activate", NAME) in ops.calls)


def test_装不上_站点出告警(站点, tmp_path):
    s = 站点
    alice = _登(s, "alice")
    s.rel_catalog.add(做包(tmp_path))
    _等(lambda: s.disp.clients["A"].capabilities is not None
        and "release_install" in s.disp.clients["A"].capabilities.tasks)
    s.agent.releases.fail = "盘满了"
    s.req("POST", "/api/robots/A/release", {"action": "install", "name": NAME}, token=alice)
    got = _等(lambda: [a for a in s.req("GET", "/api/alerts", token=alice)[1]["alerts"]
                       if a["kind"] == "release_failed"], timeout=8)
    assert "盘满了" in got[0]["detail"]


def test_狗上开机守卫退回了上一版_站点出告警(tmp_path):
    ops = 假发布()
    ops.note = {"from": NAME, "to": "2026-09-20-aaaaaa", "attempts": 3, "at_ms": 1}
    s = 站(tmp_path, alerts=True, releases={"ops": ops})
    try:
        alice = _登(s, "alice")
        got = _等(lambda: [a for a in s.req("GET", "/api/alerts", token=alice)[1]["alerts"]
                           if a["kind"] == "release_failed"], timeout=8)
        assert "退回" in got[0]["title"] and NAME in got[0]["title"]
    finally:
        s.close()


def test_上一版退不回去_狗留在新版_站点告警说清楚(tmp_path):
    ops = 假发布()
    ops.note = {"from": NAME, "to": NAME, "attempts": 3, "at_ms": 1, "no_fallback": True}
    s = 站(tmp_path, alerts=True, releases={"ops": ops})
    try:
        alice = _登(s, "alice")
        got = _等(lambda: [a for a in s.req("GET", "/api/alerts", token=alice)[1]["alerts"]
                           if a["kind"] == "release_failed"], timeout=8)
        assert "退不回去" in got[0]["title"] and "留在新版" in got[0]["title"]
    finally:
        s.close()
