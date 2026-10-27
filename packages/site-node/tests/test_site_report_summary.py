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
