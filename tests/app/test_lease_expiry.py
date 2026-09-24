"""租约到期 -> 就地停下 + 升 P1,**绝不自动续跑**(§5.8)。

``LeaseBook`` 是惰性结算的:没人问它,那 30 秒就"还没过去"。而 §5.8 要的
恰恰是**没人在的时候**它自己动作 —— 所以必须有一条协程主动去问。这一份
测的就是那条协程,以及它认"到期"的判据:**审计流水里的 ``expired``**,
而不是"holder 由有变无" —— 正常释放也是那个形状,靠 holder 分不开。

**遥控那一半为什么要换一个冻住的钟。** ``Teleop`` 自带守死人开关
(``HEARTBEAT_TIMEOUT_S`` = 0.6 秒,走的是真 ``time.monotonic``),不换钟
的话 ``active`` 会在半秒后自己掉下来 —— 那样"狗停下来了"这句断言就跟看门
狗没关系了,把 ``teleop.stop()`` 那一行删掉测试照样绿(docs/测试为什么会
说谎.md 的第一种)。冻住 ``Teleop`` 自己的钟之后,``active`` 只有一条路会
变成 ``False``:有人调了 ``stop()``。
"""

from __future__ import annotations

import contextlib
import itertools
import json
import time
from urllib.parse import quote

import pytest

from d1max_agent.engine.alerts import Level
from d1max_agent.engine.lease import LEASE_TTL_MS
from d1max_agent.engine.machine import RunState
from d1max_patrol.app.server import (
    _LEASE_WATCH_PERIOD_S,
    _WATER_EVERY,
    AppServer,
    _该量水位了,
)
from d1max_patrol.app.teleop import Teleop
from tests.app.conftest import make_ctx, request

PIN = "428913"
T0 = 1_757_000_000_000
走一拍 = {"fwd": 0.2, "lat": 0.0, "yaw": 0.0}

#: 轮询窗口的**唯一单位是看门狗自己的巡查周期**,这个文件里一个写死的秒数
#: 都不许有。
#:
#: 这条规矩 ``test_suspend_e2e.py`` 已经治过一遍(见那边的 ``观察窗口``),
#: 而这边的 ``等到`` 是同一份东西的第二份拷贝 —— 当时只修好了一份。写死的
#: ``3.0``/``1.0`` 今天正好是 6 拍/2 拍,纯属 ``_LEASE_WATCH_PERIOD_S`` 现在
#: 是 0.5;哪天有人把它调到 1.0 秒,这两个数就缩成 3 拍/1 拍,而 1 拍的窗口
#: 跟被观察者同长还踩着边界。表现是这一组开始**偶尔**红,而且是「第一次红被
#: 当成 flake 去调窗口」那一类。

#: 要等的是几拍。``test_suspend_e2e.py`` 里有个同名的第二份,**两边各改各的** ——
#: 那边等的是闸门协程醒几拍,这边等的是看门狗醒几拍,**不是同一件事**。
#: 今天两边都是 3,那是巧合不是约束:**这里只保证写法一致,不保证数值一致。**
#: 谁要是把两边绑成同一个常量,就是给两件不相干的事造了一份假的同一性,
#: 调其中一边会连坐另一边。(对面那份注释是同样的口径,别只读一侧。)
#:
#: **抽出来是因为下面那个 ``6`` 本来是裸的**:注释论证它「= 3 拍的两倍余量」,
#: 而 ``3`` 只活在注释里。下一个人把拍数改成 5,只会改到注释那一半,
#: 而两边**都不会红**。
拍数 = 3

#: 等一件**该发生**的事发生。``* 2`` 是 :data:`拍数` 拍的两倍余量 —— 看门狗
#: 跑在真线程上,拍与拍之间受机器负载和调度抖动影响,只给一倍就是把「刚好
#: 赶上」当合格线。
观察窗口 = _LEASE_WATCH_PERIOD_S * 拍数 * 2

#: 守一件**不该发生**的事不发生。2 拍 —— 这里要的不是余量而是「看门狗确实
#: 又醒过几拍、有过机会动手却没动」,窗口拉长只是让整套测试白等。
#:
#: 这个 ``2`` **不走** :data:`拍数`:它跟上面那个「要等几拍」不是同一件事,
#: 也不是它的倍数 —— 它自己就是「再看几拍」。挂到 :data:`拍数` 上去,
#: 下一个人调等待拍数会把守望窗口一起拖长,那是白等。
守望窗口 = _LEASE_WATCH_PERIOD_S * 2


