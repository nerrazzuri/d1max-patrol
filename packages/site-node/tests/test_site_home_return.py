"""POC 第 4 项:回原点、中止并回原点。``POST /api/robots/<id>/home/return {abort?}``(dispatch 权限):
没原点 409 说去哪标;有任务在跑不带 abort 409;带 abort 先中止、等它停下再去原点(任务号 ``standby-``,
自动回程的去重认它)。真 HTTP、真代理。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站


@pytest.fixture
def 站点(tmp_path):
    from nexwar_site.standby import StandbyManager
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    s.api.standby = StandbyManager(s.db, s.disp, now_ms=s.disp._now)
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _备好(s):
    _等(lambda: s.disp.clients["A"].capabilities is not None
        and "mark_home" in s.disp.clients["A"].capabilities.tasks
        and s.disp.clients["A"].status is not None)


def _任务(s):
    t = s.disp.clients["A"].status.task
    return t.task_id if t is not None else None


def _去远处(s, tok):
    target = {"map_id": "estate-1", "map_version": "7", "frame_id": "map", "x": 30.0, "y": 0.0,
              "yaw": 0.0}
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target}, token=tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _任务(s) == d["task_id"])
    return d["task_id"]


def test_没原点_409_说去哪标(站点):
    s = 站点
    gina = _登(s, "gina")
    _备好(s)
    code, d = s.req("POST", "/api/robots/A/home/return", {}, token=gina)
    assert code == 409 and "还没有原点" in d["error"] and "设为原点" in d["error"], d


def test_回原点_有任务在跑要先中止_带abort就先停再去(站点):
    s = 站点
    alice, gina, olga = _登(s, "alice"), _登(s, "gina"), _登(s, "olga")
    _备好(s)
    assert s.req("POST", "/api/robots/A/home/here", {"name": "dock"}, token=alice)[0] == 200
    assert s.req("POST", "/api/robots/A/home/return", {}, token=olga)[0] == 403
    tid = _去远处(s, gina)
    code, d = s.req("POST", "/api/robots/A/home/return", {}, token=gina)
    assert code == 409 and "中止并回原点" in d["error"], d
    assert _任务(s) == tid, "没动它"
    code, d = s.req("POST", "/api/robots/A/home/return", {"abort": True}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    assert d["task_id"].startswith("standby-") and d["aborted"] == tid, d
    _等(lambda: _任务(s) == d["task_id"])
    rows = [r for r in s.api.audit.list() if r["action"] == "POST /api/robots/A/home/return"]
    assert any(r["detail"].get("abort") is True for r in rows)


def test_没任务_直接回原点(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    _备好(s)
    assert s.req("POST", "/api/robots/A/home/here", {"name": "dock"}, token=alice)[0] == 200
    code, d = s.req("POST", "/api/robots/A/home/return", {}, token=gina)
    assert code == 200 and d["task_id"].startswith("standby-") and d.get("aborted") is None, d
