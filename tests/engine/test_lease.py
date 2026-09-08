"""L1 控制权租约的核心:取、续、还、到期。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.lease import (
    AUDIT_KINDS,
    LEASE_HEARTBEAT_MS,
    LEASE_TTL_MS,
    TAKEOVER_GRACE_MS,
    Holder,
    LeaseBook,
    LeaseBusy,
    LeaseLost,
    _剩余毫秒,
    每拍都变的键,
)

T0 = 1_757_000_000_000


@pytest.fixture
def book() -> LeaseBook:
    return LeaseBook()


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
        "expires_in_ms": LEASE_TTL_MS,
        "grace_in_ms": None,
    }


# ------------------------------------------ 倒计时读的是相对量(挂账 52)


def test_到期余量是服务端算好的相对量():
    """**手机端的倒计时不许拿绝对时刻减自己的钟。**

    狗上没有 NTP,它的 RTC 和手机的钟必然漂;两边差几秒,一个"还剩 15 秒"的
    倒计时就可能显示成负数。所以余量在服务端、在同一个 ``now_ms`` 上算好。
    """
    book = LeaseBook()
    st = book.acquire("aa11bb22", "张三", now_ms=T0)
    assert st.expires_in_ms == LEASE_TTL_MS
    st = book.state(now_ms=T0 + 8_000)
    assert st.expires_in_ms == LEASE_TTL_MS - 8_000
    # 绝对时刻**保留** —— 审计和日志要拿它跟别的记录对时间。
    assert st.expires_ms == T0 + LEASE_TTL_MS


def test_礼貌接管的宽限期也有相对量():
    """挑战者那 15 秒倒计时是 §3.5 规则 3 的核心交互,它**没有第二条路**能算。"""
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    assert st.grace_in_ms == TAKEOVER_GRACE_MS
    st = book.state(now_ms=T0 + 1_000 + 5_000)
    assert st.grace_in_ms == TAKEOVER_GRACE_MS - 5_000
    assert st.grace_ends_ms == T0 + 1_000 + TAKEOVER_GRACE_MS


def test_没有持有者时余量是none不是零():
    """"这件事不存在"和"这件事还剩 0 毫秒"在界面上是两种画法。"""
    st = LeaseBook().state(now_ms=T0)
    assert st.expires_in_ms is None
    assert st.grace_in_ms is None


def test_余量不会是负数():
    """过期之后送 0,手机端不必自己夹 —— 读到 0 就是"这一刻已经没了"。"""
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    # 直接问一个远在到期之后的时刻:结算会把租约收掉,余量归 None。
    assert book.state(now_ms=T0 + LEASE_TTL_MS * 10).expires_in_ms is None
    # 而在结算之前的那一刻,余量必须是 0 而不是负数。
    assert _剩余毫秒(T0, T0 + 5_000) == 0
    assert _剩余毫秒(None, T0) is None


# --------------------------------------------- 每拍都变的键(修复轮 2 N1)


def test_两份快照不一样的键集合等于每拍都变的键():
    """给将来兜底的核心断言:谁再加一个相对量却忘了登记进
    ``每拍都变的键``,这条测试就会红。

    **必须同时有持有者和挑战者**,不然 ``expires_in_ms``/``grace_in_ms``
    都是 ``None``,两个时刻的 ``to_wire()`` 不会有任何差异,这条测试就白
    测了 —— 拿不到"变了的键"这个集合去跟 ``每拍都变的键`` 比。
    """
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    before = book.state(now_ms=T0 + 1_000).to_wire()
    after = book.state(now_ms=T0 + 1_500).to_wire()
    变了的键 = {k for k in before if before[k] != after[k]}
    assert 变了的键 == 每拍都变的键


def test_to_wire_stable在两个相隔的时刻完全相等():
    """``to_wire_stable()`` 是给常连的 SSE 用的 —— 去掉每拍都变的相对量
    之后,同一份租约在两个不同时刻的快照必须逐字段相等。
    """
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    before = book.state(now_ms=T0 + 1_000).to_wire_stable()
    after = book.state(now_ms=T0 + 1_500).to_wire_stable()
    assert before == after


def test_审计能整个发出去():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    assert book.audit[0].to_wire() == {
        "seq": 1, "at_ms": T0, "kind": "acquired",
        "ref": "aa11bb22", "operator": "张三", "detail": "",
    }


# ------------------------------------------------------ 切档留痕(§7.8 Task 7)


def test_留痕的种类是白名单(book):
    """``AUDIT_KINDS`` 不许只是个摆设。

    写一条没登记过的种类进去,值守屏和手机上的图标映射就会漏一个 —— 而漏的
    表现是界面上一条空白记录,不是报错。**在写入口拦住,比在读的地方兜底好**:
    读的地方有好几处,写只有这一处。
    """
    with pytest.raises(ValueError):
        book.note(at_ms=1, kind="随便编的", who=Holder("ab12"), detail="")


def test_模式切换是一种留痕():
    assert "mode_switched" in AUDIT_KINDS


def test_note写进同一个环(book):
    book.note(at_ms=1, kind="mode_switched", who=Holder("ab12", "张三"),
              detail="切到远程遥控")
    最后 = book.audit[-1]
    assert 最后.kind == "mode_switched"
    assert 最后.operator == "张三"
    assert 最后.ref == "ab12"
