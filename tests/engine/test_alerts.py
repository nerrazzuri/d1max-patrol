"""告警的事实与三级判定(§5.2)。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.alerts import LEVEL_OF, AlertBook, Level


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
