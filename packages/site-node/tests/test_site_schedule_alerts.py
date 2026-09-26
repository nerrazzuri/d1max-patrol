"""排程没派出去要报警(W00c6c,核查 C)。

以前除了 ``started``,排程每一轮的别的去向(``no_robot``、``ambiguous``、``skew``、
``dispatch_failed``、``alarm``、``skip``)只在 ``schedule_runs`` 里记一行,没人知道 —— 连排程自己写了
``on_missed: alarm`` 也不推给人;只有整拍抛异常才报 ``schedule_died``。现在执行器在**知道这一轮
不会按时跑**的时候告诉告警源一声,**一轮只说一次**。台子同 ``test_site_schedule.py``。

内审(先修再批准)之后的规矩:

- 狗在忙、这一拍给了更优先的:记 ``busy``/``displaced``,**不说** —— 多半等一会儿就跑了;窗口过了
  还没跑,由 ``skip``/``alarm`` 那一步说。
- 没狗可派、不止一台、钟不可信:先等一个宽限期(窗口与 5 分钟取小),还派不出去才说。
- 狗明确拒收、窗口过了(``alarm``/``skip``)、狗要人监护(``supervised``):当场说。
- 回执超时:过 2 分钟还没见狗在跑这一趟(也没收到晚到的回执、没有结果),说 ``lost``。
- 说过了(``told_ms``)才算;回调炸了下一拍再说。
"""

from __future__ import annotations

import pytest
from test_site_dispatcher import 台子
from test_site_schedule import 打包, 排程, 毫秒

from d1max_contract.messages import Ack, AckResult
from d1max_site.catalog import import_bundle
from d1max_site.scheduler import SiteScheduler


@pytest.fixture
async def 站(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)
    await t.start()
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    t.heard = []
    t.sched = SiteScheduler(t.db, t.site, now_ms=t.clock,
                            on_outcome=lambda *a: t.heard.append(a))
    yield t
    await t.close()


async def _拍(t, 时=None, 分=None, 秒=0) -> None:
    if 时 is not None:
        t.clock.ms = 毫秒(时, 分, 秒)
    await t.send(t.sched.tick())


def _去向(t) -> list[tuple]:
    return [(e, r, o) for e, r, o, _ in t.heard]


def _换排程(t, tmp_path, text, v=2):
    import_bundle(t.db, 打包(tmp_path, v, schedule=text), imported_by="alice", now_ms=t.clock())


async def _狗掉线(t):
    await t.agent.close()
    await t.broker.drain()
    t.agent = None


# ------------------------------------------------------------ 该跑的跑了:不说


async def test_正常起跑_不说(站):
    t = 站
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["started"]
    assert t.heard == []


async def test_给了更优先的_下一拍狗在忙_跑完照常起跑_全程不说(站, tmp_path):
    """内审阻断 1:以前第二拍狗在跑 vip,nightly 记 ``no_robot`` 当场报 P1,随后它在窗口里照常起跑。"""
    t = 站
    两条 = """\
timezone: Asia/Kuala_Lumpur
entries:
  - id: vip
    mission: loop
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 30
    on_missed: skip
    priority: 5
    robot: A
  - id: nightly
    mission: loop
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 30
    on_missed: alarm
    robot: A
"""
    _换排程(t, tmp_path, 两条)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert {r["entry_id"]: r["outcome"] for r in t.sched.runs()} == \
        {"vip": "started", "nightly": "displaced"}
    await t.run(5)
    await _拍(t)                                          # 狗在跑 vip
    assert "busy" in [r["outcome"] for r in t.sched.runs("nightly")]
    for _ in range(40):                                   # vip 跑完
        await t.run(20)
        await _拍(t)
        if "started" in [r["outcome"] for r in t.sched.runs("nightly")]:
            break
    assert "started" in [r["outcome"] for r in t.sched.runs("nightly")]
    t.clock.ms = 毫秒(22, 20)
    await _拍(t)
    assert t.heard == []


