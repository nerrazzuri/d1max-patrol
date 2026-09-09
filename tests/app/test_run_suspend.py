"""``POST /api/run/suspend``:让开腿那条路由(§5.10)。

Task 5 把引擎层做全了(``MissionEngine.suspend()``、``SuspendPoint``、
``RunSnapshot.suspended_at``、上线字段),Task 6 又照着 ``yielding`` 给遥控
开了放行闸 —— 但**一直没人把路由接上**。缺了这一条,上面那两截今天只有
Python 测试进得去:任务真跑起来的时候,人拿手机接管不了。

所以这一组不满足于"打过去回 200"。真正要钉住的是**那条链路通了**:
``test_让开腿之后遥控才动得了`` 先证任务在跑时遥控是 409,再证让开腿之后
同一个请求变成 200。这一对前后对照挪不走 —— 前半句为假(比如闸本来就开着)
的时候,后半句就什么都不算了。

鉴权和控制权照 ``/api/run/pause`` 一族:``tests/app/test_control_gate.py``
的那张镜像表和 ``test_auth.py`` 的参数表里都加了这条路径,这里再各钉一条
本地的,免得有人只改了正主没改镜像表时这个文件看起来还是绿的。
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from d1max_patrol.app.server import AppServer
from d1max_patrol.engine.machine import RunState
from tests.app.conftest import make_ctx, request

PIN = "428913"
走一拍 = {"fwd": 0.2, "lat": 0.0, "yaw": 0.0}


def _mission() -> dict:
    """一份能过 ``parse_mission`` 的最小任务定义(跟 test_api_missions 同款)。"""
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


def _post(server, path: str, payload=None):
    code, body, _ = request(server, path, method="POST", payload=payload or {})
    return code, json.loads(body)


@pytest.fixture
def 跑起来的服务(server, ctx):
    """存一份任务、起飞、等到真在跑。返回 (server, ctx)。

    假导航的 ``goto`` 只记不到,所以任务会一直停在"走着"上 —— 正是现场
    要接管的那一刻。
    """
    code, body, _ = request(server, "/api/missions/" + quote("巡检一号"),
                            method="PUT", payload=_mission())
    assert code == 200, body
    起飞 = "/api/missions/" + quote("巡检一号") + "/run"
    assert _post(server, 起飞)[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.RUNNING))
    return server, ctx


def test_让开腿把引擎推进SUSPENDED(跑起来的服务):
    server, ctx = 跑起来的服务
    code, _ = _post(server, "/api/run/suspend", {"reason": "门口有箱子"})
    assert code == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))
    assert ctx.engine.yielding
    assert ctx.engine.snapshot.suspended_at is not None
    assert ctx.engine.snapshot.suspended_at.reason == "门口有箱子"


def test_让开腿之后遥控才动得了(跑起来的服务):
    """**这一条是整条路由存在的理由。**

    先钉住"任务在跑的时候遥控是 409"—— 没有这半句,后面那个 200 可能只是
    因为闸本来就没关,断言就成了可以整体平移的空话(``docs/测试为什么会说
    谎.md`` 的第五种)。
    """
    server, ctx = 跑起来的服务
    挡住了, 理由 = _post(server, "/api/teleop", 走一拍)
    assert 挡住了 == 409, "任务在跑却让遥控进来了:下面那个 200 什么都证明不了"
    assert "任务在跑" in 理由.get("detail", "")

    assert _post(server, "/api/run/suspend", {"reason": "人接管"})[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))

    assert _post(server, "/api/teleop", 走一拍)[0] == 200


def test_让开腿之后还能让它接着跑(跑起来的服务):
    """接管完得能还回去。没这一条,"让开腿"就是个单程票。"""
    server, ctx = 跑起来的服务
    assert _post(server, "/api/run/suspend", {"reason": "人接管"})[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))
    assert _post(server, "/api/run/resume")[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.RUNNING))


def test_没任务在跑时让开腿给409(server):
    """跟 ``/api/run/pause`` 一样的规矩:点了没反应比报错更糟。"""
    code, body = _post(server, "/api/run/suspend")
    assert code == 409
    assert "没有任务在跑" in body["error"]


def test_不写理由的让开腿也有个说得出口的理由(跑起来的服务):
    """归档里留一条空理由,事后没人查得出这段路为什么是人开的。"""
    server, ctx = 跑起来的服务
    assert _post(server, "/api/run/suspend")[0] == 200
    ctx.bridge.call(lambda: ctx.engine.wait_state(RunState.SUSPENDED))
    点 = ctx.engine.snapshot.suspended_at
    assert 点 is not None and 点.reason.strip()


def test_返回体跟暂停一族是同一个形状(跑起来的服务):
    """别自创返回体。

    比的是键集合不是具体值:``_run_cmd`` 把命令投进队列就返回了,那一刻状态
    还可能没翻页 —— 拿 ``state == "SUSPENDED"`` 当断言是在赌调度顺序。
    """
    server, _ctx = 跑起来的服务
    _码, 暂停体 = _post(server, "/api/run/pause")
    _码2, 让开体 = _post(server, "/api/run/suspend")
    assert 让开体.keys() == 暂停体.keys()


# ------------------------------------------------------------ 鉴权和控制权


@pytest.fixture
def 有pin的服务(bridge, tmp_path):
    ctx = make_ctx(bridge, tmp_path)
    s = AppServer(ctx, port=0, pin=PIN)
    s.start()
    yield s
    s.stop()


def 解锁(server) -> str:
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload={"pin": PIN, "operator": "张三"})
    assert code == 200, body
    return json.loads(body)["token"]


def test_没token的让开腿是401(有pin的服务):
    code, _body, _h = request(有pin的服务, "/api/run/suspend", method="POST",
                              payload={})
    assert code == 401


def test_没控制权的让开腿是409先取控制权(有pin的服务):
    """闸在路由之前:这里没任务在跑,业务层本来也会 409 —— 所以要连
    ``error`` 一起断,不能只看状态码。"""
    tok = 解锁(有pin的服务)
    code, body, _ = request(有pin的服务, "/api/run/suspend", method="POST",
                            payload={}, headers={"Authorization": f"Bearer {tok}"})
    assert code == 409
    assert json.loads(body)["error"] == "先取控制权"
