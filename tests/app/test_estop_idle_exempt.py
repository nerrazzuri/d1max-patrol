"""急停豁免闲置期:凭证闲太久了照样按得停这条狗,但身份一样也不放松。

第 8 卷值守那一段的现场形状是这样的:人在狗的热点上输一次 PIN 换到只读凭证,
手机架在值守屏上盯着狗跑;热点凭证的闲置期只有 30 分钟(``AP_TOKEN_IDLE_S``),
而值守屏不轮询、SSE 长连也只在建连那一刻过一次闸。半小时后人抓起手机按急停 ——
401「登录过期了,重新输一次 PIN」。

这个文件钉的是那条修法的两头:

* **松的那头** —— ``/api/estop`` 豁免闲置期(``IDLE_EXEMPT_PATHS``);
* **紧的那头** —— 豁免只豁免"多久没用了"。编出来的、被吊销过的、过了绝对期
  (``TOKEN_ABS_S``)的 token 打急停照样 401,而闲置过期打**别的**路由也照样
  401。**急停没有被放进 ``OPEN_PATHS``** —— 那等于同一个热点上任何人都能按停
  这条狗,是一次要产品主拍板的安全权衡,不是这一轮能顺手做掉的事。

另外还有 SSE 长连那一半:长连上有流量就算一次活动,刷闲置计时、但推不动绝对期。
"""

from __future__ import annotations

import json
import time

import pytest

from d1max_patrol.app.auth import (
    AP_TOKEN_IDLE_S,
    CHANNEL_AP,
    IDLE_EXEMPT_PATHS,
    OPEN_PATHS,
    TOKEN_ABS_S,
    TOKEN_IDLE_S,
    Denied,
    Guard,
    TokenStore,
)
from d1max_patrol.app.server import AppServer
from tests.app.conftest import make_ctx, request, sse, status

PIN = "428913"