async def test_短暂派不出去_宽限期里恢复了_不说(站, monkeypatch):
    t = 站
    real = t.site.dispatchable
    monkeypatch.setattr(t.site, "dispatchable", lambda rid, kind: f"{rid} 不在线")
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["no_robot"]
    monkeypatch.setattr(t.site, "dispatchable", real)
    await _拍(t, 22, 2)
    assert "started" in [r["outcome"] for r in t.sched.runs("nightly")]
    await _拍(t, 22, 10)
    assert t.heard == []


# ------------------------------------------------------------ 没派出去:宽限期之后说一次


async def test_狗掉线到点_宽限期过了说一次_窗口过了不再另说(站):
    """内审应修 1:以前一轮报两三条(先 no_robot、窗口过了再 skip/alarm)。"""
    t = 站
    await _狗掉线(t)
    await _拍(t, 22, 0, 30)
    await _拍(t, 22, 3)
    assert t.heard == [], "宽限期(半个窗口与 5 分钟取小)里不说"
    await _拍(t, 22, 5, 40)
    await _拍(t, 22, 6)
    assert _去向(t) == [("nightly", None, "no_robot")] and "不在线" in t.heard[0][3]
    await _拍(t, 22, 45)
    await _拍(t, 22, 46)
    assert _去向(t) == [("nightly", None, "no_robot")], "窗口过了记 skip,但这一轮已经说过了"
    assert "skip" in [r["outcome"] for r in t.sched.runs("nightly")]


async def test_窗口很短_宽限期跟着缩到半个窗口(站, tmp_path):
    t = 站
    _换排程(t, tmp_path, 排程.replace("window_min: 30", "window_min: 2"))
    await _狗掉线(t)
    await _拍(t, 22, 0, 10)
    await _拍(t, 22, 1, 0)
    assert t.heard == []
    await _拍(t, 22, 1, 20)
    assert _去向(t) == [("nightly", None, "no_robot")], "窗口关之前就说了"


async def test_不止一台能派_宽限期过了说ambiguous(站):
    t = 站
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    await t.site.add_robot("B")
    t.site.clients["B"].status = t.site.clients["A"].status
    t.site.clients["B"].capabilities = t.site.clients["A"].capabilities
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    t.site.clients["B"].status_live_at = t.clock()
    await _拍(t)
    assert t.heard == []
    t.clock.ms = 毫秒(22, 6)
    for rid in ("A", "B"):                                # 假钟跳了几分钟:两台都还新鲜
        t.site.clients[rid].status_live_at = t.clock()
    await _拍(t)
    assert _去向(t) == [("nightly", None, "ambiguous")]


async def test_钟不可信_宽限期过了说skew(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)
    await t.start()
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    heard = []
    s = SiteScheduler(t.db, t.site, now_ms=t.clock, on_outcome=lambda *a: heard.append(a),
                      time_reference=lambda: (t.clock() - 3_600_000, "ntp"))
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await t.send(s.tick())
    assert heard == []
    t.clock.ms = 毫秒(22, 6)
    await t.send(s.tick())
    await t.send(s.tick())
    assert [(e, r, o) for e, r, o, _ in heard] == [("nightly", None, "skew")]
    await t.close()


async def test_狗一直在忙_不说no_robot_窗口过了按排程说skip(站, monkeypatch):
    t = 站
    monkeypatch.setattr(t.site, "busy", lambda rid: "goto-别的")
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    await _拍(t, 22, 10)
    assert t.heard == [] and "busy" in [r["outcome"] for r in t.sched.runs("nightly")]
    await _拍(t, 22, 45)
    assert _去向(t) == [("nightly", None, "skip")]


# ------------------------------------------------------------ 当场说


async def test_站点停了一整个窗口_窗口过了当场说_alarm与skip(站, tmp_path):
    t = 站
    await _拍(t, 22, 45)
    assert _去向(t) == [("nightly", None, "skip")]
    _换排程(t, tmp_path, 排程.replace("on_missed: skip", "on_missed: alarm"))
    t.heard.clear()
    t.clock.ms = 毫秒(22, 45) + 86_400_000                  # 第二天,整窗口没拍
    await _拍(t)
    assert _去向(t) == [("nightly", None, "alarm")]


