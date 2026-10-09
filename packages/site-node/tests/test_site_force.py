"""狗翻倒了、被抱起来了(W26,决策 52),站点这一头:按狗能力里的状态对账报 P1、一回只报一次;
布防时被抱起来响警笛警灯 45 秒、发不出去下一拍再发;被撞报 P2;门槛文件坏了报 P2。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_site_charging import 假告警台

from d1max_site.db import SiteDB
from d1max_site.force import ForceWatch
from d1max_site.modes import ArmingDesk


class 假派遣:
    def __init__(self):
        self.clients = {"A": SimpleNamespace(
            status=SimpleNamespace(online=True, task=None),
            capabilities=SimpleNamespace(tasks={"force": {"state": "ok"},
                                                "deter": {"outputs": ["siren", "strobe"]}}),
            telemetry=SimpleNamespace(pose=SimpleNamespace(map_id="m", map_version="1",
                                                           x=3.0, y=4.0, yaw=0.0)))}
        self.sent = []
        self.fail = False

    def force(self, st):
        self.clients["A"].capabilities.tasks["force"] = {"state": st}

    async def deter(self, rid, payload, *, issued_by, priority):
        if self.fail:
            raise OSError("狗没信儿")
        self.sent.append(payload)
        return {"ack": {"result": "accepted"}}


@pytest.fixture
def 台(tmp_path):
    ms = [1_800_000_000_000]
    db = SiteDB(tmp_path / "s.db")
    d = 假派遣()
    arming = ArmingDesk(db, now_ms=lambda: ms[0])
    w = ForceWatch(db, d, now_ms=lambda: ms[0], arming=arming)
    w.alerts = 假告警台()
    return SimpleNamespace(db=db, d=d, w=w, arming=arming, ms=ms)


async def test_翻倒了报P1带位置_一回只报一次_扶正了清_再翻再报(台):
    t = 台
    t.arming.set_mode("home", by="olga")
    t.d.force("flipped")
    await t.w.tick()
    await t.w.tick()
    [a] = t.w.alerts.raised
    assert a["kind"] == "force_flipped" and a["robot"] == "A" and "急停" in a["detail"]
    assert a["context"]["pose"]["x"] == 3.0 and a["context"]["force"] == "flipped"
    assert t.d.sent == [], "翻倒不响警笛"
    t.d.force("ok")
    await t.w.tick()
    assert not t.db.query("SELECT 1 FROM force_episodes")
    t.w.alerts.raised.clear()                             # 保安已经处理掉了那一条
    t.d.force("flipped")
    await t.w.tick()
    assert [x["kind"] for x in t.w.alerts.raised] == ["force_flipped"], "新的一回:再报"


async def test_说不清不报不清_老代理不报(台):
    t = 台
    t.d.force("flipped")
    await t.w.tick()
    t.d.clients["A"].status.online = False               # 掉线:说不清
    t.d.force("ok")
    await t.w.tick()
    assert t.w.db.query("SELECT 1 FROM force_episodes"), "掉线时报的 ok 不算数:这一回还在"
    t.d.clients["A"].status.online = True
    t.d.force("flipped")
    await t.w.tick()
    assert len(t.w.alerts.raised) == 1, "掉线不算扶正了:不重报"
    del t.d.clients["A"].capabilities.tasks["force"]
    await t.w.tick()
    assert t.w.db.query("SELECT 1 FROM force_episodes")


async def test_布防时被抱起来_报P1_警笛警灯响到45秒_发不出去下一拍再发(台):
    t = 台
    t.arming.set_mode("armed", by="gina")
    t.d.fail = True
    t.d.force("lifted")
    await t.w.tick()
    [a] = t.w.alerts.raised
    assert a["kind"] == "force_lifted" and "警笛" in a["detail"]
    assert t.d.sent == []
    t.d.fail = False
    t.ms[0] += 5_000
    await t.w.tick()
    assert {p["output"] for p in t.d.sent} == {"siren", "strobe"}
    assert all(p["on"] and p["max_s"] == 40 for p in t.d.sent), "开到这一回开始后 45 秒"
    await t.w.tick()
    assert len(t.d.sent) == 2, "开成了就不再发"


async def test_撤防时被抱起来只报不响_过了45秒才发出去就不发了(台):
    t = 台
    t.arming.set_mode("home", by="olga")
    t.d.force("lifted")
    await t.w.tick()
    assert t.d.sent == [] and "警笛" not in t.w.alerts.raised[0]["detail"]
    t.d.force("ok")
    await t.w.tick()
    t.arming.set_mode("armed", by="gina")
    t.d.fail = True
    t.d.force("lifted")
    await t.w.tick()
    t.d.fail = False
    t.ms[0] += 50_000
    await t.w.tick()
    assert t.d.sent == [] and not t.w.db.query("SELECT 1 FROM force_episodes WHERE siren=1")


async def test_报不成_下一拍再报(台):
    from d1max_site.alert_store import AlertDesk
    t = 台
    desk = AlertDesk(t.db, now_ms=lambda: t.ms[0])
    real = desk.raise_alert
    desk.raise_alert = lambda **kw: (_ for _ in ()).throw(OSError("库写不进"))
    t.w.alerts = desk
    t.d.force("flipped")
    await t.w.tick()
    assert not desk.book.open()
    desk.raise_alert = real
    await t.w.tick()
    assert [a.kind for a in desk.book.open()] == ["force_flipped"]


def test_被撞报P2_门槛文件坏了报P2(tmp_path):
    from d1max_contract.messages import Event
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    desk = AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1)
    src = SiteAlertSources(desk, now_ms=lambda: 1)

    def ev(seq, kind, data):
        return Event(event_id=f"e{seq}", seq=seq, boot_id="b", stamp=1, kind=kind, data=data)
    src.on_event("A", ev(1, "force_bump", {"shock_g": 2.3}))
    src.on_event("A", ev(2, "force_config_bad", {"reason": "认不出 bump"}))
    src.on_event("A", ev(3, "force_flipped", {}))       # 翻倒按能力对账,事件不另报
    got = {a.kind: a for a in desk.book.open()}
    assert set(got) == {"force_bump", "force_config_bad"}
    assert "2.3 g" in got["force_bump"].title and got["force_bump"].level.name == "P2"
