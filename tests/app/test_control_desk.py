"""控制权台: 租约绑在会话上, 哪几条接口要控制权。"""

from __future__ import annotations

import pytest

from d1max_patrol.app.auth import Denied, Guard
from d1max_patrol.app.control import ControlDesk, needs_lease
from d1max_patrol.engine.lease import LEASE_HEARTBEAT_MS, LEASE_TTL_MS

PIN = "428913"
T0 = 1_757_000_000_000


class 假墙钟:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


class 假单调钟:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def 起一台():
    单调 = 假单调钟()
    墙 = 假墙钟()
    g = Guard(PIN, clock=单调)
    return g, ControlDesk(g), 单调, 墙


# -------------------------------------------------------- 哪几条要控制权


def test_遥控要控制权():
    assert needs_lease("POST", "/api/teleop") is True
    assert needs_lease("POST", "/api/teleop/heartbeat") is True


def test_起飞和跑控要控制权():
    assert needs_lease("POST", "/api/missions/m1/run") is True
    assert needs_lease("POST", "/api/run/pause") is True
    assert needs_lease("POST", "/api/run/resume") is True
    assert needs_lease("POST", "/api/run/abort") is True


def test_换地图和改位姿要控制权():
    assert needs_lease("POST", "/api/maps/load") is True
    assert needs_lease("POST", "/api/pose/initial") is True
    assert needs_lease("POST", "/api/pose/reset") is True


def test_录包起停要控制权():
    assert needs_lease("POST", "/api/mapping/record/start") is True
    assert needs_lease("POST", "/api/mapping/record/stop") is True


def test_急停永远不要控制权():
    """§3.5 规则 2。这一条错了会死人。"""
    assert needs_lease("POST", "/api/estop") is False


def test_只读永远不要控制权():
    """§3.5 规则 1。"""
    for path in ("/api/state", "/api/events", "/api/runs", "/api/video/rgb",
                 "/api/missions", "/api/maps", "/api/schedule",
                 "/api/identity", "/api/selfcheck", "/api/storage"):
        assert needs_lease("GET", path) is False


def test_改任务定义不要控制权():
    """编任务是案头活, 常常是第二个人在改航点, 而第一个人在开狗。要控制权
    只会让两个人为了改一个数去抢方向盘。
    """
    assert needs_lease("PUT", "/api/missions/m1") is False
    assert needs_lease("PUT", "/api/maps/m1/home") is False


def test_数据和运维不要控制权():
    """导出、备份、清盘、升级都不改变"这只狗正在做什么"。升级尤其不能要
    —— 一次恢复性的回滚不该被一个已经掉线的会话挡住。
    """
    for path in ("/api/storage/sweep", "/api/exports", "/api/backup/sync",
                 "/api/backup/eject", "/api/release/install",
                 "/api/release/activate", "/api/release/rollback",
                 "/api/bundle/apply", "/api/bundle/rollback",
                 "/api/mapping/rebuild", "/api/runs/r1/judge"):
        assert needs_lease("POST", path) is False


def test_控制权自己那几条不要控制权():
    for path in ("/api/control/acquire", "/api/control/heartbeat",
                 "/api/control/release", "/api/control/takeover",
                 "/api/control/takeover/approve"):
        assert needs_lease("POST", path) is False


def test_方法不对就不算():
    assert needs_lease("GET", "/api/teleop") is False


def test_前缀像但不是的不算():
    assert needs_lease("POST", "/api/teleopXX") is False
    assert needs_lease("POST", "/api/run/pause/extra") is False


# ---------------------------------------------------------------- 收租