class 假墙钟:
    """能拨的墙上钟。租约的每一条判定都读它(§8.5 第 2 条)。"""

    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def 前进(self, ms: int) -> None:
        self.t += ms


@pytest.fixture
def 墙钟() -> 假墙钟:
    return 假墙钟()


@pytest.fixture
def 服务器夹具(bridge, tmp_path, 墙钟):
    """一台有 PIN 的真服务,墙上钟可拨,遥控那一头的钟冻住(见模块开头)。"""
    ctx = make_ctx(bridge, tmp_path, clock=墙钟)
    # 换掉,别让守死人开关替看门狗背锅。服务是在这之后才建的,它读
    # ``ctx.teleop`` 又是每次请求现读(见 ``server._teleop_pulse``),所以
    # 换在这一刻是干净的。
    ctx.teleop = Teleop(ctx.device, ctx.engine, video_gate=lambda: "",
                        clock=lambda: 0.0)
    srv = AppServer(ctx, port=0, pin=PIN)
    srv.start()
    yield srv
    srv.stop()
    # 有用例真把任务起起来了。不收掉的话,引擎那条协程会活到桥停为止,
    # 而它中途还往归档目录里写 —— 写进一个 pytest 正在删的临时目录。
    with contextlib.suppress(Exception):
        bridge.call(ctx.engine.aclose, timeout_s=10.0)


# ------------------------------------------------------------------ 小工具


def 解锁(srv: AppServer, operator: str = "老王") -> str:
    code, body, _ = request(srv, "/api/auth", method="POST",
                            payload={"pin": PIN, "operator": operator})
    assert code == 200, body
    return json.loads(body)["token"]


def 打(srv: AppServer, path: str, token: str, payload=None):
    code, body, _ = request(srv, path, method="POST", payload=payload or {},
                            headers={"Authorization": f"Bearer {token}"})
    return code, json.loads(body)


def 拿到租约(srv: AppServer, operator: str = "老王") -> str:
    token = 解锁(srv, operator)
    code, body = 打(srv, "/api/control/acquire", token)
    assert code == 200, body
    return token


def 释放租约(srv: AppServer, token: str) -> None:
    code, body = 打(srv, "/api/control/release", token)
    assert code == 200, body


def 开始遥控(srv: AppServer, token: str) -> None:
    code, body = 打(srv, "/api/teleop", token, 走一拍)
    assert code == 200, body


def 等到(条件, *, 最多等: float = 观察窗口) -> bool:
    """轮到条件成立为止,**有超时上界**。

    等的是一条真的后台协程(它跑在桥那根线程上),没有可注入的钟能拨快它
    —— §8.5 第 2 条管的是被测代码里的时间,不管这种轮询。不写死
    ``sleep(周期 + 余量)``:那是拿机器负载赌,会产生没人复现得出来的假红。

    **窗口一律走 :data:`观察窗口` / :data:`守望窗口`,不收裸秒数。**
    跟 ``test_suspend_e2e.py`` 里那个是同一份东西、同一个理由 —— 那边的
    ``等到`` 也这么写,两份拷贝这次一起修好了(两份副本本身就是"这条不变量
    没有唯一落脚点"的表现,见本轮报告)。
    """
    截止 = time.monotonic() + 最多等
    while True:
        if 条件():
            return True
        if time.monotonic() >= 截止:
            return False
        time.sleep(0.01)


def 看门狗看过一眼(srv: AppServer) -> None:
    """等到看门狗至少醒过一拍、并且**看见过"有人握着"这个状态**。

    **不加这一句,``test_正常释放不报警`` 就是一条假测试。** 它要防的是"照
    holder 由有变无来判",而取租约和释放之间只隔一次 HTTP 往返 —— 远短于
    :data:`_LEASE_WATCH_PERIOD_S`。那样的话,一个照 holder 判的实现从头到尾
    只看到过"没人",它也不会报警,这条测试照样绿(实测过:不等这一下,把判
    据换成 holder 由有变无,这条不红)。

    等的是游标:看门狗每醒一拍都会把新留痕读进自己那份 ``_lease_cursor``,
    取租约那一条 ``acquired`` 被读走,就说明它在"有人握着"的那段时间里确实
    醒过。
    """
    assert 等到(lambda: srv.hub._lease_cursor > 0), "看门狗一拍都没醒"


