"""W44 第二期:布防时间表(设计 §5.1「布防时间」、§11 第 5 条)。

一周七天、每个防区的布防时间段,加例外日期;业主手动切了模式,到下一个时间段的边界之前以手动为准;访客到点
回时间表。时间按站点时区(这里一律吉隆坡;主机时区不管)。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_site_api import PW, 站

from d1max_site.arming_schedule import ArmingSchedule, ScheduleError
from d1max_site.db import SiteDB
from d1max_site.modes import ArmingDesk

KL = ZoneInfo("Asia/Kuala_Lumpur")


def kl(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=KL).timestamp() * 1000)


# 2026-10-26 是星期一
MON = (2026, 10, 26)


class 钟:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    clock = 钟(kl(*MON, 12))
    pushed: list[dict] = []
    desk = ArmingDesk(db, now_ms=clock, publish=pushed.append)
    desk.schedule = ArmingSchedule(db, now_ms=clock, tz=lambda: KL)
    desk.clock, desk.pushed = clock, pushed
    yield desk
    db.close()


def slot(day, start, end, zone=""):
    return {"zone": zone, "day": day, "start": start, "end": end}


def test_没开时间表_照旧按模式(台):
    sch = 台.schedule
    sch.put({"enabled": False, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    assert sch.scheduled("前门", 台.clock.t) is None
    台.set_mode("home", by="olga")
    assert 台.armed("前门") == (True, "home")
    assert 台.view()["source"] == "manual"


def test_时间段内布防段外撤防(台):
    sch = 台.schedule
    sch.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    assert sch.scheduled("前门", kl(*MON, 23)) is True
    assert sch.scheduled("前门", kl(*MON, 21)) is False


def test_时间段到2400(台):
    sch = 台.schedule
    sch.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    assert sch.scheduled("前门", kl(*MON, 23, 59)) is True
    assert sch.scheduled("前门", kl(2026, 10, 27, 0, 0)) is False      # 星期二没有段
    assert sch.view()["slots"] == [{"zone": "", "day": 0, "start": "22:00", "end": "24:00"}]


def test_防区有自己的段用自己的(台):
    sch = 台.schedule
    sch.put({"enabled": True, "slots": [slot(0, "00:00", "24:00"),
                                        slot(0, "20:00", "24:00", "后花园"),
                                        slot(0, "00:00", "06:00", "后花园")]}, by="alice")
    assert sch.scheduled("后花园", kl(*MON, 12)) is False
    assert sch.scheduled("后花园", kl(*MON, 21)) is True
    assert sch.scheduled("前门", kl(*MON, 12)) is True


def test_没配过的防区一律布防(台):
    sch = 台.schedule
    sch.put({"enabled": True, "slots": [slot(0, "20:00", "22:00", "后花园")]}, by="alice")
    assert sch.scheduled("车库", kl(*MON, 12)) is True
    assert sch.scheduled("后花园", kl(*MON, 12)) is False


def test_例外两头都算(台):
    sch = 台.schedule
    sch.put({"enabled": True, "slots": []}, by="alice")      # 平时全天撤防(开着、没有段)
    sch.add_exception({"first_day": "2026-10-30", "last_day": "2026-11-02", "state": "armed",
                       "note": "出远门"}, by="alice")
    assert sch.scheduled("前门", kl(2026, 10, 29, 23, 59)) is False
    assert sch.scheduled("前门", kl(2026, 10, 30, 0, 0)) is True
    assert sch.scheduled("前门", kl(2026, 11, 2, 23, 59)) is True
    assert sch.scheduled("前门", kl(2026, 11, 3, 0, 0)) is False


def test_例外撤防盖过时间段(台):
    sch = 台.schedule
    sch.put({"enabled": True, "slots": [slot(d, "00:00", "24:00") for d in range(7)]}, by="alice")
    eid = sch.add_exception({"first_day": "2026-10-26", "last_day": "2026-10-26",
                             "state": "disarmed"}, by="alice")
    assert sch.scheduled("前门", kl(*MON, 12)) is False
    assert sch.remove_exception(eid) is True
    assert sch.remove_exception(eid) is False
    assert sch.scheduled("前门", kl(*MON, 12)) is True


@pytest.mark.parametrize("bad", [
    {"first_day": "2026-11-02", "last_day": "2026-10-30", "state": "armed"},
    {"first_day": "2026-10-30", "last_day": "2027-12-30", "state": "armed"},
    {"first_day": "2026-02-30", "last_day": "2026-03-01", "state": "armed"},
    {"first_day": "2026-10-30", "last_day": "2026-10-30", "state": "maybe"},
])
def test_例外不合规矩(台, bad):
    with pytest.raises(ScheduleError):
        台.schedule.add_exception(bad, by="alice")


def test_时间表按站点时区(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    sch = ArmingSchedule(db, now_ms=lambda: 0, tz=lambda: KL)
    sch.put({"enabled": True, "slots": [slot(0, "22:00", "23:00")]}, by="alice")
    t = kl(*MON, 22, 30)                    # 吉隆坡 22:30 = UTC 14:30
    assert sch.scheduled("前门", t) is True
    utc = ArmingSchedule(db, now_ms=lambda: 0, tz=lambda: ZoneInfo("UTC"))
    assert utc.scheduled("前门", t) is False


def test_下一个边界(台):
    sch = 台.schedule
    assert sch.next_boundary(kl(*MON, 21)) is None                       # 没开
    sch.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    assert sch.next_boundary(kl(*MON, 21)) == kl(*MON, 22)
    assert sch.next_boundary(kl(*MON, 22)) == kl(2026, 10, 27, 0, 0)     # 24:00 也是 0 点
    assert sch.next_boundary(kl(*MON, 22, 30)) == kl(2026, 10, 27, 0, 0)


@pytest.mark.parametrize("slots, why", [
    ([slot(0, "20:00", "23:00"), slot(0, "22:00", "24:00")], "重叠"),
    ([slot(0, "22:00", "22:00")], "开始不早于结束"),
    ([slot(0, "23:00", "22:00")], "开始不早于结束"),
    ([slot(7, "20:00", "22:00")], "星期"),
    ([slot(0, "8:00", "22:00")], "格式"),
    ([slot(0, "20:00", "24:30")], "格式"),
    ([slot(0, "24:00", "24:00")], "格式"),
    ([{"zone": "a\x00", "day": 0, "start": "20:00", "end": "22:00"}], "防区"),
    ([slot(0, "00:00", "00:30")] * 501, "太多"),
])
def test_时间段不合规矩不让存(台, slots, why):
    with pytest.raises(ScheduleError):
        台.schedule.put({"enabled": True, "slots": slots}, by="alice")


def test_相接的段可以存(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "20:00", "22:00"),
                                                slot(0, "22:00", "24:00")]}, by="alice")
    assert len(台.schedule.view()["slots"]) == 2


def test_今天的安排(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "22:00", "24:00"),
                                                slot(0, "00:00", "06:00"),
                                                slot(1, "22:00", "24:00"),
                                                slot(0, "20:00", "24:00", "后花园")]}, by="alice")
    today = 台.schedule.view()["today"]
    assert {"zone": "", "windows": [["00:00", "06:00"], ["22:00", "24:00"]]} in today
    assert {"zone": "后花园", "windows": [["20:00", "24:00"]]} in today


# ------------------------------------------------------------ 跟模式合起来


def test_时间表管着时_按时间表判防区(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    台.clock.t = kl(*MON, 12)
    assert 台.armed("前门") == (False, "schedule")
    台.clock.t = kl(*MON, 22, 30)
    assert 台.armed("前门") == (True, "schedule")
    v = 台.view()
    assert v["source"] == "schedule" and v["state"] in ("armed", "disarmed", "partial")


def test_手动切到下一个边界为止(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    台.set_zone_home("前门", False)
    台.clock.t = kl(*MON, 12)
    台.set_mode("armed", by="olga")                       # 段外(撤防)手动布防
    v = 台.view()
    assert v["source"] == "manual" and v["manual_until_ms"] == kl(*MON, 22)
    assert 台.armed("前门") == (True, "armed")
    台.clock.t = kl(*MON, 21, 59)
    assert 台.armed("前门") == (True, "armed")
    台.clock.t = kl(*MON, 22, 0)
    assert 台.armed("前门") == (True, "schedule")
    assert 台.view()["source"] == "schedule"
    台.clock.t = kl(2026, 10, 27, 1)
    assert 台.armed("前门") == (False, "schedule")


def test_访客到点回时间表(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "00:00", "24:00")]}, by="alice")
    台.clock.t = kl(*MON, 12)
    台.set_mode("home", by="olga")
    台.set_mode("visitor", by="olga", zones=["前门"], minutes=30)
    assert 台.view()["manual_until_ms"] == kl(*MON, 12, 30)
    assert 台.armed("前门")[0] is False
    台.clock.t = kl(*MON, 12, 31)
    assert 台.tick() is True
    v = 台.view()
    assert v["source"] == "schedule" and v["manual_until_ms"] is None
    assert 台.armed("前门") == (True, "schedule")


def test_没开时间表_访客到点照旧退回(台):
    台.set_mode("home", by="olga")
    台.set_mode("visitor", by="olga", zones=["前门"], minutes=30)
    assert 台.view()["manual_until_ms"] is None
    台.clock.t += 31 * 60_000
    assert 台.tick() is True
    assert 台.view()["mode"] == "home"


def test_保安全部布防也算手动(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    台.clock.t = kl(*MON, 12)
    台.set_mode("armed", by="gina")
    assert 台.view()["source"] == "manual"
    assert 台.view()["manual_until_ms"] == kl(*MON, 22)


def test_时间表变了推一次记一条(台):
    seen: list[dict] = []
    台.on_schedule = seen.append
    台.schedule.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    台.clock.t = kl(*MON, 21, 59)
    台.tick()
    台.pushed.clear()
    台.tick()
    assert 台.pushed == [] and seen == []
    台.clock.t = kl(*MON, 22, 0)
    assert 台.tick() is True
    assert len(台.pushed) == 1 and 台.pushed[0]["kind"] == "mode"
    assert len(seen) == 1 and seen[0]["state"] == "armed"
    台.tick()
    assert len(台.pushed) == 1 and len(seen) == 1


def test_手动期过了推一次(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "22:00", "24:00")]}, by="alice")
    台.clock.t = kl(*MON, 12)
    台.set_mode("armed", by="olga")
    台.tick()
    台.pushed.clear()
    台.clock.t = kl(*MON, 22, 0, ) + 1000
    assert 台.tick() is True
    assert len(台.pushed) == 1 and 台.pushed[0]["mode"]["source"] == "schedule"


def test_view_state(台):
    台.schedule.put({"enabled": True, "slots": [slot(0, "00:00", "24:00"),
                                                slot(0, "20:00", "24:00", "后花园")]}, by="alice")
    台.set_zone_home("车库", False)        # 让「车库」成为已知防区(没自己的段 → 用 '' 那一份)
    台.clock.t = kl(*MON, 12)
    v = 台.view()
    assert {z["zone"]: z["armed"] for z in v["zones"]}["后花园"] is False
    assert v["state"] == "partial" and v["mode"] == "home"
    assert "按时间表" in v["label"]
    台.clock.t = kl(*MON, 21)
    v = 台.view()
    assert v["state"] == "armed" and v["mode"] == "armed"
    assert v["next_change_ms"] == kl(2026, 10, 27)


# ------------------------------------------------------------ 接口


def test_接口权限(tmp_path):
    s = 站(tmp_path)
    try:
        s.api.arming = ArmingDesk(s.db, now_ms=lambda: kl(*MON, 12))
        s.api.arming.schedule = ArmingSchedule(s.db, now_ms=lambda: kl(*MON, 12), tz=lambda: KL)
        s.accounts.add("gina", PW, role="guard")
        gina = s.req("POST", "/api/login", {"name": "gina", "password": PW})[1]["token"]
        alice = s.login()
        code, d = s.req("GET", "/api/arming/schedule", token=gina)
        assert code == 200 and d["enabled"] is False
        body = {"enabled": True, "slots": [slot(0, "22:00", "24:00")]}
        assert s.req("POST", "/api/arming/schedule", body, token=gina)[0] == 403
        code, d = s.req("POST", "/api/arming/schedule", body, token=alice)
        assert code == 200 and d["enabled"] is True and len(d["slots"]) == 1
        assert s.req("POST", "/api/arming/schedule", {"enabled": True,
                     "slots": [slot(0, "23:00", "22:00")]}, token=alice)[0] == 400
        ex = {"first_day": "2026-10-30", "last_day": "2026-10-31", "state": "armed"}
        assert s.req("POST", "/api/arming/exceptions", ex, token=gina)[0] == 403
        code, d = s.req("POST", "/api/arming/exceptions", ex, token=alice)
        assert code == 200 and len(d["exceptions"]) == 1
        eid = d["exceptions"][0]["id"]
        assert s.req("POST", f"/api/arming/exceptions/{eid}/remove", {}, token=alice)[1][
            "exceptions"] == []
        assert s.req("POST", f"/api/arming/exceptions/{eid}/remove", {}, token=alice)[0] == 404
        code, d = s.req("GET", "/api/mode", token=gina)
        assert code == 200 and d["source"] == "schedule"
    finally:
        s.close()


# ---- 外审 R1:开关和时间段一起存;存失败整份还是旧的 ----

def test_外审R1_关时间表写一半失败_整份还是旧的(台, tmp_path, monkeypatch):
    s = 台.schedule
    s.put({"enabled": True, "slots": [slot(0, "00:00", "24:00")]}, by="alice")
    assert 台.armed("gate")[0] is True
    import d1max_site.site_settings as ss

    def 坏(*a, **k):
        raise OSError("写设置失败")
    monkeypatch.setattr(ss.SiteSettings, "put_in", 坏)
    with pytest.raises(OSError):
        s.put({"enabled": False, "slots": []}, by="alice")
    monkeypatch.undo()
    v = s.view()
    assert v["enabled"] is True and len(v["slots"]) == 1
    assert 台.armed("gate")[0] is True
    # 重开一次库(站点重启)也是旧的
    db2 = SiteDB(tmp_path / "site.db")
    try:
        s2 = ArmingSchedule(db2, now_ms=台.clock, tz=lambda: KL)
        assert s2.enabled() is True and len(s2.view()["slots"]) == 1
    finally:
        db2.close()


def test_外审R1_读的时候看到的是同一份(台, monkeypatch):
    """读开关和读时间段之间插进一次保存,也不会拼出「开着 + 空时间表」(= 全天撤防)。"""
    s = 台.schedule
    s.put({"enabled": True, "slots": [slot(0, "00:00", "24:00")]}, by="alice")
    real = s._slots_in
    done = []

    def 插一脚(c):
        if not done:
            done.append(1)
            import threading
            t = threading.Thread(target=lambda: s.put({"enabled": False, "slots": []}, by="bob"))
            t.start()
            t.join(0.3)            # 拿不到锁就等着,不会插进来
            done.append(t)
        return real(c)
    monkeypatch.setattr(s, "_slots_in", 插一脚)
    assert s.scheduled("gate", 台.clock()) is True
    done[1].join(5)
    monkeypatch.undo()
    assert s.scheduled("gate", 台.clock()) is None      # 后来那次关掉了:整份关


def test_外审复查R1_判这一处布不布防时插进一次关时间表_要么整份旧要么整份新(台, monkeypatch):
    """开关读完、时间段还没读时另一个线程关掉时间表:原来拼出「按时间表 + 没开」= 撤防(被抱起警笛
    整回不响)。现在整个判断拿着同一把读锁:关时间表等它判完。"""
    import threading
    s = 台.schedule
    s.put({"enabled": True, "slots": [slot(0, "00:00", "24:00")]}, by="alice")
    real = s.enabled
    box = []

    def 插一脚():
        got = real()
        if not box:
            t = threading.Thread(target=lambda: s.put({"enabled": False, "slots": []}, by="bob"))
            t.start()
            t.join(0.3)
            box.append(t)
        return got
    monkeypatch.setattr(s, "enabled", 插一脚)
    assert 台.armed("gate") == (True, "schedule")       # 整份旧的:布防
    box[0].join(5)
    monkeypatch.undo()
    assert 台.armed("gate") == (True, "armed")          # 整份新的:回到手动布防
