"""挂起没人管要报警(12a),以及从路由挂起再从路由继续(12c)。

12a 原来报的是告警簿上的 P1(``suspend_stale``);狗上不再记告警(决策 8),
这条通知随 W00c5c 在站点重建,狗上只剩 ``server`` 那个 logger 上的一行
ERROR。这一组断的就是那一行 —— 判据(何时报、报几次、读哪口钟)一个字没变。

**这一组起的是真服务、打真 HTTP、等的是真后台协程。** 12a 要盯的那件事
(人接管完忘了还回来,这趟就永远挂着)只会发生在没人操作的时候 —— 判定它
的是 ``_StateHub`` 那条闸门协程,直接调处理函数一个字都测不到。

12c 同理:引擎层的 suspend/resume 第 7 卷就做全了,缺的一直是**路由层**
那一条链 —— 而人按的是路由层那个按钮。
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from urllib.parse import quote

import pytest

from d1max_agent.engine.machine import RunSnapshot, RunState, SuspendPoint
from d1max_patrol.app.server import (
    _LEASE_WATCH_PERIOD_S,
    SUSPEND_STALE_MS,
    AppContext,
    AppServer,
    _wall_ms,
    _挂起超时了,
)
from d1max_patrol.app.teleop import HEARTBEAT_TIMEOUT_S, Teleop
from tests.app.conftest import make_ctx, request
from tests.app.test_run_suspend import _mission

走一拍 = {"fwd": 0.2, "lat": 0.0, "yaw": 0.0}

#: 遥控守死人开关那条协程的巡查周期。调小只是为了别让这一组等得太久。
TICK = 0.02

#: 注给 ``Teleop`` 的守死人超时。**故意不等于 ``HEARTBEAT_TIMEOUT_S``** ——
#: 这个差值就是 12c 那条端到端唯一的判别力所在:参数被忽略、仍去读模块常量
#: 的实现,会在 ``超时秒 / 2`` 那一步就把手松开,而那一步断的正是「还没到点,
#: 手必须还在」。两个数要是相等,那一刀砍下去测试照样绿。
超时秒 = 2.0

#: 「再守几拍」的那几条断言等的是多少拍。
#:
#: **抽出来是因为它有两份拷贝在各写各的**:下面 ``观察窗口`` 的论证用的是这
#: 个数,而三处 ``等到(lambda: srv.hub._lease_ticks >= 起始拍 + …)`` 也用的
#: 是这个数。有人把它改成 5 拍,窗口和注释不会跟着走 —— 那不是红一条,是
#: 窗口变得比要等的拍数还短,几条断言一起开始 flake。
#:
#: ``test_lease_expiry.py`` 里有同名的第二份(那边的 ``等到`` 跟这边的
#: 互称同一份东西)。**两边各自改各自的** —— 两个文件等的不是同一件事,
#: 这里只保证写法一致,不保证数值一致。
拍数 = 3

#: 「等闸门醒几拍」的观察窗口。**按 ``_LEASE_WATCH_PERIOD_S`` 的倍数写,一个
#: 写死的秒数都不许有。**
#:
#: 这一组原来写的是 ``最多等=0.5``,正好等于闸门周期本身 —— 窗口跟被观察者
#: 同长还踩着边界,一个"下一拍才真放行"的实现能整个溜过去。改成写死的 ``3.0``
#: 只是把病换了个数字:哪天有人把巡查周期调到 1.0 秒,3 拍就要 3.0 秒,这几条
#: 当场开始 flake,而且是「第一次红被当成 flake 去调窗口」那一类。
#:
#: **``* 2`` 是「等 :data:`拍数` 拍」的两倍余量**,不是一个可以随手拧的旋钮:
#: 闸门那条协程跑在真线程上,拍与拍之间的间隔受机器负载和调度抖动影响,窗口
#: 只给一倍就等于把「刚好赶上」当成合格线。倍数和拍数在这儿各写一次,
#: 常量一改所有窗口跟着走。
观察窗口 = _LEASE_WATCH_PERIOD_S * 拍数 * 2


class 跟着墙走的钟:
    """app 那口墙钟(``ctx.clock``,毫秒):真墙上时钟 + 一个能拨的偏移量。

    **这口钟不再参与 ``suspend_stale`` 的判据。** 那条 P1 的三个数现在全从
    引擎那口钟推(见 :class:`跟着表走的秒表` 和 ``server._挂起超时了``),
    这口钟只剩下租约判 TTL 这类用途。拨它**只该**拨出「墙钟跳了」这一幕 ——
    见 ``test_墙钟往前跳不许因此报这条P1``。

    **仍然不许换成别处那种冻住的 ``T0``。** 这一组起的是真服务,租约 TTL
    按这口钟算;带偏移的真钟保住「现在就是现在」,``前进`` 拨的是「从现在起
    又过了多久」。
    """

    def __init__(self) -> None:
        self.偏移 = 0

    def __call__(self) -> int:
        return int(time.time() * 1000) + self.偏移

    def 前进(self, ms: int) -> None:
        self.偏移 += ms


class 跟着表走的秒表:
    """引擎那口**单调钟**(秒):真 ``time.monotonic()`` + 一个能拨的偏移。

    **``suspend_stale`` 那条 P1 的三个数全从这口钟推。** ``SuspendPoint``
    的 ``at_ms``(开跑对表的锚点 + 这口钟走过的量)、``prior_suspend_ms``
    (这口钟量出来的累计)、``MissionEngine.now_ms()``(同一个锚点 + 这口钟)
    —— 见 ``engine/machine.py`` 的 ``_stamp_ms``。所以这一组要让「挂起了十
    分钟」发生,拨的是**这一个**,不是上面那口墙钟。

    **为什么是「真 monotonic + 偏移」而不是一个冻住的计数器。** 引擎拿这口
    钟做的不止时刻:到点超时、导航回落等待、遥控那一档的节拍都读它。冻住的
    话引擎在任何一处 ``deadline - self._clock()`` 上都永远等不到头,而这一组
    起的是**真引擎 + 真协程**。偏移量 0 的时候行为跟缺省的
    ``time.monotonic`` 一模一样,拨过之后才开始"过了多久"。

    **拨它的时候引擎得在 ``SUSPENDED`` 上。** 那条路径 ``await self._next(None)``
    没有超时(``engine/machine.py`` 的 ``_suspend``),拨多远都只影响时刻和
    累计量;在 ``RUNNING`` 上拨十分钟则会真的撞上点位的 ``waypoint_timeout_s``
    —— 那是引擎的正当行为,但会把这一组要测的那件事冲掉。
    """

    def __init__(self) -> None:
        self.偏移 = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.偏移

    def 前进(self, ms: int) -> None:
        """按**毫秒**拨,跟 ``SUSPEND_STALE_MS`` 一个单位,省得每处都换算。"""
        self.偏移 += ms / 1000.0


class 假秒表:
    """遥控那一头的单调钟(秒)。跟上面那个是两根不同的轴,别混。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def 前进(self, s: float) -> None:
        self.t += s


