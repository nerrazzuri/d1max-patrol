"""告警的事实与三级判定(§5.2)。"""

from __future__ import annotations

import pytest

from d1max_agent.engine.alerts import (
    ESCALATE_AFTER_MS,
    LEVEL_OF,
    AlertBook,
    AlertNotFound,
    Channel,
    Level,
)


def test_三级各自认得出来():
    book = AlertBook()
    p1 = book.raise_alert(level=Level.P1, kind="stuck", robot="dog-1",
                          title="卡住了", now_ms=1000)
    p3 = book.raise_alert(level=Level.P3, kind="run_done", robot="dog-1",
                          title="跑完了", now_ms=1000)
    assert p1.level is Level.P1
    assert p3.level is Level.P3


def test_同一只狗同一类型合成一条并累加():
    """§5.4:一个根因产生 50 条告警,真要紧的那条就被埋了。"""
    book = AlertBook()
    book.raise_alert(level=Level.P1, kind="stuck", robot="dog-1",
                     title="卡住了", now_ms=1000)
    a = book.raise_alert(level=Level.P1, kind="stuck", robot="dog-1",
                         title="卡住了", now_ms=4000)
    assert len(book.open()) == 1
    assert a.count == 2
    assert a.first_ms == 1000 and a.last_ms == 4000


def test_P1不跨狗合并():
    """§5.4:两只狗同时卡住是两件事,要跑两趟。"""
    book = AlertBook()
    book.raise_alert(level=Level.P1, kind="stuck", robot="dog-1",
                     title="卡住了", now_ms=1000)
    book.raise_alert(level=Level.P1, kind="stuck", robot="dog-2",
                     title="卡住了", now_ms=1000)
    assert len(book.open()) == 2


def test_kind与级别的对应表只有一处():
    """判级不许散在调用点上 —— 散了就会有两处说法不一样的那天。"""
    for kind in ("stuck", "battery_abort", "fallen", "loc_lost_paused",
                 "estop_pressed", "lease_expired"):
        assert LEVEL_OF[kind] is Level.P1
    assert LEVEL_OF["schedule_died"] is Level.P1 and "schedule_died" != "watchdog_died"
    for kind in ("finding", "disk_80", "upload_backlog", "bundle_lag",
                 "clock_skew"):
        assert LEVEL_OF[kind] is Level.P2
    for kind in ("run_done", "run_start", "battery_swap"):
        assert LEVEL_OF[kind] is Level.P3


def test_没登记的kind会抛而不是猜一个级别():
    """猜出来的级别会让人在错误的时刻起身,或者错过该起身的那次。"""
    book = AlertBook()
    with pytest.raises(KeyError):
        book.raise_alert(kind="从没见过的东西", robot="dog-1",
                         title="?", now_ms=1000)


def test_解决了不等于确认过_两个状态各存各的():
    """§5.3:修一个问题合理地可以花一小时。**没人看见**才是真正的失败。"""
    book = AlertBook()
    a = book.raise_alert(level=Level.P1, kind="stuck", robot="dog-1",
                         title="卡住了", now_ms=1000)
    b = book.resolve(a.key, now_ms=2000)
    assert b.resolved_ms == 2000
    assert b.acked_ms is None          # 解决了,但没有人看见过
    assert b.acked_by == ""


def test_传入的level跟表不一致会抛而不是悄悄接受():
    """这条分支存在的全部意义是逼 Task 12 在新增 kind 时去注册它 —— 没测试
    守着,它跟不存在没有区别,下一个人"顺手放宽一下"也不会有任何东西变红。
    异常信息里要能看出两边分别是什么:表里是哪一档,传的是哪一档。"""
    book = AlertBook()
    with pytest.raises(ValueError) as exc_info:
        book.raise_alert(kind="stuck", robot="dog-1", title="卡住了",
                         now_ms=1000, level=Level.P3)
    message = str(exc_info.value)
    assert "P1" in message   # LEVEL_OF 里登记的那一档
    assert "P3" in message   # 调用方传进来的那一档

    # 跟"kind 根本没注册"是两条不同的路:那条抛 KeyError,这条抛 ValueError,
    # 不能混。
    with pytest.raises(KeyError):
        book.raise_alert(kind="从没见过的东西", robot="dog-1",
                         title="?", now_ms=1000, level=Level.P3)


def test_窗口内合并窗口外另起一条():
    book = AlertBook(window_ms=1000)
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=900)
    assert len(book.open()) == 1

    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=2500)
    assert len(book.open()) == 2       # 隔了这么久,是新的一件事


def test_窗口从最后一次算起不是从第一次():
    """连续不断地卡,是一件事在持续,不是每 window 就变成新的一件。"""
    book = AlertBook(window_ms=1000)
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=900)
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=1700)
    assert len(book.open()) == 1
    assert book.open()[0].count == 3


def test_确认过的告警不再吸收新的():
    """人已经看见并确认了这一条,再发生就是**新的一次**,要重新被看见。"""
    book = AlertBook(window_ms=10_000)
    a = book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    book.ack(a.key, who="老王", now_ms=100)
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=200)
    assert len(book.open()) == 2


