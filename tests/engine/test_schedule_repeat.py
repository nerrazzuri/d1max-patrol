"""W14 夜间加密:一条排程从 ``at`` 起每隔 ``every_min`` 一轮,到 ``until`` 为止(可以跨过午夜);每一轮
各算各的窗口;``days`` 看第一轮那一天。纯函数。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from d1max_contract.schedule import (
    DecisionKind,
    ScheduleError,
    decide,
    next_run,
    parse_schedule,
)

吉隆坡 = ZoneInfo("Asia/Kuala_Lumpur")


def 刻(月, 日, 时, 分) -> datetime:
    return datetime(2026, 月, 日, 时, 分, tzinfo=吉隆坡)


def 排程(**改动):
    e = {"id": "night", "mission": "m", "at": "22:00", "days": ["fri"], "window_min": 20,
         "on_missed": "alarm", "every_min": 60, "until": "02:00"}
    e.update(改动)
    return parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [e]}).entries[0]


def test_从at到until每隔一段一轮_跨过午夜():
    e = 排程()
    assert e.offsets_min() == (1320, 1380, 1440, 1500, 1560)       # 22、23、0、1、2 点
    # 2026-10-02 是星期五
    d = decide(e, now=刻(10, 2, 22, 5), last_started_ms=None)
    assert d.kind is DecisionKind.DUE and d.scheduled == 刻(10, 2, 22, 0)
    d = decide(e, now=刻(10, 3, 1, 10), last_started_ms=None)
    assert d.kind is DecisionKind.DUE and d.scheduled == 刻(10, 3, 1, 0), \
        "星期六凌晨那几轮算星期五的"
    started = int(刻(10, 3, 1, 1).timestamp() * 1000)
    assert decide(e, now=刻(10, 3, 1, 30), last_started_ms=started).kind is DecisionKind.NOT_YET
    d = decide(e, now=刻(10, 3, 1, 30), last_started_ms=int(刻(10, 3, 0, 1).timestamp() * 1000))
    assert d.kind is DecisionKind.ALARM and d.scheduled == 刻(10, 3, 1, 0), "1 点那一轮迟了"
    assert decide(e, now=刻(10, 3, 3, 0), last_started_ms=started).kind is DecisionKind.ALARM, \
        "2 点那一轮没跑"
    late = int(刻(10, 3, 2, 1).timestamp() * 1000)
    assert decide(e, now=刻(10, 3, 3, 0), last_started_ms=late).kind is DecisionKind.NOT_YET
    assert decide(e, now=刻(10, 3, 22, 5), last_started_ms=late).kind is DecisionKind.NOT_YET, \
        "星期六不是排的日子"


def test_下一轮():
    e = 排程()
    assert next_run(e, now=刻(10, 2, 22, 30)) == 刻(10, 2, 23, 0)
    assert next_run(e, now=刻(10, 3, 0, 30)) == 刻(10, 3, 1, 0), "昨天起头、跨过午夜的那几轮"
    assert next_run(e, now=刻(10, 3, 2, 30)) == 刻(10, 9, 22, 0)


def test_不跨午夜的重复():
    e = 排程(at="09:00", until="12:00", every_min=90, days=["mon"])
    assert e.offsets_min() == (540, 630, 720)
    assert e.to_wire()["every_min"] == 90 and e.to_wire()["until"] == "12:00"


def test_不写重复照旧一天一轮_线格式不变():
    e = parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [
        {"id": "a", "mission": "m", "at": "08:00", "days": ["mon"], "window_min": 30,
         "on_missed": "skip"}]}).entries[0]
    assert e.offsets_min() == (480,)
    assert "every_min" not in e.to_wire() and "standby" not in e.to_wire()


@pytest.mark.parametrize("改动,话", [
    ({"every_min": 5}, "every_min"), ({"every_min": 800}, "every_min"),
    ({"every_min": True}, "every_min"), ({"until": None}, "until"),
    ({"until": "25:00"}, "until"), ({"until": "22:00"}, "跟 at 一样"),
    ({"window_min": 60}, "要小于 every_min"), ({"standby": 3}, "standby"),
])
def test_重复写错了当场报(改动, 话):
    with pytest.raises(ScheduleError, match=话):
        排程(**改动)


def test_只写until不写every也报():
    with pytest.raises(ScheduleError, match="every_min"):
        parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [
            {"id": "a", "mission": "m", "at": "08:00", "days": ["mon"], "window_min": 30,
             "on_missed": "skip", "until": "10:00"}]})


def test_回哪个待命点():
    assert 排程(standby="gate").to_wire()["standby"] == "gate"


def test_W14外审_一段时间里的每一轮_包括前一天起头跨午夜的():
    from d1max_contract.schedule import occurrences_between
    e = 排程()
    got = occurrences_between(e, 刻(10, 2, 21, 0), 刻(10, 3, 2, 30))
    assert got == [刻(10, 2, 22, 0), 刻(10, 2, 23, 0), 刻(10, 3, 0, 0), 刻(10, 3, 1, 0),
                   刻(10, 3, 2, 0)]
    assert occurrences_between(e, 刻(10, 3, 0, 30), 刻(10, 3, 1, 0)) == [刻(10, 3, 1, 0)], \
        "从午夜之后看起:昨天起头的那几轮也在"
    assert occurrences_between(e, 刻(10, 3, 3, 0), 刻(10, 3, 1, 0)) == []
    with pytest.raises(ValueError):
        occurrences_between(e, datetime(2026, 10, 2), 刻(10, 3, 1, 0))