def 等到(条件, *, 最多等: float = 观察窗口) -> bool:
    """轮到条件成立为止,**有超时上界**。

    等的是真的后台协程(跑在桥那根线程上),没有可注入的钟能拨快它 ——
    §8.5 第 2 条管的是被测代码里的时间,不管这种轮询。不写死
    ``sleep(周期 + 余量)``:那是拿机器负载赌,会产生没人复现得出来的假红。
    跟 ``test_lease_expiry.py`` 里那个是同一份东西,同一个理由。
    """
    截止 = time.monotonic() + 最多等
    while True:
        if 条件():
            return True
        if time.monotonic() >= 截止:
            return False
        time.sleep(0.01)


@pytest.fixture
def 钟() -> 跟着墙走的钟:
    return 跟着墙走的钟()


@pytest.fixture
def 引擎秒表() -> 跟着表走的秒表:
    return 跟着表走的秒表()


@pytest.fixture
def 秒表() -> 假秒表:
    return 假秒表()


@pytest.fixture
def 服务(bridge, tmp_path, 钟, 秒表, 引擎秒表):
    ctx = make_ctx(bridge, tmp_path, clock=钟, engine_clock=引擎秒表)
    # 换掉 ``make_ctx`` 那份默认遥控:这一组要能拨守死人开关的钟,还要一个
    # 跟模块常量不一样的超时(见 ``超时秒``)。服务是在这之后才建的,而每次
    # 请求都是现读 ``ctx.teleop``(见 ``server._teleop_pulse``),换在这一刻
    # 是干净的。
    ctx.teleop = Teleop(ctx.device, ctx.engine, video_gate=lambda: "",
                        clock=秒表, watch_period_s=TICK,
                        heartbeat_timeout_s=超时秒)
    srv = AppServer(ctx, port=0)
    srv.start()
    yield srv
    srv.stop()
    # 这一组真把任务起起来了。不收掉的话引擎那条协程会活到桥停为止,
    # 而它中途还往归档目录里写 —— 写进一个 pytest 正在删的临时目录。
    with contextlib.suppress(Exception):
        bridge.call(ctx.engine.aclose, timeout_s=10.0)


@pytest.fixture
def 没起的服务(bridge, tmp_path, 钟):
    """只建不起:没有任何后台协程。

    ``test_算的是让开腿那一刻不是开跑那一刻`` 要临时把 ``ctx.engine`` 换成
    一个摆好的假引擎。在起着的服务上换,闸门协程随时可能在换掉的那半拍里
    读到它 —— 那条测试会时红时绿,而且红得跟它要测的事情没关系。
    """
    ctx = make_ctx(bridge, tmp_path, clock=钟)
    return AppServer(ctx, port=0)


def _post(srv, path: str, payload=None):
    code, body, _ = request(srv, path, method="POST", payload=payload or {})
    return code, json.loads(body)


@pytest.fixture
def 跑起来的服务(服务):
    """存一份任务、起飞、等到真在跑。

    任务定义从 ``test_run_suspend`` 里**导过来,不手抄一份** —— 两份「最小
    任务」迟早会分叉,而分叉那天没有人看得出来。
    """
    srv = 服务
    ctx = srv.ctx
    路径 = "/api/missions/" + quote("巡检一号")
    code, body, _ = request(srv, 路径, method="PUT", payload=_mission())
    assert code == 200, body
    assert _post(srv, 路径 + "/run")[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.RUNNING))
    return srv


@pytest.fixture(autouse=True)
def _收日志(caplog):
    """原来的 P1 现在是 ``server`` 那个 logger 上的 ERROR,这一组靠它断言。"""
    caplog.set_level(logging.WARNING, logger="d1max_patrol.app.server")


def 挂起超时记录(caplog) -> list[logging.LogRecord]:
    """日志里的 ``[suspend_stale]``(格式见 ``server._判定挂起超时``:``args``
    就是 ``(标题, 正文)``)。**按 kind 挑** —— 没盖点那句 ERROR 是另一件事。
    """
    return [r for r in caplog.records
            if r.name == "d1max_patrol.app.server"
            and r.getMessage().startswith("[suspend_stale]")]


def 让开腿(srv, reason: str = "人要接管") -> None:
    ctx = srv.ctx
    assert _post(srv, "/api/run/suspend", {"reason": reason})[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))


# ----------------------------------------------------- 12a 挂起太久没人管


def test_挂起太久没人管记一条ERROR(跑起来的服务, 引擎秒表, caplog):
    """挂账 67a:第 7 卷判过 ``SUSPENDED`` 期间无超时无强制放行,这条就是来
    盯这个的。人接管完忘了还回来,这趟就永远挂着,而屏幕上一切如常。

    **拨的是引擎那口钟**(:class:`跟着表走的秒表`),不是 ``ctx.clock``:
    这条 P1 的三个数全从引擎那口钟推。拨墙钟**不该**让它报,那一格由
    ``test_墙钟往前跳不许因此报这条P1`` 单独钉。
    """
    srv = 跑起来的服务
    让开腿(srv)
    assert not 挂起超时记录(caplog), \
        "钟还没拨就报了 —— 下面那句「到点了才报」什么也证明不了"

    引擎秒表.前进(SUSPEND_STALE_MS + 1)
    assert 等到(lambda: 挂起超时记录(caplog)), "挂起超过十分钟,一行 ERROR 都没有"
    r = 挂起超时记录(caplog)[0]
    assert r.levelno == logging.ERROR, r
    # 标题/正文里要说得出这是「人接管着没还回来」,不是一句「出错了」。
    assert "接管" in r.getMessage(), r.getMessage()
    # 处置没变:狗还挂在那儿,没被谁 resume / abort。
    assert srv.ctx.engine.state is RunState.SUSPENDED