async def test_排程写了robot_说在那只狗名下(站, tmp_path):
    """内审小问题 5:以前只钉了 ``no_robot`` 的归属。"""
    t = 站
    _换排程(t, tmp_path,
          排程.replace("    on_missed: skip\n", "    on_missed: skip\n    robot: B\n"))
    await _拍(t, 22, 45)
    assert _去向(t) == [("nightly", "B", "skip")]


async def test_发之前就被拦下_当场说dispatch_failed_在那只狗名下(站, monkeypatch):
    from d1max_site.dispatcher import DispatchRefused
    t = 站

    async def 拦(*a, **k):
        raise DispatchRefused("A 急停没松开")
    monkeypatch.setattr(t.site, "patrol", 拦)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert _去向(t) == [("nightly", "A", "dispatch_failed")] and "急停" in t.heard[0][3]


async def test_狗回执拒收_started改成dispatch_failed_说一次(站):
    t = 站
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    [run] = t.sched.runs("nightly")
    rej = Ack(command_id="c-x", task_id=run["task_id"], result=AckResult.REJECTED, reason="busy")
    t.sched._on_ack(rej)
    t.sched._on_ack(rej)
    assert _去向(t) == [("nightly", "A", "dispatch_failed")], "改成 dispatch_failed 的那一次才说"
    assert "busy" in t.heard[0][3]


async def test_狗要人监护_当场说supervised_一轮一次(站, monkeypatch):
    """内审小问题 6:要人监护的狗排程到点每轮 P1、还会升到响声 —— 人要做的是改配置,不是立刻动身。
    改成单独的去向 ``supervised``(告警源给 P2),一轮只说一次。"""
    t = 站
    monkeypatch.setattr(t.site, "autonomy", lambda rid: "supervised")
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    await _拍(t, 22, 6)
    await _拍(t, 22, 45)
    assert _去向(t) == [("nightly", None, "supervised")]


# ------------------------------------------------------------ 回执超时


async def test_回执超时_狗一直没接_两分钟后说lost(站, monkeypatch):
    """内审应修 4:以前只报 P2、之后不跟进;狗早一秒掉线就是 P1。"""
    t = 站
    _丢回执(monkeypatch, t)                               # 发之前记账照走:这一轮算起跑过了
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    await _拍(t, 22, 1, 30)
    assert t.heard == []
    await _拍(t, 22, 3)
    await _拍(t, 22, 4)
    assert _去向(t) == [("nightly", "A", "lost")]


async def test_回执超时_其实狗收到了在跑_不说(站, monkeypatch):
    from d1max_contract.dispatch import DispatchTimeout
    t = 站
    real_send = t.site._send

    async def 回执丢(*a, **k):
        await real_send(*a, **k)
        raise DispatchTimeout("回执丢了")
    monkeypatch.setattr(t.site, "_send", 回执丢)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    await t.run(5)
    await _拍(t, 22, 3)
    await t.run(600)
    await _拍(t, 22, 20)
    assert t.heard == []


def _丢回执(monkeypatch, t):
    from d1max_contract.dispatch import DispatchTimeout

    async def 没发出去(*a, before_send=None, **k):
        if before_send is not None:
            with t.db.tx() as tx:                     # 派遣器给记账开的那个事务
                before_send(None, tx)
        raise DispatchTimeout("回执丢了")
    monkeypatch.setattr(t.site, "_send", 没发出去)


async def test_回执超时_晚到的回执说收下了_不算丢(站, monkeypatch):
    t = 站
    _丢回执(monkeypatch, t)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    [run] = t.sched.runs("nightly")
    with t.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, ack_result) VALUES ('late', ?, 'A', 'patrol', '{}', 'x', 1, "
                  "'accepted')", (run["task_id"],))
    monkeypatch.setattr(t.site, "busy", lambda rid: None)   # 收下了,还排着没开跑
    await _拍(t, 22, 4)
    assert t.heard == []


