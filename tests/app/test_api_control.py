"""控制权那七条路由。"""

from __future__ import annotations

import json
import time

import pytest

from d1max_patrol.app.auth import proof_for
from d1max_patrol.app.server import AppServer
from d1max_patrol.app.teleop import REMOTE_CONFIRM
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


# ------------------------------------------------------------ 现场档/远程档


def test_切远程要确认(有pin的服务):
    """**没确认就切,是 409 不是 400。**

    400 的意思是「你发的东西不对」,而这里发的东西完全正确 —— 缺的是一次
    确认。分错了,手机上只能显示一句「请求格式错误」,人不知道该点什么。
    """
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    code, body = 打(有pin的服务, "/api/teleop/mode", tok, {"mode": "remote"})
    assert code == 409
    assert body["detail"] == REMOTE_CONFIRM


def test_确认之后切得过去(有pin的服务):
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    code, body = 打(有pin的服务, "/api/teleop/mode", tok,
                    {"mode": "remote", "confirmed": True})
    assert code == 200
    assert body["mode"] == "remote"
    assert body["confirm"] == REMOTE_CONFIRM


def test_这次确认自动写进留痕(有pin的服务):
    """§7.8:**留痕才是真兜底。** 一个被点掉的弹窗证明不了任何事。

    盯的是「切档这个动作本身产生了记录」,不是「界面弹过框」—— 后者在事后
    什么都证明不了,而这一条是删不掉的。
    """
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    打(有pin的服务, "/api/teleop/mode", tok, {"mode": "remote", "confirmed": True})
    留痕 = get_json(有pin的服务, "/api/control/audit", headers=auth(tok))["audit"]
    最后 = 留痕[-1]
    assert 最后["kind"] == "mode_switched"
    assert "远程" in 最后["detail"]
    assert 最后["ref"]              # 谁切的,有指纹


def test_切回现场不用确认(有pin的服务):
    """往安全的方向走不该设卡。多设一道,人就会懒得切回来。"""
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    打(有pin的服务, "/api/teleop/mode", tok, {"mode": "remote", "confirmed": True})
    code, _body = 打(有pin的服务, "/api/teleop/mode", tok, {"mode": "onsite"})
    assert code == 200


def test_没控制权不许切档(有pin的服务):
    """切档要留名,而没控制权的人没有名字可留。

    **断的是闸,不是处理函数自己的防御。** 只查 ``code in (401, 403, 409)``
    不够 —— 处理函数里也有一条"没有持有者就拒"的判断(B-1 修完之后),两条
    判断凑巧都吐 409,单看状态码分不出今天挡人的到底是 ``CONTROLLED`` 那道
    闸,还是处理函数自己兜底。这里断言的措辞只在闸的拒绝信息里出现
    (``ControlDesk.require()``,见 ``control.py``),处理函数自己的 409
    文案里没有 ``/api/control/acquire`` 这个子串 —— 挡错了地方这条测试才会
    真的变红。
    """
    tok = 解锁(有pin的服务, "张三")
    code, body = 打(有pin的服务, "/api/teleop/mode", tok,
                    {"mode": "remote", "confirmed": True})
    assert code == 409
    assert body["error"] == "先取控制权"
    assert "/api/control/acquire" in body["detail"]


