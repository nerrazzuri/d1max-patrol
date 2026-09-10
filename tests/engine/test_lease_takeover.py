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


def test_挑战者和宽限终点永远同生共死():
    """``_challenger`` 和 ``_grace_ends`` **要么一起有、要么一起没**。

    **这条不服务于哪一次接管, 它服务于一条别处在用的推理。** app 那边
    ``control_panel.dart`` 的 ``_sceneOf`` 注释里有一整条链子, 论证"漏掉
    ``challenging`` 这一位也看不见"; 那条链子中间踩的正是这条不变量 ——
    带 challenger 的快照 ``grace_in_ms`` 必然不是 null, 于是那一侧一定走
    ``_buttons`` 的 ``someone`` 支、按钮一定是灰的。这条不变量今天靠的是
    "五个赋值点碰巧都写成对了", 没有任何东西拦着第六个赋值点只写一半。
    真被拆开了, 红的会是 app 上一颗本该灰掉的按钮 —— 隔着一个进程、一门
    语言, 那时候没人会回头怀疑到 ``lease.py`` 这两行。

    **内省的是真对象的两个字段, 不是源码文本。** 扫源码只能证明"文件里长
    得像成对", 换一种写法(比如挪进一个 ``_park(who, until)`` 小函数)就
    溜过去了; 而这里读的是一次真的走完之后 ``book`` 身上的状态, 谁写的、
    写在哪儿都不重要。

    **诚实交代它的边界:** 它只能守住这条走位**踩得到**的赋值点。新增一个
    只在别的路径上才碰得到的写入点, 得有人往这条走位里补一步, 它才看得见。
    """
    book = LeaseBook()
    甲, 乙, 丙 = "aa11bb22", "cc33dd44", "ee55ff66"
    走过: list[tuple[str, bool]] = []

    def 看一眼(那一步: str) -> None:
        # 直接摸字段。名字改了就 AttributeError, 那也是一种该红的红:
        # 这条不变量说的就是这两个名字。
        挑战者 = book._challenger
        宽限终点 = book._grace_ends
        走过.append((那一步, 挑战者 is not None))
        assert (挑战者 is None) == (宽限终点 is None), (
            f"「{那一步}」之后这一对被拆开了:_challenger={挑战者!r}、"
            f"_grace_ends={宽限终点!r}。有挑战者却没有宽限终点, "
            f"``_settle()`` 那道扶正的门槛(两个都非 None 才进)就永远不成立, "
            f"这个挑战者会一直挂在那儿等不到头; 反过来则是一个没有主人的倒计时, "
            f"快照里的 grace_in_ms 会凭空冒出来。"
        )
        # **同一条 Dart 链子踩的另一块砖:有挑战者 ⇒ 一定有持有者。**
        # ``ask_takeover()`` 在没人持有时是直接把控制权给他、根本不排队, 所以
        # "有 challenger 却没 holder"这种快照不该存在。app 那边靠它排除
        # ``_buttons`` 的兜底支(那一支的灰只看 full, 不看 grace); 没有这一句,
        # 它跟"五个赋值点成对"一样, 又是一条 Dart 注释在替 Python 记账。
        assert 挑战者 is None or book._holder is not None, (
            f"「{那一步}」之后出现了没有持有者的挑战者:_challenger={挑战者!r}、"
            f"_holder=None。app 那边会把这份快照判进「没人拿着」那一支, "
            f"「取得控制权」按钮照样按得动, 而狗这头其实正在走一轮接管。"
        )

    钟 = T0
    看一眼("刚建出来")

    book.acquire(甲, "张三", now_ms=钟)
    看一眼("甲拿到租约")
    book.ask_takeover(乙, "李四", now_ms=钟)
    看一眼("乙请求接管")
    book.renew(甲, now_ms=钟)
    看一眼("甲续租")
    book.keep_only([甲], now_ms=钟)
    看一眼("乙的会话没了")

    book.ask_takeover(乙, "李四", now_ms=钟)
    看一眼("乙又来请求接管")
    book.approve(甲, now_ms=钟)
    看一眼("甲同意移交")
    book.ask_takeover(丙, "王五", now_ms=钟)
    看一眼("丙请求接管")
    book.release(乙, now_ms=钟)
    看一眼("乙交还")

    book.acquire(甲, "张三", now_ms=钟)
    book.ask_takeover(乙, "李四", now_ms=钟)
    看一眼("乙在排队等宽限期")
    钟 += TAKEOVER_GRACE_MS
    book.state(now_ms=钟)
    看一眼("宽限期走完自动移交")
    # **钉住"扶正真的发生了"。** ``_settle()`` 那道扶正整个失效的话, 这一步之后
    # 挑战者还在、宽限终点也还在、持有者还是甲 —— 成对性成立,
    # 「有挑战者 ⇒ 有持有者」也成立, 上面两句一句都不红。少了这一句,
    # ``control_panel.dart`` 那条链子里「宽限期走完会自动扶正」那一环
    # 就没有任何东西守着, 而它正是那条链子说得最响的一环。
    assert book._holder == Holder(乙, "李四") and book._challenger is None, (
        f"宽限期走完了, 挑战者却没被扶正:_holder={book._holder!r}、"
        f"_challenger={book._challenger!r}。``_settle()`` 那道"
        f"「两个都非 None 且到点」的门槛没走通, 乙会一直挂在队里等不到头。")

    book.ask_takeover(丙, "王五", now_ms=钟)
    看一眼("丙请求接管")
    book.force(甲, "张三", now_ms=钟, reason="现场要人挪设备")
    看一眼("甲强制接管")

    book.ask_takeover(乙, "李四", now_ms=钟)
    看一眼("乙请求接管")
    book.keep_only([], now_ms=钟)
    看一眼("两边的会话一起没了")
    # **钉住下一步的前提。** 下一步的全部意义是"没人持有时 ask_takeover 是直接
    # 给、不排队", 而"这时候确实没人持有"这件事本身没有任何东西守着 ——
    # ``走过`` 那本流水只记挑战者在不在, 不记持有者。哪天 ``keep_only([])`` 的
    # 语义一变(比如改成留下最后一个持有人), 下一步就悄悄不再检验那件事,
    # 而下面两条正向锚点一条都不会红。
    assert book._holder is None, (
        f"``keep_only([])`` 之后还有人持有:_holder={book._holder!r}。"
        f"下一步「没人持有时请求接管」就不再走 ``ask_takeover`` 里"
        f"「没人持有就直接给」那一支了, 上面那句"
        f"「有挑战者 ⇒ 有持有者」当场失去它唯一咬得动的那种改法。")

    # 没人持有时请求接管 —— 狗这头是**直接给**, 不排队。上面那句"有挑战者就
    # 一定有持有者"只有走到这一步才咬得住人, 不走到它就是一句恒真。
    book.ask_takeover(丙, "王五", now_ms=钟)
    看一眼("没人持有时请求接管")
    book.release(丙, now_ms=钟)
    看一眼("丙交还")

    book.acquire(甲, "张三", now_ms=钟)
    book.ask_takeover(乙, "李四", now_ms=钟)
    看一眼("乙在排队等甲的租约到期")
    钟 += LEASE_TTL_MS
    book.state(now_ms=钟)
    看一眼("甲的租约到期, 排队的乙被扶正")

    # **正向锚点。** 上面那句 assert 只有在"确实有过挑战者"的那些步上才咬得
    # 住人:一路 None 到底的话它句句成立, 什么也没证明。这两条钉住这趟走位
    # 真的两种局面都走到了 —— 哪天有人把 ask_takeover 改成静默失败, 先红的
    # 是这里, 而不是让上面那句悄悄退化成恒真。
    assert [步 for 步, 有 in 走过 if 有], (
        "整趟走下来一次挑战者都没排上队 —— 上面那句成对检查全程在空集上恒真, "
        f"没有守住任何东西。走过的是:{走过}")
    # 第二条要的是**一次真的清空**, 所以查的是相邻两步之间的「真 → 假」转变,
    # 不是"走过里有一步没有挑战者"。后者是**恒真的**:第一步「刚建出来」看的
    # 是一个刚 ``LeaseBook()`` 出来的对象, ``_challenger`` 必然是 None, 于是
    # 那种写法在任何实现下都成立 —— 把下面所有清空赋值点全删光它照样绿,
    # 它证明的只是"测试自己在动手之前看过一眼"。
    # ``strict=False`` 是有意的:两条流水本来就差一格(错开一位配对),
    # ``strict=True`` 在这儿必抛。
    相邻 = zip(走过, 走过[1:], strict=False)
    清掉过 = [后 for 前, 后 in 相邻 if 前[1] and not 后[1]]
    assert 清掉过, (
        "整趟走下来一次都没有从「有挑战者」变回「没挑战者」 —— 清空那一半的"
        "赋值点根本没走到, 上面那句成对检查只在「一直有挑战者」这半边转。"
        f"走过的是:{走过}")
