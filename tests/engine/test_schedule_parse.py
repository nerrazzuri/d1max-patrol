"""排程块的解析。**这一层只管「这份排程写得对不对」,不管「该不该跑」。**

该不该跑在 test_schedule_decide.py。分开是因为解析的错都是写包的人当场
就该看见的,而判据的错要等到某个具体时刻才显形。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from d1max_contract.schedule import (
    DAYS,
    MAX_WINDOW_MIN,
    ON_MISSED,
    Schedule,
    ScheduleEntry,
    ScheduleError,
    parse_schedule,
)

好的一条 = {
    "id": "night-1",
    "mission": "perimeter-full",
    "at": "22:00",
    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "window_min": 45,
    "on_missed": "skip",
}


def 一份(**改动):
    条 = dict(好的一条)
    条.update(改动)
    return {"timezone": "Asia/Kuala_Lumpur", "entries": [条]}


def test_一份写对的排程解得出来():
    got = parse_schedule(一份())
    assert isinstance(got, Schedule)
    assert got.timezone == "Asia/Kuala_Lumpur"
    assert len(got.entries) == 1
    e = got.entries[0]
    assert isinstance(e, ScheduleEntry)
    assert (e.id, e.mission) == ("night-1", "perimeter-full")
    assert (e.at_h, e.at_m) == (22, 0)
    assert e.days == frozenset(DAYS)
    assert e.window_min == 45
    assert e.on_missed == "skip"
    assert e.priority == 0            # 没写就是 0


def test_没有时区就不许解():
    """§3.3 第 1 条。**这是本模块最重要的一条测试。**

    时区读系统的话,刷机、OTA、换主板都会把它打回 UTC,然后整晚的巡检
    时间全错 —— 而且没人会发现:狗一脸认真地在错误的时间巡逻,没有任何
    一条告警会响。所以「没写时区」必须是个当场就炸的错,不是一个兜底。
    """
    raw = 一份()
    del raw["timezone"]
    with pytest.raises(ScheduleError, match="timezone"):
        parse_schedule(raw)


def test_时区认不出来也不许解():
    """打错一个字母跟没写一样危险,而且更隐蔽。"""
    with pytest.raises(ScheduleError, match="认不出来"):
        parse_schedule({"timezone": "Asia/Kuala_Lampur",     # 拼错了
                        "entries": [dict(好的一条)]})


def test_时区库真的装着():
    """``zoneinfo`` 在 Windows 上没有 ``tzdata`` 这个包就一个时区都解不出来。

    今天本机能跑是因为它碰巧被别的东西装进来了 —— 没写进 pyproject 的
    依赖不是依赖,是运气。这条测试的价值在于它会在 CI 里红,而不是在现场
    晚上 10 点红。
    """
    got = parse_schedule(一份())
    assert got.tz() is not None
    # 真拿它转一次:ZoneInfo 造得出来但数据是空的这种坏法,只有转过才看得见。
    刻 = datetime(2026, 9, 7, 22, 0, tzinfo=got.tz())
    偏移 = 刻.utcoffset()
    assert 偏移 is not None
    assert 偏移.total_seconds() == 8 * 3600           # 马来西亚是 UTC+8


@pytest.mark.parametrize("坏值", ["22", "22:0", "2:00", "22:60", "24:00",
                                  "22:00:00", "晚上十点", "", None, 2200])
def test_钟点写不对就不许解(坏值):
    with pytest.raises(ScheduleError, match="at"):
        parse_schedule(一份(at=坏值))


def test_星期写不对就不许解():
    with pytest.raises(ScheduleError, match="days"):
        parse_schedule(一份(days=["mon", "funday"]))


def test_星期不许空():
    """空的 days 是一条永远不会跑的排程。写包的人几乎一定是漏了,不是故意的。"""
    with pytest.raises(ScheduleError, match="days"):
        parse_schedule(一份(days=[]))


def test_星期重复了不当错但会去重():
    got = parse_schedule(一份(days=["mon", "mon", "tue"]))
    assert got.entries[0].days == frozenset({"mon", "tue"})


def test_没写window_min就不许解():
    """§3.3 第 2 条:``window_min`` 是核心不是装饰。

    给它一个默认值,等于替写包的人回答了「迟到多久就不跑了」这个问题 ——
    而那个答案是跟现场绑死的:22:00 那轮拖到 22:50 才轮上,跑它可能比不跑
    更糟(跟下一轮撞、或者天亮了)。每条都必须自己回答。
    """
    条 = dict(好的一条)
    del 条["window_min"]
    with pytest.raises(ScheduleError, match="window_min"):
        parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [条]})


@pytest.mark.parametrize("坏值", [0, -1, MAX_WINDOW_MIN + 1, "45", 45.0, None, True])
def test_window_min的取值范围(坏值):
    """上界一整天。比这还长的迟到窗口意味着「永远不算迟到」,那就是把
    §3.3 第 2 条整条绕过去了。

    ``True`` 也在这份名单里:``isinstance(True, int)`` 是真的,用 isinstance
    判类型的话 ``window_min: true`` 会被当成 1 分钟收下。
    """
    with pytest.raises(ScheduleError, match="window_min"):
        parse_schedule(一份(window_min=坏值))


def test_没写on_missed就不许解():
    """三种处置的区别是「人的动作有什么不同」:skip 没人管、run_late 照跑、
    alarm 要把人叫醒。给默认值等于替现场选了「没人被叫醒」,而且是静默地选。
    """
    条 = dict(好的一条)
    del 条["on_missed"]
    with pytest.raises(ScheduleError, match="on_missed"):
        parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [条]})


def test_on_missed只认三个值():
    assert ON_MISSED == frozenset({"skip", "run_late", "alarm"})
    with pytest.raises(ScheduleError, match="on_missed"):
        parse_schedule(一份(on_missed="retry"))


def test_id重复就不许解():
    """id 是「上次跑过」那份记录的键。重了的话两条排程共用一份记录,
    后一条永远被前一条压住,而且不报错。
    """
    甲 = dict(好的一条)
    乙 = dict(好的一条, mission="perimeter-short")
    with pytest.raises(ScheduleError, match="id"):
        parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [甲, 乙]})


def test_entries可以是空的():
    """只手动跑、不排程的狗是合法的产品形态。空列表不是错。"""
    got = parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": []})
    assert got.entries == ()


def test_entries缺了这个键也当空的():
    got = parse_schedule({"timezone": "Asia/Kuala_Lumpur"})
    assert got.entries == ()


def test_entries不是列表就不许解():
    with pytest.raises(ScheduleError, match="entries"):
        parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": {"a": 1}})


def test_priority读得进来():
    assert parse_schedule(一份(priority=5)).entries[0].priority == 5


@pytest.mark.parametrize("坏值", ["5", 5.5, None])
def test_priority不是整数就不许解(坏值):
    with pytest.raises(ScheduleError, match="priority"):
        parse_schedule(一份(priority=坏值))


def test_上线的形状():
    """``days`` 按 DAYS 的顺序出,不按 set 的迭代顺序。

    后者每次运行都可能不同,那会让「同一份包在两台狗上的接口输出不一样」——
    而那种不一致查起来要人命。
    """
    got = parse_schedule(一份(days=["sat", "mon", "wed"]))
    assert got.to_wire() == {
        "timezone": "Asia/Kuala_Lumpur",
        "entries": [{
            "id": "night-1",
            "mission": "perimeter-full",
            "at": "22:00",
            "days": ["mon", "wed", "sat"],
            "window_min": 45,
            "on_missed": "skip",
            "priority": 0,
        }],
    }


def test_解出来的东西是冻的():
    """排程会被到处传。可变的话,一个函数改了它,别的函数看到的就变了。"""
    e = parse_schedule(一份()).entries[0]
    with pytest.raises((AttributeError, TypeError)):
        e.window_min = 1                                    # type: ignore[misc]


def test_排程条目可以指定狗_不写时线格式不变():
    """W00c2a:站点执行排程时用 ``robot`` 指定哪台跑。老包没这个字段,导出也不多出来。"""
    from d1max_contract.schedule import parse_schedule
    s = parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [
        {"id": "a", "mission": "m", "at": "22:00", "days": ["mon"], "window_min": 30,
         "on_missed": "skip", "robot": "D1MAX-001"},
        {"id": "b", "mission": "m", "at": "23:00", "days": ["mon"], "window_min": 30,
         "on_missed": "skip"}]})
    a, b = s.entries
    assert a.robot == "D1MAX-001" and a.to_wire()["robot"] == "D1MAX-001"
    assert b.robot == "" and "robot" not in b.to_wire()
    import pytest

    from d1max_contract.schedule import ScheduleError
    with pytest.raises(ScheduleError):
        parse_schedule({"timezone": "UTC", "entries": [
            {"id": "c", "mission": "m", "at": "01:00", "days": ["mon"], "window_min": 5,
             "on_missed": "skip", "robot": 7}]})