def test_挂起超时只报警不自己动狗(跑起来的服务, 引擎秒表, caplog):
    """人还在现场,狗自己动起来是这套系统里最不该发生的事(§5.8 同理)。

    所以超时的处置是**记一笔**(原来是升 P1),不是自动 ``resume``、不是自动
    ``abort``。
    """
    srv = 跑起来的服务
    ctx = srv.ctx
    让开腿(srv)
    走过 = len(ctx.device.walk_calls)
    导过 = len(ctx.nav.goto_calls)

    引擎秒表.前进(SUSPEND_STALE_MS * 5)
    assert 等到(lambda: 挂起超时记录(caplog)), "先得真报出来,不然下面全是空转"
    # 报完之后要真的多给它几拍:自动 resume 是异步的,报警那一刻还没轮到它。
    #
    # **等的是闸门醒了几拍,不是墙上的半秒。** 原来写的是
    # ``最多等=0.5``,而 ``_LEASE_WATCH_PERIOD_S`` 正好也是 0.5 —— 观察窗口
    # 跟被观察者的周期一样长,还踩着边界:上一句 ``等到(挂起超时记录)`` 是在
    # 报警那一刹那返回的,这 0.5 秒最多覆盖到"下一拍"。有人把自动 resume 写
    # 成"先报警、下一拍再放行",或者写成一条投进引擎命令队列、要等引擎协程
    # 下一轮才处理的异步命令,实际放行落在报警后 0.5~1.0 秒 —— 这条断言有
    # 相当概率仍然绿,而且是**只在慢机器上偶尔红**的那一种。比恒绿更糟:
    # 第一次红会被当成 flake 去调窗口。跟 ``test_一次挂起只报一条`` 统一。
    # 窗口本身按周期的倍数写,不写死秒数 —— 见 :data:`观察窗口`。
    起始拍 = srv.hub._lease_ticks
    assert 等到(lambda: srv.hub._lease_ticks >= 起始拍 + 拍数, 最多等=观察窗口), \
        "闸门没再醒过,下面那句「引擎没自己动」是因为它根本没机会动"
    assert ctx.engine.state is RunState.SUSPENDED, "超时之后引擎自己离开了 SUSPENDED"
    # ``goto`` 才是「狗真的动了」在这台假后端上的样子:自动 resume 会重发当前
    # 点。``walk`` 一起看着,那是遥控档上的直接位移。
    assert ctx.nav.goto_calls[导过:] == [], ctx.nav.goto_calls[导过:]
    assert ctx.device.walk_calls[走过:] == [], ctx.device.walk_calls[走过:]


def test_没到点不报(跑起来的服务, 引擎秒表, caplog):
    """差一分钟就报,等于把十分钟这个数写进了注释而没写进代码。

    拨的是引擎那口钟 —— 拨墙钟的话,这条在**判据换钟之后**永远是绿的
    (墙钟压根不参与判据了),它守的那件事就没了。
    """
    srv = 跑起来的服务
    让开腿(srv)
    引擎秒表.前进(SUSPEND_STALE_MS - 60_000)
    assert not 等到(lambda: 挂起超时记录(caplog), 最多等=观察窗口), "还差一分钟就报了"


def test_墙钟往前跳不许因此报这条P1(跑起来的服务, 钟, 引擎秒表, caplog):
    """**现场没有 NTP,狗上墙钟随时会被校一下 —— 校完不许挨一条 P1。**

    这一条是"判据换钟"这件事的回归守卫,摆的是换钟之后**才会出现**的那一格:
    ``SuspendPoint.at_ms`` 已经是"开跑那一刻的墙钟锚点 + 单调钟走过的量",
    它不跟着墙钟跳了;这时候判据那一边要是还在现问 ``ctx.clock()``,墙钟往前
    跳一小时,差值当场多出一小时 —— **人刚让开腿、手还在狗身上,就挨一条
    「人接管着没还回来」的 P1**。误报会很快教会人无视这条告警,然后真出事那
    一次也一起被无视掉。

    把 ``server._判定挂起超时`` 里 ``now_ms=engine.now_ms()`` 换成
    ``now_ms=self._ctx.clock()``(那口墙钟),这一条必须红。

    **两个方向都摆在这儿。** 只断"墙钟拨了不报"的话,一个"这条 P1 整个哑了"
    的实现照样绿 —— 所以后半段把引擎那口钟拨过线,它必须报出来。
    """
    srv = 跑起来的服务
    让开腿(srv)

    # 墙钟往前跳一大截(现场就是一次 NTP 对时、或者手动改了系统时间)。
    # 引擎那口钟一动不动 —— 人还站在狗边上,腿是刚让开的。
    钟.前进(SUSPEND_STALE_MS * 6)
    assert not 等到(lambda: 挂起超时记录(caplog), 最多等=观察窗口), (
        "墙钟往前跳了一下,人就挨了一条「接管着没还回来」—— 判据那一边还在"
        "现问 ctx.clock(),而 SuspendPoint.at_ms 已经不跟着墙钟跳了。"
        "见 server._挂起超时了 的文档串。")

    # 正向锚点:同一台服务、同一次挂起,只把引擎那口钟拨过线,它就必须报。
    # 没有这一段,上面那句 ``not`` 分不开"实现对"和"这条 P1 整个哑了"。
    引擎秒表.前进(SUSPEND_STALE_MS + 1)
    assert 等到(lambda: 挂起超时记录(caplog)), (
        "引擎那口钟都走过十分钟了还是没报 —— 上面那句 not 什么也没证明,"
        "这条 P1 本身就是哑的。")


def test_没让开腿就不会有这条(跑起来的服务, 钟, caplog):
    """任务正常跑着,墙钟拨多远都不该有这条。

    **挡的是「没让开腿也报」这一件事,别把名牌挂大了。** 一个照
    ``started_ms`` 算、但**还留着 ``yielding`` 判断**的实现在这儿照样绿 ——
    真正接住那一刀的是 ``test_算的是让开腿那一刻不是开跑那一刻``,它把
    "开跑很久但刚让开腿"摆出来了。

    **这一条接不住「删掉 ``if not engine.yielding: return``」那一刀,别写成
    它接得住。** 这里从头到尾没让开腿过,快照上的 ``suspended_at`` 是
    ``None`` —— 那道闸删掉之后落进的是 ``点 is None`` 那个出口(照样不报警,
    只多刷日志),这条断言原样绿。真接住那一刀的是
    ``test_接管结束之后残留的挂起点不许再报``,它摆的才是「有一个陈旧的
    ``suspended_at`` 摆在那儿,而唯一挡着告警的就是 ``yielding``」。

    这一条今天挡的是**完全不看让位状态、并且拿 ``ctx.clock`` 去算**的实现:
    照 ``started_ms`` 算、或者照"这一趟开跑到现在流逝了多久"算 —— 那一类在
    这儿会当场报出来。

    **拨的是墙钟,不是引擎那口钟,这是有意的。** 引擎在 ``RUNNING`` 上被拨
    十分钟会真的撞上点位的 ``waypoint_timeout_s``(见 :class:`跟着表走的秒表`
    最后一段),那一趟当场按失败收尾,这条断言就变成在一个已经不跑了的引擎
    上测的,什么也证明不了。「拿 ``engine.now_ms()`` 去减 ``started_ms``」那
    一类由 ``test_算的是让开腿那一刻不是开跑那一刻`` 接住 —— 那边用的是摆好
    的假引擎,拨钟不会带坏别的东西。
    """
    钟.前进(SUSPEND_STALE_MS * 3)          # 跑起来的服务:只要它在跑,不用它本人
    assert not 等到(lambda: 挂起超时记录(caplog), 最多等=观察窗口), "没让开腿也报了挂起超时"