def test_P1没人确认就换通道升级():
    book = AlertBook()
    a = book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    assert a.channel is Channel.SCREEN

    due = book.due_escalations(now_ms=ESCALATE_AFTER_MS[0] + 1)
    assert [(x.key, ch) for x, ch in due] == [(a.key, Channel.PUSH)]

    due = book.due_escalations(now_ms=ESCALATE_AFTER_MS[1] + 1)
    assert [ch for _, ch in due] == [Channel.SOUND]


def test_确认了就停止升级():
    book = AlertBook()
    a = book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    book.ack(a.key, who="老王", now_ms=10)
    assert book.due_escalations(now_ms=ESCALATE_AFTER_MS[1] + 1) == ()


def test_解决了但没人确认_照样升级():
    """§5.3 的原句:升级看的是**有没有人确认**,不是问题有没有解决。"""
    book = AlertBook()
    a = book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    book.resolve(a.key, now_ms=10)
    due = book.due_escalations(now_ms=ESCALATE_AFTER_MS[0] + 1)
    assert [ch for _, ch in due] == [Channel.PUSH]


def test_确认必须记名():
    book = AlertBook()
    a = book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    with pytest.raises(ValueError):
        book.ack(a.key, who="", now_ms=10)      # 匿名确认不算确认
    b = book.ack(a.key, who="老王", now_ms=10)
    assert b.acked_by == "老王" and b.acked_ms == 10


def test_P2不升级():
    """"今天之内"被升级成"立刻",那它本来就该是 P1。"""
    book = AlertBook()
    book.raise_alert(kind="disk_80", robot="dog-1", title="盘 80%", now_ms=0)
    assert book.due_escalations(now_ms=ESCALATE_AFTER_MS[1] + 1) == ()


def test_同一档只报一次():
    """升级是换通道,不是每次问都重报一遍 —— 重报会让声音一直响。"""
    book = AlertBook()
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    t = ESCALATE_AFTER_MS[0] + 1
    assert len(book.due_escalations(now_ms=t)) == 1
    assert book.due_escalations(now_ms=t) == ()      # 第二次问,已经报过了


def test_持续刷新的告警照样按first_ms升级():
    """升级计时基准是 first_ms,不是 last_ms —— 一条一直刷新 last_ms 的告警
    (比如持续卡着不断重复触发)不能因为 last_ms 一直是最近而永远升不上去。
    这条测试专门补 first_ms 与 last_ms 会分道扬镳的场景(现有测试只
    raise 了一次,测不出这条)。"""
    book = AlertBook()
    a = book.raise_alert(kind="stuck", robot="dog-1", title="卡住了", now_ms=0)
    # 持续刷新 last_ms,但 first_ms 仍然是 0。
    book.raise_alert(kind="stuck", robot="dog-1", title="卡住了",
                     now_ms=ESCALATE_AFTER_MS[0] - 1)
    due = book.due_escalations(now_ms=ESCALATE_AFTER_MS[0] + 1)
    assert [(x.key, ch) for x, ch in due] == [(a.key, Channel.PUSH)]


def test_open按级别排序_同级里新的在前():
    """brief 和 Task 3 报告都承诺"P1 永远在最上面",但只断言 len(open())
    的测试守不住排序键写反(正序、忘了负号、级别和时间调换)。这条测试
    用不同的 robot 和不同的 kind,跟 Task 4 的聚合行为(窗口/序号)无关,
    只钉排序本身。"""
    book = AlertBook()
    book.raise_alert(kind="disk_80", robot="dog-1", title="盘满", now_ms=100)     # P2
    book.raise_alert(kind="run_done", robot="dog-2", title="跑完", now_ms=200)    # P3
    book.raise_alert(kind="stuck", robot="dog-3", title="卡住了", now_ms=50)      # P1,较旧
    book.raise_alert(kind="fallen", robot="dog-4", title="倒了", now_ms=500)      # P1,较新
    assert [a.kind for a in book.open()] == ["fallen", "stuck", "disk_80", "run_done"]


def test_ack不存在的key抛AlertNotFound():
    """前端点了一条已经不在的告警 —— 正常的用户误操作,不是代码写错了
    (那是 kind 没注册的 KeyError,见下面那条测试)。"""
    book = AlertBook()
    with pytest.raises(AlertNotFound):
        book.ack("dog-1/nope#1", who="老王", now_ms=10)


def test_resolve不存在的key抛AlertNotFound():
    book = AlertBook()
    with pytest.raises(AlertNotFound):
        book.resolve("dog-1/nope#1", now_ms=10)


def test_kind没注册抛的是裸KeyError不是AlertNotFound():
    """AlertNotFound 继承 KeyError,子类也满足 pytest.raises(KeyError) ——
    必须用 type(exc) is KeyError 精确钉死,不能只断言 KeyError。"""
    book = AlertBook()
    with pytest.raises(KeyError) as exc_info:
        book.raise_alert(kind="从没见过的东西", robot="dog-1",
                         title="?", now_ms=1000)
    assert type(exc_info.value) is KeyError