def 到期告警(srv: AppServer) -> list:
    """簿子里的 ``lease_expired``。

    **按 kind 挑,不是整份比对。** 这台机器的盘水位可能本来就过 80%,那时
    候簿子里会多一条完全正确的 ``disk_80`` P2(见 ``AlertSources.on_tick``)
    —— 那是环境,不是这一组要测的事。

    **但"挑"会把守卫挖松:多报出来的第三种告警从此没人看得见。** 所以
    ``test_租约到期狗停下来并升P1`` 里另有一句整份比对
    (``... - {"disk_80"} == {"lease_expired"}``)—— 环境那一条按名字扣掉,
    剩下的必须一条不多。挑 kind 只用在"等它出现"和"数 count"的地方。
    """
    return [a for a in srv.alerts.open() if a.kind == "lease_expired"]


# ------------------------------------------------------------------ 正题


def test_租约到期狗停下来并升P1(服务器夹具, 墙钟):
    srv = 服务器夹具
    token = 拿到租约(srv, operator="老王")
    开始遥控(srv, token)
    assert srv.ctx.teleop.active is True, "遥控都没起来,下面那个 False 什么也不证明"

    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: srv.ctx.teleop.active is False), "TTL 过了,狗还在遥控档上"

    a = 到期告警(srv)
    assert a[0].level is Level.P1
    # 整份比对(见 ``到期告警`` 的 docstring):``disk_80`` 是这台机器的盘况,
    # 按名字扣掉;除此之外多出来任何一条,这里都得红。
    assert {x.kind for x in srv.alerts.open()} - {"disk_80"} == {"lease_expired"}


def test_租约到期绝不自动续跑(服务器夹具, 墙钟):
    """§5.8:最后已知状态是人正在接管,那它就不许自己动起来。

    "一只狗在最后已知状态是人正在接管的情况下自己动起来,是这套系统里最不
    该发生的事" —— 所以看门狗只停,不 ``resume``。
    """
    srv = 服务器夹具
    ctx = srv.ctx
    token = 拿到租约(srv, operator="老王")
    起飞(srv, token)
    assert 打(srv, "/api/run/suspend", token, {"reason": "人要接管"})[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))
    assert ctx.engine.state is RunState.SUSPENDED

    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: 到期告警(srv)), "TTL 过了,一条 P1 都没有"
    # **不是当场看一眼就完。** 告警那一拍和"续跑"那一拍不必是同一拍:看门狗
    # 先报警、下一拍才 resume,当场断言照样绿。所以往后再守几拍
    # (见 :data:`守望窗口` —— 按周期的倍数写,不写死秒数)。
    assert not 等到(lambda: ctx.engine.state is not RunState.SUSPENDED,
                   最多等=守望窗口), "它自己动起来了"


def test_到期只报一条不是每拍一条(服务器夹具, 墙钟):
    """看门狗每 :data:`_LEASE_WATCH_PERIOD_S` 秒醒一次,而事实只发生了一次。

    **断言 ``count``,不只断言条数。** 去重失效时,重复的那些会被 §5.4 的
    15 分钟聚合窗口收成同一条,``len()`` 照样是 1 —— 只有 ``count`` 说得出
    到底报了几次。
    """
    srv = 服务器夹具
    拿到租约(srv, operator="老王")
    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: 到期告警(srv))

    墙钟.前进(int(_LEASE_WATCH_PERIOD_S * 3000))
    assert not 等到(lambda: 到期告警(srv)[0].count > 1, 最多等=守望窗口), \
        "每醒一次就报一条 —— 一个根因把真要紧的那条埋了"
    assert 到期告警(srv)[0].count == 1