def test_一次挂起只报一条(跑起来的服务, 引擎秒表, caplog):
    """闸门半秒醒一拍。报重了的话十分钟就是一千两百条,日志当场没法看。"""
    srv = 跑起来的服务
    让开腿(srv)
    引擎秒表.前进(SUSPEND_STALE_MS + 1)
    assert 等到(lambda: 挂起超时记录(caplog))

    起始拍 = srv.hub._lease_ticks
    assert 等到(lambda: srv.hub._lease_ticks >= 起始拍 + 拍数, 最多等=观察窗口), \
        "闸门没再醒过,下面那个 1 是因为它根本没机会报第二次"
    条 = 挂起超时记录(caplog)
    assert len(条) == 1, [r.getMessage() for r in 条]


def test_超时的边界正好在这个常量上():
    """纯判定,不碰服务。``SUSPEND_STALE_MS`` 那一刻还不算超时,再多一毫秒才算。"""
    起 = 1_757_000_000_000
    点 = SuspendPoint(waypoint_index=0, waypoint_name="P1_transformer",
                      pose=None, reason="人要接管", at_ms=起,
                      from_state=RunState.RUNNING, prior_suspend_ms=0)
    assert not _挂起超时了(点, now_ms=起)
    assert not _挂起超时了(点, now_ms=起 + SUSPEND_STALE_MS)
    assert _挂起超时了(点, now_ms=起 + SUSPEND_STALE_MS + 1)
    # 没让开腿就没有这个点。``None`` 不许当成「很久以前」。
    assert not _挂起超时了(None, now_ms=起 + SUSPEND_STALE_MS * 10)


def test_反复短接管按累计算不按单次算():
    """狗在返航路上被反复拉开:每一次都四分钟,加起来早过线了。

    现场那一幕(挂账 56 放行返航途中让开腿之后新长出来的洞):狗电量到线转
    返航,走一段被人拉到一边、放回、又被拉开。``_go_home`` 那个 ``while True``
    没有上限、每圈重算 ``RETURN_TIMEOUT_S``,挂起期间电量事件只留底不中止,
    看门狗只在两次接管之间那几秒的缝里有机会开火 —— 于是狗耗到没电,而按
    单次口径(``now_ms - 点.at_ms``)这条 P1 **一次都不报**,值守屏上一切
    如常。

    **不封接管次数、不加新阈值**:理由见 ``_挂起超时了`` 的文档串。判据换成
    「这一趟累计有多久没在跑」,阈值仍然是同一个 ``SUSPEND_STALE_MS``。

    引擎那一半(账要真的加起来)的守卫在
    ``tests/engine/test_machine_suspend.py::test_同一趟返航里反复短接管_挂起时长会累计``。
    """
    # 前提断言,不是推算式:这条测试的三档(4/8/12 分钟)是照着
    # ``SUSPEND_STALE_MS`` 当前的 10 分钟手选的(``2 × 四分钟 < 阈值 <
    # 3 × 四分钟``)。常量已经排进真机清单要量,改了的话这一句先红,红在
    # 这儿人才知道要回来重新选「四分钟」——写成从常量推算的表达式看着更
    # 顺眼,但常量怎么变都能凑过,等于把断言删了。
    assert SUSPEND_STALE_MS == 10 * 60_000, (
        "SUSPEND_STALE_MS 不再是 10 分钟,这条测试的「四分钟」三档要重新选")
    四分钟 = 4 * 60_000
    起 = 1_757_000_000_000

    def 第几次(n: int) -> SuspendPoint:
        """第 ``n`` 次接管刚开始的那一刻。之前 ``n-1`` 次各挂了四分钟。"""
        return SuspendPoint(waypoint_index=0, waypoint_name="P1_transformer",
                            pose=None, reason="走廊被堵了", at_ms=起,
                            from_state=RunState.RETURNING,
                            prior_suspend_ms=四分钟 * (n - 1))

    # 头两次挂满四分钟:累计 4、8 分钟,都还不到线 —— 正常接管不该挨 P1,
    # 一个总在误报的 P1 很快就没人当回事。
    assert not _挂起超时了(第几次(1), now_ms=起 + 四分钟)
    assert not _挂起超时了(第几次(2), now_ms=起 + 四分钟)
    # 第三次也只挂了四分钟,但这一趟累计已经 12 分钟 —— 单次口径下这里仍然
    # 是"才 4 分钟",这一句就是两种口径唯一分得开的地方。
    assert _挂起超时了(第几次(3), now_ms=起 + 四分钟)


