"""狗没在驱离时自己看见人(W33,决策 48):布防时报 P1、一回只报一次、在家访客撤防不报、驱离中不报;
就地驱离。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_site_charging import 假告警台

from d1max_site.db import SiteDB
from d1max_site.modes import ArmingDesk
from d1max_site.sightings import PersonWatch


class 假派遣:
    def __init__(self):
        self.clients = {"A": SimpleNamespace(
            status=SimpleNamespace(online=True, task=None),
            capabilities=SimpleNamespace(tasks={"persons": {"state": "ok", "present": False}}),
            telemetry=SimpleNamespace(pose=SimpleNamespace(map_id="m", map_version="1",
                                                           x=3.0, y=4.0, yaw=0.0)))}

    def persons(self, rid, **kw):
        self.clients[rid].capabilities.tasks["persons"] = {"state": "ok", **kw}


@pytest.fixture
def 台(tmp_path):
    ms = [1_800_000_000_000]
    db = SiteDB(tmp_path / "s.db")
    d = 假派遣()
    arming = ArmingDesk(db, now_ms=lambda: ms[0])
    det = SimpleNamespace(sessions={})
    w = PersonWatch(db, d, now_ms=lambda: ms[0], arming=arming, deterrence=det)
    w.alerts = 假告警台()
    return SimpleNamespace(db=db, d=d, w=w, arming=arming, det=det, ms=ms)


def test_布防时看见人报P1_带人数距离位置_现场照片在persons那几趟(台):
    t = 台
    t.d.persons("A", present=True, count=2, nearest_m=3.5)
    t.w.tick()
    [a] = t.w.alerts.raised
    assert a["kind"] == "dog_sees_person" and a["robot"] == "A"
    assert "2 个人" in a["title"] and "3.5 m" in a["title"]
    assert a["context"]["task_id"] == "persons" and a["context"]["pose"]["x"] == 3.0
    assert "就地驱离" in a["detail"]


def test_一回看见只报一次_人走了才清_再看见再报_说不清不清(台):
    t = 台
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    t.w.tick()
    assert len(t.w.alerts.raised) == 1
    t.d.persons("A", present=None)                       # 检测断了:说不清
    t.w.tick()
    t.w.alerts.raised.clear()                             # 保安已经处理掉了那一条
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert t.w.alerts.raised == [], "说不清不算走了:不重报"
    t.d.persons("A", present=False)
    t.w.tick()
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert [x["kind"] for x in t.w.alerts.raised] == ["dog_sees_person"]


@pytest.mark.parametrize("mode", ["home", "visitor"])
def test_在家访客不报_切回布防人还在就报(台, mode):
    t = 台
    kw = {"zones": ["drive"], "minutes": 30} if mode == "visitor" else {}
    t.arming.set_mode(mode, by="olga", **kw)
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert t.w.alerts.raised == []
    t.arming.set_mode("armed", by="gina")
    t.w.tick()
    assert len(t.w.alerts.raised) == 1


def test_驱离中不报_那一场归W24管(台):
    t = 台
    t.det.sessions["A"] = object()
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert t.w.alerts.raised == []


def test_狗掉线_检测不在正常看_都不报(台):
    t = 台
    t.d.clients["A"].capabilities.tasks["persons"] = {"state": "stale", "present": True}
    t.w.tick()
    t.d.persons("A", present=True, count=1)
    t.d.clients["A"].status.online = False
    t.w.tick()
    assert t.w.alerts.raised == []


def test_告警台报不出去_下一拍补报_不重复记(台):
    t = 台
    t.w.alerts = None
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert t.db.query("SELECT 1 FROM pending_alerts")
    t.w.alerts = 假告警台()
    t.w.tick()
    assert len(t.w.alerts.raised) == 1


# ---- 外审 R2:时间表部分布防时,按机器人可靠的现在位置看它那个防区布不布防 ----

def _分区台(台, *, default_on: bool = True):
    """gate(0–10)全天布防、house(20–30)撤防;「全部防区一样」那份 default_on 决定没画到的地方。"""
    from datetime import timezone

    from d1max_site.areas import AreaBook
    from d1max_site.arming_schedule import ArmingSchedule
    t = 台
    sch = t.arming.schedule = ArmingSchedule(t.db, now_ms=lambda: t.ms[0], tz=lambda: timezone.utc)
    slots = [{"zone": "gate", "day": d, "start": "00:00", "end": "24:00"} for d in range(7)]
    slots += [{"zone": "house", "day": 6, "start": "00:00", "end": "00:01"}]
    slots += [{"zone": "", "day": d, "start": "00:00", "end": "24:00" if default_on else "00:01"}
              for d in range(7)]
    sch.put({"enabled": True, "slots": slots}, by="olga")
    areas = AreaBook(t.db, now_ms=lambda: t.ms[0])
    areas.set("m", "1", "gate", [[0, 0], [10, 0], [10, 10], [0, 10]], by="olga")
    areas.set("m", "1", "house", [[20, 0], [30, 0], [30, 10], [20, 10]], by="olga")
    t.w.areas = areas
    c = t.d.clients["A"]
    c.status.ready = SimpleNamespace(loc_ok=True)
    t.d.telemetry_at, t.d.stale_ms = {"A": t.ms[0]}, 10_000
    assert t.arming.view()["state"] == "partial"
    return t


def _站在(t, x, y):
    t.d.clients["A"].telemetry.pose.x, t.d.clients["A"].telemetry.pose.y = x, y


def test_外审R2_部分布防_机器人在布防的门口看见人报P1(台):
    t = _分区台(台)
    _站在(t, 3, 4)
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["dog_sees_person"]


def test_外审R2_在撤防的屋边不报_走进布防的门口就报(台):
    t = _分区台(台)
    _站在(t, 25, 4)
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert t.w.alerts.raised == [] and t.db.query("SELECT * FROM person_sightings") == []
    _站在(t, 3, 4)
    t.w.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["dog_sees_person"]


def test_外审R2_位置说不清_部分布防时宁可报(台):
    t = _分区台(台)
    _站在(t, 25, 4)                                   # 最后报的位置在撤防的屋边,但已经过期
    t.d.telemetry_at["A"] = t.ms[0] - 60_000
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert len(t.w.alerts.raised) == 1
    t.w.alerts.raised.clear()
    with t.db.tx() as c:
        c.execute("DELETE FROM person_sightings")
    t.d.telemetry_at["A"] = t.ms[0]
    t.d.clients["A"].status.ready.loc_ok = False      # 定位丢了
    t.w.tick()
    assert len(t.w.alerts.raised) == 1


def test_外审R2_没画防区的地方按全部防区那一份(台):
    t = _分区台(台, default_on=False)
    _站在(t, 50, 50)                                  # 位置可靠,不在任何防区里;那一份此刻撤防
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert t.w.alerts.raised == []


def test_外审R2_布防的门口授权在场不报_授权没了补报(台):
    t = _分区台(台)
    who = ["王师傅"]
    t.arming.authz = SimpleNamespace(active=lambda zone: who[0] if zone == "gate" else None)
    _站在(t, 3, 4)
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["authorized_person"]
    who[0] = None
    t.w.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["authorized_person", "dog_sees_person"]


def test_外审R2_armed_now_给受力警报用(台):
    t = _分区台(台)
    _站在(t, 3, 4)
    assert t.w.armed_now("A") is True
    _站在(t, 25, 4)
    assert t.w.armed_now("A") is False
    t.arming.set_mode("home", by="olga")              # 手动在家:老约定,不报
    _站在(t, 3, 4)
    assert t.w.armed_now("A") is False


def _具名都撤防台(台, *, default_on=True):
    """gate、house 此刻都撤防;「全部防区一样」那份(没画到的地方)按 default_on。"""
    from datetime import timezone

    from d1max_site.areas import AreaBook
    from d1max_site.arming_schedule import ArmingSchedule
    t = 台
    sch = t.arming.schedule = ArmingSchedule(t.db, now_ms=lambda: t.ms[0], tz=lambda: timezone.utc)
    slots = [{"zone": z, "day": 6, "start": "00:00", "end": "00:01"} for z in ("gate", "house")]
    slots += [{"zone": "", "day": d, "start": "00:00", "end": "24:00" if default_on else "00:01"}
              for d in range(7)]
    sch.put({"enabled": True, "slots": slots}, by="olga")
    areas = AreaBook(t.db, now_ms=lambda: t.ms[0])
    areas.set("m", "1", "gate", [[0, 0], [10, 0], [10, 10], [0, 10]], by="olga")
    areas.set("m", "1", "house", [[20, 0], [30, 0], [30, 10], [20, 10]], by="olga")
    t.w.areas = areas
    t.d.clients["A"].status.ready = SimpleNamespace(loc_ok=True)
    t.d.telemetry_at, t.d.stale_ms = {"A": t.ms[0]}, 10_000
    return t


def test_外审复查R2_具名防区都撤防_没画到的地方还布防_整体算部分布防_站在外面看见人照报(台):
    t = _具名都撤防台(台)
    assert t.arming.view()["state"] == "partial", "默认区域还布防:不是全站撤防"
    _站在(t, 50, 50)
    assert t.w.armed_now("A") is True
    t.d.persons("A", present=True, count=1)
    t.w.tick()
    assert [a["kind"] for a in t.w.alerts.raised] == ["dog_sees_person"]
    _站在(t, 3, 4)                                     # 在撤防的门口:不报
    assert t.w.armed_now("A") is False


def test_外审复查R2_具名都撤防_定位说不清宁可报_全站都撤防才不报(台):
    t = _具名都撤防台(台)
    t.d.clients["A"].status.ready.loc_ok = False
    assert t.w.armed_now("A") is True
    t2 = _具名都撤防台(台, default_on=False)
    assert t2.arming.view()["state"] == "disarmed"
    assert t2.w.armed_now("A") is False, "全站都撤防:说不清也不报"
