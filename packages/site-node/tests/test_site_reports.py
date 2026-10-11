"""值班日报(C-B3b):数按时间段数、不截;明细截了写明;时间段不对 400;能看的都能看。"""
from __future__ import annotations

import pytest
from test_site_api import PW, 站

from d1max_site import reports
from d1max_site.db import SiteDB
from d1max_site.reports import ReportError, daily

T0 = 1_800_000_000_000
H = 3_600_000


def _alert(db, key, level, first, *, kind="k", robot="A", acked=None, by="", closed=None,
           closed_by="", ctx="{}"):
    db.query("INSERT INTO alerts(key, level, kind, robot, title, detail, first_ms, last_ms, "
             "count, acked_by, acked_ms, resolved_by, resolved_ms, escalated, context) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (key, level, kind, robot, f"t-{key}", "", first, first, 1, by, acked, closed_by,
              closed, 0, ctx))


@pytest.fixture
def 库(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    yield db
    db.close()


def test_空的一天_全是零_不出错(库):
    r = daily(库, T0, T0 + 24 * H)
    assert r["alarms"]["total"] == 0 and r["alarms"]["by_level"] == {"P1": 0, "P2": 0, "P3": 0}
    assert r["alarms"]["ack"]["P1"] == {"n": 0, "median_s": None, "max_s": None}
    assert r["alarms"]["handlers"] == [] and r["alarms"]["rows"] == []
    assert not r["alarms"]["truncated"]
    assert r["incidents"] == {"total": 0, "by_outcome": {}, "rows": [], "truncated": False}
    assert r["patrols"]["total"] == 0 and r["patrols"]["by_result"] == {}
    assert r["runs"] == {"total": 0, "finished": 0, "photos": 0, "by_robot": {}}
    assert (r["since_ms"], r["until_ms"], r["rows_max"]) == (T0, T0 + 24 * H, 1000)


def test_告警_按第一次报的时刻归段_含头不含尾_各级多少_多久确认_谁处理的(库):
    _alert(库, "before", "P1", T0 - 1, acked=T0 + 5, by="gina")
    _alert(库, "at-start", "P1", T0, kind="dog_sees_person", acked=T0 + 10_000, by="gina",
           closed=T0 + 60_000, closed_by="gina", ctx='{"zone": "East wall"}')
    _alert(库, "p1-b", "P1", T0 + H, acked=T0 + H + 30_000, by="alice")
    _alert(库, "p1-c", "P1", T0 + 2 * H, acked=T0 + 2 * H + 110_000, by="gina")
    _alert(库, "p1-open", "P1", T0 + 3 * H)
    _alert(库, "p2-a", "P2", T0 + 4 * H, acked=T0 + 4 * H - 500, by="alice")   # 钟差:当 0
    _alert(库, "p2-auto", "P2", T0 + 5 * H, closed=T0 + 6 * H, closed_by="")  # 自己好了
    _alert(库, "p3", "P3", T0 + 6 * H, ctx="not json")
    _alert(库, "at-end", "P1", T0 + 24 * H)
    a = daily(库, T0, T0 + 24 * H)["alarms"]
    assert a["total"] == 7 and a["by_level"] == {"P1": 4, "P2": 2, "P3": 1}
    assert (a["acked"], a["resolved"], a["waiting"], a["closed_unacked"]) == (4, 2, 2, 1)
    assert a["ack"]["P1"] == {"n": 3, "median_s": 30.0, "max_s": 110.0}
    assert a["ack"]["P2"] == {"n": 1, "median_s": 0.0, "max_s": 0.0}
    assert a["handlers"] == [{"name": "", "acked": 0, "resolved": 1},
                             {"name": "alice", "acked": 2, "resolved": 0},
                             {"name": "gina", "acked": 2, "resolved": 1}]
    assert {"kind": "dog_sees_person", "level": "P1", "n": 1} in a["by_kind"]
    assert a["by_kind"][0] == {"kind": "k", "level": "P1", "n": 3}, "同级里多的在前"
    assert [r["key"] for r in a["rows"]] == ["at-start", "p1-b", "p1-c", "p1-open", "p2-a",
                                             "p2-auto", "p3"]
    assert a["rows"][0]["zone"] == "East wall" and a["rows"][-1]["zone"] is None
    assert a["rows"][0]["resolved_by"] == "gina" and not a["truncated"]


def test_中位数_偶数个取中间两个的平均():
    assert reports._median([]) is None
    assert reports._median([5]) == 5.0
    assert reports._median([9, 1]) == 5.0
    assert reports._median([3, 1, 2]) == 2.0
    assert reports._median([4, 1, 3, 2]) == 2.5


def test_明细最多带那么多行_截了写明_数不截(库, monkeypatch):
    monkeypatch.setattr(reports, "ROWS_MAX", 3)
    for i in range(5):
        _alert(库, f"a{i}", "P2", T0 + i)
        库.query("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome) "
                "VALUES ('nvr', ?, 'intrusion', 'z', ?, 'dispatched')", (f"e{i}", T0 + i))
        库.query("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, decided_at) "
                "VALUES ('e', ?, 'started', 1)", (T0 + i,))
    r = daily(库, T0, T0 + H)
    for part in ("alarms", "incidents", "patrols"):
        assert r[part]["total"] == 5 and len(r[part]["rows"]) == 3 and r[part]["truncated"]
    assert [x["key"] for x in r["alarms"]["rows"]] == ["a0", "a1", "a2"], "从早到晚"
    monkeypatch.setattr(reports, "ROWS_MAX", 5)
    assert not daily(库, T0, T0 + H)["alarms"]["truncated"]


def test_入侵事件按收到的时刻_排程按该跑的时刻_各种结果分开数(库):
    def inc(eid, at, outcome, robot=None, result=None):
        库.query("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome, "
                "robot_id, result, note) VALUES ('nvr', ?, 'intrusion', 'front', ?, ?, ?, ?, 'n')",
                (eid, at, outcome, robot, result))

    def run(entry, at, outcome, result=None, robot="A"):
        库.query("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, result, "
                "note, decided_at) VALUES (?,?,?,?,?,'',1)", (entry, at, outcome, robot, result))
    inc("e1", T0, "dispatched", "A", "done")
    inc("e2", T0 + 1, "dispatched", "A")
    inc("e3", T0 + 2, "merged")
    inc("e4", T0 + 3, "disarmed")
    inc("old", T0 - 1, "dispatched")
    inc("late", T0 + H, "dispatched")
    run("night", T0, "started", "done")
    run("night", T0 + 1, "started", "failed")
    run("night", T0 + 2, "started")
    run("night", T0 + 3, "skipped")
    run("night", T0 + 4, "dispatch_failed")
    run("night", T0 + H, "started", "done")
    r = daily(库, T0, T0 + H)
    i, p = r["incidents"], r["patrols"]
    assert i["total"] == 4 and i["by_outcome"] == {"dispatched": 2, "merged": 1, "disarmed": 1}
    assert [x["outcome"] for x in i["rows"]] == ["dispatched", "dispatched", "merged", "disarmed"]
    assert i["rows"][0] == {"id": 1, "received_at": T0, "source": "nvr", "type": "intrusion",
                            "zone": "front", "intercept": None, "outcome": "dispatched",
                            "robot_id": "A", "result": "done", "note": "n"}
    assert p["total"] == 5 and p["started"] == 3
    assert p["by_outcome"] == {"started": 3, "skipped": 1, "dispatch_failed": 1}
    assert p["by_result"] == {"done": 1, "failed": 1, "running": 1}, "没报结果的 = 还在跑"
    assert p["rows"][0]["entry_id"] == "night" and len(p["rows"]) == 5


def test_运行记录_几趟_完了几趟_多少照片_每台狗几趟(库):
    def rec(robot, stamp, first, finished, photos):
        库.query("INSERT INTO runs(robot_id, mission, stamp, first_ms, last_ms, finished, photos) "
                "VALUES (?,?,?,?,?,?,?)", (robot, "m", stamp, first, first, finished, photos))
    rec("A", "s1", T0, 1, 4)
    rec("A", "s2", T0 + 1, 0, 2)
    rec("B", "s1", T0 + 2, 1, 0)
    rec("A", "s0", T0 - 1, 1, 9)
    rec("A", "s9", T0 + H, 1, 9)
    assert daily(库, T0, T0 + H)["runs"] == {"total": 3, "finished": 2, "photos": 6,
                                            "by_robot": {"A": 2, "B": 1}}


@pytest.mark.parametrize("since,until", [
    (T0, T0), (T0 + 1, T0), (-1, T0), (T0, T0 + 31 * 24 * H + 1), ("1", 2), (1.0, 2), (True, 2),
    (None, 5), (0, None),
])
def test_时间段不对_拒(库, since, until):
    with pytest.raises(ReportError):
        daily(库, since, until)


def test_最长正好31天_收(库):
    assert daily(库, T0, T0 + 31 * 24 * H)["alarms"]["total"] == 0


def test_接口_能看的都能看_没登录401_时间段不对400_只收GET(tmp_path):
    s = 站(tmp_path)
    try:
        s.accounts.add("olga", PW, role="owner")
        tok = s.req("POST", "/api/login", {"name": "olga", "password": PW})[1]["token"]
        _alert(s.db, "x", "P1", T0 + 5, acked=T0 + 2005, by="olga")
        code, d = s.req("GET", f"/api/reports/daily?since={T0}&until={T0 + H}", token=tok)
        assert code == 200 and d["alarms"]["total"] == 1
        assert d["alarms"]["ack"]["P1"] == {"n": 1, "median_s": 2.0, "max_s": 2.0}
        assert s.req("GET", f"/api/reports/daily?since={T0}&until={T0 + H}")[0] == 401
        for q in ("", "?since=1", f"?since={T0}&until={T0}", "?since=a&until=2",
                  "?since=-1&until=2", f"?since=0&until={32 * 24 * H}",
                  "?since=1&until=9999999999999999"):
            assert s.req("GET", f"/api/reports/daily{q}", token=tok)[0] == 400, q
        assert s.req("POST", f"/api/reports/daily?since={T0}&until={T0 + H}", {},
                     token=tok)[0] == 405
    finally:
        s.close()
