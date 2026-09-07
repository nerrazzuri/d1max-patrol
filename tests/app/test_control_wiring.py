"""控制权接进服务:请求带着会话走,快照里有 control 段,留痕上事件流。"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import pytest

from d1max_patrol.app.server import _TICK_S, AppServer
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
    time.sleep(_TICK_S + 0.3)
    snap2 = get_json(有pin的服务, "/api/state", headers=auth(tok))
    assert snap2["control"]["sessions"] == 1


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
    次接线漏掉的。所以这里真等一拍(``_TICK_S`` 一点余量),顺带把
    ``_tick`` 里 sweep → drain → _rebuild 那条链路也走一遍,这才是这个任务
    真正要交付的东西。
    """
    tok = 解锁(有pin的服务, "张三")
    sess = 有pin的服务.auth.session_of(tok)
    assert sess is not None
    有pin的服务.control.book.acquire(sess.ref, sess.operator, now_ms=墙钟.t)
    time.sleep(_TICK_S + 0.3)
    snap = get_json(有pin的服务, "/api/state", headers=auth(tok))
    assert snap["control"]["holder"] == {"ref": sess.ref, "operator": "张三"}


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
