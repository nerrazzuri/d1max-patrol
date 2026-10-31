"""W44 第二期:报表按天汇总(设计 §5.1「报表」):巡了多少趟、出了几次事、误报几次、机器人出问题几次;
按站点时区的日子分,没事的日子也列;事件只列给保安的那四类。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_site_api import PW, 站

from d1max_site.db import SiteDB
from d1max_site.reports import ReportError, summary

KL = ZoneInfo("Asia/Kuala_Lumpur")


def kl(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=KL).timestamp() * 1000)


def _cmd(db, i, at, kind="patrol", ack="accepted"):
    with db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, ack_result) VALUES (?,?,?,?,?,?,?,?)",
                  (f"c{i}", f"t{i}", "A", kind, "{}", "alice", at, ack))


def _alert(db, key, kind, first, verdict="", level="P1"):
    with db.tx() as c:
        c.execute("INSERT INTO alerts(key, level, kind, robot, title, detail, first_ms, last_ms, "
                  "count, acked_by, resolved_by, escalated, verdict) "
                  "VALUES (?,?,?,?,?,?,?,?,1,'','',0,?)",
                  (key, level, kind, "A", kind, "", first, first, verdict))


@pytest.fixture
def db(tmp_path):
    d = SiteDB(tmp_path / "s.db")
    yield d
    d.close()


WEEK = (kl(2026, 10, 26), kl(2026, 11, 2))


def test_汇总_四个数(db):
    _cmd(db, 1, kl(2026, 10, 26, 9))
    _cmd(db, 2, kl(2026, 10, 27, 9))
    _cmd(db, 3, kl(2026, 10, 27, 10), ack="rejected")
    _cmd(db, 4, kl(2026, 10, 27, 11), kind="goto")
    _alert(db, "i1", "intrusion", kl(2026, 10, 27, 1), verdict="false_alarm")
    _alert(db, "i2", "intrusion_person", kl(2026, 10, 28, 1), verdict="intruder")
    _alert(db, "h1", "stuck", kl(2026, 10, 28, 2))
    _alert(db, "o1", "robot_offline", kl(2026, 10, 28, 3))
    _alert(db, "t1", "cert_expiring", kl(2026, 10, 28, 4), level="P2")
    got = summary(db, *WEEK, tz=KL)
    assert got["totals"] == {"patrols": 2, "incidents": 2, "false_alarms": 1,
                             "robot_problems": 2}


def test_汇总_没事的日子也列(db):
    got = summary(db, *WEEK, tz=KL)
    assert [d["day"] for d in got["days"]] == [f"2026-10-{d}" for d in range(26, 32)] + [
        "2026-11-01"]
    assert all(d["patrols"] == 0 and d["incidents"] == 0 for d in got["days"])


def test_按天分_按站点时区(db):
    _alert(db, "i1", "intrusion", kl(2026, 10, 27, 0, 30))      # 吉隆坡 27 号 00:30 = UTC 26 号
    got = summary(db, *WEEK, tz=KL)
    assert {d["day"]: d["incidents"] for d in got["days"]}["2026-10-27"] == 1
    utc = summary(db, *WEEK, tz=ZoneInfo("UTC"))
    assert {d["day"]: d["incidents"] for d in utc["days"]}["2026-10-26"] == 1


def test_汇总_最长31天(db):
    with pytest.raises(ReportError):
        summary(db, kl(2026, 10, 1), kl(2026, 11, 2), tz=KL)
    with pytest.raises(ReportError):
        summary(db, kl(2026, 10, 2), kl(2026, 10, 1), tz=KL)


def test_汇总_事件只列给保安的(db):
    _alert(db, "i1", "intrusion", kl(2026, 10, 27, 1), verdict="false_alarm")
    _alert(db, "t1", "cert_expiring", kl(2026, 10, 27, 2), level="P2")
    _alert(db, "h1", "stuck", kl(2026, 10, 27, 3))
    got = summary(db, *WEEK, tz=KL)
    assert [e["key"] for e in got["events"]] == ["i1", "h1"]
    assert got["events"][0]["verdict"] == "false_alarm"
    assert got["events"][0]["audience"] == "intrusion" and got["truncated"] is False


def test_接口(tmp_path):
    s = 站(tmp_path)
    try:
        s.accounts.add("gina", PW, role="guard")
        with s.db.tx() as c:                       # 站点时区设成吉隆坡(CI 的主机是 UTC)
            c.execute("INSERT OR REPLACE INTO route_store(id, timezone) VALUES (1, ?)",
                      ("Asia/Kuala_Lumpur",))
        gina = s.req("POST", "/api/login", {"name": "gina", "password": PW})[1]["token"]
        code, d = s.req("GET", f"/api/reports/summary?since={WEEK[0]}&until={WEEK[1]}",
                        token=gina)
        assert code == 200 and len(d["days"]) == 7 and "totals" in d
        assert s.req("GET", "/api/reports/summary?since=x&until=1", token=gina)[0] == 400
    finally:
        s.close()


def test_实测_摄像头报的事带上防区(db):
    """报表「机器人」一栏:站点自己(摄像头)报的写哪个防区,网页不写 site(演示站点实测记下)。"""
    with db.tx() as c:
        c.execute("INSERT INTO alerts(key, level, kind, robot, title, detail, first_ms, last_ms, "
                  "count, acked_by, resolved_by, escalated, verdict, context) "
                  "VALUES ('s1','P1','intrusion_unanswered','site','x','',?,?,1,'','',0,'',?)",
                  (kl(2026, 10, 27, 1), kl(2026, 10, 27, 1), '{"zone": "前门"}'))
    _alert(db, "h1", "stuck", kl(2026, 10, 27, 3))
    ev = {e["key"]: e for e in summary(db, *WEEK, tz=KL)["events"]}
    assert ev["s1"]["zone"] == "前门"
    assert "zone" not in ev["h1"]


# ------------------------------------------------------------------ W60 关键指标

def _done(db, i, at, kind="task_done", robot="A"):
    import json as _j
    with db.tx() as c:
        c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                  "received_at) VALUES (?,?,?,?,?,?,?,?)",
                  (robot, "b", i, f"e{i}", kind, _j.dumps({"task_id": f"t{i}"}), at, at))


def _al(db, key, kind, first, *, robot="A", acked=None, resolved=None, level="P1"):
    with db.tx() as c:
        c.execute("INSERT INTO alerts(key, level, kind, robot, title, detail, first_ms, last_ms, "
                  "count, acked_by, acked_ms, resolved_by, resolved_ms, escalated) "
                  "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,0)",
                  (key, level, kind, robot, kind, "", first, first, "g" if acked else "", acked,
                   "g" if resolved else "", resolved))


def test_W60_指标_巡逻完成率_干预率(db):
    h = 3_600_000
    _cmd(db, 1, kl(2026, 10, 26, 9))
    _done(db, 1, kl(2026, 10, 26, 9, 20))
    _cmd(db, 2, kl(2026, 10, 27, 9))
    _done(db, 2, kl(2026, 10, 27, 9, 30), kind="task_failed")
    _cmd(db, 3, kl(2026, 10, 28, 9))
    _done(db, 3, kl(2026, 10, 28, 9, 25))
    _cmd(db, 4, kl(2026, 10, 28, 10), ack="rejected")          # 被拒的不算派出
    _al(db, "h1", "stuck", kl(2026, 10, 28, 9, 10))            # 第 3 趟中途要人帮忙
    _al(db, "h2", "stuck", kl(2026, 10, 28, 20))               # 不在任何一趟里:不算
    k = summary(db, *WEEK, tz=KL)["kpis"]
    assert k["patrols_sent"] == 3 and k["patrols_done"] == 2
    assert k["patrol_completion"] == pytest.approx(2 / 3)
    assert k["intervention_rate"] == pytest.approx(1 / 3)
    del h


def test_W60_指标_平均响应_确认率_只算给保安的(db):
    t0 = kl(2026, 10, 27, 1)
    _al(db, "i1", "intrusion", t0, acked=t0 + 30_000)
    _al(db, "i2", "intrusion", t0 + 1, acked=t0 + 1 + 50_000)
    _al(db, "i3", "stuck", t0 + 2)                              # 没人确认
    _al(db, "c1", "cert_expiring", t0 + 3, acked=t0 + 900_000, level="P2")   # 技术类不算
    k = summary(db, *WEEK, tz=KL)["kpis"]
    assert k["response_s"] == pytest.approx(40.0)
    assert k["ack_rate"] == pytest.approx(2 / 3)


def test_W60_指标_机器人和摄像头可用率_按掉线告警扣(db):
    from d1max_site.registry import Registry
    reg = Registry(db, site_id="s")
    reg.enroll("A", fingerprint="fa", issued_at=0, expires_at=2**50)
    reg.enroll("B", fingerprint="fb", issued_at=0, expires_at=2**50)
    with db.tx() as c:
        c.execute("INSERT INTO cameras(name, onvif_url, zone, added_ms) VALUES ('n1','x','z',0)")
    span = WEEK[1] - WEEK[0]
    _al(db, "o1", "robot_offline_idle", WEEK[0] + 1000, robot="A",
        resolved=WEEK[0] + 1000 + span // 10, level="P2")
    _al(db, "o2", "cctv_offline", WEEK[1] - span // 4, robot="cctv:n1", level="P2")   # 到现在还没好
    k = summary(db, *WEEK, tz=KL)["kpis"]
    assert k["robot_availability"] == pytest.approx(1 - 0.1 / 2, abs=1e-6)
    assert k["camera_availability"] == pytest.approx(0.75, abs=1e-6)


def test_W60_指标_什么都没有时是空_不是零(db):
    k = summary(db, *WEEK, tz=KL)["kpis"]
    assert k["patrol_completion"] is None and k["response_s"] is None and k["ack_rate"] is None
    assert k["robot_availability"] is None and k["camera_availability"] is None
    assert k["intervention_rate"] is None


def test_W60_指标_带上一段同样长的_好算比上一段(db):
    prev0 = WEEK[0] - (WEEK[1] - WEEK[0])
    _cmd(db, 1, prev0 + 3_600_000)
    _done(db, 1, prev0 + 4_000_000)
    got = summary(db, *WEEK, tz=KL)
    assert got["kpis_prev"]["patrols_sent"] == 1 and got["kpis_prev"]["patrol_completion"] == 1.0
