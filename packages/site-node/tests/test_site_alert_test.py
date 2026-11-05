"""POC 第 8 项:告警测试模式。管理员开(带结束时刻,最长 2 小时);期间的告警打 ``test`` 标记、标题前加
「测试」,照常推;「结束测试并清掉」删掉带标记的(审计写删了几条),没标记的不动;到点自动结束、不删。"""

from __future__ import annotations

import pytest
from test_site_api import PW, 站

from nexwar_site.api import _sync


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True)
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _报(s, title, kind="stuck", robot="A"):
    return s.loop.call(lambda: _sync(s.desk.raise_alert, kind=kind, robot=robot, title=title))


def _开着的(s, tok):
    return s.req("GET", "/api/alerts", token=tok)[1]["alerts"]


def test_开测试_告警带标记_标题加测试_结束清掉只删测试的_审计(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    real = _报(s, "真的卡住了", robot="B")
    start = {"action": "start", "minutes": 30}
    assert s.req("POST", "/api/alert-test", start, token=gina)[0] == 403
    for bad in ({"action": "start", "minutes": 0}, {"action": "start", "minutes": 121},
                {"action": "start"}, {"action": "x"}):
        assert s.req("POST", "/api/alert-test", bad, token=alice)[0] == 400, bad
    code, d = s.req("POST", "/api/alert-test", {"action": "start", "minutes": 30}, token=alice)
    assert code == 200 and d["active"] and d["by"] == "alice", d
    assert s.req("GET", "/api/alert-test", token=gina)[1]["active"] is True
    t1 = _报(s, "演练:卡住了")
    assert t1.context.get("test") is True and t1.title.startswith("测试："), t1
    _报(s, "演练:卡住了", robot="B")                 # 真告警那台:另起一条,不合进真告警
    rows = _开着的(s, alice)
    assert {r["title"] for r in rows} >= {"测试：演练:卡住了", "真的卡住了"}
    code, d = s.req("POST", "/api/alert-test", {"action": "end", "clear": True}, token=alice)
    assert code == 200 and d["cleared"] == 2 and not d["active"], d
    left = _开着的(s, alice)
    assert [r["key"] for r in left] == [real.key], left
    assert s.db.query("SELECT COUNT(*) AS n FROM alerts")[0]["n"] == 1
    acts = [r for r in s.api.audit.list() if r["action"] == "POST /api/alert-test"]
    assert any(r["detail"].get("cleared") == 2 for r in acts), acts
    t2 = _报(s, "结束之后的")
    assert "test" not in t2.context and t2.title == "结束之后的"


def test_到点自动结束_不删(站点):
    s = 站点
    alice = _登(s, "alice")
    s.req("POST", "/api/alert-test", {"action": "start", "minutes": 1}, token=alice)
    _报(s, "演练")
    s.loop.call(lambda: _sync(setattr, s.desk, "_now", lambda: s.disp._now() + 61_000))
    assert s.req("GET", "/api/alert-test", token=alice)[1]["active"] is False
    after = _报(s, "到点之后", kind="intrusion")
    assert "test" not in after.context
    assert s.db.query("SELECT COUNT(*) AS n FROM alerts")[0]["n"] == 2, "到点不删"