def test_这条P1的三个数必须同源(没起的服务, caplog):
    """**``suspend_stale`` 的判据只许读引擎那一口钟,``ctx.clock`` 不许沾。**

    这条测试是换掉的:原来那条叫 ``test_闸门读的钟必须还是墙钟``,钉的是
    "``ctx.clock`` 必须还是墙钟,否则这条 P1 静默失效"。那个前提死了 ——
    判据现在一个字都不读 ``ctx.clock``,原来那条**跑绿但守不住任何东西**
    (docs/测试为什么会说谎.md 里的那一类)。它不该被删,该被换成钉住新
    不变量的那一条。

    新不变量,两个方向各钉一次:

    1. **把 ``ctx.clock`` 换成一口完全不同纪元的钟,这条 P1 的行为一点都
       不变。** 下面拿 ± 一亿毫秒(约 ±27.8 小时)去偏 —— 往前偏是"NTP 把
       钟往前校了一天",往后偏是"校回来了",两种真机上都会发生。判据要是还
       在读 ``ctx.clock``:往前偏那一轮会当场报一条 P1(人刚让开腿就挨骂),
       往后偏则连真该报的都报不出来。
    2. **把引擎那口钟拨过线,它必须跟着变。** 只钉第 1 条的话,一个"这条 P1
       整个哑了"的实现照样全绿。

    这里断的是**报不报**,不是**报在几点**。
    """
    srv = 没起的服务
    ctx = srv.ctx
    真的引擎 = ctx.engine
    真的钟 = ctx.clock
    基准 = 1_757_000_000_000
    #: 一亿毫秒 ≈ 27.8 小时。取这个量级是为了让"读错了钟"跟"调度抖动"差出
    #: 好几个数量级 —— 差一点点的偏移分不开这两件事。
    别的纪元 = 100_000_000

    def 摆(*, 引擎钟: int, 让开腿于: int) -> None:
        ctx.engine = 假引擎(_快照(开跑=基准 - SUSPEND_STALE_MS * 100,
                                  让开腿于=让开腿于),
                            引擎钟=引擎钟)

    try:
        # 一、差一毫秒没过线。``ctx.clock`` 摆到哪个纪元上都不许报。
        for 偏移 in (0, 别的纪元, -别的纪元):
            ctx.clock = lambda 偏移=偏移: 基准 + 偏移
            摆(引擎钟=基准, 让开腿于=基准 - SUSPEND_STALE_MS)
            srv.hub._判定挂起超时()
            assert not 挂起超时记录(caplog), (
                f"ctx.clock 偏了 {偏移} 毫秒,这条 P1 就报了 —— 判据还在读它。"
                "引擎那口钟说的是「才挂了十分钟整,差一毫秒没过线」。")

        # 二、``ctx.clock`` 留在错纪元上(而且是往**回**偏的那一边:判据要是
        # 还读它,这里算出来是个大负数,永远不报),只把引擎那口钟往前拨过线。
        ctx.clock = lambda: 基准 - 别的纪元
        摆(引擎钟=基准 + 1, 让开腿于=基准 - SUSPEND_STALE_MS)
        srv.hub._判定挂起超时()
        assert 挂起超时记录(caplog), (
            "引擎那口钟已经走过线了,这条 P1 还是没出来 —— 要么判据读的是"
            "ctx.clock(它被摆到一个过去的纪元上),要么这条 P1 本身是哑的。")
    finally:
        ctx.engine = 真的引擎
        ctx.clock = 真的钟


def test_累计挂起也走引擎那口钟(没起的服务, caplog):
    """``prior_suspend_ms`` 是式子里的第三个数,它也在引擎那口钟上。

    反复短接管那一幕(狗在返航路上被拉开、放回、又被拉开)的判据是
    ``engine.now_ms() - 点.at_ms + 点.prior_suspend_ms``。纯判定那一层由
    ``test_反复短接管按累计算不按单次算`` 钉着(它直接调 ``_挂起超时了``,
    不碰钟),**这一条补的是取数口那一层**:``_判定挂起超时`` 把累计量原样
    喂进去了没有,而且喂的时候 ``ctx.clock`` 一样不许沾。

    两个方向:同样一次"才挂了四分钟"的接管,``prior_suspend_ms`` 是 0 就
    不许报,把之前累计的那段补上、刚好过线就必须报 —— 唯一的差别就是这一个
    字段。``ctx.clock`` 全程摆在一个往回偏的错纪元上,它要是参与判据,
    **两个方向会一起变成"永远不报"**,后半段当场红。
    """
    srv = 没起的服务
    ctx = srv.ctx
    真的引擎 = ctx.engine
    真的钟 = ctx.clock
    基准 = 1_757_000_000_000
    四分钟 = 4 * 60_000
    # 前提断言,不是推算式:下面那个"四分钟"是照着 10 分钟这个阈值手选的
    # (四分钟本身远不到线,补上 ``差一点就到线`` 那一段刚好过)。理由跟
    # ``test_反复短接管按累计算不按单次算`` 里那一句一样。
    assert SUSPEND_STALE_MS == 10 * 60_000, (
        "SUSPEND_STALE_MS 不再是 10 分钟,这条测试的「四分钟」要重新选")
    try:
        ctx.clock = lambda: 基准 - 100_000_000
        # 这一次才挂了四分钟,之前一次都没挂过 —— 不许报。
        ctx.engine = 假引擎(_快照(开跑=基准 - SUSPEND_STALE_MS * 100,
                                  让开腿于=基准 - 四分钟, 之前挂过=0),
                            引擎钟=基准)
        srv.hub._判定挂起超时()
        assert not 挂起超时记录(caplog), "才挂了四分钟、之前没挂过,就报了"

        # 同一次接管、同一刻,只把"之前累计挂过多久"补上,刚好过线一毫秒。
        ctx.engine = 假引擎(_快照(开跑=基准 - SUSPEND_STALE_MS * 100,
                                  让开腿于=基准 - 四分钟,
                                  之前挂过=SUSPEND_STALE_MS - 四分钟 + 1),
                            引擎钟=基准)
        srv.hub._判定挂起超时()
        assert 挂起超时记录(caplog), (
            "这一趟累计已经过线了,还是没报 —— prior_suspend_ms 没被喂进判据"
            "(反复短接管那一幕就是这么一条都报不出来的),或者判据读的是"
            "被摆到过去纪元上的 ctx.clock。")
    finally:
        ctx.engine = 真的引擎
        ctx.clock = 真的钟


def test_生产缺省的那口墙钟还是墙钟(没起的服务):
    """``ctx.clock`` 不再管 ``suspend_stale``,但它**仍然**得是墙钟。

    原来这几句长在 ``test_闸门读的钟必须还是墙钟`` 里,理由是"挂起超时那条
    减法靠它"。那个理由没了,**这几句本身没过期**:租约 TTL 判到期、排程
    到点都按它算,而这些都是要跟现场的人对表的。换了纪元之后它们会一起悄悄
    失效,所以留在这儿单独钉一次,**并且把理由改成真的那个**。
    """
    ctx = 没起的服务.ctx
    差 = abs(ctx.clock() - time.time() * 1000)
    assert 差 < 5_000, (
        f"ctx.clock() 跟墙上时钟差了 {差} 毫秒 —— 租约 TTL、排程到点"
        "全按它算,换了纪元这些会一起悄悄失效。")
    # 注进去的那份自己对得上,不代表真机上那条默认路径对得上:生产缺省也得
    # 是墙钟。
    assert AppContext.__dataclass_fields__["clock"].default is _wall_ms
    # **上面两句都只盯身份,不盯本体。** 第一句测的是这一组自己注进去的钟
    # (fixture 那份本来就从 ``time.time()`` 派生,两边同源,离自测只差一步),
    # 第二句只问"缺省还是不是 ``_wall_ms`` 这个对象"。把 ``_wall_ms`` **本体**
    # 改掉(第 9 卷最可能的下手方式:让它读 ``time.monotonic()``、或者读一个
    # 回放/仿真的场景钟),这两句一句都不红。所以直接量它本人 —— 它是纯
    # 函数,不用起服务。
    本体差 = abs(_wall_ms() - time.time() * 1000)
    assert 本体差 < 5_000, (
        f"_wall_ms() 跟墙上时钟差了 {本体差} 毫秒 —— 它已经不是墙钟了。"
        "租约 TTL、排程到点全按它算。")


