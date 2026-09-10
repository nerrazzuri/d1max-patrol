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
import json
import time
from urllib.parse import quote

import pytest

from d1max_patrol.app.server import _LEASE_WATCH_PERIOD_S, AppServer
from d1max_patrol.app.teleop import Teleop
from d1max_patrol.engine.alerts import Level
from d1max_patrol.engine.lease import LEASE_TTL_MS
from d1max_patrol.engine.machine import RunState
from tests.app.conftest import make_ctx, request

PIN = "428913"
T0 = 1_757_000_000_000
走一拍 = {"fwd": 0.2, "lat": 0.0, "yaw": 0.0}


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


def 等到(条件, *, 最多等: float = 3.0) -> bool:
    """轮到条件成立为止,**有超时上界**。

    等的是一条真的后台协程(它跑在桥那根线程上),没有可注入的钟能拨快它
    —— §8.5 第 2 条管的是被测代码里的时间,不管这种轮询。不写死
    ``sleep(周期 + 余量)``:那是拿机器负载赌,会产生没人复现得出来的假红。
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
    assert [x.kind for x in a] == ["lease_expired"]
    assert a[0].level is Level.P1


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
    assert ctx.engine.state is RunState.SUSPENDED     # 没有偷偷 resume


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
    assert not 等到(lambda: 到期告警(srv)[0].count > 1, 最多等=1.0), \
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
    assert not 等到(lambda: 到期告警(srv), 最多等=1.0), \
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
    assert not 等到(lambda: 到期告警(srv), 最多等=1.0), \
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
