"""挂起没人管要报警(12a),以及从路由挂起再从路由继续(12c)。

**这一组起的是真服务、打真 HTTP、等的是真后台协程。** 12a 要盯的那件事
(人接管完忘了还回来,这趟就永远挂着)只会发生在没人操作的时候 —— 判定它
的是 ``_StateHub`` 那条闸门协程,直接调处理函数一个字都测不到。

12c 同理:引擎层的 suspend/resume 第 7 卷就做全了,缺的一直是**路由层**
那一条链 —— 而人按的是路由层那个按钮。
"""

from __future__ import annotations

import contextlib
import json
import time
from urllib.parse import quote

import pytest

from d1max_patrol.app.server import SUSPEND_STALE_MS, AppServer, _挂起超时了
from d1max_patrol.app.teleop import HEARTBEAT_TIMEOUT_S, Teleop
from d1max_patrol.engine.alerts import Level
from d1max_patrol.engine.machine import RunSnapshot, RunState, SuspendPoint
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


class 跟着墙走的钟:
    """真墙上时钟 + 一个能拨的偏移量。

    **这一组不能用别处那种冻住的 ``T0``。** 让开腿那一刻的时间戳
    (``SuspendPoint.at_ms``)是引擎自己盖的,盖的是真墙上时钟
    (``engine/machine.py`` 里的 ``int(time.time() * 1000)``),不走
    ``ctx.clock`` —— 引擎不认识外壳,注不进去。给 ``ctx.clock`` 冻一个 2025
    年的 ``T0``,``now_ms - at_ms`` 就是个负到离谱的数,超时判定永远为假,
    而这一组会「全绿」在一个根本没进去过的分支上(docs/测试为什么会说谎.md
    的第一种)。带偏移的真钟把两头放回同一根时间轴上:``前进`` 拨的是「从
    现在起又过了多久」,跟 ``at_ms`` 是可比的。
    """

    def __init__(self) -> None:
        self.偏移 = 0

    def __call__(self) -> int:
        return int(time.time() * 1000) + self.偏移

    def 前进(self, ms: int) -> None:
        self.偏移 += ms