class 假引擎:
    """摆好的引擎:``_判定挂起超时`` 只看 ``yielding``、``snapshot``、``now_ms()``。

    ``yielding`` 允许显式指定,是为了摆出"引擎说自己在让位、快照上却没盖点"
    那个**不该发生的状态**(见 ``test_让开腿了却没盖点不许静默走掉``)。

    **这个替身能构造的状态空间比真引擎大 —— 这是有意的,不是疏漏。**
    下面那张清单说清楚哪几种组合真引擎摆得出来、哪几种是故意开的口子。
    拿这个替身写新测试之前先读那张清单:摆一个现实里不存在的组合,
    **红了没意义,绿了更没意义**。

    真引擎那边的两条硬事实(``engine/machine.py``):

    * ``yielding`` 就是 ``state is RunState.SUSPENDED``,一个字都不多 ——
      所以真引擎身上 ``yielding`` 和 ``state`` 是**同一位**,分不开;
    * 让开腿会往快照上盖 ``suspended_at``,继续/中止会把它抹掉 —— 所以
      真引擎身上 ``suspended_at`` 有没有跟 ``yielding`` 是同生同灭的。

    **不加运行时校验。** 加了就把"故意构造非法态"这个用途本身堵死了,
    而这一组里最要紧的三条测试摆的正是非法态。这是一张文档,不是一道闸。

    ``引擎钟`` 是 ``MissionEngine.now_ms()`` 那口钟此刻的读数(Unix epoch
    毫秒)。**判据现在从这儿取"现在",不再从 ``ctx.clock`` 取** —— 这个替身
    要能跟 ``ctx.clock`` **分别拨动**,不然"两个数同源"这件事在测试里根本
    摆不出来。留空的话跟真引擎"没在跑就回墙钟"那一支对齐(``_live is None``
    时 ``now_ms()`` 直接回 ``self._wall_ms()``),够那几条不关心时刻的用例
    使。
    """

    def __init__(self, snapshot: RunSnapshot, *,
                 yielding: bool | None = None,
                 引擎钟: int | None = None) -> None:
        self.snapshot = snapshot
        self.yielding = (snapshot.state is RunState.SUSPENDED
                         if yielding is None else yielding)
        self.引擎钟 = (int(time.time() * 1000) if 引擎钟 is None else 引擎钟)

    def now_ms(self) -> int:
        return self.引擎钟


def _快照(*, 开跑: int, 让开腿于: int, 挂着: bool = True,
          盖着点: bool | None = None, 之前挂过: int = 0) -> RunSnapshot:
    """摆一份快照。``盖着点`` 缺省跟 ``挂着`` 走。

    ``之前挂过`` 就是 ``SuspendPoint.prior_suspend_ms``:这一次让开腿**之前**
    这一趟已经累计挂了多久。缺省 0(这一趟头一次让开腿),
    ``test_累计挂起也走引擎那口钟`` 把它拨起来。

    ``盖着点`` 单开一个口子,是为了摆出「任务已经回到 ``RUNNING``、快照上却
    还留着上一次接管的 ``suspended_at``」那个**残留**状态 —— 真引擎摆不出来
    (它一继续就把点抹掉),而那正是
    ``test_接管结束之后残留的挂起点不许再报`` 要盯的那一幕。

    ------------------------------------------------------------------

    **``挂着`` / ``盖着点`` / ``假引擎.yielding`` 三位的合法组合清单。**
    (``挂着`` 决定 ``state`` 是 ``SUSPENDED`` 还是 ``RUNNING``;``盖着点``
    决定 ``suspended_at`` 是不是 ``None``。)

    真引擎能出现的,只有两种 —— 三位是**锁在一起**的:

    1. ``挂着=True, 盖着点=True, yielding=True``
       正在被接管。缺省组合(两个缺省都跟着 ``挂着`` 走),不用显式写。
    2. ``挂着=False, 盖着点=False, yielding=False``
       正常在跑,没人接管。写 ``挂着=False`` 就是这一种。

    故意允许构造的非法态,每一种都对应一个**真实的失效形态**:

    3. ``挂着=False, 盖着点=False, yielding=True``
       引擎嘴上说在让位,快照上却没盖点。真引擎摆不出来(``yielding`` 就是
       ``state is SUSPENDED``),但**这条隐含前提没有编译器守着**:任务 13
       新加了一种让位入口,下一种还会有,哪一种忘了盖点这条 P1 就整个哑掉。
       用在 ``test_让开腿了却没盖点不许静默走掉`` / ``test_没盖点那句惨叫
       只喊一次``。
    4. ``挂着=False, 盖着点=True, yielding=False``
       人早把腿还回来了,快照上还留着上一次接管的点。真引擎摆不出来(它一
       继续就抹点),但引擎哪天忘了抹,真机上就是这一幕:狗好端端跑着,
       值守屏十分钟一条 P1。用在 ``test_接管结束之后残留的挂起点不许再报``
       的**否定方向**。
    5. ``挂着=False, 盖着点=True, yielding=True``
       跟第 4 种只差 ``yielding`` 这一位。**这一位正是 ``_判定挂起超时``
       里那道闸读的东西**,而 ``state`` 它一眼都不看 —— 所以对被测代码而
       言这两份输入的差别恰好只有一位。用在
       ``test_接管结束之后残留的挂起点不许再报`` 的**正向锚点**:摆第 4 种
       不报、只翻这一位就必须报,那条「挡着它的只有那道闸」才立得住。

    **不在清单上的组合(比如 ``挂着=True, 盖着点=False``)不是被禁止,是
    还没有人为它写过理由。** 要用就先想清楚它对应真机上的哪一幕,
    然后把它加进这张清单 —— 想不出对应的失效形态,那条测试就不该存在。
    """
    点 = SuspendPoint(waypoint_index=0, waypoint_name="P1_transformer",
                      pose=None, reason="人要接管", at_ms=让开腿于,
                      from_state=RunState.RUNNING, prior_suspend_ms=之前挂过)
    留点 = 挂着 if 盖着点 is None else 盖着点
    return RunSnapshot(
        state=RunState.SUSPENDED if 挂着 else RunState.RUNNING,
        mission="巡检一号", waypoint_index=0,
        waypoint_name="P1_transformer", total=1, started_ms=开跑,
        suspended_at=点 if 留点 else None)


