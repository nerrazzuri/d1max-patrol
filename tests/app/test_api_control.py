"""控制权那七条路由。"""

from __future__ import annotations

import json
import time

import pytest

from d1max_patrol.app.auth import proof_for
from d1max_patrol.app.server import AppServer
from d1max_patrol.engine.lease import AUDIT_MAX, TAKEOVER_GRACE_MS
from tests.app.conftest import get_err, get_json, make_ctx, request

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


def 打(server, path, token, payload=None):
    code, body, _ = request(server, path, method="POST",
                            payload=payload or {}, headers=auth(token))
    return code, json.loads(body)


def 等到(取值, 期望, *, 最多等=3.0):
    """一直读到期望的值出现为止, 最多等这么久。

    照 ``tests/app/test_control_wiring.py`` 的 ``等到`` 写:不写死
    ``sleep(固定余量)`` —— 那是拿机器负载赌, 会产生没人复现得出来的假红。
    这里"到点就走、不到就再看一眼", §8.5 第 2 条管的是被测代码里的时间,
    不管这种等一个真后台线程的轮询。
    """
    截止 = time.monotonic() + 最多等
    while True:
        got = 取值()
        if got == 期望 or time.monotonic() >= 截止:
            return got
        time.sleep(0.02)


# ---------------------------------------------------------------- 看状态


def test_一开始没人持有(有pin的服务):
    tok = 解锁(有pin的服务)
    body = get_json(有pin的服务, "/api/control", headers=auth(tok))
    assert body["holder"] is None
    assert body["mine"] is False
    assert body["ttl_ms"] == 30_000
    assert body["heartbeat_ms"] == 10_000
    assert body["max_sessions"] == 3
    assert "不核实" in body["notice"]


def test_看控制权要token(有pin的服务):
    get_err(有pin的服务, "/api/control", 401)


# ------------------------------------------------------------------ 取还


def test_取到之后是自己的(有pin的服务):
    tok = 解锁(有pin的服务, "张三")
    code, body = 打(有pin的服务, "/api/control/acquire", tok)
    assert code == 200
    assert body["holder"]["operator"] == "张三"
    assert body["mine"] is True
    assert body["expires_ms"] == T0 + 30_000


def test_再取一次是续不是错(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务)
    打(有pin的服务, "/api/control/acquire", tok)
    墙钟.t += 5_000
    code, body = 打(有pin的服务, "/api/control/acquire", tok)
    assert code == 200
    assert body["expires_ms"] == 墙钟.t + 30_000