def test_正常释放不报警(服务器夹具):
    """人自己交回来是正常动作,报 P1 会让人学会无视 P1。

    **这一条最容易漏。** 看门狗认的是"上一拍还有人握着、这一拍没了",而正常
    释放也是这个形状 —— 只能靠审计流水里 ``released`` / ``expired`` 的区别
    分开,靠 ``holder`` 有无是分不开的。
    """
    srv = 服务器夹具
    token = 拿到租约(srv, operator="老王")
    看门狗看过一眼(srv)                    # 见这个函数自己的 docstring
    释放租约(srv, token)
    assert not 等到(lambda: 到期告警(srv), 最多等=守望窗口), \
        "人自己交回来的租约被报成了 P1"


def test_到期时有人在排队接管_不算没人管(服务器夹具, 墙钟):
    """到期的那一刻正好有人排着队,租约直接交给他 —— 狗没有失主。

    ``LeaseBook._settle`` 在这条路径上照样写一条 ``expired`` 审计(前任确实
    到期了),但接下来立刻 ``taken_over`` 给了排队的人。只认审计不看结果的
    话,一次**正常的接管**会被报成 P1,而 §5.8 说的是"人揣着手机走了"。
    """
    srv = 服务器夹具
    甲 = 拿到租约(srv, operator="老王")
    乙 = 解锁(srv, operator="小李")
    assert 打(srv, "/api/control/takeover", 乙)[0] == 200
    assert 甲                                  # 甲的 token 还活着,只是不再持有

    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: srv.control.book.state(now_ms=墙钟()).holder is not None
                and srv.control.book.state(now_ms=墙钟()).holder.operator == "小李")
    assert not 等到(lambda: 到期告警(srv), 最多等=守望窗口), \
        "换了个人接着开,不是没人管"


def test_接线_周期事实真的会被这条协程带出来(bridge, tmp_path, 墙钟):
    """三条 P2 周期事实(§5.2)挂在这条协程上,而不是挂在 ``_tick`` 上。

    **用 ``bundle_lag`` 来证,不用 ``disk_80``。** 后者读的是跑测试这台机器
    的真盘:开发机的盘本来就可能过了 80%,那样这条测试在"接线断了"的时候也
    是绿的 —— 它证的会是这台机器的盘况,不是接线。任务包这一条只看 tmp 目录
    里的两个空目录,判据完全在测试自己手里。
    """
    包 = tmp_path / "bundles"
    (包 / "site-kl-1").mkdir(parents=True)      # 落好了,current 一条链都没有
    ctx = make_ctx(bridge, tmp_path, clock=墙钟, bundles_root=包)
    srv = AppServer(ctx, port=0, pin=PIN)
    srv.start()
    try:
        assert 等到(lambda: [a for a in srv.alerts.open()
                            if a.kind == "bundle_lag"]), \
            "看门狗没把 on_tick 带上,盘上的滞后没人看得见"
    finally:
        srv.stop()


def test_看门狗是独立的一条协程(服务器夹具):
    """**不并进 ``_tick``**(§5.8)。

    ``_tick`` 自己的 docstring 写着"这一拍只管显示和事件流,闸门不靠它"——
    显示晚一拍没关系,停车不能晚,两条相反的容忍度不该落在同一条协程上。
    """
    名字 = {t.get_coro().__qualname__ for t in 服务器夹具.hub._tasks}
    assert "_StateHub._lease_watchdog" in 名字
    assert "_StateHub._tick" in 名字


def test_人揣着手机走了_遥控停下而且任务不自己接着跑(服务器夹具, 墙钟):
    """§5.8 那一幕的**整场**:接管中 -> 人走 -> TTL 过 -> 停腿,且不续跑。

    **为什么要合成一条。** 上面两条各测了一半:一条起飞前就到期(证遥控停了
    但引擎压根没在跑),一条挂起后不发遥控(证没续跑但没有腿在动)。真实的
    §5.8 是两半同时成立 —— 人接管着,腿在动,然后人走了。分开测有个漏得掉
    的形状:一个实现在 ``teleop.stop()`` 之后顺手把任务恢复了(常见的"接管
    结束就交还给任务"的写法),两条半场测试**都是绿的**。

    读过 ``app/teleop.py``:``stop()`` 只做三件事 —— 落 ``active``、撤看门
    狗、发一拍零速度,``_watchdog()`` 也一样,整个文件里没有任何
    resume/交还语义。所以这条测试现在是绿的;它钉的是**以后别加**。
    """
    srv = 服务器夹具
    ctx = srv.ctx
    token = 拿到租约(srv, operator="老王")
    起飞(srv, token)
    assert 打(srv, "/api/run/suspend", token, {"reason": "人要接管"})[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))

    开始遥控(srv, token)
    assert ctx.teleop.active is True, "遥控没起来,下面那个 False 什么也不证明"

    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: ctx.teleop.active is False), "TTL 过了,腿还在遥控档上"
    assert ctx.engine.state is RunState.SUSPENDED
    assert not 等到(lambda: ctx.engine.state is not RunState.SUSPENDED,
                   最多等=守望窗口), "停完腿之后把任务交还着跑起来了"