def test_甲放手乙拿走甲再拿回来也掉回现场档(有pin的服务):
    """S-3:档挂在**这一次租约**上,不挂在 token 指纹(``ref``)上。

    评审实测过的场景:甲切远程、留痕落下一条 ``mode_switched``,release;
    乙 acquire 又 release;甲用**同一个 token**再 acquire —— 甲回来不算
    "换人",但那是**新的一段作业**,不是接着上一段没完的那段。用 ref 相
    等判"还是原来那个人在远程"会漏掉这一段:甲第二段整段是在远程档上开
    的狗,档案里却一个字都没有(既没有第二次确认,也没有第二条
    ``mode_switched``)。这条测试直接验证那个反面表现不会发生:同一个
    ref 重新拿到租约之后,不确认就切不到远程 —— 说明档确实随着这一次授
    予被重置回了现场。
    """
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")

    打(有pin的服务, "/api/control/acquire", 甲)
    code, body = 打(有pin的服务, "/api/teleop/mode", 甲,
                    {"mode": "remote", "confirmed": True})
    assert code == 200 and body["mode"] == "remote"
    打(有pin的服务, "/api/control/release", 甲)

    打(有pin的服务, "/api/control/acquire", 乙)
    打(有pin的服务, "/api/control/release", 乙)

    打(有pin的服务, "/api/control/acquire", 甲)
    # 甲这一次拿到的是新一轮租约。如果档还认 ref、不认这一次授予,这里会
    # 直接 200(因为 ``teleop.mode()`` 一直以为还是 remote,没什么可确认
    # 的)——那正是评审探针实测出来的那个反面表现。
    code, body = 打(有pin的服务, "/api/teleop/mode", 甲, {"mode": "remote"})
    assert code == 409
    assert body["detail"] == REMOTE_CONFIRM
    留痕 = get_json(有pin的服务, "/api/control/audit",
                    headers=auth(甲))["audit"]
    assert sum(1 for r in 留痕 if r["kind"] == "mode_switched") == 1

    # 补上这一次的确认,新的一段作业就该留下自己的一条痕,不是复用第一条。
    code, body = 打(有pin的服务, "/api/teleop/mode", 甲,
                    {"mode": "remote", "confirmed": True})
    assert code == 200
    留痕 = get_json(有pin的服务, "/api/control/audit",
                    headers=auth(甲))["audit"]
    assert sum(1 for r in 留痕 if r["kind"] == "mode_switched") == 2


def test_切档这条路要租约():
    from d1max_patrol.app.control import needs_lease
    assert needs_lease("POST", "/api/teleop/mode")


# -------------------------------------------------------------- 边角情形


def test_没设pin的部署上取控制权说得清楚(server):
    code, body, _ = request(server, "/api/control/acquire", method="POST",
                            payload={})
    assert code == 400
    assert "没设 PIN" in json.loads(body)["error"]


def test_没设pin的部署上切档说得清楚(server):
    """B-1:``require()`` 对没设 PIN 的部署直接放行(只听本机,没有"谁是谁"
    这回事),``/api/teleop/mode`` 因此没经过闸就进了处理函数 —— 那里必须自
    己拒绝而不是 ``assert`` 崩溃(§8.5:``python -O`` 下 assert 整条被删,
    崩得比这条测试还彻底)。这条钉的是那条拒绝路径本身,跟
    ``test_没控制权不许切档`` 钉闸不是同一件事:那条服务器设了 PIN、有身
    份、没租约;这条压根没有身份可言。
    """
    code, body, _ = request(server, "/api/teleop/mode", method="POST",
                            payload={"mode": "remote", "confirmed": True})
    assert code == 409
    body = json.loads(body)
    assert body["error"] == "先取控制权"
    assert "没设 PIN" in body["detail"]


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
                        headers=auth(本机))["control"]["remote_sessions"]

    # /api/state 读的是 _StateHub 的缓存快照,新会话得等下一拍重建才看得见
    # (跟 tests/app/test_control_wiring.py 的 test_持有租约之后快照里看得见
    # 是谁 是同一个原因)——不写死 sleep,等到期望值出现或者超时为止。
    state_sessions = 等到(state段sessions, 1)
    control_sessions = get_json(有pin的服务, "/api/control",
                                headers=auth(本机))["remote_sessions"]

    assert state_sessions == 1
    assert control_sessions == 1
    assert control_sessions == state_sessions


def test_一个名字不许有两种类型(有pin的服务):
    """``sessions`` 在 ``GET /api/sessions`` 上是**数组**,所以别的接口上不许

    再有一个叫 ``sessions`` 的**整数**。手机端两条接口都要读,同一个名字一个
    是数组一个是整数,写出来的客户端会"看起来能跑、偶尔炸一下"。那个整数在三
    条接口上统一叫 ``remote_sessions``。
    """
    本机 = 解锁(有pin的服务, "张三")
    ctl = get_json(有pin的服务, "/api/control", headers=auth(本机))
    assert "sessions" not in ctl
    assert isinstance(ctl["remote_sessions"], int)
    段 = get_json(有pin的服务, "/api/state", headers=auth(本机))["control"]
    assert "sessions" not in 段
    assert isinstance(段["remote_sessions"], int)
    # 而 /api/sessions 上那个名字仍然是数组,两边合起来才说明问题被解掉了。
    表 = get_json(有pin的服务, "/api/sessions", headers=auth(本机))
    assert isinstance(表["sessions"], list)
    assert isinstance(表["remote_sessions"], int)
