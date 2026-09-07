"""控制权接进服务:请求带着会话走,快照里有 control 段,留痕上事件流。"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import pytest

from d1max_patrol.app.server import AppServer
from tests.app.conftest import get_json, make_ctx, request, sse

PIN = "428913"
T0 = 1_757_000_000_000


class 假墙钟:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def 墙钟():
    return 假墙钟()


@pytest.fixture
def 有pin的服务(bridge, tmp_path, 墙钟):
    ctx = make_ctx(bridge, tmp_path, clock=墙钟)
    s = AppServer(ctx, port=0, pin=PIN)
    s.start()
    yield s
    s.stop()


def 解锁(server, operator: str = "张三") -> str:
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload={"pin": PIN, "operator": operator})
    assert code == 200, body
    return json.loads(body)["token"]


def 等到(取值, 期望, *, 最多等=3.0):
    """一直读到快照重建过为止, 最多等这么久。

    **不写死 ``sleep(_TICK_S + 余量)``。** 那种写法的余量是拿机器负载赌的:
    这台机器上跑全量套件的时候, 一拍 0.5 秒的余量给 0.3 秒并不宽裕, 偶尔
    错过一拍就红一次, 而这种红没人复现得出来。这里改成"到点就走、不到就
    再看一眼", 常见情况下比写死的 sleep 还快, 负载高的时候也不会假红。

    等的是墙上时间, 不是被测代码里的时刻 —— ``_StateHub`` 那一拍是真的
    ``asyncio.sleep`` 在后台线程里跑, 没有可注入的钟能拨快它(§8.5 第 2 条
    管的是被测代码, 不是等一个真线程)。
    """
    截止 = time.monotonic() + 最多等
    while True:
        got = 取值()
        if got == 期望 or time.monotonic() >= 截止:
            return got
        time.sleep(0.02)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_服务上挂得到闸门和控制权台(有pin的服务):
    assert 有pin的服务.auth.enabled is True
    assert 有pin的服务.control.book is not None


def test_没设pin的服务也有控制权台(server):
    assert server.auth.enabled is False
    assert server.control.book.state(now_ms=T0).holder is None


def test_请求带着会话走(有pin的服务):
    from d1max_patrol.app.server import json_response

    tok = 解锁(有pin的服务, "张三")
    seen: list[object] = []

    def 处理(req):
        seen.append(req.session)
        return json_response({})

    有pin的服务.route("GET", "/api/试一下", 处理)
    # 请求行是纯 ASCII 的(http.client 强制 .encode('ascii')),中文路径
    # 得先在客户端这边转义;% 留白名单免得转义链路本身出问题。
    request(有pin的服务, quote("/api/试一下", safe="/.%"), headers=auth(tok))
    assert seen and seen[0] is not None
    assert seen[0].operator == "张三"


def test_没设pin时会话是空的(server):
    seen: list[object] = []
    from d1max_patrol.app.server import json_response

    def 处理(req):
        seen.append(req.session)
        return json_response({})

    server.route("GET", "/api/试一下", 处理)
    request(server, quote("/api/试一下", safe="/.%"))
    assert seen == [None]


def test_状态快照里有control段(有pin的服务):
    tok = 解锁(有pin的服务)
    snap = get_json(有pin的服务, "/api/state", headers=auth(tok))
    assert "control" in snap
    assert snap["control"]["holder"] is None
    assert snap["control"]["ttl_ms"] == 30_000
    assert snap["control"]["heartbeat_ms"] == 10_000
    assert snap["control"]["max_sessions"] == 3
    # 这一条会话是从 127.0.0.1(测试客户端)进来的 —— 回环无条件判
    # CHANNEL_LOCAL,而 ControlDesk.snapshot() 的 sessions 按设计只数非本机
    # 通道(要跟 max_sessions 用同一个分母,见 app/control.py 的 snapshot()
    # docstring 和 test_control_desk.py::test_快照的sessions不含本机)。本机
    # 不占那 3 个名额,所以这里是 0,不是 1。
    assert snap["control"]["sessions"] == 0

    # 换一个非本机地址解锁一次,这回真的算进去 —— 钉住"sessions 只数非本机"
    # 这条裁定本身,而不是只验证"回环不算数"这一半。
    有pin的服务.auth.unlock(PIN, "192.168.1.50", operator="李四")
    # /api/state 读的是缓存快照,新会话得等下一拍重建才看得见,原因同下面
    # test_持有租约之后快照里看得见是谁 的 docstring。
    got = 等到(lambda: get_json(有pin的服务, "/api/state",
                                headers=auth(tok))["control"]["sessions"], 1)
    assert got == 1


def test_没设pin时control段也在(server):
    snap = get_json(server, "/api/state")
    assert snap["control"]["holder"] is None
    assert snap["control"]["sessions"] == 0


def test_快照里没有token(有pin的服务):
    tok = 解锁(有pin的服务)
    body = json.dumps(get_json(有pin的服务, "/api/state", headers=auth(tok)))
    assert tok not in body


def test_持有租约之后快照里看得见是谁(有pin的服务, 墙钟):
    """``/api/state`` 读的是 ``_StateHub`` 的缓存快照,跟 ``links``/``procs``/
    ``caps`` 一样只在 tick(或导航/设备事件)时重建 —— 这是既有设计,不是这
    次接线漏掉的。所以这里真等一拍(见 ``等到``),顺带把
    ``_tick`` 里 sweep → drain → _rebuild 那条链路也走一遍,这才是这个任务
    真正要交付的东西。
    """
    tok = 解锁(有pin的服务, "张三")
    sess = 有pin的服务.auth.session_of(tok)
    assert sess is not None
    有pin的服务.control.book.acquire(sess.ref, sess.operator, now_ms=墙钟.t)
    想要 = {"ref": sess.ref, "operator": "张三"}
    got = 等到(lambda: get_json(有pin的服务, "/api/state",
                                headers=auth(tok))["control"]["holder"], 想要)
    assert got == 想要


def test_留痕会上事件流(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务, "张三")
    sess = 有pin的服务.auth.session_of(tok)
    assert sess is not None
    with sse(有pin的服务, f"/api/events?token={tok}") as frames:
        assert next(frames)["kind"] == "state"
        有pin的服务.control.book.acquire(sess.ref, sess.operator,
                                          now_ms=墙钟.t)
        控制帧 = None
        for frame in frames:
            if frame["kind"] == "control":
                控制帧 = frame
                break
        assert 控制帧 is not None
        assert 控制帧["event"]["kind"] == "acquired"
        assert 控制帧["event"]["operator"] == "张三"
        assert 控制帧["event"]["ref"] == sess.ref


def test_控制权台每拍都会结算(有pin的服务, 墙钟):
    """租约到期没有人会来通知,只能靠 tick 结算。0.5 秒一拍。

    这里直接摸 ``book.acquire()``(绕过还没接的 ``/api/control/acquire``,
    那是 Task 10 的路由),所以 "acquired" 这条留痕不会像走真路由那样立刻被
    drain 掉 —— 手动 drain 一次正是模拟那条路由将来会做的事,不是在迁就
    测试。
    """
    tok = 解锁(有pin的服务, "张三")
    sess = 有pin的服务.auth.session_of(tok)
    assert sess is not None
    有pin的服务.control.book.acquire(sess.ref, sess.operator, now_ms=墙钟.t)
    有pin的服务.control.drain()
    墙钟.t += 30_000
    with sse(有pin的服务, f"/api/events?token={tok}") as frames:
        for frame in frames:
            if frame["kind"] == "control":
                assert frame["event"]["kind"] == "expired"
                break


def test_token没了租约也会被收走(有pin的服务, 墙钟):
    """**TTL 一秒没走,租约照样得没**  —— 持有人的 token 被吊销了。

    这条测的是 ``ControlDesk.sweep()`` 独有的那一半:``LeaseBook.state()``
    自己会算 TTL 到期,所以光靠"等到期"根本分不出接的是 ``sweep()`` 还是
    ``book.state()``。只有"token 死了但 TTL 还没到"这种情形是
    ``keep_only(live_refs)`` 专属的 —— 而它恰好是现实里最常见的那种:人把
    app 杀了、退出了、或者闲置超时了,狗这边不该一直替他锁着控制权到 30 秒
    走完。

    墙钟一动不动,所以任何靠 TTL 蒙对的实现在这条上都会红。
    """
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    sess = 有pin的服务.auth.session_of(甲)
    assert sess is not None
    有pin的服务.control.book.acquire(sess.ref, sess.operator, now_ms=墙钟.t)

    def 看():
        return get_json(有pin的服务, "/api/state",
                        headers=auth(乙))["control"]["holder"]

    # 先确认它真的上过屏 —— 不然下面那个 None 可能只是"从来没出现过"。
    assert 等到(看, {"ref": sess.ref, "operator": "张三"}) is not None
    有pin的服务.auth.logout(甲)
    assert 等到(看, None) is None