def test_别人拿着就409而且说得出是谁(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    code, body = 打(有pin的服务, "/api/control/acquire", 乙)
    assert code == 409
    assert "张三" in body["error"]


def test_在别人眼里mine是假的(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    assert get_json(有pin的服务, "/api/control",
                    headers=auth(乙))["mine"] is False


def test_心跳把到期推后(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务)
    打(有pin的服务, "/api/control/acquire", tok)
    墙钟.t += 9_000
    code, body = 打(有pin的服务, "/api/control/heartbeat", tok)
    assert code == 200
    assert body["expires_ms"] == 墙钟.t + 30_000


def test_不是持有者心跳不了(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    code, body = 打(有pin的服务, "/api/control/heartbeat", 乙)
    assert code == 409
    assert "不是持有者" in body["error"]


def test_到点没心跳就自己过期(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务)
    打(有pin的服务, "/api/control/acquire", tok)
    墙钟.t += 30_000
    assert get_json(有pin的服务, "/api/control",
                    headers=auth(tok))["holder"] is None


def test_交回之后没人持有(有pin的服务):
    tok = 解锁(有pin的服务)
    打(有pin的服务, "/api/control/acquire", tok)
    code, body = 打(有pin的服务, "/api/control/release", tok)
    assert code == 200
    assert body["holder"] is None


def test_没拿着也能交回(有pin的服务):
    """app 退出时无脑发一次,不该因为租约刚过期就收到一个错。"""
    tok = 解锁(有pin的服务)
    assert 打(有pin的服务, "/api/control/release", tok)[0] == 200


# ------------------------------------------------------------------ 接管


def test_请求接管不立刻夺权(有pin的服务, 墙钟):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    code, body = 打(有pin的服务, "/api/control/takeover", 乙)
    assert code == 200
    assert body["holder"]["operator"] == "张三"
    assert body["challenger"]["operator"] == "李四"
    assert body["grace_ends_ms"] == 墙钟.t + TAKEOVER_GRACE_MS


def test_持有者同意就立刻移交(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    打(有pin的服务, "/api/control/takeover", 乙)
    code, body = 打(有pin的服务, "/api/control/takeover/approve", 甲)
    assert code == 200
    assert body["holder"]["operator"] == "李四"
    assert body["challenger"] is None


def test_只有持有者能同意(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    打(有pin的服务, "/api/control/takeover", 乙)
    assert 打(有pin的服务, "/api/control/takeover/approve", 乙)[0] == 409


def test_没人请求时同意是400(有pin的服务):
    tok = 解锁(有pin的服务)
    打(有pin的服务, "/api/control/acquire", tok)
    assert 打(有pin的服务, "/api/control/takeover/approve", tok)[0] == 400


def test_宽限期到了自动移交(有pin的服务, 墙钟):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    打(有pin的服务, "/api/control/takeover", 乙)
    墙钟.t += TAKEOVER_GRACE_MS
    assert get_json(有pin的服务, "/api/control",
                    headers=auth(乙))["holder"]["operator"] == "李四"


def test_强制接管立刻夺权(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    code, body = 打(有pin的服务, "/api/control/takeover", 乙,
                    {"force": True, "reason": "现场有人要摔了"})
    assert code == 200
    assert body["holder"]["operator"] == "李四"


def test_强制接管不写理由就400(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    code, body = 打(有pin的服务, "/api/control/takeover", 乙, {"force": True})
    assert code == 400
    assert "理由" in body["error"]


# ------------------------------------------------------------------ 留痕


def test_留痕翻得出来(有pin的服务):
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    打(有pin的服务, "/api/control/release", tok)
    body = get_json(有pin的服务, "/api/control/audit", headers=auth(tok))
    assert [r["kind"] for r in body["audit"]] == ["acquired", "released"]
    assert body["audit"][0]["operator"] == "张三"
    assert body["max"] == AUDIT_MAX


def test_强制接管的理由留在痕里(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    打(有pin的服务, "/api/control/acquire", 甲)
    打(有pin的服务, "/api/control/takeover", 乙,
       {"force": True, "reason": "现场有人要摔了"})
    痕 = get_json(有pin的服务, "/api/control/audit", headers=auth(甲))["audit"]
    强制 = [r for r in 痕 if r["kind"] == "forced"]
    assert len(强制) == 1
    assert "现场有人要摔了" in 强制[0]["detail"]


def test_留痕里没有token(有pin的服务):
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    body = request(有pin的服务, "/api/control/audit", headers=auth(tok))[1]
    assert tok not in body.decode("utf-8")


# -------------------------------------------------------------- 边角情形


def test_没设pin的部署上取控制权说得清楚(server):
    code, body, _ = request(server, "/api/control/acquire", method="POST",
                            payload={})
    assert code == 400
    assert "没设 PIN" in json.loads(body)["error"]


def test_没设pin的部署上看控制权还是能看(server):
    assert get_json(server, "/api/control")["holder"] is None


def test_只读凭证取不了控制权(bridge, tmp_path, 墙钟):
    """§6.5:热点上用明文 PIN 换来的凭证只能看。"""
    ctx = make_ctx(bridge, tmp_path, clock=墙钟)
    s = AppServer(ctx, port=0, pin=PIN)
    s.start()
    try:
        tok = s.auth._issue("192.168.168.9", "张三", readonly=True,
                            now=s.auth._clock())
        code, body, _ = request(s, "/api/control/acquire", method="POST",
                                payload={}, headers=auth(tok))
        assert code == 403
        assert "只能看" in json.loads(body)["error"]
    finally:
        s.stop()


def test_质询换来的凭证取得了控制权(有pin的服务):
    nonce = get_json(有pin的服务, "/api/auth/challenge")["nonce"]
    code, body, _ = request(有pin的服务, "/api/auth", method="POST",
                            payload={"nonce": nonce,
                                     "proof": proof_for(PIN, nonce),
                                     "operator": "张三"})
    assert code == 200
    tok = json.loads(body)["token"]
    assert 打(有pin的服务, "/api/control/acquire", tok)[0] == 200


def test_退出会把租约还掉而且留痕(有pin的服务):
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    request(有pin的服务, "/api/auth/logout", method="POST", headers=auth(tok))
    旁观 = 解锁(有pin的服务, "李四")
    assert get_json(有pin的服务, "/api/control",
                    headers=auth(旁观))["holder"] is None
    痕 = get_json(有pin的服务, "/api/control/audit",
                  headers=auth(旁观))["audit"]
    assert 痕[-1]["kind"] == "dropped"


# --------------------------------------------------- 裁定 C: 两个接口同一个分母


def test_control和state报的sessions必须一致(有pin的服务):
    """``GET /api/control`` 和 ``/api/state`` 的 ``control.sessions`` 必须
    永远是同一个数 —— 手机端两个接口都读, 分母在两处各写一份就会在同一时刻
    对同一件事报出两个不同的答案(见 ``app/control.py`` 的
    ``ControlDesk.seats()`` docstring)。

    本机会话(走 HTTP 解锁,回环判 CHANNEL_LOCAL)不该计进分母;非本机会话
    (直接调 ``Guard.unlock`` 带一个局域网地址)要计进去。构造两种会话之后,
    两个接口读到的 ``sessions`` 必须相等, 而且等于 1(只有那个非本机的算)。
    """
    本机 = 解锁(有pin的服务, "张三")  # 走 HTTP,client 是 127.0.0.1,判本机
    有pin的服务.auth.unlock(PIN, "192.168.1.50", operator="李四")  # 非本机

    def state段sessions():
        return get_json(有pin的服务, "/api/state",
                        headers=auth(本机))["control"]["sessions"]

    # /api/state 读的是 _StateHub 的缓存快照,新会话得等下一拍重建才看得见
    # (跟 tests/app/test_control_wiring.py 的 test_持有租约之后快照里看得见
    # 是谁 是同一个原因)——不写死 sleep,等到期望值出现或者超时为止。
    state_sessions = 等到(state段sessions, 1)
    control_sessions = get_json(有pin的服务, "/api/control",
                                headers=auth(本机))["sessions"]

    assert state_sessions == 1
    assert control_sessions == 1
    assert control_sessions == state_sessions
