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
