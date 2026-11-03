"""W73 第二期:值守预报(设计 §5.1、§7.1 不含电量那一半)。用户 11-04:第二期先显示「大约几圈、
每个防区多久看一次」,电量放第四期。纯计算:布防时间表 + 每圈用时 → 每段约几圈、每个防区多久
被看一次、超时线、提示。"""

from __future__ import annotations

import pytest

from d1max_site.guard_forecast import (
    MIN_LIMIT_MIN,
    forecast,
    weekly_windows,
)

WED = 2


def _slot(day, start, end, zone=""):
    return {"zone": zone, "day": day, "start": start, "end": end}


def _entry(eid=1, route="night-guard", robot="", gap_min=0):
    return {"id": eid, "route": route, "robot": robot, "gap_min": gap_min, "shuffle": False}


def _fc(entries, lap_s, zones_of, slots=(), enabled=True, limits=None, armable=()):
    sched = {"enabled": enabled, "slots": list(slots)}
    return forecast(entries, lap_s=lap_s, zones_of=zones_of, schedule=sched,
                    limits=limits or {}, armable=list(armable))


def test_两小时一段_一圈9分35秒_约12圈():
    got = _fc([_entry()], {"night-guard": 575.0}, {"night-guard": ["东区", "西区"]},
              slots=[_slot(WED, "01:00", "03:00")])
    [e] = got["entries"]
    assert e["mode"] == "schedule" and e["lap_s"] == 575.0 and e["cycle_s"] == 575.0
    assert e["windows"] == [{"day": WED, "start": "01:00", "end": "03:00", "minutes": 120,
                             "laps": 12, "topups": 0}]
    z = {x["zone"]: x for x in got["zones"]}
    assert z["东区"]["interval_s"] == pytest.approx(575.0)
    assert z["东区"]["limit_min"] == MIN_LIMIT_MIN == 15, "1.5 倍不到 15 分钟按 15"
    assert z["东区"]["limit_set"] is False


def test_每30分钟一圈_按30分钟算():
    got = _fc([_entry(gap_min=30)], {"night-guard": 575.0}, {"night-guard": ["东区"]},
              slots=[_slot(WED, "01:00", "03:00")])
    e = got["entries"][0]
    assert e["cycle_s"] == 1800 and e["windows"][0]["laps"] == 4
    assert got["zones"][0]["limit_min"] == 45, "30 分钟 × 1.5"


def test_跨午夜的两段连成一段():
    wins = weekly_windows([_slot(0, "22:00", "24:00"), _slot(1, "00:00", "06:00")], ["东区"],
                          enabled=True)
    assert wins == [(0 * 1440 + 22 * 60, 1 * 1440 + 6 * 60)]
    wrap = weekly_windows([_slot(6, "22:00", "24:00"), _slot(0, "00:00", "06:00")], ["东区"],
                          enabled=True)
    assert wrap == [(6 * 1440 + 22 * 60, 7 * 1440 + 6 * 60)], "星期日连到星期一"


def test_防区有自己的段用自己的_没有用全部一样的那份():
    slots = [_slot(WED, "01:00", "03:00"), _slot(WED, "20:00", "21:00", zone="东区")]
    assert weekly_windows(slots, ["东区"], enabled=True) == [(WED * 1440 + 1200, WED * 1440 + 1260)]
    assert weekly_windows(slots, ["西区"], enabled=True) == [(WED * 1440 + 60, WED * 1440 + 180)]
    both = weekly_windows(slots, ["东区", "西区"], enabled=True)
    assert both == [(WED * 1440 + 60, WED * 1440 + 180), (WED * 1440 + 1200, WED * 1440 + 1260)]


def test_时间表开着一段没画_全天撤防_没开按手动():
    assert weekly_windows([], ["东区"], enabled=True) == []
    got = _fc([_entry()], {"night-guard": 600.0}, {"night-guard": ["东区"]}, enabled=False)
    e = got["entries"][0]
    assert e["mode"] == "manual" and e["windows"] == [], "按手动布防:布防期间一直值守,不算段"


def test_两条路线都看东区_间隔变短_提示重叠():
    entries = [_entry(1, "a"), _entry(2, "b")]
    got = _fc(entries, {"a": 600.0, "b": 600.0}, {"a": ["东区"], "b": ["东区", "西区"]},
              slots=[_slot(WED, "01:00", "03:00")])
    z = {x["zone"]: x for x in got["zones"]}
    assert z["东区"]["interval_s"] == pytest.approx(300.0)
    assert z["西区"]["interval_s"] == pytest.approx(600.0)
    assert any("东区" in w and "两条" in w for w in got["warnings"])


def test_布防的防区没有路线去看_提示_不拦():
    got = _fc([_entry()], {"night-guard": 600.0}, {"night-guard": ["东区"]},
              slots=[_slot(WED, "01:00", "03:00")], armable=["东区", "西区"])
    assert any("西区" in w and "没有机器人去看" in w for w in got["warnings"])


def test_管理员设了超时线_按设的():
    got = _fc([_entry()], {"night-guard": 600.0}, {"night-guard": ["东区"]},
              slots=[_slot(WED, "01:00", "03:00")], limits={"东区": 40})
    z = got["zones"][0]
    assert z["limit_min"] == 40 and z["limit_set"] is True


def test_算不出一圈多久_说清楚():
    got = _fc([_entry()], {}, {"night-guard": ["东区"]}, slots=[_slot(WED, "01:00", "03:00")])
    e = got["entries"][0]
    assert e["lap_s"] is None and e["windows"][0]["laps"] is None
    assert got["zones"][0]["interval_s"] is None and got["zones"][0]["limit_min"] == MIN_LIMIT_MIN


def test_W73第四期_补一次能能开几圈_每段要补几次():
    from d1max_site import energy
    f = _fc([_entry()], {"night-guard": 900.0}, {"night-guard": ["东区"]},
            slots=[_slot(2, "22:00", "24:00"), _slot(3, "00:00", "06:00")])
    e = f["entries"][0]
    n = energy.laps_per_charge(900.0, 900.0)
    assert e["laps_per_charge"] == n and n > 3
    w = e["windows"][0]
    assert w["laps"] == 32 and w["topups"] == (32 - 1) // n
    assert any(f"约每 {n} 圈要补一次能" in x for x in f["warnings"])