class 假单调钟:
    """可注入的单调钟(秒)。拨快它就等于"过了这么久",不用真等。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def 钟():
    return 假单调钟()


@pytest.fixture
def 有pin的服务(bridge, tmp_path, 钟):
    ctx = make_ctx(bridge, tmp_path)
    s = AppServer(ctx, port=0, pin=PIN, auth_clock=钟)
    s.start()
    yield s
    s.stop()


def 解锁(server) -> str:
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload={"pin": PIN, "operator": "值班"})
    assert code == 200, body
    return json.loads(body)["token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def 按急停(server, token: str) -> int:
    return status(server, "/api/estop", method="POST", payload={},
                  headers=auth(token))


def 闲了多久(server, ref: str) -> float:
    """这个会话在鉴权钟上闲了多久。

    读的是 ``sessions()`` —— 它**不续期**(注释就写在 ``TokenStore.sessions``
    上),所以问它不会把要测的那个闲置计时自己抹掉。
    """
    for s in server.auth.sessions():
        if s.ref == ref:
            return s.idle_for_s
    raise AssertionError(f"会话 {ref} 已经不在了")


# ------------------------------------------------- 边界一:别的路由不许被放松


def test_闲置过期的凭证打别的路由仍然401(有pin的服务, 钟):
    """急停那条豁免**不许**顺手把整套闲置判定放松掉。

    这条是上面那条豁免的对照组:同一张凭证、同一个时刻,换一条路由就该被拦下。
    它一红就说明豁免漏到了不该漏的地方。
    """
    tok = 解锁(有pin的服务)
    钟.t += TOKEN_IDLE_S + 1.0
    assert status(有pin的服务, "/api/state", headers=auth(tok)) == 401


# ------------------------------------------------- 边界二:豁免的是闲置不是身份


def test_吊销过的凭证按不动急停(有pin的服务, 钟):
    """豁免的是"多久没用了", 不是"你是谁"。

    退出登录(``/api/auth/logout``)把这张凭证从表里删了 —— 它不再是一张"真被
    签发过、还认得出来"的凭证, 急停那条旁路认不出它, 也不该认出它。
    """
    tok = 解锁(有pin的服务)
    assert status(有pin的服务, "/api/auth/logout", method="POST", payload={},
                  headers=auth(tok)) == 200
    钟.t += TOKEN_IDLE_S + 1.0
    assert 按急停(有pin的服务, tok) == 401


# ------------------------------------------------- 边界三:绝对期是豁免的天花板


def test_过了绝对期的凭证按不动急停(有pin的服务, 钟):
    """绝对期推不掉, 它是那条豁免唯一的天花板。

    这个时刻上闲置期早就过了 —— 而闲置期是豁免掉的, 所以还能把这一下拦住的
    **只可能是绝对期**。没有它, "豁免闲置"就等于"这张凭证永远按得停这条狗"。
    """
    tok = 解锁(有pin的服务)
    钟.t += TOKEN_ABS_S + 1.0
    assert 按急停(有pin的服务, tok) == 401


# ------------------------------------------------- 边界四:闲置过期照样按得动


def test_闲置过期但别的都有效的凭证按得动急停(有pin的服务, 钟):
    """现场那一下:手机在值守屏上放了半小时以上, 回来按急停要按得响。"""
    tok = 解锁(有pin的服务)
    钟.t += TOKEN_IDLE_S + 1.0
    assert 按急停(有pin的服务, tok) == 200


# ------------------------------------------------- 边界五:长连上有流量算活动


def test_长连上有流量之后同一个凭证打别的路由不再因闲置被拒(有pin的服务, 钟):
    """值守屏挂着的时候人确实在看, 把它判成闲置本身就是错的。

    时间线:建连(0 时)→ 拨到 11 小时、推一帧(刷闲置计时)→ 再拨 6 小时。
    距离建连一共 17 小时, 早过了 12 小时的闲置期;距离最后一帧只有 6 小时。
    没有那次刷新, 最后一行就该是 401。
    """
    tok = 解锁(有pin的服务)
    sess = 有pin的服务.auth.session_of(tok)
    assert sess is not None
    with sse(有pin的服务, f"/api/events?token={tok}") as frames:
        assert next(frames)["kind"] == "state"
        钟.t += 11 * 3600.0
        # 推一帧出来。租约留痕会上事件流, 控制权段一变快照也跟着重建 ——
        # 一次 acquire 至少换得到一帧。
        有pin的服务.control.book.acquire(sess.ref, sess.operator,
                                         now_ms=有pin的服务.ctx.clock())
        截止 = time.monotonic() + 5.0
        while 闲了多久(有pin的服务, sess.ref) > 1.0:
            assert time.monotonic() < 截止, "长连上有流量, 闲置计时却没被刷"
            next(frames)
    钟.t += 6 * 3600.0
    assert status(有pin的服务, "/api/state", headers=auth(tok)) == 200


# ------------------------------------------------------------ 豁免不许越界


def test_急停不在不要token的那张表里():
    """**这条是产品边界, 不是实现细节。**

    把 ``/api/estop`` 放进 ``OPEN_PATHS`` 等于同一个热点上任何人都能按停这条
    狗 —— 不要凭证, 也不留任何指向按的人的痕迹。那是一次安全权衡, 得由产品主
    拍板。这个文件拿到的是中间解:豁免闲置期, 但身份照要。
    """
    assert "/api/estop" not in OPEN_PATHS
    assert IDLE_EXEMPT_PATHS == frozenset({"/api/estop"})


def test_编出来的token按不动急停(有pin的服务):
    """豁免的前提是"这个 token 真被签发过"。凭空编一个进不来。"""
    # 编的 token 用 ASCII —— 请求头是 latin-1 编的, 中文塞不进去。
    assert 按急停(有pin的服务, "not-a-real-token") == 401


def test_按过急停不会把凭证在别的路由上复活(有pin的服务, 钟):
    """急停那条旁路**不续期**。

    按一下急停就把一张早该歇的凭证重新变成通行证, 等于让豁免从"急停一条"漏
    成"任何一条路由, 先按一下急停就行"。
    """
    tok = 解锁(有pin的服务)
    钟.t += TOKEN_IDLE_S + 1.0
    assert 按急停(有pin的服务, tok) == 200
    assert status(有pin的服务, "/api/state", headers=auth(tok)) == 401


def test_热点上的只读凭证闲置半小时后照样按得动急停():
    """现场原版:热点上换来的只读凭证, 闲置期 30 分钟。

    服务是绑回环起的, 打进去的请求一律算 ``local`` 通道 —— 热点那条通道
    (闲置期短得多、而且只发只读凭证)在真服务上摆不出来, 所以这一条直接对着
    闸门本身摆。
    """
    钟 = 假单调钟()
    guard = Guard(PIN, clock=钟)
    tok = guard.tokens.issue(now=钟.t, operator="值班", channel=CHANNEL_AP,
                             readonly=True, idle_s=AP_TOKEN_IDLE_S)
    头 = {"host": "192.168.168.100:8095", "authorization": f"Bearer {tok}"}
    钟.t += AP_TOKEN_IDLE_S + 60.0
    with pytest.raises(Denied):
        guard.gate("GET", "/api/state", 头, {})
    sess = guard.gate("POST", "/api/estop", 头, {})
    assert sess is not None
    assert sess.channel == CHANNEL_AP and sess.readonly is True


# ------------------------------------------------------------ token 表这一层


def test_绝对期跟闲置期是两条独立的判据():
    """一直在用也逃不过绝对期;闲置期过了绝对期没到, 记录还认得出身份。"""
    s = TokenStore(idle_s=100.0, abs_s=1000.0)
    t = s.issue(now=0.0)
    for now in (50.0, 140.0, 230.0, 320.0):     # 一直在用, 闲置期推得掉
        assert s.info(t, now=now) is not None
    assert s.info(t, now=1001.0) is None        # 绝对期推不掉
    assert s.info_idle_exempt(t, now=1001.0) is None


def test_闲置过期的记录对外算死的但急停那条门认得出():
    s = TokenStore(idle_s=100.0, abs_s=1000.0)
    t = s.issue(now=0.0)
    assert s.info(t, now=101.0) is None
    assert s.count == 0                          # 名额照常腾出来
    assert s.live_refs(now=101.0) == frozenset()
    sess = s.info_idle_exempt(t, now=101.0)
    assert sess is not None and sess.idle_for_s == pytest.approx(101.0)


def test_长连上的活动刷得动闲置期刷不动绝对期():
    s = TokenStore(idle_s=100.0, abs_s=1000.0)
    t = s.issue(now=0.0)
    ref = s.info(t, now=0.0).ref                 # type: ignore[union-attr]
    for now in (90.0, 180.0, 270.0, 360.0):      # 每 90 秒一帧, 一直活着
        assert s.touch_ref(ref, now=now)
        assert s.info(t, now=now) is not None
    assert s.info(t, now=1001.0) is None         # 绝对期照样到点
    assert not s.touch_ref(ref, now=1001.0)


def test_已经闲置过期的刷不活():
    """复活一张死凭证要重新解锁, 不能靠一帧迟到的事件。"""
    s = TokenStore(idle_s=100.0, abs_s=1000.0)
    t = s.issue(now=0.0)
    ref = s.info(t, now=0.0).ref                 # type: ignore[union-attr]
    assert not s.touch_ref(ref, now=200.0)
    assert s.info(t, now=200.0) is None
