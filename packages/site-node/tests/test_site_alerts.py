"""W00c5a:告警搬到站点。告警簿落站点库(重启读回);站点从 MQTT 的事实里自己判
(「狗只报事实,判定在站点」)。这里直接喂契约对象,不起 broker;
端到端见 ``test_site_alerts_api.py``。"""

from __future__ import annotations

import pytest

from d1max_contract.hal import Fault
from d1max_contract.messages import (
    Event,
    MapPose,
    Ready,
    Status,
    TaskState,
    TaskSummary,
    Telemetry,
    fault_event_data,
)
from d1max_site.alert_sources import SiteAlertSources
from d1max_site.alert_store import AlertDesk
from d1max_site.alerts import AlertNotFound, Channel
from d1max_site.db import SiteDB


class 钟:
    def __init__(self) -> None:
        self.ms = 1_800_000_000_000

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def 台(tmp_path):
    c = 钟()
    db = SiteDB(tmp_path / "site.db")
    pushed: list[dict] = []
    desk = AlertDesk(db, now_ms=c, publish=pushed.append)
    stale: set[str] = set()
    src = SiteAlertSources(desk, now_ms=c, is_stale=lambda rid: rid in stale)
    yield c, db, desk, src, pushed, stale
    db.close()


def _ready(**kw) -> Ready:
    d = dict(control=True, motion=True, estop_clear=True, loc_ok=True) | kw
    return Ready(**d)


def _status(*, online=True, task=None, **ready) -> Status:
    return Status(online=online, boot_id="b1", ready=_ready(**ready), control_epoch=1,
                  last_seen=1, task=task)


def _running(tid="t1", kind="patrol") -> TaskSummary:
    return TaskSummary(task_id=tid, kind=kind, state=TaskState.RUNNING)


_seq = iter(range(1, 10_000))


def _ev(kind: str, **data) -> Event:
    n = next(_seq)
    return Event(event_id=f"e{n}", seq=n, boot_id="b1", stamp=1, kind=kind, data=data)


def _kinds(desk) -> list[str]:
    return sorted(a.kind for a in desk.book.all())


# ------------------------------------------------------------ 告警簿落库


def test_告警落库_站点重启读回_序号接着走_还在吸收(台, tmp_path):
    c, db, desk, src, pushed, _ = 台
    a = desk.raise_alert(kind="stuck", robot="A", title="点位 gate 没到")
    desk.ack(a.key, who="gina")
    b = desk.raise_alert(kind="stuck", robot="A", title="点位 pond 没到")
    assert b.key != a.key
    desk2 = AlertDesk(db, now_ms=c)
    got = {x.key: x for x in desk2.book.all()}
    assert got[a.key].acked_by == "gina" and got[b.key].count == 1
    c.ms += 1000
    again = desk2.raise_alert(kind="stuck", robot="A", title="点位 pond 又没到")
    assert again.key == b.key and again.count == 2, "重启后未确认的那条仍在吸收"
    desk2.ack(b.key, who="gina")
    new = desk2.raise_alert(kind="stuck", robot="A", title="x")
    assert new.key not in (a.key, b.key), "重启后序号不许撞"
    assert [p["kind"] for p in pushed] == ["alert"] * 3 and pushed[-1]["alert"]["key"] == b.key


def test_确认要人名_不存在的键404(台):
    c, db, desk, *_ = 台
    a = desk.raise_alert(kind="run_done", robot="A", title="跑完了")
    with pytest.raises(ValueError):
        desk.ack(a.key, who="")
    with pytest.raises(AlertNotFound):
        desk.ack("A/stuck#99", who="gina")
    r = desk.resolve(a.key, who="gina")
    assert r.resolved_by == "gina" and r.resolved_ms == c.ms
    assert desk.open() == []