class 假秒表:
    """遥控那一头的单调钟(秒)。跟上面那个是两根不同的轴,别混。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def 前进(self, s: float) -> None:
        self.t += s


def 等到(条件, *, 最多等: float = 3.0) -> bool:
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
def 秒表() -> 假秒表:
    return 假秒表()


@pytest.fixture
def 服务(bridge, tmp_path, 钟, 秒表):
    ctx = make_ctx(bridge, tmp_path, clock=钟)
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


def 挂起超时告警(srv) -> list:
    """簿子里的 ``suspend_stale``。

    **按 kind 挑,不是整份比对。** 这台开发机的盘水位本来就过了 80% 的线,
    起真服务就会合法地多一条 ``disk_80``(挂账 76)—— 那是环境,不是这一组
    要测的事。
    """
    return [a for a in srv.alerts.open() if a.kind == "suspend_stale"]


def 让开腿(srv, reason: str = "人要接管") -> None:
    ctx = srv.ctx
    assert _post(srv, "/api/run/suspend", {"reason": reason})[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))


# ----------------------------------------------------- 12a 挂起太久没人管


def test_挂起太久没人管升P1(跑起来的服务, 钟):
    """挂账 67a:第 7 卷判过 ``SUSPENDED`` 期间无超时无强制放行,而**这一卷
    的 alerts 就是来盯这个的**。人接管完忘了还回来,这趟就永远挂着,而屏幕上
    一切如常。
    """
    srv = 跑起来的服务
    让开腿(srv)
    assert not 挂起超时告警(srv), \
        "钟还没拨就报了 —— 下面那句「到点了才报」什么也证明不了"

    钟.前进(SUSPEND_STALE_MS + 1)
    assert 等到(lambda: 挂起超时告警(srv)), "挂起超过十分钟,一条告警都没有"
    a = 挂起超时告警(srv)[0]
    assert a.level is Level.P1, a
    assert a.robot == srv.ctx.identity.sn, a
    # 标题/正文里要说得出这是「人接管着没还回来」,不是一句「出错了」。
    assert "接管" in (a.title + a.detail), a


def test_挂起超时只报警不自己动狗(跑起来的服务, 钟):
    """人还在现场,狗自己动起来是这套系统里最不该发生的事(§5.8 同理)。

    所以超时的处置是**升 P1**,不是自动 ``resume``、不是自动 ``abort``。
    """
    srv = 跑起来的服务
    ctx = srv.ctx
    让开腿(srv)
    走过 = len(ctx.device.walk_calls)
    导过 = len(ctx.nav.goto_calls)

    钟.前进(SUSPEND_STALE_MS * 5)
    assert 等到(lambda: 挂起超时告警(srv)), "先得真报出来,不然下面全是空转"
    # 报完之后再多等一会儿:自动 resume 是异步的,报警那一刻还没轮到它。
    assert not 等到(lambda: ctx.engine.state is not RunState.SUSPENDED,
                    最多等=0.5), "超时之后引擎自己离开了 SUSPENDED"
    assert ctx.engine.state is RunState.SUSPENDED
    # ``goto`` 才是「狗真的动了」在这台假后端上的样子:自动 resume 会重发当前
    # 点。``walk`` 一起看着,那是遥控档上的直接位移。
    assert ctx.nav.goto_calls[导过:] == [], ctx.nav.goto_calls[导过:]
    assert ctx.device.walk_calls[走过:] == [], ctx.device.walk_calls[走过:]


def test_没到点不报(跑起来的服务, 钟):
    """差一分钟就报,等于把十分钟这个数写进了注释而没写进代码。"""
    srv = 跑起来的服务
    让开腿(srv)
    钟.前进(SUSPEND_STALE_MS - 60_000)
    assert not 等到(lambda: 挂起超时告警(srv), 最多等=1.5), "还差一分钟就报了"


def test_没让开腿就不会有这条(跑起来的服务, 钟):
    """任务正常跑着,钟拨多远都不该有这条。

    判据是「让开腿之后过了多久」,不是「这趟跑了多久」 —— 少了这条守卫,
    一个照 ``started_ms`` 算的实现会在长任务上凭空报 P1。
    """
    srv = 跑起来的服务
    钟.前进(SUSPEND_STALE_MS * 3)
    assert not 等到(lambda: 挂起超时告警(srv), 最多等=1.5), "没让开腿也报了挂起超时"


def test_一次挂起只报一条(跑起来的服务, 钟):
    """闸门半秒醒一拍。报重了的话十分钟就是一千两百条,值守屏当场没法看。"""
    srv = 跑起来的服务
    让开腿(srv)
    钟.前进(SUSPEND_STALE_MS + 1)
    assert 等到(lambda: 挂起超时告警(srv))

    起始拍 = srv.hub._lease_ticks
    assert 等到(lambda: srv.hub._lease_ticks >= 起始拍 + 3, 最多等=3.0), \
        "闸门没再醒过,下面那个 1 是因为它根本没机会报第二次"
    条 = 挂起超时告警(srv)
    assert len(条) == 1, 条
    assert 条[0].count == 1, 条[0]


def test_超时的边界正好在这个常量上():
    """纯判定,不碰服务。``SUSPEND_STALE_MS`` 那一刻还不算超时,再多一毫秒才算。"""
    起 = 1_757_000_000_000
    点 = SuspendPoint(waypoint_index=0, waypoint_name="P1_transformer",
                      pose=None, reason="人要接管", at_ms=起,
                      from_state=RunState.RUNNING)
    assert not _挂起超时了(点, now_ms=起)
    assert not _挂起超时了(点, now_ms=起 + SUSPEND_STALE_MS)
    assert _挂起超时了(点, now_ms=起 + SUSPEND_STALE_MS + 1)
    # 没让开腿就没有这个点。``None`` 不许当成「很久以前」。
    assert not _挂起超时了(None, now_ms=起 + SUSPEND_STALE_MS * 10)


class 假引擎:
    """摆好的引擎:``_判定挂起超时`` 只看 ``yielding`` 和 ``snapshot``。"""

    def __init__(self, snapshot: RunSnapshot) -> None:
        self.snapshot = snapshot
        self.yielding = snapshot.state is RunState.SUSPENDED


def _快照(*, 开跑: int, 让开腿于: int, 挂着: bool = True) -> RunSnapshot:
    点 = SuspendPoint(waypoint_index=0, waypoint_name="P1_transformer",
                      pose=None, reason="人要接管", at_ms=让开腿于,
                      from_state=RunState.RUNNING)
    return RunSnapshot(
        state=RunState.SUSPENDED if 挂着 else RunState.RUNNING,
        mission="巡检一号", waypoint_index=0,
        waypoint_name="P1_transformer", total=1, started_ms=开跑,
        suspended_at=点 if 挂着 else None)


def test_算的是让开腿那一刻不是开跑那一刻(没起的服务, 钟):
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
        ctx.engine = 假引擎(_快照(开跑=此刻 - SUSPEND_STALE_MS * 100,
                                  让开腿于=此刻))
        srv.hub._判定挂起超时(now_ms=此刻)
        assert not 挂起超时告警(srv), "刚让开腿就报了 —— 算的不是让开腿那一刻"

        ctx.engine = 假引擎(_快照(开跑=此刻 - SUSPEND_STALE_MS * 100,
                                  让开腿于=此刻 - SUSPEND_STALE_MS - 1))
        srv.hub._判定挂起超时(now_ms=此刻)
        assert 挂起超时告警(srv), "让开腿都超过十分钟了,还是没报"
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

    # 左手还压在摇杆上。
    assert _post(srv, "/api/run/resume")[0] == 200
    assert not 等到(lambda: ctx.engine.state is not RunState.SUSPENDED,
                    最多等=0.5), "人还握着摇杆,引擎就自己跑起来了"
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