def _惨叫(caplog) -> list:
    """caplog 里 ERROR 及以上的那几条。"""
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_算的是让开腿那一刻不是开跑那一刻(没起的服务, 钟, caplog):
    """一趟任务可以跑一整天,人在最后一分钟才让开腿。

    拿 ``started_ms`` 算的实现,会在人刚把手机掏出来的那一秒就报 P1 —— 而
    真正该报的那种(让开腿之后没人还回来)它一条也不会漏,所以只测「该报的
    报了」根本分辨不出这两个实现。**两个方向都摆在这儿**:开跑很久但刚让开
    腿的不许报,让开腿本身超了十分钟的必须报。

    这条测的是 ``_StateHub`` 那一层的取数口,不是 ``_挂起超时了`` 那个纯
    判定 —— 后者的签名里根本没有 ``started_ms``,把判据换掉这一刀砍不进去,
    真会砍中的是这里。
    """
    srv = 没起的服务
    ctx = srv.ctx
    此刻 = 钟()
    真的 = ctx.engine
    try:
        # ``引擎钟=此刻``:判据从 ``engine.now_ms()`` 取"现在",跟 ``at_ms``
        # 同源。
        ctx.engine = 假引擎(_快照(开跑=此刻 - SUSPEND_STALE_MS * 100,
                                  让开腿于=此刻), 引擎钟=此刻)
        srv.hub._判定挂起超时()
        assert not 挂起超时记录(caplog), "刚让开腿就报了 —— 算的不是让开腿那一刻"

        ctx.engine = 假引擎(_快照(开跑=此刻 - SUSPEND_STALE_MS * 100,
                                  让开腿于=此刻 - SUSPEND_STALE_MS - 1),
                            引擎钟=此刻)
        srv.hub._判定挂起超时()
        assert 挂起超时记录(caplog), "让开腿都超过十分钟了,还是没报"
    finally:
        ctx.engine = 真的


def test_让开腿了却没盖点不许静默走掉(没起的服务, 钟, caplog):
    """``yielding`` 为真而 ``suspended_at`` 为空 —— **这是不该发生的状态**。

    ``_判定挂起超时`` 取的是 ``engine.yielding``(不在外壳里复刻状态表,这个
    决定是对的),可它把"``yielding`` 的每一种成因都会往快照上盖一个
    ``suspended_at``"变成了一条**没人守着的隐含前提**:两种情况原来走的是
    同一条静默出口 —— 「没让开腿」和「让开腿了但没盖点」都是 ``return``,
    一行痕迹不留。

    **前提已经在被搬动了**:任务 13 刚加了一种新的让位(``RETURNING`` 途中
    挂起),下一种还会有。哪一种忘了盖点,这条 P1 就整个哑掉 —— 狗在返航路
    上被拉到一边、然后没人管一整夜,``suspend_stale`` 一条不报、屏上一切如常,
    挂账 67a 原样复发,只是换了个入口。

    所以这里断的不是"报了一条告警"(不动狗、也不该拿一条 P1 去说一件本质是
    内部不一致的事),而是**它没有静默走掉**:日志上必须留下一条 ERROR。
    """
    srv = 没起的服务
    ctx = srv.ctx
    此刻 = 钟()
    真的 = ctx.engine
    try:
        # 快照是"没挂起"的那一份(``suspended_at`` 是 ``None``),而引擎嘴上
        # 说自己在让位 —— 正是那个对不上的状态。
        ctx.engine = 假引擎(_快照(开跑=此刻, 让开腿于=此刻, 挂着=False),
                            yielding=True)
        with caplog.at_level(logging.ERROR, logger="d1max_patrol.app.server"):
            srv.hub._判定挂起超时()
        惨叫 = _惨叫(caplog)
        assert 惨叫, (
            "引擎说自己在让位、快照上却没盖点,而这一拍一声不吭地走掉了 —— "
            "这条 P1 从此是哑的,没有任何人会知道。")
        assert "suspended_at" in 惨叫[0].getMessage(), 惨叫[0].getMessage()
    finally:
        ctx.engine = 真的


def test_没盖点那句惨叫只喊一次(没起的服务, 钟, caplog):
    """上面那条 ERROR 守的是一个**会一直存在**的状态,而闸门半秒醒一拍。

    不节流就是 2 条 ERROR/秒、一夜二十万条。这在别的项目上只是吵,在这台狗上
    是**自伤**:这条日志刷的盘,正是存证据的那块盘 —— 一条诊断日志把自己
    的盘写满,比不报还糟。

    **两个方向都钉:** 连着两拍只喊一次;可是回到正常状态之后再出一次同样的
    问题,必须**还会再喊**。只钉前一半的话,一个"喊过就永远闭嘴"的实现照样
    绿,而它在真机上的样子是:重启前的第二次故障从此无声。
    """
    srv = 没起的服务
    ctx = srv.ctx
    此刻 = 钟()
    真的 = ctx.engine

    def 没盖点的():
        # 快照是"没挂起"的那一份(``suspended_at`` 是 ``None``),引擎却说
        # 自己在让位 —— 那个不该发生的状态。
        return 假引擎(_快照(开跑=此刻, 让开腿于=此刻, 挂着=False), yielding=True)

    try:
        with caplog.at_level(logging.ERROR, logger="d1max_patrol.app.server"):
            ctx.engine = 没盖点的()
            srv.hub._判定挂起超时()
            srv.hub._判定挂起超时()
            assert len(_惨叫(caplog)) == 1, (
                f"连着两拍喊了 {len(_惨叫(caplog))} 声。闸门半秒醒一拍,"
                "这么喊一夜就是二十万条 ERROR,写满的正是存证据的那块盘。")

            # 接管结束(``yielding`` 落回假),账要清掉。
            ctx.engine = 假引擎(_快照(开跑=此刻, 让开腿于=此刻, 挂着=False),
                                yielding=False)
            srv.hub._判定挂起超时()
            ctx.engine = 没盖点的()
            srv.hub._判定挂起超时()
            assert len(_惨叫(caplog)) == 2, (
                "状态恢复过一次之后又坏了,这一次一声不吭 —— 节流写成了"
                "「这辈子只喊一次」,重启之前的第二次故障从此没有任何痕迹。")
    finally:
        ctx.engine = 真的


