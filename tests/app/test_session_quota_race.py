"""必修 5:会话名额的判定必须是原子的。

服务是 ``ThreadingHTTPServer`` —— 一请求一线程。原来 ``Guard._issue`` 是
**三步**:``tokens.sessions()`` 读一份名单(锁里)、``len(pool) >= cap`` 判一
下(锁**外**)、``issue()`` 写一条(另一把锁里)。两个人几乎同时输 PIN 就是
两条真线程,两边都在锁外读到"现在 2 个、上限 3",于是两边都签发,落地 4 个。

§3.6 那个上限不是个礼貌数字:CPE 的无线上行是共享的,``video.MAX_VIEWERS = 6``
那个推导、值守屏上"几分之几"的分母,全挂在它上面。现场执行单 9.7 那条
「第 4 个人被拒」验的就是它 —— 不原子的话那条会飘。

**这个文件不满足于"跑两遍没超发"。** 先用一个把竞态窗口撑开的夹具证明这条
路径上真的有窗口(``test_旧的三步写法在这个夹具下必然超发``),再拿同一个夹具
去验新写法(``test_并发解锁一个名额都不许多发``)。少了前一条,后一条的绿
说不清是"修好了"还是"这条测试根本没造出并发"。
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from d1max_patrol.app.auth import (
    CHANNEL_LOCAL,
    MAX_LOCAL_SESSIONS,
    MAX_SESSIONS,
    Denied,
    Guard,
    Session,
    TokenStore,
)
from d1max_patrol.app.server import AppServer

from .conftest import make_ctx, request

PIN = "428913"
#: 单调钟上的一个固定时刻。名额判定全程用同一个 ``now``:这个文件要暴露的是
#: 线程交错, 不是闲置期 —— 让钟也跟着动就分不清红的是哪一个。
T0 = 10_000.0


class 撑开窗口的TokenStore(TokenStore):
    """一个把"读名额"那一步拖住的 ``TokenStore``。

    ``sessions()`` 一被调到就在栅栏上等齐所有人再返回。对**在锁外判名额**的
    写法来说, 这等于把"大家读到的是同一份旧名单"从"偶尔撞上的几微秒"变成
    "一定发生" —— 竞态于是从概率问题变成确定性问题, 测试也就不再是抽奖。

    对新写法来说这个夹具是**哑的**:整条签发路径一次都不碰 ``sessions()``,
    栅栏一次都不会被撞上。这本身就是一条断言(见 ``撞过几次``)—— 判名额那
    一步回到锁外去的话, 它会立刻变成非零。
    """

    def __init__(self, 人数: int, **kw) -> None:
        super().__init__(**kw)
        self._栅栏 = threading.Barrier(人数, timeout=5.0)
        #: 用 list.append 而不是 ``+= 1``:后者在多线程里自己就是个竞态。
        self._撞到的: list[int] = []

    def sessions(self, *, now: float | None = None) -> tuple[Session, ...]:
        # **栅栏摆在读完之后, 不是读之前。** 摆在前面只同步了"进门"这一下,
        # 出门时大家还是在 TokenStore 自己那把锁上排队, 先出门的那个已经把
        # token 发掉了 —— 后面的人读到的就不是旧名单了, 竞态又被磨平。
        # 摆在后面才是"所有人手上都攥着同一份旧名单"那个局面。
        got = super().sessions(now=now)
        self._撞到的.append(1)
        with contextlib.suppress(threading.BrokenBarrierError):
            self._栅栏.wait()
        return got

    @property
    def 撞过几次(self) -> int:
        return len(self._撞到的)

    def 名单(self) -> tuple[Session, ...]:
        """不经过栅栏地读一份名单 —— 给断言用, 不给被测代码用。"""
        return TokenStore.sessions(self, now=T0)


def _并发跑(做事, 人数: int) -> tuple[list, list]:
    """``人数`` 条真线程同时做一件事, 回 (成功的, 被拒的)。

    **一起放闸**:线程起好之后统一 ``set()`` 一个事件, 免得第一条线程在最后
    一条还没起来的时候就把活干完了 —— 那样跑出来的是串行, 不是并发。
    """
    起跑 = threading.Event()
    成功: list = []
    被拒: list = []
    锁 = threading.Lock()

    def 一个人(i: int) -> None:
        起跑.wait(5.0)
        try:
            got = 做事(i)
        except Denied as exc:
            with 锁:
                被拒.append(exc)
        else:
            with 锁:
                成功.append(got)

    线程 = [threading.Thread(target=一个人, args=(i,), daemon=True)
            for i in range(人数)]
    for t in 线程:
        t.start()
    起跑.set()
    for t in 线程:
        t.join(timeout=15.0)
    assert not any(t.is_alive() for t in 线程), "有线程卡住了 —— 多半是死锁"
    return 成功, 被拒


def _做一个guard(人数: int) -> Guard:
    store = 撑开窗口的TokenStore(人数)
    return Guard(PIN, tokens=store, clock=lambda: T0)


# ------------------------------------------------- 先证明这个夹具真的能逼出竞态


def test_旧的三步写法在这个夹具下必然超发():
    """**这一条测的不是产品代码, 是这个文件本身。**

    下面那段 ``旧写法`` 是第一轮之前 ``Guard._issue`` 的原样三步(读名单、
    锁外判、再签发), 只在这个文件里存在。它在这个夹具下必然超发 —— 这就
    证明了这条路径上真的有一个竞态窗口, 而不是"并发跑一跑碰巧没事"。

    哪天有人把判名额挪回锁外, 下面那条会红; 而这一条仍然绿, 两条合起来才
    说得清"红的是产品代码, 不是测试"。
    """
    人数 = MAX_SESSIONS + 3
    guard = _做一个guard(人数)

    def 旧写法(i: int) -> str:
        # 第一步:读名单(在 TokenStore 自己的锁里)。
        pool = [s for s in guard.tokens.sessions(now=T0)
                if s.channel != CHANNEL_LOCAL]
        # 第二步:判名额。**在锁外。** 这就是那个窗口。
        if len(pool) >= guard.max_sessions:
            raise Denied(409, "满了", "")
        # 第三步:签发(又一把锁)。
        return guard.tokens.issue(now=T0, operator=f"老{i}")

    成功, _被拒 = _并发跑(旧写法, 人数)
    assert guard.tokens.撞过几次 == 人数, "栅栏没等齐, 这个夹具就没起作用"
    assert len(成功) > MAX_SESSIONS, (
        "旧写法在这个夹具下居然没超发 —— 那说明这条测试造不出并发, "
        "下面那条的绿也就不作数了")
    assert len(guard.tokens.名单()) > MAX_SESSIONS


# --------------------------------------------------------- 新写法在同一个夹具下


def test_并发解锁一个名额都不许多发():
    """必修 5 的正主。同一个夹具, 换成真的 ``Guard.unlock``。

    ``cap + 3`` 个人同时输对 PIN、各报各的名字、各从一个不同的局域网 IP 来。
    落地必须**正好** ``MAX_SESSIONS`` 个会话, 多出来的那几个拿 409。
    """
    人数 = MAX_SESSIONS + 3
    guard = _做一个guard(人数)
    成功, 被拒 = _并发跑(
        lambda i: guard.unlock(PIN, f"192.168.8.{20 + i}",
                               operator=f"操作员{i}", now=T0),
        人数)
    assert len(成功) == MAX_SESSIONS, [len(成功), len(被拒)]
    assert len(被拒) == 人数 - MAX_SESSIONS
    assert all(e.status == 409 for e in 被拒), [e.status for e in 被拒]
    活着 = [s for s in guard.tokens.名单() if s.channel != CHANNEL_LOCAL]
    assert len(活着) == MAX_SESSIONS
    # token 各不相同 —— "正好 3 个"不许是靠发了同一个 token 三次凑出来的。
    assert len(set(成功)) == MAX_SESSIONS


def test_签发路径一次都不碰sessions():
    """上面那条绿的**原因**:判名额这一步回到了锁里面。

    这条是那句话的机械化版本。``sessions()`` 是原来那个锁外判定的入口,
    新写法整条路径不碰它 —— 谁要是把它挪回去, 这条当场红, 而且红得比
    "并发跑出来的数不对"好查得多(并发的红是概率性的)。
    """
    人数 = MAX_SESSIONS + 3
    guard = _做一个guard(人数)
    _并发跑(lambda i: guard.unlock(PIN, f"192.168.8.{20 + i}",
                                   operator=f"操作员{i}", now=T0), 人数)
    assert guard.tokens.撞过几次 == 0


def test_并发的同名换座只换掉一个座位():
    """名额满了之后**同名换座**那条路也得是原子的。

    局面:名额先占满, 其中一个座位上坐的就是"老王"; 然后 ``cap + 3`` 条线程
    同时用"老王"这个名字来换座。换座是"把自己的老会话删掉再发一个新的",
    不原子的话每条线程都会读到"有一个同名的可以挤", 于是各挤各的、各发各的
    —— 一个名字占掉一堆座位, 上限一样破了。
    """
    人数 = MAX_SESSIONS + 3
    guard = _做一个guard(人数)
    for i in range(MAX_SESSIONS - 1):
        guard.unlock(PIN, f"192.168.8.{60 + i}", operator=f"先来的{i}", now=T0)
    guard.unlock(PIN, "192.168.8.79", operator="老王", now=T0)
    assert len(guard.tokens.名单()) == MAX_SESSIONS

    成功, 被拒 = _并发跑(
        lambda i: guard.unlock(PIN, f"192.168.8.{80 + i}",
                               operator="老王", now=T0),
        人数)
    活着 = [s for s in guard.tokens.名单() if s.channel != CHANNEL_LOCAL]
    assert len(活着) == MAX_SESSIONS, [s.operator for s in 活着]
    # 每条线程挤掉的都是"上一个老王", 无论怎么交错, 叫"老王"的座位**只有一
    # 个**; 别人的座位一个都不许被碰。
    assert sum(1 for s in 活着 if s.operator == "老王") == 1
    assert sorted(s.operator for s in 活着) == ["先来的0", "先来的1", "老王"]
    assert len(成功) + len(被拒) == 人数


def test_本机那一池也是原子的():
    """本机(回环)有自己的一套上限 ``MAX_LOCAL_SESSIONS``, 走的是同一段代码。

    钉它一条是因为 ``local`` 这个参数是新加的:判据从"调用方自己过滤名单"
    变成了"传一个布尔进去", 传反了的话两个池子会互相吃名额, 而那种错在单
    线程的测试里看不出来。
    """
    人数 = MAX_LOCAL_SESSIONS + 3
    guard = _做一个guard(人数)
    成功, 被拒 = _并发跑(
        lambda i: guard.unlock(PIN, "127.0.0.1", operator=f"本机{i}", now=T0),
        人数)
    assert len(成功) == MAX_LOCAL_SESSIONS, [len(成功), len(被拒)]
    活着 = guard.tokens.名单()
    assert len(活着) == MAX_LOCAL_SESSIONS
    assert all(s.channel == CHANNEL_LOCAL for s in 活着)


# ------------------------------------------------------------- 走真 HTTP 再验一遍


@pytest.fixture
def 有pin的服务(bridge, tmp_path):
    ctx = make_ctx(bridge, tmp_path)
    s = AppServer(ctx, port=0, pin=PIN)
    s.start()
    yield s
    s.stop()


def test_真并发打解锁接口也不超发(有pin的服务):
    """上面几条是在 ``Guard`` 那一层验的, 这条走真 HTTP、真线程。

    这里没有任何插桩, 所以它未必每次都撞上那个窗口 —— 它防的是另一件事:
    ``Guard`` 那一层改对了, 而 ``server.py`` 把名额判定又抄了一份到外面。
    从回环打进来的都算本机, 上限是 ``MAX_LOCAL_SESSIONS``。
    """
    人数 = MAX_LOCAL_SESSIONS + 4

    def 打一发(i: int) -> int:
        code, _body, _h = request(有pin的服务, "/api/auth", method="POST",
                                  payload={"pin": PIN, "operator": f"甲{i}"},
                                  timeout=15.0)
        return code

    码, _ = _并发跑(打一发, 人数)
    assert sorted(码) == [200] * MAX_LOCAL_SESSIONS + [409] * (
        人数 - MAX_LOCAL_SESSIONS), sorted(码)
    活着 = 有pin的服务._auth.sessions()
    assert len(活着) == MAX_LOCAL_SESSIONS, [s.operator for s in 活着]