def test_到期告警不许在让开腿的时候承诺它会自己跑完(服务器夹具, 墙钟):
    """**第二种假话:反过来的那一种。**

    局面是"取控制权 → 起一趟 → 按暂停接管 → 人走开 → TTL 到期",跟上面那条
    ``test_人揣着手机走了_遥控停下而且任务不自己接着跑`` 摆的是同一格 ——
    那条已经钉死了:这一格里引擎**保证不会**自己恢复,谁不喂 resume 它就
    一直挂着。

    而 ``engine.running`` 在 ``SUSPENDED`` 上也是真的。所以只按 ``running``
    一位分文案的话,人在这一格会收到"该走点位的时候会自己接着走……不管它的
    话它会自己把这趟跑完"——**这是假话,而且是让人转身就走的那一种**:
    现场的人放心离开,这一趟巡检从此挂死在半道上,屏上只剩
    ``suspend_stale`` 隔一阵一条 P1 在刷。比"狗已停在原地"更难发现,因为
    没有人会去核一句"它会自己跑完"。

    **但反过来也不是"换一套新词"。** spec §5.8 写的"人接管完,手机往兜里
    一揣走了,TTL 到期 → 停在原地 + 升 P1。绝不自动续跑"**说的正是这一格**,
    那两句在这里是真话,必须留着。这一格要做的是 §5.8 + 一件它没写的事:
    这趟是"挂着"不是"结束了",不回来它就一直挂着,收尾得回来重新取一次
    控制权,再在"继续 / 中止"里选一个。

    **变异验证:** 把 ``_lease_gone`` 里 ``elif engine.yielding:`` 那一格的
    文案拍回下面 ``else`` 那一格(也就是只按 ``running`` 分两套的老样子),
    这一条当场红。
    """
    srv = 服务器夹具
    ctx = srv.ctx
    token = 拿到租约(srv, operator="老王")
    起飞(srv, token)
    assert 打(srv, "/api/run/suspend", token, {"reason": "人要接管"})[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))
    开始遥控(srv, token)
    # 前提断言,不是推算式:这两位同时为真才是这条测试要打的那一格。
    assert ctx.engine.running is True, "任务都没起来,下面断的是空的"
    assert ctx.engine.yielding is True, "腿没让开,这就不是要打的那一格了"

    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: 到期告警(srv)), "TTL 过了,一条 P1 都没有"
    a = 到期告警(srv)[0]
    话 = a.title + a.detail
    # 不许说"它会自己接着跑"。这两句是"引擎在跑、腿没让开"那一格的原话,
    # 落到这一格上就是假话 —— 变异回去的话正是它们冒出来。
    for 假话 in ("会自己接着走", "把这趟跑完"):
        assert 假话 not in 话, (
            f"腿已经让开了,这条告警却承诺「{假话}」—— 人会就这么走掉,"
            f"这一趟从此挂死。title={a.title!r} detail={a.detail!r}")
    # **spec §5.8 那两句承诺必须还在。** "人接管完,手机往兜里一揣走了,
    # TTL 到期" 写的就是这一格,它的结论是"停在原地 + 升 P1。绝不自动续跑"
    # —— 在这一格里这两句都是真话(teleop.stop() 发了零速,引擎挂在
    # SUSPENDED 上不发运动指令,链路上没有 resume)。钉住它们,是防止以后
    # 有人把这一格整个换成"任务还在跑"那一格的说法。
    assert "停在原地" in 话, f"§5.8 的「停在原地」丢了。title={a.title!r}"
    assert "没有自动续跑" in a.detail, f"§5.8 的「绝不自动续跑」丢了:{a.detail!r}"
    # 光把 §5.8 说全了还不够 —— 它没说"这趟还挂着"。得说出来:
    # 还挂着、要回来、回来之后二选一。
    assert "挂" in a.title, a.title
    assert "没跑完" in a.detail or "既没跑完" in a.detail, a.detail
    assert "中止" in a.detail, a.detail
    assert "继续" in a.detail, a.detail
    assert "控制权" in a.detail, a.detail
    # 在哪一档要摆出来,跟另外两格一个待遇。
    assert ctx.engine.snapshot.state.value in a.detail, a.detail


