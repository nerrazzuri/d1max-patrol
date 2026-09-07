"""接管:礼貌地要、到点自动给、必要时硬抢 —— 每一下都留痕。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.lease import (
    LEASE_TTL_MS,
    TAKEOVER_GRACE_MS,
    Holder,
    LeaseBook,
    LeaseBusy,
    LeaseError,
    LeaseLost,
)

T0 = 1_757_000_000_000


def test_请求接管不立刻夺权():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    assert st.holder == Holder("aa11bb22", "张三")
    assert st.challenger == Holder("cc33dd44", "李四")
    assert st.grace_ends_ms == T0 + 1_000 + TAKEOVER_GRACE_MS


def test_请求接管进审计():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    rec = book.audit[-1]
    assert rec.kind == "takeover_asked"
    assert rec.ref == "cc33dd44"
    assert rec.operator == "李四"
    assert "张三" in rec.detail


def test_没人持有时请求接管直接给():
    book = LeaseBook()
    st = book.ask_takeover("cc33dd44", "李四", now_ms=T0)
    assert st.holder == Holder("cc33dd44", "李四")
    assert st.challenger is None
    assert [r.kind for r in book.audit] == ["acquired"]


def test_自己已经持有还去请求接管是错的():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    with pytest.raises(LeaseError):
        book.ask_takeover("aa11bb22", "张三", now_ms=T0 + 1_000)


def test_同一时刻只排一个挑战者():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    with pytest.raises(LeaseBusy):
        book.ask_takeover("ee55ff66", "王五", now_ms=T0 + 2_000)


def test_同一个人再请求一次只是把宽限期重置():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 4_000)
    assert st.grace_ends_ms == T0 + 4_000 + TAKEOVER_GRACE_MS


def test_持有者同意就立刻移交():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.approve("aa11bb22", now_ms=T0 + 2_000)
    assert st.holder == Holder("cc33dd44", "李四")
    assert st.challenger is None
    assert st.expires_ms == T0 + 2_000 + LEASE_TTL_MS
    assert book.audit[-1].kind == "taken_over"
    assert "同意" in book.audit[-1].detail


def test_只有持有者能同意():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    with pytest.raises(LeaseLost):
        book.approve("ee55ff66", now_ms=T0 + 2_000)


def test_没人请求接管时同意是错的():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    with pytest.raises(LeaseError):
        book.approve("aa11bb22", now_ms=T0 + 1_000)


def test_宽限期到了自动移交():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.state(now_ms=T0 + 1_000 + TAKEOVER_GRACE_MS)
    assert st.holder == Holder("cc33dd44", "李四")
    assert book.audit[-1].kind == "taken_over"
    assert "没有回应" in book.audit[-1].detail


def test_一直心跳也拖不掉接管():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    book.renew("aa11bb22", now_ms=T0 + 5_000)
    book.renew("aa11bb22", now_ms=T0 + 15_000)
    with pytest.raises(LeaseLost):
        book.renew("aa11bb22", now_ms=T0 + 1_000 + TAKEOVER_GRACE_MS)
    assert book.state(now_ms=T0 + 20_000).holder == Holder("cc33dd44", "李四")


def test_持有者先过期挑战者直接接上():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 25_000)
    st = book.state(now_ms=T0 + LEASE_TTL_MS)
    assert st.holder == Holder("cc33dd44", "李四")
    assert [r.kind for r in book.audit][-2:] == ["expired", "taken_over"]
    assert "到期" in book.audit[-1].detail


def test_持有者交回时挑战者要重新来一次():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.release("aa11bb22", now_ms=T0 + 2_000)
    assert st.holder is None
    assert st.challenger is None


def test_强制接管立刻夺权():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.force("cc33dd44", "李四", now_ms=T0 + 1_000,
                    reason="张三的手机没电了,现场要挪狗")
    assert st.holder == Holder("cc33dd44", "李四")
    assert st.expires_ms == T0 + 1_000 + LEASE_TTL_MS


def test_强制接管必须写理由():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    with pytest.raises(LeaseError) as err:
        book.force("cc33dd44", "李四", now_ms=T0 + 1_000, reason="   ")
    assert "理由" in str(err.value)
    assert book.state(now_ms=T0 + 1_000).holder == Holder("aa11bb22", "张三")


def test_强制接管的理由进审计():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.force("cc33dd44", "李四", now_ms=T0 + 1_000, reason="手机没电了")
    rec = book.audit[-1]
    assert rec.kind == "forced"
    assert rec.ref == "cc33dd44"
    assert rec.operator == "李四"
    assert "张三" in rec.detail
    assert "手机没电了" in rec.detail


def test_没人持有时也能强制接管():
    book = LeaseBook()
    st = book.force("cc33dd44", "李四", now_ms=T0, reason="接手现场")
    assert st.holder == Holder("cc33dd44", "李四")
    assert book.audit[-1].kind == "forced"


def test_强制接管把在排队的挑战者也清掉():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.force("ee55ff66", "王五", now_ms=T0 + 2_000, reason="总控接管")
    assert st.holder == Holder("ee55ff66", "王五")
    assert st.challenger is None
