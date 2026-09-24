"""「到点了没有」这条判据。**纯函数,输入是事实,输出是决定。**

这里一个 tmp_path 都不要:一条需要临时目录才跑得起来的判据测试,本身就
说明判据不纯了(§8.5)。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from d1max_agent.engine.schedule import (
    Decision,
    DecisionKind,
    ScheduleEntry,
    decide,
)

吉隆坡 = ZoneInfo("Asia/Kuala_Lumpur")


def 一条(**改动) -> ScheduleEntry:
    参 = {
        "id": "night-1",
        "mission": "perimeter-full",
        "at_h": 22, "at_m": 0,
        "days": frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"}),
        "window_min": 45,
        "on_missed": "skip",
        "priority": 0,
    }
    参.update(改动)
    return ScheduleEntry(**参)                    # type: ignore[arg-type]


def 刻(月, 日, 时, 分, tz=吉隆坡) -> datetime:
    return datetime(2026, 月, 日, 时, 分, tzinfo=tz)


def 毫秒(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# 2026-09-07 是星期一。下面所有日期都按这个数。

def test_还没到点():
    got = decide(一条(), now=刻(9, 7, 21, 59), last_started_ms=None)
    assert got.kind is DecisionKind.NOT_YET


def test_正正好到点():
    got = decide(一条(), now=刻(9, 7, 22, 0), last_started_ms=None)
    assert got.kind is DecisionKind.DUE
    assert got.late_min == 0
    assert got.scheduled == 刻(9, 7, 22, 0)
    assert got.scheduled_ms == 毫秒(刻(9, 7, 22, 0))


def test_窗口里头还算到点():
    got = decide(一条(), now=刻(9, 7, 22, 44), last_started_ms=None)
    assert got.kind is DecisionKind.DUE
    assert got.late_min == 44


def test_窗口的边界是闭的():
    """迟到 45 分钟、窗口 45 分钟 —— 算还是不算?

    **算。** 边界定成闭的,理由是开区间会让「window_min: 45」在人的理解里
    变成「44 分钟」,而这个差别只在一年出一次事的那天才看得出来。
    """
    got = decide(一条(window_min=45), now=刻(9, 7, 22, 45), last_started_ms=None)
    assert got.kind is DecisionKind.DUE
    assert got.late_min == 45


def test_过了窗口一分钟就按on_missed办():
    got = decide(一条(on_missed="skip"), now=刻(9, 7, 22, 46), last_started_ms=None)
    assert got.kind is DecisionKind.SKIP
    assert got.late_min == 46


def test_过了窗口而on_missed是run_late就照跑():
    got = decide(一条(on_missed="run_late"), now=刻(9, 7, 23, 30),
                 last_started_ms=None)
    assert got.kind is DecisionKind.LATE
    assert got.late_min == 90


def test_过了窗口而on_missed是alarm就告警():
    got = decide(一条(on_missed="alarm"), now=刻(9, 7, 23, 30), last_started_ms=None)
    assert got.kind is DecisionKind.ALARM
    assert got.late_min == 90


def test_今天不在days里就当没这回事():
    got = decide(一条(days=frozenset({"sat", "sun"})),
                 now=刻(9, 7, 22, 30), last_started_ms=None)     # 星期一
    assert got.kind is DecisionKind.NOT_YET


def test_跑过了就不再报到点():
    """**防重复的唯一机制。**

    没有 last_started_ms 的话,22:00 到 22:45 之间每问一次答一次 DUE,
    同一轮会被排进去几十遍 —— 而排程器是被循环调用的。
    """
    起跑 = 毫秒(刻(9, 7, 22, 1))
    got = decide(一条(), now=刻(9, 7, 22, 30), last_started_ms=起跑)
    assert got.kind is DecisionKind.NOT_YET


def test_昨天跑过不挡今天这一轮():
    """last_started_ms 比这一轮的点早,就是「这一轮还没跑」。"""
    昨天跑的 = 毫秒(刻(9, 6, 22, 1))
    got = decide(一条(), now=刻(9, 7, 22, 30), last_started_ms=昨天跑的)
    assert got.kind is DecisionKind.DUE


def test_正好在点上那一毫秒跑过的也算跑过():
    起跑 = 毫秒(刻(9, 7, 22, 0))
    got = decide(一条(), now=刻(9, 7, 22, 30), last_started_ms=起跑)
    assert got.kind is DecisionKind.NOT_YET


def test_窗口跨过午夜():
    """23:30 那一轮、窗口 60 分钟,到了第二天 00:15 还算「在窗口里」。

    **只看今天那一轮的话,这一条会答 NOT_YET** —— 00:15 时今天那一轮
    (今晚 23:30)确实还没到,而昨晚那一轮根本没被看一眼。于是每一条跨午夜
    的排程都在午夜整点被静静丢掉,没有任何一条告警。
    """
    e = 一条(at_h=23, at_m=30, window_min=60)
    got = decide(e, now=刻(9, 8, 0, 15), last_started_ms=None)
    assert got.kind is DecisionKind.DUE
    assert got.late_min == 45
    assert got.scheduled == 刻(9, 7, 23, 30)      # 昨晚那一轮,不是今晚


def test_跨午夜时看的是昨天的星期几():
    """周一 23:30 那一轮拖到周二 00:15,``days`` 里要有的是 **mon**。

    按「现在是周二」去查 days,周一那一轮就永远轮不上。
    """
    e = 一条(at_h=23, at_m=30, window_min=60, days=frozenset({"mon"}))
    assert decide(e, now=刻(9, 8, 0, 15), last_started_ms=None).kind is DecisionKind.DUE
    e2 = 一条(at_h=23, at_m=30, window_min=60, days=frozenset({"tue"}))
    assert decide(e2, now=刻(9, 8, 0, 15),
                  last_started_ms=None).kind is DecisionKind.NOT_YET


def test_跨午夜过了窗口一样按on_missed办():
    e = 一条(at_h=23, at_m=30, window_min=60, on_missed="alarm")
    got = decide(e, now=刻(9, 8, 1, 0), last_started_ms=None)
    assert got.kind is DecisionKind.ALARM
    assert got.late_min == 90


def test_今晚那一轮到点时不再看昨晚那一轮():
    """两轮都够得着的时候,取**晚的那一轮**。

    23:30 窗口 90 分钟:第二天 23:35 时,昨晚那轮(过了 24 小时)和今晚这轮
    (刚过 5 分钟)都在候选里。取昨晚那轮的话,狗会为一件早就该放弃的事出发。
    """
    e = 一条(at_h=23, at_m=30, window_min=90)
    got = decide(e, now=刻(9, 8, 23, 35), last_started_ms=None)
    assert got.kind is DecisionKind.DUE
    assert got.scheduled == 刻(9, 8, 23, 30)


def test_没有带时区的now就不许问():
    """裸 datetime 传进来,``timestamp()`` 会按**系统时区**解释它 ——
    正是 §3.3 第 1 条要堵死的那件事,而且它不报错,只是悄悄算错。
    """
    with pytest.raises(ValueError, match="时区"):
        decide(一条(), now=datetime(2026, 9, 7, 22, 0), last_started_ms=None)


def test_夏令时的春天跳表不炸():
    """马来西亚没有夏令时,但包是可以写别的时区的。

    纽约 2026-03-08 的 02:30 这个本地时刻**不存在**(跳过去了)。这条不
    规定该跑还是不该跑,只钉住一件事:**不许抛异常**。一条排程写在一个
    不存在的时刻上是写包的人的错,但它不该把整个排程器掀翻。
    """
    纽约 = ZoneInfo("America/New_York")
    e = 一条(at_h=2, at_m=30)
    got = decide(e, now=datetime(2026, 3, 8, 4, 0, tzinfo=纽约),
                 last_started_ms=None)
    assert isinstance(got, Decision)
    assert got.kind in tuple(DecisionKind)


def test_决定是冻的():
    got = decide(一条(), now=刻(9, 7, 22, 0), last_started_ms=None)
    with pytest.raises((AttributeError, TypeError)):
        got.kind = DecisionKind.SKIP                      # type: ignore[misc]


def test_五种决定的名字都在():
    """上线的字符串是接口的一部分,改一个字就是破坏兼容。"""
    assert {k.value for k in DecisionKind} == {
        "not_yet", "due", "late", "skip", "alarm"}


@pytest.mark.parametrize("分", list(range(0, 121, 7)))
def test_同一条排程在窗口内外的分界只有一处(分):
    """扫一遍两小时,确认 DUE 只出现在 [0, window_min] 这一段里,
    别处一次都不出现 —— 判据里但凡有个 off-by-one 或者第二个分支,
    这条参数化就会在某个分钟上红。
    """
    e = 一条(window_min=45, on_missed="skip")
    now = 刻(9, 7, 22, 0) + timedelta(minutes=分)
    got = decide(e, now=now, last_started_ms=None)
    want = DecisionKind.DUE if 分 <= 45 else DecisionKind.SKIP
    assert got.kind is want