def test_P1两分钟没人确认升推送_五分钟升声音_确认后不再升(台):
    c, db, desk, src, pushed, _ = 台
    a = desk.raise_alert(kind="estop_pressed", robot="A", title="急停被按下")
    c.ms += 2 * 60_000 + 1
    assert [(x.key, ch) for x, ch in desk.escalate()] == [(a.key, Channel.PUSH)]
    c.ms += 3 * 60_000
    assert [ch for _, ch in desk.escalate()] == [Channel.SOUND]
    assert desk.escalate() == []
    b = desk.raise_alert(kind="fallen", robot="A", title="跌倒")
    desk.ack(b.key, who="gina")
    c.ms += 10 * 60_000
    assert desk.escalate() == []
    升过 = {x.key: x.escalated for x in AlertDesk(db, now_ms=c).book.all()}
    assert 升过 == {a.key: 2, b.key: 0}, "升到第几档也落库"


# ------------------------------------------------------------ 站点的判定


def test_急停_由清变按下才报一次_掉线的遗言不算(台):
    c, db, desk, src, *_ = 台
    src.on_status("A", _status())
    src.on_status("A", _status(estop_clear=False))
    src.on_status("A", _status(estop_clear=False))
    [a] = desk.book.all()
    assert a.kind == "estop_pressed" and a.count == 1, "同一次急停只报一次(不是按状态帧数)"
    src.on_status("A", _status())
    src.on_status("A", _status(online=False, estop_clear=False, control=False, motion=False,
                               loc_ok=False))
    assert _kinds(desk).count("estop_pressed") == 1, "遗言里 ready 全是假,不是急停"


def test_开跑只报一次_跑完_失败分电量与其他_人点的中止不报(台):
    c, db, desk, src, *_ = 台
    src.on_status("A", _status(task=_running("t1")))
    src.on_status("A", _status(task=_running("t1")))
    src.on_event("A", _ev("task_done", task_id="t1"))
    src.on_event("A", _ev("task_failed", task_id="t2", reason="电量 22% 低于中止线 25%"))
    src.on_event("A", _ev("task_failed", task_id="t3", reason="关节过温"))
    src.on_event("A", _ev("task_aborted", task_id="t4", reason="operator"))
    src.on_event("A", _ev("task_preempted", task_id="t5"))
    assert _kinds(desk) == ["battery_abort", "run_abort", "run_done", "run_start"]
    ra = next(a for a in desk.book.all() if a.kind == "run_abort")
    assert "关节过温" in ra.detail and "电量" not in ra.title


def test_回待命点那一趟失败_标题说是回待命点没成_不说整趟中止(台):
    """W00c6b 内审小问题:回程巡检跳点、停在半路报 ``task_failed``,以前标题是「整趟中止了」。"""
    c, db, desk, src, *_ = 台
    src.on_event("A", _ev("task_failed", task_id="standby-ab12", reason="点位 b 失败: 到点超时"))
    src.on_event("A", _ev("task_failed", task_id="standby-cd34",
                          reason="电量 22% 低于中止线 25%,原地停止"))
    got = {a.kind: a for a in desk.book.all()}
    assert "回待命点" in got["run_abort"].title and "整趟" not in got["run_abort"].title
    assert "回待命点" in got["battery_abort"].title


def test_推送流里的没回待命点_记一条P2_别的推送不管(台):
    c, db, desk, src, *_ = 台
    src.on_feed({"kind": "alert", "alert": {}})
    src.on_feed({"kind": "standby_failed", "robot_id": "A", "after": "t9",
                 "reason": "A 要人现场监护"})
    [a] = desk.book.all()
    assert a.kind == "standby_failed" and a.robot == "A" and a.level.value == "P2"
    assert "t9" in a.detail and "监护" in a.detail


def test_点位没到报卡住_到了不报(台):
    c, db, desk, src, *_ = 台
    src.on_event("A", _ev("patrol_waypoint", task_id="t1", index=0, name="gate", ok=True, note=""))
    src.on_event("A", _ev("patrol_waypoint", task_id="t1", index=1, name="pond", ok=False,
                          note="导航失败"))
    [a] = desk.book.all()
    assert a.kind == "stuck" and "pond" in a.title and a.detail == "导航失败"


