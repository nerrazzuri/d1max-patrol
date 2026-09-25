"""W00c5a:告警与值守的站点 API。真 HTTP、真代理(SimRobot):狗报事实,站点判,手机经站点看、确认。"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from test_site_api import PW, _等, 站


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _告警(s, tok, kind):
    code, d = s.req("GET", "/api/alerts", token=tok)
    assert code == 200, d
    return next((a for a in d["alerts"] if a["kind"] == kind), None)


def _键(a):
    return quote(a["key"], safe="")


def test_狗急停_站点出P1_保安确认记登录账号_业主只能看(站点):
    s = 站点
    alice, gina, olga = _登(s, "alice"), _登(s, "gina"), _登(s, "olga")
    _等(lambda: s.req("GET", "/api/robots/A", token=alice)[1].get("fresh"))
    s.loop.call(lambda: s.dog.emergency_stop(True))
    a = _等(lambda: _告警(s, olga, "estop_pressed"))
    assert a["level"] == "P1" and a["robot"] == "A" and a["acked_by"] == ""
    code, d = s.req("POST", f"/api/alerts/{_键(a)}/ack", {"who": "mallory"}, token=olga)
    assert code == 403, d
    code, d = s.req("POST", f"/api/alerts/{_键(a)}/ack", {"who": "mallory"}, token=gina)
    assert code == 200 and d["alert"]["acked_by"] == "gina", "确认人取登录账号,不信请求体"
    code, d = s.req("POST", f"/api/alerts/{_键(a)}/resolve", {}, token=gina)
    assert code == 200 and d["alert"]["resolved_by"] == "gina"
    assert _告警(s, alice, "estop_pressed") is None, "默认只列未解决的"
    code, d = s.req("GET", "/api/alerts?all=1", token=alice)
    assert any(x["key"] == a["key"] and x["resolved_by"] == "gina" for x in d["alerts"])
    audit = s.req("GET", "/api/audit", token=alice)[1]["audit"]
    assert any(r["actor"] == "gina" and "/ack" in r["action"] and r["target"] == a["key"]
               for r in audit), audit[:3]


def test_确认不存在的键404_没登录401(站点):
    s = 站点
    gina = _登(s, "gina")
    code, _ = s.req("POST", f"/api/alerts/{quote('A/stuck#99', safe='')}/ack", {}, token=gina)
    assert code == 404
    assert s.req("GET", "/api/alerts")[0] == 401
    assert s.req("GET", "/api/watch/summary")[0] == 401


def test_狗报跌倒故障_站点认出fallen(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: s.req("GET", "/api/robots/A", token=alice)[1].get("fresh"))
    s.dog.inject_fault("3", True, "机身跌倒")
    a = _等(lambda: _告警(s, alice, "fallen"))
    assert "[3] 机身跌倒" in a["detail"]


def test_值守汇总_每台狗一行_狗上存储那几项是不知道而不是0(站点):
    s = 站点
    olga = _登(s, "olga")
    _等(lambda: s.req("GET", "/api/robots/A", token=olga)[1].get("fresh"))
    _等(lambda: s.disp.telemetry_at.get("A"))
    code, d = s.req("GET", "/api/watch/summary", token=olga)
    assert code == 200, d
    [r] = d["robots"]
    assert r["robot_id"] == "A" and r["online"] is True and r["fresh"] is True
    assert isinstance(r["battery_pct"], (int, float)) and r["battery_as_of_ms"] > 0
    assert abs(r["clock_skew_s"]) < 5
    assert r["alerts"] == {"P1": 0, "P2": 0, "P3": 0}
    for k in ("disk_used_ratio", "upload_backlog", "bundle_lag", "backup"):
        assert r[k] is None and "不知道" in r["why"][k], k
    assert d["site"]["schedule_ok"] is None and "没开排程" in d["site"]["why"]["schedule_ok"]


def test_SSE里看得到告警的变化(站点):
    import http.client
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: s.req("GET", "/api/robots/A", token=alice)[1].get("fresh"))
    host, port = s.api.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/api/events", headers={"Authorization": f"Bearer {alice}"})
    resp = conn.getresponse()
    s.loop.call(lambda: s.dog.emergency_stop(True))
    for _ in range(400):
        line = resp.fp.readline().decode()
        if line.startswith("data:"):
            d = json.loads(line[5:])
            if d.get("kind") == "alert":
                assert d["alert"]["kind"] == "estop_pressed"
                break
    else:
        raise AssertionError("SSE 里没等到告警")
    conn.close()