def test_到期告警不许在任务还在跑的时候说狗停了(服务器夹具, 墙钟):
    """**这条告警原来会说一句会伤人的假话。**

    ``_lease_gone`` 实际动作只有一句 ``teleop.stop()`` —— **只停遥控那一档,
    不停引擎正在跑的那趟任务**。而"取控制权 → 起一趟巡检 → 人走开 → TTL
    到期"是一串完全正常的动作(起飞本身就要控制权),现场随时会发生。那种
    局面下 ``teleop.stop()`` 基本是空动作:引擎照走点位、照拍照,而屏幕上和
    推送里那句"遥控租约到期,狗已停在原地"是假的 —— 现场的人据此判断狗是
    静止的,然后走过去。

    这里断的是:任务还在跑的时候,标题和正文里**不许**出现"停在原地"这种
    话,而且必须说清"这趟任务还没停"以及人该干什么。

    **变异验证:** 把 ``_lease_gone`` 里那个 ``if 跑着 ... else ...`` 拍回
    原来那一套单一文案,这一条当场红。
    """
    srv = 服务器夹具
    ctx = srv.ctx
    token = 拿到租约(srv, operator="老王")
    起飞(srv, token)
    assert ctx.engine.running is True, \
        "任务都没起来,下面断的「还在跑的时候怎么说」是空的"

    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: 到期告警(srv)), "TTL 过了,一条 P1 都没有"
    a = 到期告警(srv)[0]
    话 = a.title + a.detail
    # 钉的是原来那套单一文案的两句原话("狗已停在原地" / "已经停下"),
    # 不是泛泛地禁"停"这个字 —— 正文里要劝人"回来把它停下来",那也带"停"。
    for 假话 in ("狗已停在原地", "已经停下"):
        assert 假话 not in 话, (
            f"任务还在跑,这条告警却说「{假话}」—— 现场的人会照着它走过去。"
            f"title={a.title!r} detail={a.detail!r}")
    # 光不说假话不够:得说出真话。任务没停这件事必须出现在人看得到的地方。
    assert "没停" in a.title or "没有跟着停" in a.title, a.title
    assert "还活着" in a.detail or "没停" in a.detail, a.detail
    # 引擎在哪一档要摆出来:``running`` 为真里头还有 PAUSED / SUSPENDED,
    # 光说"任务还活着"分不出腿这一刻在不在动,得让人自己看得见。
    assert ctx.engine.snapshot.state.value in a.detail, a.detail
    # 人该干什么。
    assert "控制权" in a.detail, a.detail


def test_到期告警在没任务的时候还是说狗停在原地(服务器夹具, 墙钟):
    """另一半:引擎压根没在跑,原来那句话是对的,**不许被新分支冲掉**。

    只钉上面那一条的话,一个"两边都改成『任务还在跑』"的实现照样绿,而它在
    真机上的样子是:人只开了遥控、压根没起任务,租约到期之后屏上说"这趟任务
    还没停" —— 一条指着不存在的任务的告警,比说错还难查。
    """
    srv = 服务器夹具
    ctx = srv.ctx
    token = 拿到租约(srv, operator="老王")
    开始遥控(srv, token)
    assert ctx.engine.running is False, \
        "这条要的是「没有任务在跑」那一格,引擎却在跑"

    墙钟.前进(LEASE_TTL_MS + 1)
    assert 等到(lambda: 到期告警(srv)), "TTL 过了,一条 P1 都没有"
    a = 到期告警(srv)[0]
    assert "停在原地" in a.title, a.title
    assert "没有自动续跑" in a.detail, a.detail


