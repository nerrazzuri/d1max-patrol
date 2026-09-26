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

    def check_package(self, name):
        return ""

    def requires_mission_schema(self, name):
        return getattr(self, "schema", 1)

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



# ------------------------------------------------------------ 升级前检查(W00c6d)


def _能查(s):
    _等(lambda: s.disp.clients["A"].capabilities is not None
        and "release_precheck" in s.disp.clients["A"].capabilities.tasks)


def _查(s, tok, name=NAME):
    return s.req("POST", "/api/robots/A/release", {"action": "precheck", "name": name}, token=tok)


def test_升级前检查_狗的清单合上站点那两项_保安不能查(站点, tmp_path):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    s.rel_catalog.add(做包(tmp_path))
    _能查(s)
    assert _查(s, gina)[0] == 403
    s.agent.releases.done.add(NAME)
    code, d = _查(s, alice)
    assert code == 200, d
    got = {c["name"]: c for c in d["checks"]}
    for n in ("busy", "battery", "package", "agent_start", "localizer", "schema", "backup"):
        assert n in got, n
    assert got["schema"]["ok"] and got["schema"]["blocking"]
    assert not got["backup"]["blocking"], "备份只提示,不拦"
    assert d["ok"] is True and d["blocking"] == [] and d["name"] == NAME
    assert ("activate", NAME) not in s.agent.releases.calls, "只查,不切"


def test_升级前检查_没登记的版本_也能查_狗按槽里的自述比schema(站点, tmp_path):
    """W00c6d 内审应修 3:U 盘装上去的版本站点没登记,以前查回 404、切却跳过 schema 检查。"""
    from test_site_schedule import 打包

    from d1max_site.catalog import import_bundle
    s = 站点
    alice = _登(s, "alice")
    import_bundle(s.db, 打包(tmp_path, 1), imported_by="alice", now_ms=1)
    other = "2026-01-01-000000"
    s.agent.releases.done.add(other)
    s.agent.releases.schema = 2
    _能查(s)
    code, d = _查(s, alice, other)
    got = {c["name"]: c for c in d["checks"]}
    assert code == 200 and not got["schema"]["ok"] and "schema" in d["blocking"], d
    assert [c["name"] for c in d["checks"]].count("schema") == 1, "狗报了就不再加站点那一份"
    code, d = s.req("POST", "/api/robots/A/release", {"action": "activate", "name": other},
                    token=alice)
    assert code == 200 and d["ack"]["reason"] == "schema_mismatch", d


def test_重投的回执_清单在第一次的结果里(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _能查(s)
    first = {"checks": [{"name": "busy", "ok": True, "blocking": True, "detail": "空闲"}]}

    async def 重投(*a, **k):
        return {"ack": {"result": "duplicate", "reason": "",
                        "original": {"result": "accepted", "data": first}}}
    monkeypatch.setattr(s.disp, "map_command", 重投)
    code, d = _查(s, alice, NAME)
    assert code == 200 and d["checks"][0]["name"] == "busy", d


def test_回执里没有清单_502(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _能查(s)

    async def 没清单(*a, **k):
        return {"ack": {"result": "accepted", "reason": ""}}
    monkeypatch.setattr(s.disp, "map_command", 没清单)
    assert _查(s, alice, NAME)[0] == 502


def test_站点备份过期了_提示不拦(站点, tmp_path):
    s = 站点
    alice = _登(s, "alice")
    s.rel_catalog.add(做包(tmp_path))
    _能查(s)

    class 过期的备份:
        def status(self):
            return {"configured": True, "last_ok_ms": 1, "stale": True}
    s.api.backup = 过期的备份()
    got = {c["name"]: c for c in _查(s, alice)[1]["checks"]}
    assert not got["backup"]["ok"] and not got["backup"]["blocking"] and "过期" in \
        got["backup"]["detail"]


def test_新版要的任务包schema比站点当前包高_清单不过_切也不许(站点, tmp_path):
    from test_site_schedule import 打包

    from d1max_site.catalog import import_bundle
    s = 站点
    alice = _登(s, "alice")
    import_bundle(s.db, 打包(tmp_path, 1), imported_by="alice", now_ms=1)
    s.rel_catalog.add(做包(tmp_path, schema=2))
    s.agent.releases.done.add(NAME)
    s.agent.releases.schema = 2                           # 狗槽里的自述跟登记的是同一个包
    _能查(s)
    code, d = _查(s, alice)
    got = {c["name"]: c for c in d["checks"]}
    assert code == 200 and not got["schema"]["ok"] and "schema" in d["blocking"], d
    assert d["ok"] is False
    assert [c["name"] for c in d["checks"]].count("schema") == 1, \
        "登记过的版本:狗按槽里比过了,站点那一份不再重复"
    code, d = s.req("POST", "/api/robots/A/release", {"action": "activate", "name": NAME},
                    token=alice)
    assert code == 409 and "schema" in d["error"], d
    assert ("activate", NAME) not in s.agent.releases.calls


def test_站点没有任务包_schema这一项没有可比的_过(站点, tmp_path):
    s = 站点
    alice = _登(s, "alice")
    s.rel_catalog.add(做包(tmp_path, schema=9))
    _能查(s)
    got = {c["name"]: c for c in _查(s, alice)[1]["checks"]}
    assert got["schema"]["ok"] and "没有任务包" in got["schema"]["detail"]


def test_狗拒了切版本_回执里带着清单(站点, tmp_path):
    s = 站点
    alice = _登(s, "alice")
    s.rel_catalog.add(做包(tmp_path))
    _能查(s)
    code, d = s.req("POST", "/api/robots/A/release", {"action": "activate", "name": NAME},
                    token=alice)
    assert code == 200 and d["ack"]["reason"] == "not_installed", d
    assert "installed" in d["ack"]["data"]["blocking"]


def test_老代理不会回清单_站点说清楚(站点, tmp_path):
    s = 站点
    alice = _登(s, "alice")
    s.rel_catalog.add(做包(tmp_path))
    _能查(s)
    s.disp.clients["A"].capabilities.tasks.pop("release_precheck")
    code, d = _查(s, alice)
    assert code == 409 and "release_precheck" in d["error"], d


def test_站点当前任务包是新格式_新版要的也够_过(站点, tmp_path):
    """任务包的 schema 按包记(不是写死 1):包写着 2、新版要 2,schema 这一项过。"""
    import yaml
    from test_site_schedule import 打包

    from d1max_site.catalog import import_bundle
    s = 站点
    alice = _登(s, "alice")
    b = 打包(tmp_path, 1)
    raw = yaml.safe_load((b / "bundle.yaml").read_text(encoding="utf-8"))
    raw["schema"] = 2
    (b / "bundle.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    import_bundle(s.db, b, imported_by="alice", now_ms=1)
    s.rel_catalog.add(做包(tmp_path, schema=2))
    _能查(s)
    got = {c["name"]: c for c in _查(s, alice)[1]["checks"]}
    assert got["schema"]["ok"], got["schema"]
