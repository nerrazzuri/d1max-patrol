"""租约闸门:要控制权的那几条,没租约就进不去。"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.app.server import AppServer
from tests.app.conftest import make_ctx, request

PIN = "428913"
T0 = 1_757_000_000_000
走一拍 = {"fwd": 0.0, "lat": 0.0, "yaw": 0.0}

#: ``CONTROLLED`` 那几条正则展开之后能匹配到的全部具体路径。
#: 逐条钉住"没租约就 409", 少一条闸就是少一道闸, 而漏掉不会有任何报错。
#: **名字里不带数字**:带数字的名字每加一条路由就要跟着改, 改漏了也没人
#: 红 —— 数字本身就是下一次遗漏的种子。底下
#: ``test_这张表跟CONTROLLED两边对得上`` 补的就是这张表跟正主的一致性。
要控制权的每一条 = (
    "/api/teleop",
    "/api/teleop/heartbeat",
    "/api/teleop/mode",
    "/api/mapping/record/start",
    "/api/mapping/record/stop",
    "/api/missions/m1/run",
    "/api/run/pause",
    "/api/run/resume",
    "/api/run/abort",
    "/api/maps/load",
    "/api/pose/initial",
    "/api/pose/reset",
)


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


def 取控制权(server, token):
    assert 打(server, "/api/control/acquire", token)[0] == 200


def test_没租约就动不了(有pin的服务):
    tok = 解锁(有pin的服务)
    code, body = 打(有pin的服务, "/api/teleop", tok, 走一拍)
    assert code == 409
    assert "先取控制权" in body["error"]
    assert "/api/control/acquire" in body["detail"]


def test_取了租约就动得了(有pin的服务):
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 200


def test_急停任何时候都按得下去(有pin的服务):
    """§3.5 规则 2。这一条错了会死人。"""
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    assert 打(有pin的服务, "/api/estop", 乙)[0] == 200


def test_一个租约都没有时急停照样按得下去(有pin的服务):
    """§3.5 规则 2 的另一半:没人持有租约时也不许把急停挡在闸外。

    ``CONTROLLED`` 里如果哪天混进了 ``/api/estop``, 上面那条(有人持有)
    会红, 这一条(无人持有)也会红 —— 两种局面各钉一条, 免得只补上其中
    一半。
    """
    tok = 解锁(有pin的服务)
    assert 打(有pin的服务, "/api/estop", tok)[0] == 200


def test_看不需要控制权(有pin的服务):
    """§3.5 规则 1。"""
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    code, _body, _h = request(有pin的服务, "/api/state", headers=auth(乙))
    assert code == 200


def test_没租约也读得到状态控制权和留痕(有pin的服务):
    """§3.5 规则 1:``CONTROLLED`` 里一条 GET 都没有,值守屏永远看得见。

    值守的人多半根本不打算开狗 —— 要他先抢一次方向盘才看得到"现在谁拿着"
    是荒唐的, 而且那正是出事时最需要看的一屏。
    """
    tok = 解锁(有pin的服务)
    for 路径 in ("/api/state", "/api/control", "/api/control/audit"):
        code, _body, _h = request(有pin的服务, 路径, headers=auth(tok))
        assert code == 200, 路径


@pytest.mark.parametrize("路径", 要控制权的每一条)
def test_每条路径没租约一律进不去(有pin的服务, 路径):
    """``CONTROLLED`` 展开后的每一条都得真的挂上闸。

    ``/api/missions/m1/run`` 指的是一个不存在的任务 —— 闸在路由匹配之前,
    所以拿到的必须是 409 而不是 404。这正是"按路径判、不按处理函数判"要的
    效果。
    """
    tok = 解锁(有pin的服务)
    code, body = 打(有pin的服务, 路径, tok, 走一拍)
    assert code == 409, (路径, body)
    # 光断 409 不够: ``/api/run/pause`` 这几条在"没在跑"时业务本身就回 409,
    # 闸拆掉它们照样绿。认文案才认得出这一下是谁挡的。
    assert body["error"] == "先取控制权", (路径, body)


@pytest.mark.parametrize("路径", 要控制权的每一条)
def test_每条路径取了租约就不再是409(有pin的服务, 路径):
    """取了控制权之后, 这道闸就不该再说话了。

    不断 200:``/api/missions/m1/run`` 该 404、``/api/run/pause`` 没在跑该
    409-但那是引擎说的。这里只断"不是这道闸说的话" —— 靠错误文案区分,
    免得把一条闸的测试写成一堆业务前置条件的测试。

    **只认文案,不认状态码。** 原来写的是
    ``not (code == 409 and "控制权" in error)``,那把状态码和文案绑在了一
    起:哪天有人把闸的拒绝码从 409 改成 403 或 423,``code == 409`` 当场为
    假,整条断言**静默转绿** —— 闸照样在错误地拦着,而没有一条测试会红。

    不认状态码之后还剩一个口子:一个体是空的 500 也没有"控制权"三个字,
    照样过。所以底下补一句 —— 取了控制权之后打这些路径,**结果可以是 404、
    可以是业务的 409,就是不许是服务端自己崩了**。
    """
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    code, body = 打(有pin的服务, 路径, tok, 走一拍)
    assert code != 500, (路径, code, body)
    assert "控制权" not in body.get("error", ""), (路径, body)


def test_这张表跟CONTROLLED两边对得上():
    """手抄的镜像必须跟正主对得上 —— **两个方向都要**。

    只查一个方向都不够:只查"元组里每条都要租约",往 ``CONTROLLED`` 加一条
    新路由照样静默漏掉(这一卷刚发生过);只查"每个模式都被命中",元组里混进
    一条不该要租约的也照样绿。
    """
    from d1max_patrol.app.control import CONTROLLED, needs_lease
    for 路径 in 要控制权的每一条:
        assert needs_lease("POST", 路径), 路径
    for 方法, 模式 in CONTROLLED:
        assert any(方法 == "POST" and 模式.match(p) for p in 要控制权的每一条), \
            模式.pattern


def test_别人拿着时动不了而且说得出是谁(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    code, body = 打(有pin的服务, "/api/teleop", 乙, 走一拍)
    assert code == 409
    assert "张三" in body["error"]
    assert "takeover" in body["detail"]


def test_租约过期之后就动不了了(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 200
    墙钟.t += 30_000
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 409


def test_心跳续得住(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    for _ in range(5):
        墙钟.t += 10_000
        assert 打(有pin的服务, "/api/control/heartbeat", tok)[0] == 200
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 200


def test_遥控心跳也要控制权(有pin的服务):
    """守死人那一层的心跳和租约心跳是两条不同的线(§6.4),但发遥控心跳的人
    必须是拿着控制权的那个 —— 否则一个没有控制权的会话可以替持有者把死人
    开关按住,而持有者自己早就走开了。
    """
    tok = 解锁(有pin的服务)
    assert 打(有pin的服务, "/api/teleop/heartbeat", tok)[0] == 409
    取控制权(有pin的服务, tok)
    assert 打(有pin的服务, "/api/teleop/heartbeat", tok)[0] == 200


def test_接管之后原来的人动不了了(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    打(有pin的服务, "/api/control/takeover", 乙,
       {"force": True, "reason": "现场有人要摔了"})
    assert 打(有pin的服务, "/api/teleop", 甲, 走一拍)[0] == 409
    assert 打(有pin的服务, "/api/teleop", 乙, 走一拍)[0] == 200


def test_没设pin的部署上这一层不生效(server):
    code, _body, _h = request(server, "/api/teleop", method="POST",
                              payload=走一拍)
    assert code == 200


def test_退出之后别人立刻拿得到(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    request(有pin的服务, "/api/auth/logout", method="POST", headers=auth(甲))
    取控制权(有pin的服务, 乙)
    assert 打(有pin的服务, "/api/teleop", 乙, 走一拍)[0] == 200