def test_闸门协程死了_这件事本身会被报出来(服务器夹具):
    """看门狗的兜底只网 ``OSError`` / ``ValueError``,别的会静悄悄弄死它。

    **不去拓宽 catch**(``except Exception`` 过不了 ruff 的 BLE,而且真要网
    住 ``KeyError`` 也不对:告警注册表少一条 kind 是代码错,应该炸出来)。
    要补的是另一件事:它死了得有人知道 —— 否则 ``self._tasks`` 要等到
    ``stop()`` 才被 await,一台服务可以顶着一条死掉的闸门跑一整天,而 §5.8
    的"人走了狗自己停"从此不成立,屏上还什么都不显示。
    """
    srv = 服务器夹具

    def 炸(*a, **k):
        raise KeyError("这一拍撞上了一个没注册的 kind")

    srv.hub._lease_once = 炸                 # type: ignore[method-assign]
    assert 等到(lambda: [a for a in srv.alerts.open()
                        if a.kind == "watchdog_died"]), "闸门死了,没人吭一声"
    a = [x for x in srv.alerts.open() if x.kind == "watchdog_died"][0]
    assert a.level is Level.P1
    assert "KeyError" in a.detail, "报了,但没说是什么弄死的"


def test_水位不是每拍都量(服务器夹具):
    """闸门 2 Hz 是为了"人走了赶紧停腿";水位那三条不该跟着 2 Hz 走。

    它们里头有 ``shutil.disk_usage``、一次目录遍历、一次 ``landed.json``
    读盘 —— **同步 I/O,而且就在 asyncio 那根线程上**。盘水位是分钟到小时
    尺度的事实,2 Hz 去问它既拖 loop,又在盘被拔掉时按 2 Hz 刷日志。
    """
    srv = 服务器夹具
    # 先让第 1 拍过去 —— 那一拍是**该**量的(见 ``_该量水位了`` 的起点),
    # 装在它前面的话数出来的 1 次是对的,这条测试会变成一条假红。
    assert 等到(lambda: srv.hub._lease_ticks >= 1), "看门狗一拍都没醒"
    次数 = itertools.count()
    srv.hub._alerts.on_tick = lambda: next(次数)   # type: ignore[method-assign]
    起 = srv.hub._lease_ticks
    assert 等到(lambda: srv.hub._lease_ticks >= 起 + 4), "看门狗不接着醒了"
    assert next(次数) == 0, "水位还是每拍都量"


def test_水位分频_第一拍就量_之后六十拍一次():
    """分频的两头都要钉:**起点**和**间隔**。

    起点:服务刚起来那一刻,盘满/包没生效/钟偏通常**已经**成立了。要是写成
    "第 60 拍才第一次量",值守屏开机头 30 秒会显示一句它并不知道的"没事"。
    间隔:600 拍(5 分钟)里正好 ``600 // _WATER_EVERY`` 次。
    """
    assert _该量水位了(1) is True
    命中 = [拍 for 拍 in range(1, 601) if _该量水位了(拍)]
    assert len(命中) == 600 // _WATER_EVERY
    assert 命中[0] == 1
    assert 命中[1] - 命中[0] == _WATER_EVERY


# ------------------------------------------------------------------ 起飞


def _mission() -> dict:
    """一份能过 ``parse_mission`` 的最小任务(跟 test_run_suspend 同款)。"""
    return {
        "mission": "巡检一号",
        "map_id": "map_test",
        "waypoints": [{
            "name": "P1_transformer",
            "pose": {"position": {"x": 1.0, "y": 0.0, "z": 0.0},
                     "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
            "check": "配电柜门是否关闭",
            "actions": [{"type": "dwell", "seconds": 0.1}],
        }],
    }


def 起飞(srv: AppServer, token: str) -> None:
    """存一份任务、起飞、等到真在跑。假导航的 ``goto`` 只记不到,所以它会

    一直停在"走着"上 —— 正是现场要接管的那一刻。
    """
    code, body, _ = request(srv, "/api/missions/" + quote("巡检一号"),
                            method="PUT", payload=_mission(),
                            headers={"Authorization": f"Bearer {token}"})
    assert code == 200, body
    assert 打(srv, "/api/missions/" + quote("巡检一号") + "/run", token)[0] == 200
    srv.ctx.bridge.call(lambda: srv.ctx.engine.wait_state(RunState.RUNNING))
