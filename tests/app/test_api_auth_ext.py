"""鉴权面的三条新路由,和扩写过的 /api/auth。"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.app.auth import (
    CHANNEL_LOCAL,
    MAX_LOCAL_SESSIONS,
    NONCE_TTL_S,
    PROOF_ALG,
    Denied,
    proof_for,
    token_ref,
)
from d1max_patrol.app.server import AppServer
from tests.app.conftest import get_err, get_json, request

PIN = "428913"


@pytest.fixture
def 有pin的服务(ctx):
    s = AppServer(ctx, port=0, pin=PIN)
    s.start()
    yield s
    s.stop()


def 解锁(server, **payload):
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload=payload)
    return code, json.loads(body)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ 解锁


def test_明文pin还是换得到token(有pin的服务):
    code, body = 解锁(有pin的服务, pin=PIN)
    assert code == 200
    assert len(body["token"]) > 20


def test_解锁响应带着记账免责声明(有pin的服务):
    _code, body = 解锁(有pin的服务, pin=PIN, operator="张三")
    assert body["operator"] == "张三"
    assert body["operator_verified"] is False
    assert "不核实" in body["notice"]


def test_本机解锁拿到的是能操作的凭证(有pin的服务):
    _code, body = 解锁(有pin的服务, pin=PIN)
    assert body["readonly"] is False
    assert body["channel"] == "local"
    assert body["idle_s"] == 12 * 3600.0


def test_不报名字也能解锁(有pin的服务):
    code, body = 解锁(有pin的服务, pin=PIN)
    assert code == 200
    assert body["operator"] == ""


def test_pin错了还是401(有pin的服务):
    code, _body = 解锁(有pin的服务, pin="000000")
    assert code == 401


def test_请求体不是对象就400(有pin的服务):
    code, body, _ = request(有pin的服务, "/api/auth", method="POST",
                            payload=[1, 2, 3])
    assert code == 400


# ------------------------------------------------------------------ 质询


def test_取质询不要token(有pin的服务):
    body = get_json(有pin的服务, "/api/auth/challenge")
    assert len(body["nonce"]) == 32
    assert body["ttl_s"] == NONCE_TTL_S
    assert body["alg"] == PROOF_ALG


def test_两次质询不一样(有pin的服务):
    a = get_json(有pin的服务, "/api/auth/challenge")["nonce"]
    b = get_json(有pin的服务, "/api/auth/challenge")["nonce"]
    assert a != b


def test_用证明换token(有pin的服务):
    nonce = get_json(有pin的服务, "/api/auth/challenge")["nonce"]
    code, body = 解锁(有pin的服务, nonce=nonce, proof=proof_for(PIN, nonce),
                      operator="张三")
    assert code == 200
    assert body["operator"] == "张三"
    assert body["readonly"] is False


def test_证明不对就401(有pin的服务):
    nonce = get_json(有pin的服务, "/api/auth/challenge")["nonce"]
    code, _body = 解锁(有pin的服务, nonce=nonce,
                       proof=proof_for("000000", nonce))
    assert code == 401


def test_同一个质询用两次不行(有pin的服务):
    nonce = get_json(有pin的服务, "/api/auth/challenge")["nonce"]
    proof = proof_for(PIN, nonce)
    assert 解锁(有pin的服务, nonce=nonce, proof=proof)[0] == 200
    assert 解锁(有pin的服务, nonce=nonce, proof=proof)[0] == 400


def test_pin从不出现在质询这条路上(有pin的服务):
    """§6.5 措施 2 的整条意义就在这一句断言上。"""
    nonce = get_json(有pin的服务, "/api/auth/challenge")["nonce"]
    proof = proof_for(PIN, nonce)
    assert PIN not in nonce
    assert PIN not in proof
    code, body, _ = request(有pin的服务, "/api/auth", method="POST",
                            payload={"nonce": nonce, "proof": proof})
    assert code == 200
    assert PIN not in body.decode("utf-8")


def test_没设pin的服务上质询接口说清楚(server):
    get_err(server, "/api/auth/challenge", 400)


# ------------------------------------------------------------------ 退出


def test_退出之后token就废了(有pin的服务):
    tok = 解锁(有pin的服务, pin=PIN)[1]["token"]
    code, body, _ = request(有pin的服务, "/api/auth/logout", method="POST",
                            headers=auth(tok))
    assert code == 200
    assert json.loads(body)["was_live"] is True
    assert get_err(有pin的服务, "/api/state", 401, headers=auth(tok))


def test_不带token退出是401(有pin的服务):
    code, _body, _h = request(有pin的服务, "/api/auth/logout", method="POST")
    assert code == 401


def test_退出会把租约一起还掉(有pin的服务):
    tok = 解锁(有pin的服务, pin=PIN, operator="张三")[1]["token"]
    sess = 有pin的服务.auth.session_of(tok)
    assert sess is not None
    now = 有pin的服务.ctx.clock()
    有pin的服务.control.book.acquire(sess.ref, sess.operator, now_ms=now)
    request(有pin的服务, "/api/auth/logout", method="POST", headers=auth(tok))
    assert 有pin的服务.control.book.state(now_ms=now).holder is None


def test_只读凭证也能退出把名额还回去(有pin的服务):
    """``/api/auth/logout`` 在 ``READONLY_OPEN_PATHS`` 里,热点(§6.5)上换来
    的只读 token 也必须能退出。**没有这条测试,将来谁把它从白名单里去掉,
    热点上的人就再也还不回名额了,而且不会有测试变红。**"""
    token = 有pin的服务.auth.unlock(PIN, "192.168.168.5", operator="张三")
    code, body, _ = request(有pin的服务, "/api/auth/logout", method="POST",
                            headers=auth(token))
    assert code == 200
    assert json.loads(body)["was_live"] is True


# ---------------------------------------------------------------- 看名额


def test_看得到谁占着名额(有pin的服务):
    甲 = 解锁(有pin的服务, pin=PIN, operator="张三")[1]["token"]
    解锁(有pin的服务, pin=PIN, operator="李四")
    body = get_json(有pin的服务, "/api/sessions", headers=auth(甲))
    assert body["max_sessions"] == 3
    assert {row["operator"] for row in body["sessions"]} == {"张三", "李四"}
    assert "不核实" in body["notice"]


def test_名额表分清本机和非本机(有pin的服务):
    """``max_sessions`` 只管非本机通道(见 ``Guard._issue``)。这条钉住
    ``GET /api/sessions`` 不会把两个分母(全量会话数、非本机会话数)混在
    一起显示成一个数 —— 屏幕上不许出现"6 条会话,上限 3"这种假象。"""
    甲 = 解锁(有pin的服务, pin=PIN, operator="张三")[1]["token"]
    解锁(有pin的服务, pin=PIN, operator="李四")
    body = get_json(有pin的服务, "/api/sessions", headers=auth(甲))
    assert body["remote_sessions"] == 0
    assert body["local_sessions"] == 2
    assert body["max_local_sessions"] == MAX_LOCAL_SESSIONS
    有pin的服务.auth.unlock(PIN, "192.168.1.55", operator="王五")
    body = get_json(有pin的服务, "/api/sessions", headers=auth(甲))
    assert body["remote_sessions"] == 1
    assert body["local_sessions"] == 2


def test_认得出哪个是自己(有pin的服务):
    甲 = 解锁(有pin的服务, pin=PIN, operator="张三")[1]["token"]
    解锁(有pin的服务, pin=PIN, operator="李四")
    rows = get_json(有pin的服务, "/api/sessions", headers=auth(甲))["sessions"]
    我的 = [r for r in rows if r["mine"]]
    assert len(我的) == 1
    assert 我的[0]["ref"] == token_ref(甲)


def test_名额表里没有token(有pin的服务):
    甲 = 解锁(有pin的服务, pin=PIN, operator="张三")[1]["token"]
    body = request(有pin的服务, "/api/sessions", headers=auth(甲))[1]
    assert 甲 not in body.decode("utf-8")


def test_每一行都写着姓名没被核实(有pin的服务):
    甲 = 解锁(有pin的服务, pin=PIN, operator="张三")[1]["token"]
    rows = get_json(有pin的服务, "/api/sessions", headers=auth(甲))["sessions"]
    assert all(row["operator_verified"] is False for row in rows)


def test_看名额要token(有pin的服务):
    get_err(有pin的服务, "/api/sessions", 401)


def test_第四个人被拒而且说得出原因(有pin的服务):
    """§3.6 的 3 个名额只管非本机通道。``request()`` 打的是回环,永远拿不到
    非本机地址,所以这条直接调 ``Guard.unlock`` 从三个不同的非本机地址占满
    名额 —— 三个不同地址、三个不同名字,呼应 ``test_control_wiring.py`` 里
    同样的手法。第四个不管换成哪个新地址都一样会被拒,因为挡的是"非本机
    这个池子满没满",不是"这个地址来过没有"。"""
    for addr, name in (("192.168.1.51", "张三"), ("192.168.1.52", "李四"),
                       ("192.168.1.53", "王五")):
        有pin的服务.auth.unlock(PIN, addr, operator=name)
    with pytest.raises(Denied) as exc:
        有pin的服务.auth.unlock(PIN, "192.168.1.54", operator="赵六")
    assert exc.value.status == 409
    assert "3 个人" in exc.value.error
    for name in ("张三", "李四", "王五"):
        assert name in exc.value.detail


