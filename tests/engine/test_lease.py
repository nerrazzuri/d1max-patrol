"""L1 控制权租约的核心:取、续、还、到期。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.lease import (
    AUDIT_KINDS,
    LEASE_HEARTBEAT_MS,
    LEASE_TTL_MS,
    Holder,
    LeaseBook,
    LeaseBusy,
    LeaseLost,
)

T0 = 1_757_000_000_000


def test_一开始没人持有():
    book = LeaseBook()
    st = book.state(now_ms=T0)
    assert st.holder is None
    assert st.expires_ms is None
    assert st.challenger is None


def test_拿到控制权就有了持有者和到期时刻():
    book = LeaseBook()
    st = book.acquire("aa11bb22", "张三", now_ms=T0)
    assert st.holder == Holder("aa11bb22", "张三")
    assert st.expires_ms == T0 + LEASE_TTL_MS


def test_不报名字也拿得到():
    book = LeaseBook()
    st = book.acquire("aa11bb22", now_ms=T0)
    assert st.holder is not None
    assert st.holder.operator == ""


def test_别人拿着就抢不到():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    with pytest.raises(LeaseBusy) as err:
        book.acquire("cc33dd44", "李四", now_ms=T0 + 1_000)
    assert "张三" in str(err.value)


def test_没报名字的持有者在拒绝信息里有个称呼():
    book = LeaseBook()
    book.acquire("aa11bb22", now_ms=T0)
    with pytest.raises(LeaseBusy) as err:
        book.acquire("cc33dd44", "李四", now_ms=T0)
    assert "没报名字" in str(err.value)


def test_自己再拿一次是续租不是报错():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.acquire("aa11bb22", "张三", now_ms=T0 + 5_000)
    assert st.expires_ms == T0 + 5_000 + LEASE_TTL_MS
    assert [r.kind for r in book.audit] == ["acquired"]


def test_续租把到期时刻往后推():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.renew("aa11bb22", now_ms=T0 + LEASE_HEARTBEAT_MS)
    assert st.expires_ms == T0 + LEASE_HEARTBEAT_MS + LEASE_TTL_MS


def test_不是持有者续不了():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    with pytest.raises(LeaseLost):
        book.renew("cc33dd44", now_ms=T0 + 1_000)


def test_到点没续就自己过期():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.state(now_ms=T0 + LEASE_TTL_MS)
    assert st.holder is None
    assert [r.kind for r in book.audit] == ["acquired", "expired"]


def test_丢两拍还活着丢三拍才掉():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    assert book.state(now_ms=T0 + 2 * LEASE_HEARTBEAT_MS).holder is not None
    assert book.state(now_ms=T0 + 3 * LEASE_HEARTBEAT_MS).holder is None


def test_过期之后别人拿得到():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.acquire("cc33dd44", "李四", now_ms=T0 + LEASE_TTL_MS)
    assert st.holder == Holder("cc33dd44", "李四")


def test_过期的持有者续不回来():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    with pytest.raises(LeaseLost):
        book.renew("aa11bb22", now_ms=T0 + LEASE_TTL_MS)


def test_交回之后就没人持有了():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.release("aa11bb22", now_ms=T0 + 1_000)
    assert st.holder is None
    assert [r.kind for r in book.audit] == ["acquired", "released"]


def test_交回一份不属于自己的租约不算错():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.release("cc33dd44", now_ms=T0 + 1_000)
    assert st.holder == Holder("aa11bb22", "张三")
    assert [r.kind for r in book.audit] == ["acquired"]


def test_审计记下了是谁在什么时候干的():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    rec = book.audit[0]
    assert rec.seq == 1
    assert rec.at_ms == T0
    assert rec.kind == "acquired"
    assert rec.ref == "aa11bb22"
    assert rec.operator == "张三"


def test_审计里出现的种类都在名单上():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.release("aa11bb22", now_ms=T0 + 1_000)
    book.acquire("cc33dd44", "李四", now_ms=T0 + 2_000)
    book.state(now_ms=T0 + 2_000 + LEASE_TTL_MS)
    assert {r.kind for r in book.audit} <= AUDIT_KINDS


def test_续租不进审计():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    for i in range(1, 60):
        book.renew("aa11bb22", now_ms=T0 + i * LEASE_HEARTBEAT_MS)
    assert [r.kind for r in book.audit] == ["acquired"]


def test_审计环满了从头上挤掉():
    book = LeaseBook(audit_max=4)
    for i in range(10):
        book.acquire("aa11bb22", "张三", now_ms=T0 + i * 100_000)
        book.release("aa11bb22", now_ms=T0 + i * 100_000 + 1_000)
    assert len(book.audit) == 4
    assert [r.seq for r in book.audit] == [17, 18, 19, 20]


def test_按游标只拿新的():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    cursor, fresh = book.audit_since(0)
    assert cursor == 1
    assert [r.kind for r in fresh] == ["acquired"]
    cursor, fresh = book.audit_since(cursor)
    assert fresh == ()
    book.release("aa11bb22", now_ms=T0 + 1_000)
    cursor, fresh = book.audit_since(cursor)
    assert [r.kind for r in fresh] == ["released"]


def test_状态能整个发出去():
    book = LeaseBook()
    wire = book.acquire("aa11bb22", "张三", now_ms=T0).to_wire()
    assert wire == {
        "holder": {"ref": "aa11bb22", "operator": "张三"},
        "expires_ms": T0 + LEASE_TTL_MS,
        "challenger": None,
        "grace_ends_ms": None,
        "ttl_ms": LEASE_TTL_MS,
        "heartbeat_ms": LEASE_HEARTBEAT_MS,
    }


def test_审计能整个发出去():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    assert book.audit[0].to_wire() == {
        "seq": 1, "at_ms": T0, "kind": "acquired",
        "ref": "aa11bb22", "operator": "张三", "detail": "",
    }
