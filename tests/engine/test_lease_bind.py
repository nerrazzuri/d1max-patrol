"""§6.4:token 一失效,租约立即释放。反过来不成立。"""

from __future__ import annotations

from d1max_patrol.engine.lease import Holder, LeaseBook

T0 = 1_757_000_000_000


def test_持有者的token还在就不动():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.keep_only({"aa11bb22", "cc33dd44"}, now_ms=T0 + 1_000)
    assert st.holder == Holder("aa11bb22", "张三")


def test_持有者的token没了租约跟着释放():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.keep_only({"cc33dd44"}, now_ms=T0 + 1_000)
    assert st.holder is None
    assert book.audit[-1].kind == "dropped"
    assert book.audit[-1].ref == "aa11bb22"


def test_一个token都不剩就没人持有():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    assert book.keep_only([], now_ms=T0 + 1_000).holder is None


def test_没人持有时收租什么也不记():
    book = LeaseBook()
    book.keep_only([], now_ms=T0)
    assert book.audit == ()


def test_挑战者的token没了就把请求撤掉():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.keep_only({"aa11bb22"}, now_ms=T0 + 2_000)
    assert st.holder == Holder("aa11bb22", "张三")
    assert st.challenger is None
    assert st.grace_ends_ms is None
    assert book.audit[-1].kind == "dropped"
    assert book.audit[-1].ref == "cc33dd44"


def test_持有者的token没了而挑战者还在就直接移交():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.keep_only({"cc33dd44"}, now_ms=T0 + 2_000)
    assert st.holder == Holder("cc33dd44", "李四")
    assert [r.kind for r in book.audit][-2:] == ["dropped", "taken_over"]


def test_两个token都没了就谁也不给():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    book.ask_takeover("cc33dd44", "李四", now_ms=T0 + 1_000)
    st = book.keep_only([], now_ms=T0 + 2_000)
    assert st.holder is None
    assert st.challenger is None


def test_收租时顺手把过期也结算掉():
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.keep_only({"aa11bb22"}, now_ms=T0 + 30_000)
    assert st.holder is None
    assert book.audit[-1].kind == "expired"


def test_没租约的token不受影响():
    book = LeaseBook()
    st = book.keep_only({"aa11bb22", "cc33dd44", "ee55ff66"}, now_ms=T0)
    assert st.holder is None
    assert book.audit == ()


def test_钟往回跳时租约不会凭空续命():
    """墙上钟被 NTP 往回拨了。租约到期只看"到没到那个时刻",所以往回跳会让
    一个本该到期的租约多活一会儿 —— 这是已知代价,§3.3 的 clock_skew 会把钟
    不对这件事单独报出来。这里钉死的是**不会更糟**:不炸、不负数、状态自洽。
    """
    book = LeaseBook()
    book.acquire("aa11bb22", "张三", now_ms=T0)
    st = book.state(now_ms=T0 - 3_600_000)
    assert st.holder == Holder("aa11bb22", "张三")
    assert st.expires_ms == T0 + 30_000
    st = book.state(now_ms=T0 + 30_000)
    assert st.holder is None


def test_导出的名字都在():
    from d1max_patrol.engine import lease

    for name in ("LeaseBook", "LeaseState", "Holder", "AuditRecord",
                 "LeaseError", "LeaseBusy", "LeaseLost",
                 "LEASE_TTL_MS", "LEASE_HEARTBEAT_MS",
                 "TAKEOVER_GRACE_MS", "AUDIT_MAX", "AUDIT_KINDS"):
        assert name in lease.__all__
        assert hasattr(lease, name)
