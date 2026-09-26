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
    for k in ("disk_used_ratio", "upload_backlog"):                # W00c5d:狗还没报过盘况
        assert r[k] is None and "不知道" in r["why"][k], k
    assert r["bundle_lag"] is None and "任务包" in r["why"]["bundle_lag"]
    assert r["backup"] is None and "站点" in r["why"]["backup"]
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



def test_交接班那一张_截多少条写明白_limit不对400(站点):
    s = 站点
    alice = _登(s, "alice")
    for i in range(3):
        s.loop.call(lambda i=i: _raise(s, i))
    code, d = s.req("GET", "/api/alerts?all=1&limit=2", token=alice)
    assert code == 200 and len(d["alerts"]) == 2 and d["limit"] == 2 and d["truncated"] is True
    code, d = s.req("GET", "/api/alerts?all=1", token=alice)
    assert d["limit"] == 200 and d["truncated"] is False
    for bad in ("0", "x", "99999"):
        assert s.req("GET", f"/api/alerts?all=1&limit={bad}", token=alice)[0] == 400


async def _raise(s, i):
    s.desk.raise_alert(kind="run_done", robot=f"R{i}", title="跑完了")


def test_在事件循环里报告警_直接报不等自己(tmp_path):
    """LoopAlerts 从循环线程里调(比如以后派遣器里报):跳回循环再等结果会等自己,等满 10 s 才炸。"""
    import time as _t

    from d1max_site.alert_store import AlertDesk, LoopAlerts
    from d1max_site.db import SiteDB
    from d1max_site.loop import LoopThread
    lt = LoopThread()
    lt.start()
    try:
        la = LoopAlerts(AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1), lt)

        async def 在循环里():
            return la.raise_alert(kind="backup_stale", robot="site", title="t")
        t0 = _t.monotonic()
        got = lt.call(在循环里, timeout_s=15)
        assert got is not None and _t.monotonic() - t0 < 2
        got = la.raise_alert(kind="disk_80", robot="A", title="t")
        assert got is not None, "别的线程照样跳进去"
    finally:
        lt.stop()


def test_没回待命点_进告警簿P2_值守屏看得见(站点):
    """W00c6b:``standby_failed`` 以前只推进 SSE 事件流,值守屏的告警簿不收、手机不显示 —— 报告里写的
    「推给值守的人看」其实没人看得见。告警源挂到派遣器上时顺带听推送流。"""
    s = 站点
    gina = _登(s, "gina")
    async def 推():
        s.disp.feed.publish({"kind": "standby_failed", "robot_id": "A", "after": "sched-1",
                             "reason": "来路不明"})
    s.loop.call(推)
    a = _等(lambda: _告警(s, gina, "standby_failed"))
    assert a["level"] == "P2" and "待命点" in a["title"] and "来路不明" in a["detail"]