def test_解锁之后拿控制权拿得到():
    g, desk, _单调, 墙 = 起一台()
    tok = g.unlock(PIN, "10.0.0.5", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    assert desk.sweep(now_ms=墙.t).holder is not None


def test_token没了收租就把租约收走():
    g, desk, _单调, 墙 = 起一台()
    tok = g.unlock(PIN, "10.0.0.5", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    g.logout(tok)
    assert desk.sweep(now_ms=墙.t).holder is None


def test_token闲置到期租约也跟着走():
    g, desk, 单调, 墙 = 起一台()
    tok = g.unlock(PIN, "192.168.168.9", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    单调.t += 30 * 60.0 + 1.0
    assert desk.sweep(now_ms=墙.t).holder is None


def test_留痕一次只取新的():
    g, desk, _单调, 墙 = 起一台()
    sess = g.session_of(g.unlock(PIN, "10.0.0.5", operator="张三"))
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    assert [r.kind for r in desk.drain()] == ["acquired"]
    assert desk.drain() == ()
    desk.book.release(sess.ref, now_ms=墙.t)
    assert [r.kind for r in desk.drain()] == ["released"]


# ---------------------------------------------------------------- 快照


def test_没人持有时的快照是安静的():
    _g, desk, _单调, 墙 = 起一台()
    a = desk.snapshot(now_ms=墙.t)
    墙.t += 500
    assert desk.snapshot(now_ms=墙.t) == a


def test_快照带着上限和心跳周期():
    _g, desk, _单调, 墙 = 起一台()
    snap = desk.snapshot(now_ms=墙.t)
    assert snap["ttl_ms"] == LEASE_TTL_MS
    assert snap["heartbeat_ms"] == LEASE_HEARTBEAT_MS
    assert snap["max_sessions"] == 3
    assert snap["remote_sessions"] == 0


def test_快照数得出几个人连着():
    g, desk, _单调, 墙 = 起一台()
    g.unlock(PIN, "10.0.0.1", operator="张三")
    g.unlock(PIN, "10.0.0.2", operator="李四")
    assert desk.snapshot(now_ms=墙.t)["remote_sessions"] == 2


def test_快照里没有每拍都变的量():
    """``_StateHub`` 靠"跟上一份一样就不发"保持安静。放一个每拍都变的数
    进去, 这条 SSE 会每半秒响一次 —— 在热点上那是实打实的带宽。
    """
    g, desk, _单调, 墙 = 起一台()
    g.unlock(PIN, "10.0.0.1", operator="张三")
    a = desk.snapshot(now_ms=墙.t)
    墙.t += 500
    assert desk.snapshot(now_ms=墙.t) == a


def test_快照里没有token():
    g, desk, _单调, 墙 = 起一台()
    tok = g.unlock(PIN, "10.0.0.1", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    assert tok not in str(desk.snapshot(now_ms=墙.t))


def test_快照的sessions不含本机():
    """``Guard.sessions()`` 含本机(``CHANNEL_LOCAL``), 而 ``max_sessions``
    只算非本机通道。快照里这两个数必须是同一个分母, 不然屏幕上会出现
    "8 条会话, 上限 3"这种对不上的假象。
    """
    g, desk, _单调, 墙 = 起一台()
    g.unlock(PIN, "127.0.0.1", operator="本机张三")
    g.unlock(PIN, "10.0.0.1", operator="李四")
    snap = desk.snapshot(now_ms=墙.t)
    assert snap["remote_sessions"] == 1
    assert snap["max_sessions"] == 3


# ---------------------------------------------------------------- 拦截


def test_没有租约就动不了():
    g, desk, _单调, 墙 = 起一台()
    sess = g.session_of(g.unlock(PIN, "10.0.0.5", operator="张三"))
    with pytest.raises(Denied) as err:
        desk.require(sess, "POST", "/api/teleop", now_ms=墙.t)
    assert err.value.status == 409
    assert "先取控制权" in err.value.error


def test_有租约就放行():
    g, desk, _单调, 墙 = 起一台()
    sess = g.session_of(g.unlock(PIN, "10.0.0.5", operator="张三"))
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    desk.require(sess, "POST", "/api/teleop", now_ms=墙.t)


def test_别人持着租约时说得出是谁():
    g, desk, _单调, 墙 = 起一台()
    甲 = g.session_of(g.unlock(PIN, "10.0.0.1", operator="张三"))
    乙 = g.session_of(g.unlock(PIN, "10.0.0.2", operator="李四"))
    assert 甲 is not None
    desk.book.acquire(甲.ref, 甲.operator, now_ms=墙.t)
    with pytest.raises(Denied) as err:
        desk.require(乙, "POST", "/api/teleop", now_ms=墙.t)
    assert err.value.status == 409
    assert "张三" in err.value.error
    assert "takeover" in err.value.detail


def test_租约过期之后就动不了了():
    g, desk, _单调, 墙 = 起一台()
    sess = g.session_of(g.unlock(PIN, "10.0.0.5", operator="张三"))
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    墙.t += LEASE_TTL_MS
    with pytest.raises(Denied):
        desk.require(sess, "POST", "/api/teleop", now_ms=墙.t)


def test_不要控制权的接口一律放行():
    g, desk, _单调, 墙 = 起一台()
    sess = g.session_of(g.unlock(PIN, "10.0.0.5", operator="张三"))
    desk.require(sess, "POST", "/api/estop", now_ms=墙.t)
    desk.require(sess, "GET", "/api/state", now_ms=墙.t)


def test_没设pin的部署上这一层不生效():
    """没有 PIN 就没有会话, 没有会话就没有"谁"这个概念 —— 那种部署按定义
    只听本机(见 server.check_exposure), L1 仲裁没有对象。
    """
    _g, desk, _单调, 墙 = 起一台()
    desk.require(None, "POST", "/api/teleop", now_ms=墙.t)


def test_只读凭证碰不到控制权():
    """热点上用明文 PIN 换的是只读凭证(``Session.readonly``)。正常路径下
    ``Guard.gate`` 已经把它挡在写请求之外, 这里是纵深防御的第二道: 就算
    某条调用路径漏掉了 ``gate``, 只读凭证也不该靠 ``require`` 拿到写权限
    —— 哪怕它手上正好持着租约。
    """
    g, desk, _单调, 墙 = 起一台()
    # 192.168.168.0/24 是狗自己那块热点网段, 明文 PIN 在这条通道上只换只读。
    tok = g.unlock(PIN, "192.168.168.5", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    assert sess.readonly is True
    with pytest.raises(Denied) as err:
        desk.require(sess, "POST", "/api/teleop", now_ms=墙.t)
    assert err.value.status == 403


def test_只读凭证就算持着租约也一样挡():
    g, desk, _单调, 墙 = 起一台()
    tok = g.unlock(PIN, "192.168.168.5", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    desk.book.acquire(sess.ref, sess.operator, now_ms=墙.t)
    with pytest.raises(Denied) as err:
        desk.require(sess, "POST", "/api/teleop", now_ms=墙.t)
    assert err.value.status == 403