def test_跑着任务时丢定位报一次_空闲时丢不报(台):
    c, db, desk, src, *_ = 台
    src.on_status("A", _status(loc_ok=False))
    assert _kinds(desk) == []
    src.on_status("A", _status(task=_running(), loc_ok=False))
    src.on_status("A", _status(task=_running(), loc_ok=False))
    assert _kinds(desk).count("loc_lost_paused") == 1
    src.on_status("A", _status(task=_running()))
    src.on_status("A", _status(task=_running(), loc_ok=False))
    n = sum(a.count for a in desk.book.all() if a.kind == "loc_lost_paused")
    assert n == 2, "恢复过再丢是新的一次(聚合窗口里合进同一条,count 加一)"


def test_故障里有跌倒字样才报_同一组不重复_消了再来再报(台):
    c, db, desk, src, *_ = 台
    fall = fault_event_data((Fault(code="3", fatal=True, text="机身跌倒"),))
    src.on_event("A", _ev("robot_fault", **fault_event_data((Fault("9", False, "风扇"),))))
    assert _kinds(desk) == []
    src.on_event("A", _ev("robot_fault", **fall))
    src.on_event("A", _ev("robot_fault", **fall))
    src.on_event("A", _ev("robot_fault", faults=[]))
    src.on_event("A", _ev("robot_fault", **fall))
    [a] = desk.book.all()
    assert a.kind == "fallen" and a.count == 2, "同一根因在聚合窗口里合成一条,只数两次"
    src.on_event("A", _ev("robot_fault", faults="坏的"))          # 形状不对:不炸、不报


def test_掉线_跑着任务是P1_空闲是P2_回来了再掉再报(台):
    c, db, desk, src, pushed, stale = 台
    src.on_status("A", _status(task=_running()))
    src.on_status("A", _status(online=False, estop_clear=False))
    src.on_status("A", _status(online=False, estop_clear=False))
    src.on_status("B", _status())
    stale.add("B")
    src.tick()
    src.tick()
    got = {a.robot: (a.kind, a.level.value) for a in desk.book.all()}
    assert got == {"A": ("robot_offline", "P1"), "B": ("robot_offline_idle", "P2")}
    stale.discard("B")
    src.on_status("B", _status())
    c.ms += 61_000                                    # 回来连着在线过了迟滞
    stale.add("B")
    src.tick()
    assert sum(a.count for a in desk.book.all() if a.robot == "B") == 2


def test_钟偏超过60秒报一次_回正了再偏再报(台):
    c, db, desk, src, *_ = 台
    pose = MapPose(map_id="m", map_version="1", frame_id="map", x=0, y=0, yaw=0)

    def tele(off_s):
        return Telemetry(stamp=c.ms + int(off_s * 1000), pose=pose, battery_pct=80,
                         task_state=None, loc_quality=1.0)
    src.on_telemetry("A", tele(5))
    src.on_telemetry("A", tele(90))
    src.on_telemetry("A", tele(95))
    assert _kinds(desk) == ["clock_skew"]
    a = desk.book.all()[0]
    assert "90" in a.detail


def test_排程这一拍没办成报一次_好了再坏再报(台):
    c, db, desk, src, *_ = 台
    src.on_site_error("schedule", "")
    src.on_site_error("schedule", "RuntimeError: 库锁住了")
    src.on_site_error("schedule", "RuntimeError: 库锁住了")
    assert [(a.kind, a.robot, a.count) for a in desk.book.all()] == [("schedule_died", "site", 1)]
    assert desk.book.all()[0].level.value == "P1"
    src.on_site_error("schedule", "")
    src.on_site_error("schedule", "OSError: 盘满")
    assert desk.book.all()[0].count == 2, "好了再坏是新的一次"