def test_名额满了http那一侧也是409(有pin的服务):
    """上面那条钉的是 ``Guard.unlock`` 直接抛出的 ``Denied``;这条钉的是
    ``server._auth_unlock`` 把它转换成 HTTP 响应这一层。``request()`` 只会
    从回环打过来,所以占满的是**本机自己的** ``MAX_LOCAL_SESSIONS`` 个名额
    (不是非本机的 3 个) —— 第 ``MAX_LOCAL_SESSIONS + 1`` 个 ``POST
    /api/auth`` 必须变成 409,而且响应体(不是异常对象)里能看到原因。"""
    names = ["甲", "乙", "丙", "丁"]
    assert len(names) == MAX_LOCAL_SESSIONS
    for name in names:
        assert 解锁(有pin的服务, pin=PIN, operator=name)[0] == 200
    code, body = 解锁(有pin的服务, pin=PIN, operator="戊")
    assert code == 409
    assert "error" in body and "detail" in body
    for name in names:
        assert name in body["detail"]


def test_本机名额跟非本机各是各的池子第四个还是200(有pin的服务):
    """**这条把"本机免于 MAX_SESSIONS"这条设计钉在路由层。** 把非本机的 3
    个名额占满之后,回环上再开一个必须仍然 200 —— 回环不占那条无线上行
    (见 ``auth.py`` 里 ``MAX_SESSIONS`` 常量的注释),走的是自己的
    ``MAX_LOCAL_SESSIONS`` 池子,不受非本机名额是否占满影响。"""
    for addr, name in (("192.168.1.51", "张三"), ("192.168.1.52", "李四"),
                       ("192.168.1.53", "王五")):
        有pin的服务.auth.unlock(PIN, addr, operator=name)
    code, body = 解锁(有pin的服务, pin=PIN, operator="赵六")
    assert code == 200
    assert len(body["token"]) > 20
    assert body["channel"] == CHANNEL_LOCAL
