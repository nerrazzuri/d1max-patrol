"""撞车。**同一时刻只跑一个**(§3.3 第 3 条)。

这一层同样是纯函数:入参是「每条排程此刻的决定」加「现在正在跑谁」,
出参是「起跑谁、挤掉谁、为谁告警」。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from d1max_patrol.engine.schedule import (
    Decision,
    DecisionKind,
    ScheduleEntry,
    pick,
)

吉隆坡 = ZoneInfo("Asia/Kuala_Lumpur")


def 一条(id_: str, *, priority: int = 0, on_missed: str = "skip") -> ScheduleEntry:
    return ScheduleEntry(id=id_, mission=f"m-{id_}", at_h=22, at_m=0,
                         days=frozenset({"mon"}), window_min=45,
                         on_missed=on_missed, priority=priority)


def 到点(entry: ScheduleEntry, *, 时=22, 分=0,
        kind: DecisionKind = DecisionKind.DUE) -> tuple[ScheduleEntry, Decision]:
    刻 = datetime(2026, 9, 7, 时, 分, tzinfo=吉隆坡)
    return entry, Decision(kind, 刻, int(刻.timestamp() * 1000), 0)


def test_没人到点就谁也不跑():
    got = pick([], running=None)
    assert got.chosen is None
    assert got.displaced == ()
    assert got.alarms == ()


def test_一条到点就跑它():
    甲 = 一条("a")
    got = pick([到点(甲)], running=None)
    assert got.chosen is not None
    assert got.chosen[0].id == "a"
    assert got.displaced == ()


def test_正在跑的时候谁都不起跑():
    """**这是单队列这条规矩的全部。**

    正在跑的那个不许被排程打断 —— 打断要走 §3.4 那条独立实时通道,
    而那条通道是第 8 卷的事,不在这儿。
    """
    甲, 乙 = 一条("a"), 一条("b")
    got = pick([到点(甲), 到点(乙)], running="c")
    assert got.chosen is None
    assert [e.id for e, _ in got.displaced] == ["a", "b"]


def test_正在跑的就是自己也不重复起跑():
    甲 = 一条("a")
    got = pick([到点(甲)], running="a")
    assert got.chosen is None
    assert [e.id for e, _ in got.displaced] == ["a"]


def test_撞车按priority():
    低 = 一条("low", priority=1)
    高 = 一条("high", priority=9)
    got = pick([到点(低), 到点(高)], running=None)
    assert got.chosen[0].id == "high"
    assert [e.id for e, _ in got.displaced] == ["low"]


def test_priority一样就先到先得():
    早 = 一条("early")
    晚 = 一条("late")
    got = pick([到点(早, 时=22, 分=0), 到点(晚, 时=22, 分=30)], running=None)
    assert got.chosen[0].id == "early"


def test_连到点时刻都一样就按id():
    """**这条平局判据必须有。**

    没有它,同一份包在两台狗上可能选出不同的任务(dict/set 的迭代顺序、
    输入列表的顺序都可能不同),而那种不一致查起来要人命。
    """
    甲, 乙 = 一条("aaa"), 一条("bbb")
    正 = pick([到点(甲), 到点(乙)], running=None)
    反 = pick([到点(乙), 到点(甲)], running=None)
    assert 正.chosen[0].id == 反.chosen[0].id == "aaa"


def test_被挤掉的on_missed是alarm就进alarms():
    """被挤掉跟迟到超窗一样,是「这一轮没跑成」。要不要把人叫醒,
    仍然由这条排程自己的 on_missed 决定。
    """
    跑的 = 一条("win", priority=9)
    要喊的 = 一条("loud", priority=1, on_missed="alarm")
    安静的 = 一条("quiet", priority=1, on_missed="skip")
    got = pick([到点(跑的), 到点(要喊的), 到点(安静的)], running=None)
    assert got.chosen[0].id == "win"
    assert [e.id for e, _ in got.alarms] == ["loud"]
    assert [e.id for e, _ in got.displaced] == ["loud", "quiet"]


def test_被挤掉的run_late不会被留到下一轮():
    """``run_late`` 说的是「迟到了也照跑」,不是「排队等着」。

    单队列里没有队列 —— 下一次问的时候 ``decide()`` 会重新算,那时它要么
    还在窗口里(再被排一次),要么已经超窗(按 on_missed 办)。**这里不留
    任何状态**,是因为一个存着「欠着几轮」的队列,断电之后就成了一个说不清
    的东西。
    """
    跑的 = 一条("win", priority=9)
    迟的 = 一条("later", priority=1, on_missed="run_late")
    got = pick([到点(跑的), 到点(迟的)], running=None)
    assert [e.id for e, _ in got.displaced] == ["later"]
    assert got.alarms == ()


def test_只有DUE和LATE算到点():
    """SKIP / ALARM / NOT_YET 是 decide() 已经判了不跑的,不该再进队列。

    调用方本可以自己过滤,但那意味着每个调用方都要记得过滤 —— 而漏掉的
    那个会让一条已经判了 SKIP 的排程被起跑。
    """
    甲 = 一条("a")
    got = pick([到点(甲, kind=DecisionKind.SKIP)], running=None)
    assert got.chosen is None
    assert got.displaced == ()


def test_LATE也算到点():
    甲 = 一条("a", on_missed="run_late")
    got = pick([到点(甲, kind=DecisionKind.LATE)], running=None)
    assert got.chosen[0].id == "a"


def test_挑出来的东西是冻的():
    got = pick([], running=None)
    with pytest.raises((AttributeError, TypeError)):
        got.chosen = None                                 # type: ignore[misc]