def test_主循环一拍_看掉线再升档(台):
    c, db, desk, src, pushed, stale = 台
    src.on_status("A", _status(task=_running()))
    desk.raise_alert(kind="estop_pressed", robot="A", title="急停被按下")
    stale.add("A")
    c.ms += 2 * 60_000 + 1
    src.step()
    got = {x.kind: x for x in desk.book.all()}
    assert "robot_offline" in got and got["estop_pressed"].escalated == 1


def test_掉线回来之后从它报的状态重新认起_急停还按着就再报(台):
    """掉线期间看不见:回来时急停还按着,是站点这一刻才知道的事,要报。"""
    c, db, desk, src, *_ = 台
    src.on_status("A", _status(estop_clear=False))
    src.on_status("A", _status(online=False, estop_clear=False))
    src.on_status("A", _status(estop_clear=False))
    n = sum(a.count for a in desk.book.all() if a.kind == "estop_pressed")
    assert n == 2



def test_4G抖动_回来不到一分钟又掉_算同一次(台):
    c, db, desk, src, pushed, stale = 台
    src.on_status("A", _status())
    src.on_status("A", _status(online=False))
    a = [x for x in desk.book.all() if x.robot == "A"][0]
    desk.ack(a.key, who="gina")
    for _ in range(5):                                # 抖五次
        c.ms += 10_000
        src.on_status("A", _status())
        c.ms += 5_000
        src.on_status("A", _status(online=False))
    assert len([x for x in desk.book.all() if x.robot == "A"]) == 1, \
        "确认过的那条之后不许每抖一次就一条新 P1"
    c.ms += 10_000
    src.on_status("A", _status())
    c.ms += 61_000
    src.on_status("A", _status(online=False))
    assert len([x for x in desk.book.all() if x.robot == "A"]) == 2, \
        "稳定在线一分钟后再掉是新的一次"


def test_狗重启过或掉线过_跌倒的记忆清掉_下一次跌倒照报(台):
    """W00c5a 内部评审阻断:狗跌倒 → 扶起、重启 → 再跌倒,不许不报。"""
    c, db, desk, src, *_ = 台
    fall = fault_event_data((Fault(code="3", fatal=True, text="机身跌倒"),))
    src.on_status("A", _status())
    src.on_event("A", _ev("robot_fault", **fall))
    [a] = desk.book.all()
    desk.ack(a.key, who="gina")
    # 扶起、重启:新的 boot_id(代理起来第一拍会报全集;这里故意不报,只靠 boot_id 也要清)
    src.on_status("A", Status(online=True, boot_id="b2", ready=_ready(), control_epoch=1,
                              last_seen=1, task=None))
    src.on_event("A", _ev("robot_fault", **fall))
    assert len([x for x in desk.book.all() if x.kind == "fallen"]) == 2
    b = [x for x in desk.book.all() if x.kind == "fallen"][-1]
    desk.ack(b.key, who="gina")
    src.on_status("A", Status(online=False, boot_id="b2", ready=_ready(), control_epoch=1,
                              last_seen=1, task=None))
    c.ms += 61_000
    src.on_status("A", Status(online=True, boot_id="b2", ready=_ready(), control_epoch=1,
                              last_seen=1, task=None))
    src.on_event("A", _ev("robot_fault", **fall))
    assert len([x for x in desk.book.all() if x.kind == "fallen"]) == 3, "掉线回来后同样清"