async def test_回执超时_狗在跑这一趟_不算丢(站, monkeypatch):
    t = 站
    _丢回执(monkeypatch, t)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    [run] = t.sched.runs("nightly")
    monkeypatch.setattr(t.site, "busy", lambda rid: run["task_id"])
    await _拍(t, 22, 4)
    assert t.heard == []


async def test_老库里的账_升级之后当说过了_不翻出来(tmp_path):
    import sqlite3

    from d1max_site.db import SiteDB
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE schedule_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT "
              "NOT NULL, scheduled_ms INTEGER NOT NULL, outcome TEXT NOT NULL, robot_id TEXT, "
              "task_id TEXT, result TEXT, note TEXT NOT NULL DEFAULT '', decided_at INTEGER "
              "NOT NULL, UNIQUE (entry_id, scheduled_ms, outcome))")
    c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, note, decided_at) "
              "VALUES ('nightly', 1, 'dispatch_failed', 'x', 1)")
    c.commit()
    c.close()
    db = SiteDB(path)
    assert [r["told_ms"] for r in db.query("SELECT told_ms FROM schedule_runs")] == [0]
    db.close()


# ------------------------------------------------------------ 说不出去就下一拍再说


async def test_回调炸了_账照记_下一拍再说(站, monkeypatch):
    from d1max_site.dispatcher import DispatchRefused
    t = 站

    async def 拦(*a, **k):
        raise DispatchRefused("A 急停没松开")
    monkeypatch.setattr(t.site, "patrol", 拦)
    n = {"k": 0}

    def 头一次炸(*a):
        n["k"] += 1
        if n["k"] <= 2:                                   # 记账时说一次、同一拍补说一次,都炸
            raise RuntimeError("告警库锁住了")
        t.heard.append(a)
    t.sched.on_outcome = 头一次炸
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["dispatch_failed"]
    assert t.heard == []
    await _拍(t)
    await _拍(t)
    assert _去向(t) == [("nightly", "A", "dispatch_failed")]


# ------------------------------------------------------------ 告警源:去向 → 告警


@pytest.fixture
def 告警(tmp_path):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "a.db")
    desk = AlertDesk(db, now_ms=lambda: 1_000)
    yield desk, SiteAlertSources(desk, now_ms=lambda: 1_000)
    db.close()


@pytest.mark.parametrize("outcome, kind, level", [
    ("no_robot", "schedule_missed", "P1"), ("ambiguous", "schedule_missed", "P1"),
    ("skew", "schedule_missed", "P1"), ("dispatch_failed", "schedule_missed", "P1"),
    ("alarm", "schedule_missed", "P1"), ("lost", "schedule_missed", "P1"),
    ("skip", "schedule_skipped", "P2"), ("supervised", "schedule_blocked", "P2")])
def test_去向对应的告警与级别(告警, outcome, kind, level):
    desk, src = 告警
    src.on_schedule_outcome("nightly", "A", outcome, "A 不在线")
    [a] = desk.book.all()
    assert (a.kind, a.level.value, a.robot) == (kind, level, "A")
    assert "nightly" in a.title and "A 不在线" in a.detail


def test_没派给哪只狗_报在站点名下(告警):
    from d1max_site.alert_sources import SITE
    desk, src = 告警
    src.on_schedule_outcome("nightly", None, "ambiguous", "能派的狗不止一台")
    [a] = desk.book.all()
    assert a.robot == SITE


@pytest.mark.parametrize("outcome", ["started", "displaced", "busy"])
def test_不是没跑的去向不报(告警, outcome):
    desk, src = 告警
    src.on_schedule_outcome("nightly", "A", outcome, "")
    assert not desk.book.all()


def test_几条排程合成一条告警_标题里都在(告警):
    """内审应修 2:以前合并之后标题只剩最后一条排程,前面那条从告警里消失。"""
    desk, src = 告警
    src.on_schedule_outcome("nightly", None, "no_robot", "A 不在线")
    src.on_schedule_outcome("gate", None, "no_robot", "A 不在线")
    src.on_schedule_outcome("gate", None, "no_robot", "A 不在线")
    [a] = desk.book.all()
    assert a.count == 3 and "nightly" in a.title and "gate" in a.title
    assert a.title.count("gate") == 1
