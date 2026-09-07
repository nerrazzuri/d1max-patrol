"""时钟漂移,和「下一轮是什么时候」。

漂移这件事没有任何**自然**的报警渠道:狗会一脸认真地在错误的时间巡逻,
每一步都成功,每一条日志都正常。所以它必须被显式地算出来、报出去。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from d1max_patrol.engine.schedule import (
    CLOCK_SKEW_ALARM_S,
    ScheduleEntry,
    clock_skew,
    next_run,
)

吉隆坡 = ZoneInfo("Asia/Kuala_Lumpur")


def 一条(**改动) -> ScheduleEntry:
    参 = {"id": "night-1", "mission": "m", "at_h": 22, "at_m": 0,
          "days": frozenset({"mon", "wed"}), "window_min": 45,
          "on_missed": "skip", "priority": 0}
    参.update(改动)
    return ScheduleEntry(**参)                    # type: ignore[arg-type]


def 刻(月, 日, 时, 分) -> datetime:
    return datetime(2026, 月, 日, 时, 分, tzinfo=吉隆坡)


def test_对得上就是零():
    got = clock_skew(local_ms=1_700_000_000_000,
                     reference_ms=1_700_000_000_000, source="ntp")
    assert got.skew_s == 0.0
    assert got.alarm is False
    assert got.source == "ntp"


def test_本地快了是正数():
    got = clock_skew(local_ms=1_700_000_030_000,
                     reference_ms=1_700_000_000_000, source="ntp")
    assert got.skew_s == 30.0


def test_本地慢了是负数():
    got = clock_skew(local_ms=1_700_000_000_000,
                     reference_ms=1_700_000_030_000, source="ntp")
    assert got.skew_s == -30.0


def test_超过阈值就告警():
    多一点 = int((CLOCK_SKEW_ALARM_S + 1) * 1000)
    got = clock_skew(local_ms=1_700_000_000_000 + 多一点,
                     reference_ms=1_700_000_000_000, source="ntp")
    assert got.alarm is True


def test_慢了同样告警():
    """**两边都要拦。** 只看正数的话,一台慢了半小时的机器一声不吭。"""
    多一点 = int((CLOCK_SKEW_ALARM_S + 1) * 1000)
    got = clock_skew(local_ms=1_700_000_000_000 - 多一点,
                     reference_ms=1_700_000_000_000, source="ntp")
    assert got.alarm is True


def test_正好在阈值上不告警():
    正好 = int(CLOCK_SKEW_ALARM_S * 1000)
    got = clock_skew(local_ms=1_700_000_000_000 + 正好,
                     reference_ms=1_700_000_000_000, source="ntp")
    assert got.alarm is False


def test_没有参照就不假装知道():
    """断网时没有 NTP,只有 RTC 在漂。这时候 skew 是**不知道**,不是 0。

    报 0 等于说「钟是准的」,而那正是断网久了之后最不可能成立的一句话。
    """
    got = clock_skew(local_ms=1_700_000_000_000, reference_ms=None, source="")
    assert got.skew_s is None
    assert got.alarm is False
    assert got.source == "unknown"


def test_上线的形状():
    got = clock_skew(local_ms=1_700_000_030_000,
                     reference_ms=1_700_000_000_000, source="ntp")
    assert got.to_wire() == {"clock_skew_s": 30.0, "alarm": False, "source": "ntp"}


def test_不知道时上线也说不知道():
    got = clock_skew(local_ms=1, reference_ms=None, source="")
    assert got.to_wire() == {"clock_skew_s": None, "alarm": False,
                             "source": "unknown"}


def test_下一轮是今天():
    assert next_run(一条(), now=刻(9, 7, 10, 0)) == 刻(9, 7, 22, 0)   # 周一


def test_今天这一轮已经过了就看下一个够得着的日子():
    assert next_run(一条(), now=刻(9, 7, 23, 0)) == 刻(9, 9, 22, 0)   # 周三


def test_今天不在days里就往后找():
    assert next_run(一条(), now=刻(9, 8, 10, 0)) == 刻(9, 9, 22, 0)   # 周二问


def test_正好在点上算今天这一轮():
    assert next_run(一条(), now=刻(9, 7, 22, 0)) == 刻(9, 7, 22, 0)


def test_一天都不排就没有下一轮():
    """``days`` 是空的进不了解析,但 ``ScheduleEntry`` 是可以直接构的。
    往前找的循环必须有界,不然这里会死循环。
    """
    assert next_run(一条(days=frozenset()), now=刻(9, 7, 10, 0)) is None


def test_下一轮也要带时区():
    got = next_run(一条(), now=刻(9, 7, 10, 0))
    assert got is not None and got.utcoffset() is not None


def test_没带时区的now不许问():
    with pytest.raises(ValueError, match="时区"):
        next_run(一条(), now=datetime(2026, 9, 7, 10, 0))