def test_接管结束之后残留的挂起点不许再报(没起的服务, 钟, caplog):
    """快照上留着一个陈旧的 ``suspended_at``,而人早就把腿还回来了。

    **这一条是 ``_判定挂起超时`` 里 ``if not engine.yielding: return`` 那道闸
    的唯一接盘人。** 摆出来的状态是:``yielding`` 为假(没人在接管),快照上
    却还盖着一个远超阈值的挂起点 —— 这时候挡着那条 P1 的**只有**那道闸,别的
    条件(阈值、``点 is None``)一个都不挡。删掉它,这条当场红。

    ``test_没让开腿就不会有这条`` 接不住这一刀:那边从头到尾没让开腿过,快照
    上根本没有陈旧的点可读,删掉闸之后落进的是 ``点 is None`` 那个出口,照样
    不报警。

    这个残留状态不是假想的:引擎接管结束时如果忘了把 ``suspended_at`` 抹掉,
    真机上就是这一幕 —— 狗好端端地在跑,值守屏上一条"人接管着没还回来"的 P1
    十分钟一条地往外冒,而现场一个人都没有。误报会很快教会人无视这条告警,
    然后真出事那一次也一起被无视掉。
    """
    srv = 没起的服务
    ctx = srv.ctx
    此刻 = 钟()
    真的 = ctx.engine
    try:
        ctx.engine = 假引擎(
            _快照(开跑=此刻 - SUSPEND_STALE_MS * 100,
                  让开腿于=此刻 - SUSPEND_STALE_MS * 3,
                  挂着=False, 盖着点=True),
            yielding=False, 引擎钟=此刻)
        srv.hub._判定挂起超时()
        assert not 挂起超时记录(caplog), (
            "没有人在接管(yielding 是假),快照上那个挂起点是上一次接管留下的"
            "残渣 —— 照着它报了一条 P1。挡着这一条的只有 _判定挂起超时 里那句"
            "`if not engine.yielding: return`。")

        # **正向锚点:同样的服务、同样的一拍、同一份快照,只把 ``yielding``
        # 翻成真,这条 P1 就必须出现。**
        #
        # 上面那句 ``not`` 是一条否定命题,它默认「记日志那条路在 ``没起的
        # 服务`` 这个 fixture 上是活的」—— 而那个前提在它自己身上没有落锚:
        # 那一行被删掉、或者日志没接到 caplog 上,上面那句照样绿,而它守的
        # 那道闸早就没了。**断言否定命题的测试必须自己证明肯定方向能成立**,否则它不
        # 区分「实现对」和「机器根本没在跑」。
        #
        # 形状照抄同文件的 ``test_算的是让开腿那一刻不是开跑那一刻``:
        # 两个方向都摆在这儿,唯一的差别就是那道闸读的那一位。
        ctx.engine = 假引擎(
            _快照(开跑=此刻 - SUSPEND_STALE_MS * 100,
                  让开腿于=此刻 - SUSPEND_STALE_MS * 3,
                  挂着=False, 盖着点=True),
            yielding=True, 引擎钟=此刻)
        srv.hub._判定挂起超时()
        assert 挂起超时记录(caplog), (
            "同一份快照、同一拍,只把 yielding 翻成真,这条 P1 还是没出来 —— "
            "记日志那条路本身就是断的,上面那句 not 什么也没证明。")
    finally:
        ctx.engine = 真的


# ------------------------------------------------- 12c 从路由挂起再从路由继续


def test_注进去的超时故意不等于模块缺省():
    """这一句是下面那条端到端的判别力本身,不是凑数。

    两个数要是相等,「参数被忽略、仍读模块常量」那一刀砍下去,端到端照样绿。
    """
    assert 超时秒 != HEARTBEAT_TIMEOUT_S


def test_从路由挂起再从路由继续(跑起来的服务, 钟, 秒表):
    """挂账 69:engine 层有,路由层没有 —— 而人按的是路由层那个按钮。

    ``/api/run/resume`` 那一条**投进队列就回 200**(见 ``_run_cmd``),所以
    「还不能继续」不会以 409 的形式出现在 HTTP 上:它的样子是**引擎不离开
    ``SUSPENDED``**,并且把拒绝理由推到快照上给人看。这里断的就是后者。
    """
    srv = 跑起来的服务
    ctx = srv.ctx
    让开腿(srv)

    assert _post(srv, "/api/teleop", 走一拍)[0] == 200
    assert ctx.teleop.active is True, "遥控都没起来,下面那句「继续不了」是白的"

    # 左手还压在摇杆上。**等的是闸门醒了几拍,不是墙上的半秒** —— 理由跟
    # ``test_挂起超时只报警不自己动狗`` 里那一段一样:0.5 秒的窗口正好等于
    # ``_LEASE_WATCH_PERIOD_S``,一个"下一拍才真放行"的实现能从这个窗口底下
    # 溜过去,而且只在慢机器上偶尔红。窗口按周期的倍数写,见 :data:`观察窗口`。
    assert _post(srv, "/api/run/resume")[0] == 200
    起始拍 = srv.hub._lease_ticks
    assert 等到(lambda: srv.hub._lease_ticks >= 起始拍 + 拍数, 最多等=观察窗口), \
        "闸门没再醒过,下面那句「引擎没自己跑」是因为它根本没机会跑"
    assert ctx.engine.state is RunState.SUSPENDED, "人还握着摇杆,引擎就自己跑起来了"
    assert "遥控" in ctx.engine.snapshot.reason, ctx.engine.snapshot.reason

    # 还没到注进去的那个超时:手必须还在。
    秒表.前进(超时秒 / 2)
    assert not 等到(lambda: not ctx.teleop.active, 最多等=TICK * 20), \
        f"才过了 {超时秒 / 2} 秒手就松了 —— heartbeat_timeout_s 没被用上"

    # 过了注进去的那个超时:守死人开关到期,手松了。
    秒表.前进(超时秒)
    assert 等到(lambda: not ctx.teleop.active), "守死人开关到期了,遥控还开着"

    assert _post(srv, "/api/run/resume")[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.RUNNING))
    assert ctx.engine.snapshot.suspended_at is None
