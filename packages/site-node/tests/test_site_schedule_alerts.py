"""排程没派出去要报警(W00c6c,核查 C)。

以前除了 ``started``,排程每一轮的别的去向(``no_robot``、``ambiguous``、``skew``、
``dispatch_failed``、``alarm``、``skip``)只在 ``schedule_runs`` 里记一行,没人知道 —— 连排程自己写了
``on_missed: alarm`` 也不推给人;只有整拍抛异常才报 ``schedule_died``。现在执行器每新记一行(或
``started`` 被回执改成 ``dispatch_failed``、回执超时)都告诉告警源一声。台子同
``test_site_schedule.py``。
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


async def _拍(t) -> None:
    await t.send(t.sched.tick())


def _去向(t) -> list[tuple]:
    return [(e, r, o) for e, r, o, _ in t.heard]


async def test_正常起跑_不报(站):
    t = 站
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["started"]
    assert t.heard == []


async def test_狗掉线到点_报一次no_robot_再拍不重复_窗口过了报skip(站):
    t = 站
    await t.agent.close()
    await t.broker.drain()
    t.agent = None
    t.clock.ms = 毫秒(22, 0, 30)
    await _拍(t)
    await _拍(t)
    await _拍(t)
    assert _去向(t) == [("nightly", None, "no_robot")], "同一轮同一种去向只报一次"
    assert "不在线" in t.heard[0][3]
    t.clock.ms = 毫秒(22, 45)
    await _拍(t)
    await _拍(t)
    assert _去向(t) == [("nightly", None, "no_robot"), ("nightly", None, "skip")]


async def test_排程写了on_missed_alarm_窗口过了报alarm(站, tmp_path):
    t = 站
    要报 = 排程.replace("on_missed: skip", "on_missed: alarm")
    import_bundle(t.db, 打包(tmp_path, 2, schedule=要报), imported_by="alice", now_ms=t.clock())
    await t.agent.close()
    await t.broker.drain()
    t.agent = None
    t.clock.ms = 毫秒(22, 45)
    await _拍(t)
    assert ("nightly", None, "alarm") in _去向(t)


async def test_排程写了robot_没派出去报在那只狗名下(站, tmp_path):
    t = 站
    指定 = 排程.replace("    on_missed: skip\n", "    on_missed: skip\n    robot: B\n")
    import_bundle(t.db, 打包(tmp_path, 2, schedule=指定), imported_by="alice", now_ms=t.clock())
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert _去向(t) == [("nightly", "B", "no_robot")]


async def test_不止一台能派_报ambiguous(站):
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
    assert _去向(t) == [("nightly", None, "ambiguous")]


async def test_钟不可信_报skew(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)
    await t.start()
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    t.clock.ms = 毫秒(22, 0, 30)
    heard = []
    s = SiteScheduler(t.db, t.site, now_ms=t.clock, on_outcome=lambda *a: heard.append(a),
                      time_reference=lambda: (t.clock() - 3_600_000, "ntp"))
    await t.run(2)
    await t.send(s.tick())
    await t.send(s.tick())
    assert [(e, r, o) for e, r, o, _ in heard] == [("nightly", None, "skew")]
    await t.close()


async def test_发之前就被拦下_报dispatch_failed_在那只狗名下(站, monkeypatch):
    from d1max_site.dispatcher import DispatchRefused
    t = 站

    async def 拦(*a, **k):
        raise DispatchRefused("A 急停没松开")
    monkeypatch.setattr(t.site, "patrol", 拦)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert _去向(t) == [("nightly", "A", "dispatch_failed")] and "急停" in t.heard[0][3]


async def test_狗回执拒收_started改成dispatch_failed_报一次(站):
    t = 站
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    [run] = t.sched.runs("nightly")
    rej = Ack(command_id="c-x", task_id=run["task_id"], result=AckResult.REJECTED, reason="busy")
    t.sched._on_ack(rej)
    t.sched._on_ack(rej)
    assert _去向(t) == [("nightly", "A", "dispatch_failed")], "改成 dispatch_failed 的那一次才报"
    assert "busy" in t.heard[0][3]


async def test_回执超时_报unconfirmed(站, monkeypatch):
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
    assert _去向(t) == [("nightly", "A", "unconfirmed")]


async def test_同一拍给了更优先的_displaced不报(站, tmp_path):
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
    on_missed: skip
    robot: A
"""
    import_bundle(t.db, 打包(tmp_path, 2, schedule=两条), imported_by="alice", now_ms=t.clock())
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    got = {r["entry_id"]: r["outcome"] for r in t.sched.runs()}
    assert got == {"vip": "started", "nightly": "displaced"}, got
    assert t.heard == []


async def test_报警回调炸了_排程照走_账照记(站):
    t = 站
    await t.agent.close()
    await t.broker.drain()
    t.agent = None

    def 炸(*a):
        raise RuntimeError("告警库锁住了")
    t.sched.on_outcome = 炸
    t.clock.ms = 毫秒(22, 0, 30)
    await _拍(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["no_robot"]


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
    ("alarm", "schedule_missed", "P1"), ("skip", "schedule_skipped", "P2"),
    ("unconfirmed", "schedule_unconfirmed", "P2")])
def test_去向对应的告警与级别(告警, outcome, kind, level):
    desk, src = 告警
    src.on_schedule_outcome("nightly", "A", outcome, "A 不在线")
    [a] = desk.book.all()
    assert (a.kind, a.level.value, a.robot) == (kind, level, "A")
    assert "nightly" in a.title and a.detail == "A 不在线"


def test_没派给哪只狗_报在站点名下(告警):
    from d1max_site.alert_sources import SITE
    desk, src = 告警
    src.on_schedule_outcome("nightly", None, "ambiguous", "能派的狗不止一台")
    [a] = desk.book.all()
    assert a.robot == SITE


@pytest.mark.parametrize("outcome", ["started", "displaced"])
def test_不是没跑的去向不报(告警, outcome):
    desk, src = 告警
    src.on_schedule_outcome("nightly", "A", outcome, "")
    assert not desk.book.all()
