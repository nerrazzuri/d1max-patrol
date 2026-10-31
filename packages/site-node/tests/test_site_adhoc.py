"""W53 临时巡逻:估时、撞日程排到后面、排队的到点起跑 / 作废 / 取消、通知;今天的巡逻。

派遣器换成假的(只记派了什么、照样在派单事务里跑「发之前记账」),钟注入;日程用真的路线库
(``import_bundle``,22:00 那一条,吉隆坡时区)。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_site_schedule import 任务, 打包

from d1max_contract.mission import parse_mission
from d1max_site import adhoc as ah
from d1max_site.adhoc import AdhocDesk, AdhocError, geom_estimate_s
from d1max_site.catalog import import_bundle
from d1max_site.db import SiteDB
from d1max_site.dispatcher import DispatchRefused
from d1max_site.priorities import MANUAL
from d1max_site.today import today_view

KL = ZoneInfo("Asia/Kuala_Lumpur")


def 毫秒(时: int, 分: int, 秒: int = 0, 日: int = 15) -> int:
    return int(datetime(2027, 3, 日, 时, 分, 秒, tzinfo=KL).timestamp() * 1000)


class 假派遣器:
    def __init__(self, db, robots=("A",)) -> None:
        self.db = db
        self.robots = list(robots)
        self.running: dict[str, str] = {}
        self.sent: list[dict] = []
        self.refuse = ""
        self.ack = "accepted"
        self.cannot: dict[str, str] = {}           # 派不出去的机器人 → 原因
        self.loaded = ("estate-1", "7")
        self.acks = []
        self.registry = SimpleNamespace(list=lambda: [SimpleNamespace(robot_id=r, revoked=False)
                                                      for r in self.robots])
        self.clients: dict = {}

    def busy(self, rid):
        return self.running.get(rid)

    def dispatchable(self, rid, kind):
        return self.cannot.get(rid, "")

    def autonomy(self, rid):
        return "autonomous"

    def _loaded(self, c):
        return self.loaded

    def on_ack(self, cb):
        self.acks.append(cb)

    async def patrol(self, rid, wire, *, issued_by, priority=0, task_id=None, before_send=None):
        if self.refuse:
            raise DispatchRefused(self.refuse)
        with self.db.tx() as tx:
            if before_send is not None:
                before_send(None, tx)
            tx.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, "
                       "issued_by, issued_at, priority) VALUES (?,?,?,?,?,?,?,?)",
                       (f"c-{task_id}", task_id, rid, "patrol",
                        json.dumps({"mission": wire}), issued_by, self.now(), priority))
        self.sent.append({"robot": rid, "task_id": task_id, "by": issued_by, "priority": priority,
                          "mission": wire["mission"], "wire": wire})
        if self.ack == "accepted":
            self.running[rid] = task_id
        reason = "忙" if self.ack != "accepted" else ""
        return {"task_id": task_id, "ack": {"result": self.ack, "reason": reason}}


class 假告警:
    def __init__(self) -> None:
        self.got: list[dict] = []

    def raise_alert(self, *, kind, robot, title, detail="", context=None, merge=True):
        # 跟真的告警台一个签名(外审 #2:原来多传了 now_ms,真站点上全抛 TypeError)
        self.got.append({"kind": kind, "robot": robot, "title": title, "detail": detail,
                         "context": context, "merge": merge})


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    import_bundle(db, 打包(tmp_path, 1), imported_by="alice", now_ms=1)
    now = {"t": 毫秒(20, 0)}
    d = 假派遣器(db)
    d.now = lambda: now["t"]
    a = 假告警()
    sup = {"why": ""}
    desk = AdhocDesk(db, d, now_ms=lambda: now["t"], alerts=a, refusal=lambda r: sup["why"])
    yield SimpleNamespace(db=db, now=now, disp=d, alerts=a, desk=desk, sup=sup)
    db.close()


def 跑(coro):
    return asyncio.run(coro)


def 任务定义():
    return dict(任务)


# ------------------------------------------------------------ 估时


def test_按路长估_相邻点加回待命点加动作():
    m = parse_mission(任务定义())
    # a(0.6,0) → b(0.6,0.6):0.6 m;回 (0,0.6):0.6 m;dwell 0.5 s
    assert geom_estimate_s(m, back_to=(0.0, 0.6)) == pytest.approx(1.2 * 1.3 / 0.5 + 0.5)
    assert geom_estimate_s(m) == pytest.approx(0.6 * 1.3 / 0.5 + 0.5)


def test_估时至少一分钟再乘余量_有历史按历史中位数(台):
    t = 台
    m = parse_mission(任务定义())
    assert t.desk.estimate_s("A", m) == int(60 * ah.MARGIN), "短路线按一分钟起算"
    # 三趟走完:600、900、1200 秒 → 中位数 900
    for i, dur in enumerate((600, 900, 1200)):
        t0 = 毫秒(10 + i, 0)
        with t.db.tx() as c:
            c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, "
                      "issued_by, issued_at) VALUES (?,?,?,?,?,?,?)",
                      (f"c{i}", f"t{i}", "A", "patrol", json.dumps({"mission": 任务定义()}), "g",
                       t0))
            c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                      "received_at) VALUES (?,?,?,?,?,?,?,?)",
                      ("A", "b", i, f"e{i}", "task_done", json.dumps({"task_id": f"t{i}"}),
                       t0 + dur * 1000, t0 + dur * 1000))
    assert t.desk.history_s("A", "loop") == 900
    assert t.desk.estimate_s("A", m) == int(900 * ah.MARGIN)


# ------------------------------------------------------------ 派


def test_不撞日程马上派_手动优先级_记一行_告诉经理管理员(台):
    t = 台
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert got["queued"] is False and got["ack"]["result"] == "accepted"
    sent = t.disp.sent[0]
    assert sent["priority"] == MANUAL and sent["task_id"].startswith("adhoc-")
    assert sent["by"] == "gina"
    row = t.db.query("SELECT * FROM adhoc_queue")[0]
    assert row["state"] == "started" and row["task_id"] == sent["task_id"]
    assert [a["kind"] for a in t.alerts.got] == ["adhoc_patrol"]
    assert "gina" in t.alerts.got[0]["title"] and "临时" in t.alerts.got[0]["title"]


def test_撞上日程排到日程走完之后_不派_告诉派的人排到几点(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)                         # 22:00 有日程
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert got["queued"] is True and t.disp.sent == []
    est = t.desk.estimate_s("A", parse_mission(任务定义()))
    assert got["start_after_ms"] == 毫秒(22, 0) + est * 1000 + 60_000
    assert "22:00" in got["because"] and "loop" in got["because"]
    assert "排到" in t.alerts.got[0]["title"]


def test_正在跑日程也排到后面(台):
    t = 台
    t.disp.running["A"] = "sched-abc"
    with t.db.tx() as c:
        c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, task_id, "
                  "decided_at) VALUES ('nightly', ?, 'started', 'A', 'sched-abc', ?)",
                  (毫秒(19, 50), 毫秒(19, 50)))
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at) VALUES ('c', 'sched-abc', 'A', 'patrol', '{}', 'schedule:nightly', "
                  "?)",
                  (毫秒(19, 50),))
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert got["queued"] is True and "正在执行的日程" in got["because"]


def test_日程没写哪台_别的台也能跑就不算撞_别的台跑不了就算(台):
    t = 台
    t.disp.robots = ["A", "B"]
    t.now["t"] = 毫秒(21, 59)
    assert t.desk.plan("A", 任务定义())["queued"] is False
    t.disp.cannot["B"] = "B 不在线"                 # 外审 #6:只有 A 能跑这条日程
    assert t.desk.plan("A", 任务定义())["queued"] is True
    t.disp.cannot["A"] = "A 未定位"
    assert t.desk.plan("B", 任务定义())["queued"] is False, "A 也跑无法完成:日程落不到谁头上"


def test_排队的_没到点不派_到点机器人忙不派_空了派_告诉派的人(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    跑(t.desk.tick())
    assert t.disp.sent == []
    t.now["t"] = got["start_after_ms"]
    t.disp.running["A"] = "sched-x"
    跑(t.desk.tick())
    assert t.disp.sent == [], "还正在运行日程"
    t.disp.running.clear()
    跑(t.desk.tick())
    assert len(t.disp.sent) == 1 and t.disp.sent[0]["task_id"] == f"adhoc-{got['adhoc_id']}"
    row = t.db.query("SELECT * FROM adhoc_queue")[0]
    assert row["state"] == "started" and row["started_ms"] == got["start_after_ms"]
    last = t.alerts.got[-1]
    assert last["kind"] == "adhoc_queue" and last["context"]["to"] == "gina"


def test_排队的到点又撞上下一趟日程就再往后排(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    # 第二天 22:00 不会撞;但把它挪到第二天 21:59 再跑一拍:又撞上那天的 22:00
    with t.db.tx() as c:
        c.execute("UPDATE adhoc_queue SET not_before_ms=?, created_ms=?", (毫秒(21, 59, 30, 16),
                                                                         毫秒(21, 59, 0, 16)))
    t.now["t"] = 毫秒(21, 59, 30, 16)
    跑(t.desk.tick())
    assert t.disp.sent == []
    nb = t.db.query("SELECT not_before_ms FROM adhoc_queue")[0]["not_before_ms"]
    assert nb > 毫秒(22, 0, 0, 16), got


def test_排了两个小时没轮上就作废_告诉派的人(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    t.now["t"] += ah.QUEUE_TTL_MS + 1
    跑(t.desk.tick())
    row = t.db.query("SELECT * FROM adhoc_queue")[0]
    assert row["state"] == "expired" and t.disp.sent == []
    assert t.alerts.got[-1]["context"]["to"] == "gina"


def test_排队的派不出去记失败(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    t.now["t"] = got["start_after_ms"]
    t.disp.refuse = "A 未定位"
    跑(t.desk.tick())
    row = t.db.query("SELECT * FROM adhoc_queue")[0]
    assert row["state"] == "failed" and "未定位" in row["note"]


def test_取消_派的人自己能_别的保安不能_开始跑了不能(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    with pytest.raises(PermissionError):
        t.desk.cancel(got["adhoc_id"], by="gary", may_others=False)
    t.desk.cancel(got["adhoc_id"], by="olga", may_others=True)
    assert t.db.query("SELECT state FROM adhoc_queue")[0]["state"] == "cancelled"
    with pytest.raises(AdhocError, match="不在排队"):
        t.desk.cancel(got["adhoc_id"], by="gina", may_others=False)
    with pytest.raises(AdhocError, match="没有"):
        t.desk.cancel("nope", by="gina", may_others=False)


def test_先看会怎样不派(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    p = t.desk.plan("A", 任务定义())
    assert p["queued"] is True and p["estimate_s"] > 0 and t.disp.sent == []
    assert t.db.query("SELECT COUNT(*) AS n FROM adhoc_queue")[0]["n"] == 0


# ------------------------------------------------------------ 今天的巡逻


def test_今天的巡逻_日程每一轮加临时的_按时间排_下一趟(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))         # 排到 22:00 那趟后面
    v = today_view(t.db, now_ms=t.now["t"], adhoc=t.desk)
    assert v["date"] == "2027-03-15"
    kinds = [(x["kind"], x["state"]) for x in v["items"]]
    assert kinds == [("schedule", "next"), ("adhoc", "queued")], kinds
    assert v["items"][1]["by"] == "gina" and v["items"][1]["end_ms"] > v["items"][1]["at_ms"]


def test_今天的巡逻_日程走完_没跑_在跑(台):
    t = 台
    t.now["t"] = 毫秒(23, 0)
    with t.db.tx() as c:
        c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, task_id, "
                  "decided_at) VALUES ('nightly', ?, 'started', 'A', 'sched-1', ?)",
                  (毫秒(22, 0), 毫秒(22, 0)))
    item = today_view(t.db, now_ms=t.now["t"])["items"][0]
    assert (item["state"], item["robot"]) == ("running", "A")
    with t.db.tx() as c:
        c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                  "received_at) VALUES ('A','b',1,'e1','task_failed',?,?,?)",
                  (json.dumps({"task_id": "sched-1", "reason": "第 2 点被挡住"}), 毫秒(22, 10),
                   毫秒(22, 10)))
    item = today_view(t.db, now_ms=t.now["t"])["items"][0]
    assert item["state"] == "failed" and "挡住" in item["reason"]
    # 换一天:到点了一直没机器人,窗口(30 分钟)过了 → missed
    t.now["t"] = 毫秒(23, 0, 0, 16)
    with t.db.tx() as c:
        c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, note, "
                  "decided_at) VALUES ('nightly', ?, 'no_robot', NULL, 'A 离线', ?)",
                  (毫秒(22, 0, 0, 16), 毫秒(22, 0, 0, 16)))
    item = today_view(t.db, now_ms=t.now["t"])["items"][0]
    assert item["state"] == "missed" and "离线" in item["reason"]
    t.now["t"] = 毫秒(22, 5, 0, 16)
    assert today_view(t.db, now_ms=t.now["t"])["items"][0]["state"] == "waiting"


# ------------------------------------------------------------ 外审 W53-1


def test_外审1_排队的到点起跑前再查监护_没人监护就不派(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    t.now["t"] = got["start_after_ms"]
    t.sup["why"] = "A 要人现场监护:先在手机上打开「我在现场监护」"
    跑(t.desk.tick())
    assert t.disp.sent == []
    row = t.db.query("SELECT * FROM adhoc_queue")[0]
    assert row["state"] == "failed" and "监护" in row["note"]


def test_外审2和3_通知经过真告警台_一次一条不合并_各推各的人(tmp_path):
    from d1max_site.alert_store import AlertDesk
    db = SiteDB(tmp_path / "s.db")
    import_bundle(db, 打包(tmp_path, 1), imported_by="alice", now_ms=1)
    now = {"t": 毫秒(21, 59)}
    d = 假派遣器(db)
    d.now = lambda: now["t"]
    alerts = AlertDesk(db, now_ms=lambda: now["t"])
    desk = AdhocDesk(db, d, now_ms=lambda: now["t"], alerts=alerts)
    a = 跑(desk.submit("A", 任务定义(), label="loop", by="g1"))
    b = 跑(desk.submit("A", 任务定义(), label="loop", by="g2"))
    now["t"] = max(a["start_after_ms"], b["start_after_ms"]) + ah.QUEUE_TTL_MS
    跑(desk.tick())                                   # 两条都作废:各告诉各的人
    rows = db.query("SELECT kind, context FROM alerts ORDER BY first_ms, key")
    sent = [r for r in rows if r["kind"] == "adhoc_patrol"]
    told = sorted(json.loads(r["context"])["to"] for r in rows if r["kind"] == "adhoc_queue")
    assert len(sent) == 2 and told == ["g1", "g2"], [dict(r) for r in rows]
    db.close()


def test_外审4_机器人明确没收_记成没走成_晚到的拒收也改(台):
    t = 台
    t.disp.ack = "rejected"                            # 代理忙:回 rejected、原因 busy
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert got["ack"]["result"] == "rejected"
    row = t.db.query("SELECT * FROM adhoc_queue")[0]
    assert row["state"] == "failed" and "rejected" in row["note"]
    assert [x["state"] for x in today_view(t.db, now_ms=t.now["t"], adhoc=t.desk)["items"]
            if x["kind"] == "adhoc"] == ["failed"]
    # 先收下(没回执)、后来才说不收
    t.disp.ack = "accepted"
    t.disp.running.clear()
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    from d1max_contract.messages import AckResult
    for cb in t.disp.acks:
        cb(SimpleNamespace(task_id=got["task_id"], result=AckResult.REJECTED, reason="busy"))
    row = t.db.query("SELECT * FROM adhoc_queue WHERE task_id=?", (got["task_id"],))[0]
    assert row["state"] == "failed"


def test_外审5_正在跑日程_排到它走完之后_不越推越远(台):
    t = 台
    t.disp.running["A"] = "sched-abc"
    with t.db.tx() as c:
        c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, task_id, "
                  "decided_at) VALUES ('nightly', ?, 'started', 'A', 'sched-abc', ?)",
                  (毫秒(20, 0), 毫秒(20, 0)))
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at) VALUES ('c', 'sched-abc', 'A', 'patrol', '{}', 'schedule:nightly', "
                  "?)", (毫秒(20, 0),))
    t.now["t"] = 毫秒(20, 0, 30)
    est = t.desk.estimate_s("A", parse_mission(任务定义()))
    p = t.desk.plan("A", 任务定义())
    end = max(毫秒(20, 0) + est * 1000, t.now["t"] + 60_000)
    assert p["queued"] and p["start_ms"] == end + 60_000, "两分钟左右,不是四十分钟"


def test_外审7_到点还没派的那一轮也算撞(台):
    t = 台
    t.now["t"] = 毫秒(22, 0, 1)                       # 22:00 到点,执行器下一拍还没来
    assert t.desk.plan("A", 任务定义())["queued"] is True
    with t.db.tx() as c:                               # 那一轮派出去了(在别处跑):不再算
        c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, task_id, "
                  "decided_at) VALUES ('nightly', ?, 'skip', NULL, NULL, ?)",
                  (毫秒(22, 0), 毫秒(22, 0)))
    assert t.desk.plan("A", 任务定义())["queued"] is False
    t.now["t"] = 毫秒(22, 31)                          # 窗口(30 分钟)过了:也不算
    with t.db.tx() as c:
        c.execute("DELETE FROM schedule_runs")
    assert t.desk.plan("A", 任务定义())["queued"] is False


def test_外审8_派不出去的不排队_当场说(台):
    t = 台
    with t.db.tx() as c:                               # 日程写明了 A:A 派不出去也照样落到它头上
        c.execute("UPDATE route_schedules SET definition=json_set(definition, '$.robot', 'A')")
    t.now["t"] = 毫秒(21, 59)
    t.disp.cannot["A"] = "A 不在线"
    with pytest.raises(DispatchRefused, match="不在线"):
        跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    t.disp.cannot.clear()
    t.disp.loaded = ("estate-1", "6")
    with pytest.raises(DispatchRefused, match="加载的是"):
        跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert t.db.query("SELECT COUNT(*) AS n FROM adhoc_queue")[0]["n"] == 0


def test_外审11_排到明天的也在今天的列表里_能取消(台):
    t = 台
    with t.db.tx() as c:
        c.execute("UPDATE route_schedules SET definition=json_set(definition, '$.at', '00:10')")
    from d1max_site.catalog import active_bundle
    assert active_bundle(t.db).schedule.entries[0].at_h == 0
    t.now["t"] = 毫秒(23, 59)
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    if not got["queued"]:                              # 估时不够长撞不上 00:10:直接排一条明天的
        with t.db.tx() as c:
            c.execute("UPDATE adhoc_queue SET state='queued', not_before_ms=?",
                      (毫秒(0, 20, 0, 16),))
    items = [x for x in today_view(t.db, now_ms=t.now["t"], adhoc=t.desk)["items"]
             if x["kind"] == "adhoc"]
    assert items and items[0]["state"] in ("queued", "running")


def test_外审12_今天的巡逻和先看都带站点时区的偏移(台):
    t = 台
    assert today_view(t.db, now_ms=t.now["t"], adhoc=t.desk)["utc_offset_min"] == 480
    assert t.desk.plan("A", 任务定义())["utc_offset_min"] == 480


def _站在(t, x, y, path="straight"):
    t.disp.clients["A"] = SimpleNamespace(
        telemetry=SimpleNamespace(pose=SimpleNamespace(x=x, y=y, yaw=0.0)),
        capabilities=SimpleNamespace(tasks={"goto": {"path": path}}))


def test_用户1031_走完回起点_直线的沿来路回_会规划的直接回_本来在起点不加(台):
    t = 台
    _站在(t, -2.0, -1.0)
    跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    wps = t.disp.sent[-1]["wire"]["waypoints"]
    assert [w["name"] for w in wps] == ["a", "b", "return-1", "start"], "直线:倒着走回 a 再回起点"
    assert wps[-1]["pose"]["position"]["x"] == -2.0 and wps[-1]["pose"]["position"]["y"] == -1.0
    t.disp.running.clear()
    _站在(t, -2.0, -1.0, path="planned")
    跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert [w["name"] for w in t.disp.sent[-1]["wire"]["waypoints"]] == ["a", "b", "start"]
    t.disp.running.clear()
    _站在(t, 0.6, 0.6)                                    # 就站在最后一点:不加
    跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert [w["name"] for w in t.disp.sent[-1]["wire"]["waypoints"]] == ["a", "b"]
    t.disp.clients.clear()                               # 不知道在哪:不加
    t.disp.running.clear()
    跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    assert [w["name"] for w in t.disp.sent[-1]["wire"]["waypoints"]] == ["a", "b"]


def test_用户1031_排队的到点按那时站的地方回(台):
    t = 台
    t.now["t"] = 毫秒(21, 59)
    got = 跑(t.desk.submit("A", 任务定义(), label="loop", by="gina"))
    _站在(t, 3.0, 4.0)
    t.now["t"] = got["start_after_ms"]
    跑(t.desk.tick())
    start = t.disp.sent[-1]["wire"]["waypoints"][-1]
    assert start["name"] == "start" and start["pose"]["position"]["x"] == 3.0
    stored = json.loads(t.db.query("SELECT mission FROM adhoc_queue")[0]["mission"])
    assert [w["name"] for w in stored["waypoints"]] == ["a", "b"], "存的还是原来那条"