def test_站点重启_用库里最后见过的状态做种_不重复报也不降级(台, tmp_path):
    """W00c5a 内部评审:重启前确认过、还按着的急停不许另起一条;停机期间跑着任务掉线要报 P1。"""
    import json

    c, db, desk, src, *_ = 台
    with db.tx() as t:
        for rid, st in (("A", _status(estop_clear=False)), ("B", _status(task=_running("t9")))):
            t.execute("INSERT INTO robot_state(robot_id, status, updated_at) VALUES (?,?,?)",
                      (rid, json.dumps(st.to_wire()), c.ms))

    class 假派遣:
        def __init__(self):
            from d1max_site.dispatcher import Feed
            self.db = db
            self.feed = Feed()

        def on_status(self, cb):
            pass

        def on_event(self, cb):
            pass

        def on_telemetry(self, cb):
            pass

        def is_stale(self, rid):
            return False

    src2 = SiteAlertSources(desk, now_ms=c)
    src2.attach(假派遣())
    src2.on_status("A", _status(estop_clear=False))          # retained:急停还按着
    src2.on_status("B", _status(online=False))               # 停机期间的遗言
    src2.on_status("B", _status(online=False))
    got = sorted((a.robot, a.kind) for a in desk.book.all())
    assert got == [("B", "robot_offline")], got


def test_写库失败_内存不动_接口报错之后仍是没确认(台, monkeypatch):
    c, db, desk, *_ = 台
    a = desk.raise_alert(kind="estop_pressed", robot="A", title="急停")

    def 坏(_):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(desk, "_write", 坏)
    desk.book._sink = 坏
    with pytest.raises(RuntimeError):
        desk.ack(a.key, who="gina")
    assert desk.book.all()[0].acked_ms is None, "库里没记,内存也不许算确认"
    with pytest.raises(RuntimeError):
        desk.raise_alert(kind="fallen", robot="A", title="跌倒")
    assert [x.kind for x in desk.book.all()] == ["estop_pressed"]


def test_开张只读回未解决的与最近的_序号照样接着走(tmp_path):
    """读回的只是一部分;没读回来的那些老告警的序号也不许撞(序号从整张表的键里算)。"""
    from d1max_site import alert_store

    db = SiteDB(tmp_path / "s.db")
    c = 钟()
    d = AlertDesk(db, now_ms=c)
    for i in range(5):                                # 老的:run_done#1..#5,都解决了
        c.ms += 1
        d.resolve(d.raise_alert(kind="run_done", robot="A", title=f"{i}").key, who="")
    for i in range(alert_store.RESTORE_RECENT + 10):  # 新的:把「最近 N 条」占满
        c.ms += 1
        d.resolve(d.raise_alert(kind="run_start", robot="A", title=f"{i}").key, who="")
    keep = d.raise_alert(kind="estop_pressed", robot="A", title="还没解决")
    d2 = AlertDesk(db, now_ms=c)
    loaded = {x.key for x in d2.book.all()}
    assert len(loaded) <= alert_store.RESTORE_RECENT + 1 and keep.key in loaded
    assert not any(k.startswith("A/run_done#") for k in loaded), "老的那几条确实没读回来"
    c.ms += 1
    nxt = d2.raise_alert(kind="run_done", robot="A", title="新的")
    assert nxt.key == "A/run_done#6", nxt.key
    db.close()


def test_狗上归档写不进去_报P1_证据缺了(台):
    """W00c6a:盘满、只读重挂,那一趟照跑但照片、记录没存下 —— 证据缺了要让人知道。"""
    from d1max_site.alerts import Level
    c, db, desk, src, *_ = 台
    src.on_event("A", _ev("archive_write_failed", task_id="t1",
                          reason="events.jsonl: [Errno 30] Read-only file system"))
    [a] = desk.book.all()
    assert a.kind == "archive_failed" and a.level is Level.P1
    assert "Read-only" in a.detail and "t1" in a.title


def test_点位照片存不下_不报没到_并进记录写不进去(台):
    c, db, desk, src, *_ = 台
    src.on_event("A", _ev("patrol_waypoint", task_id="t1", index=0, name="gate", ok=False,
                          note="照片存不下: [Errno 28] No space left on device"))
    [a] = desk.book.all()
    assert a.kind == "archive_failed" and "照片存不下" in a.title and "没到" not in a.title
