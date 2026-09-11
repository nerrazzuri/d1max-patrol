"""两条破坏性路由的前置闸:离线重建(必修 2)、版本回滚(必修 1)。

这两条路由的共同点是**后果不在响应体里**:

* ``POST /api/mapping/rebuild`` 第一件事是 ``forget_home(map_id)`` —— 把这张
  图的原点删掉。删完这只狗用这张图起不了飞,恢复的唯一办法是有人物理走到
  原点上重标一次。
* ``POST /api/release/rollback`` 最后一定落在 ``ctx.restart()`` 上 —— 重启的
  那几十秒里 ``POST /api/estop`` 是打不通的,而狗还在走。

两条原来一道闸都没有,任何一个持 token 的人在巡检途中点一下就成立。这个文件
钉的就是那两道闸,外加"谁点的"那条留痕。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from d1max_patrol.app.identity import CONFIRM_PHRASE, write_payload
from d1max_patrol.app.server import AppServer
from d1max_patrol.engine.release import MANIFEST_NAME, Layout, commit, tree_sha256

from .conftest import make_ctx, request, sse

#: 点位里的 dwell 给得很长:这些用例要的是"引擎一直在跑"这个状态,
#: 不是任务本身跑完。跑完了闸就该放行,那正好是另一半用例要的局面。
_久一点 = 60.0


def _任务(seconds: float = _久一点) -> dict:
    return {
        "mission": "m1",
        "map_id": "map_test",
        "waypoints": [{
            "name": "P1_transformer",
            "pose": {"position": {"x": 1.0, "y": 0.0, "z": 0.0},
                     "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
            "check": "配电柜门是否关闭",
            "actions": [{"type": "dwell", "seconds": seconds}],
        }],
    }


def _打(server, path: str, payload=None, method: str = "POST"):
    code, body, _ = request(server, path, method=method,
                            payload={} if payload is None else payload)
    return code, json.loads(body)


def _起一趟(server) -> None:
    """把一趟任务真起起来, 让 ``ctx.engine.running`` 为真。"""
    assert _打(server, "/api/missions/m1", _任务(), method="PUT")[0] == 200
    code, body = _打(server, "/api/missions/m1/run")
    assert code == 200, body
    assert server._ctx.engine.running


# --------------------------------------------------------------- 离线重建


@pytest.fixture
def 重建服务(server, ctx, tmp_path):
    """一台盘上有包、重建被换成假的服务。

    换掉 ``ctx.mapping.rebuild``:真的那个要起四个 ROS 进程,而这里要验的是
    "闸放不放行",不是重建本身。换成假的之后还多了一个断点 —— **被拦住的时候
    它一次都不该被调到**。
    """
    (tmp_path / "bags" / "w1").mkdir(parents=True)
    调用: list[tuple[Path, str]] = []

    async def 假重建(bag: Path, map_id: str) -> Path:
        调用.append((bag, map_id))
        return bag

    ctx.mapping.rebuild = 假重建
    server.调用 = 调用
    return server


def test_巡检途中不许重建地图(重建服务):
    """必修 2 的正主。

    ``forget_home`` 删的是**这只狗脚下正在用的那张图**的原点。原来这条路由
    连"引擎在不在跑"都不问,一个人在值守屏上手滑就能让在跑的这一趟结束之后
    再也起不了飞 —— 而且当场什么都看不出来,要等下一次起飞被门槛挡住才发现。
    """
    _起一趟(重建服务)
    code, body = _打(重建服务, "/api/mapping/rebuild",
                     {"bag": "w1", "map_id": "map_test"})
    assert code == 409, body
    assert "还在跑" in body["error"]
    # 认文案, 不只认码:``plan_rebuild`` 自己在忙的时候也回 409, 光断码的话
    # 把这道闸整个拆掉这条测试照样绿。
    assert "原点" in body["detail"]


def test_被拦住的时候那趟重建一步都没起(重建服务):
    """**"拒绝"和"起了一半再拒绝"是两件事。** 闸必须在 ``spawn`` 之前。

    原点是 ``rebuild()`` 协程里第一句话删的。闸要是摆在 spawn 之后,响应体
    上写着 409、原点却已经没了 —— 那比不拦还坏,因为没有人会再去查。
    """
    _起一趟(重建服务)
    assert _打(重建服务, "/api/mapping/rebuild",
               {"bag": "w1", "map_id": "map_test"})[0] == 409
    time.sleep(0.2)
    assert 重建服务.调用 == []


def test_没在跑的时候照旧重建得了(重建服务):
    """闸只挡"在跑",不挡别的。停着的狗重建地图是这条路由的正常用法。"""
    code, body = _打(重建服务, "/api/mapping/rebuild",
                     {"bag": "w1", "map_id": "map_test"})
    assert code == 202, body
    deadline = time.monotonic() + 3.0
    while not 重建服务.调用 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert 重建服务.调用 and 重建服务.调用[0][1] == "map_test"


def test_重建留痕说得清是谁删的哪张图(重建服务):
    """必修 2 的另一半:**补闸之外还要补留痕**。

    原点被删这件事原来在事件流上一个字都没有。查起来的样子是:第二天没人
    起得了飞,日志里只有一行 202,谁在什么时候对哪张图点的那一下无从查起。
    """
    with sse(重建服务) as frames:
        assert next(frames)["kind"] == "state"
        assert _打(重建服务, "/api/mapping/rebuild",
                   {"bag": "w1", "map_id": "map_test"})[0] == 202
        痕 = None
        for frame in frames:
            if frame.get("kind") == "mapping.rebuild_started":
                痕 = frame
                break
    assert 痕 is not None, "重建起了却没有任何一条留痕"
    assert 痕["map_id"] == "map_test" and 痕["bag"] == "w1"
    # 这台没设 PIN, 所以没有会话 —— 署名是空串而不是缺字段。**字段本身必须
    # 在**:值守屏按 key 读,少一个 key 是 KeyError,不是"没署名"。
    assert 痕["operator"] == "" and 痕["session"] == ""
    assert 痕["home_forgotten"] is True
    assert isinstance(痕["at_ms"], int) and 痕["at_ms"] > 0


# --------------------------------------------------------------- 版本回滚


def _包(root: Path, name: str) -> Path:
    where = root / name
    (where / "bin").mkdir(parents=True, exist_ok=True)
    (where / "bin" / "run.py").write_text("print(1)\n", encoding="utf-8")
    (where / MANIFEST_NAME).write_text(json.dumps({
        "name": name, "version": "0.2.0", "content_sha256": tree_sha256(where),
        "requires_mission_schema": 1, "built_at": "2026-09-20T03:11:00Z",
    }), encoding="utf-8")
    return where


@pytest.fixture
def 回滚服务(bridge, tmp_path):
    """一台装了两版、已经切到新版、随时可以回滚的机器。"""
    import contextlib

    payload_file = tmp_path / "payload.json"
    write_payload(has=False, by="装机", now_ms=1, path=payload_file,
                  confirm=CONFIRM_PHRASE)
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt",
                   payload_file=payload_file)
    重启记录: list = []
    ctx.restart = 重启记录.append
    s = AppServer(ctx, port=0)
    s.start()
    s.重启记录 = 重启记录
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        assert _打(s, "/api/release/install",
                   {"package": str(_包(tmp_path / name, name))})[0] == 200
    assert _打(s, "/api/release/activate", {"name": "2026-09-06-a3f9c1"})[0] == 200
    commit(Layout(root=ctx.release_root))     # 把装机那次坐实
    assert _打(s, "/api/release/activate", {"name": "2026-09-20-77b2de"})[0] == 200
    重启记录.clear()
    yield s
    s.stop()
    with contextlib.suppress(Exception):
        bridge.call(ctx.engine.aclose, timeout_s=10.0)


def test_巡检途中不许回滚版本(回滚服务):
    """必修 1 的正主。

    回滚要重启服务。重启那几秒到几十秒里软急停这条路由**是断的** —— 服务
    没起来,谁都软急停不了,而狗这时候还在走。现场只剩硬急停按钮可按,
    前提是有人正好站在够得着的地方。
    """
    _起一趟(回滚服务)
    code, body = _打(回滚服务, "/api/release/rollback")
    assert code == 409, body
    assert "还在跑" in body["error"]
    assert "急停" in body["detail"], "得说清为什么不许, 不然人只会重试一遍"
    assert 回滚服务.重启记录 == [], "拒绝了却还是重启了, 那这道闸等于没有"


def test_没在跑的时候回滚照旧走得通(回滚服务):
    """闸不许把正常的回滚一起挡掉 —— 停着的机器换回上一版是日常操作。"""
    code, body = _打(回滚服务, "/api/release/rollback")
    assert code == 200, body
    assert body["rolled_back_to"] == "2026-09-06-a3f9c1"
    assert body["forced"] is False
    assert len(回滚服务.重启记录) == 1


def test_硬回滚要写理由(回滚服务):
    """跟 ``/api/control/takeover`` 的 ``force`` 同一个规矩(§3.5 规则 3)。

    **400 而不是 409**:请求本身填得不对,重试同一份照样不行。
    """
    _起一趟(回滚服务)
    code, body = _打(回滚服务, "/api/release/rollback", {"force": True})
    assert code == 400, body
    assert "理由" in body["error"]
    assert 回滚服务.重启记录 == []
    # 空白理由跟没写是一回事 —— 一个空格不算签字。
    assert _打(回滚服务, "/api/release/rollback",
               {"force": True, "reason": "   "})[0] == 400


def test_写了理由的硬回滚走得通并且留痕(回滚服务):
    """**口子必须留着。** 新装的这一版本身就可能是"狗停不下来"的原因,
    那种时候 ``/api/run/abort`` 也未必好使 —— 不给口子等于把人逼去拔电。

    代价是留痕:谁、什么时候、为什么带着一趟没跑完的活儿把服务重启了。
    """
    _起一趟(回滚服务)
    with sse(回滚服务) as frames:
        assert next(frames)["kind"] == "state"
        code, body = _打(回滚服务, "/api/release/rollback",
                         {"force": True, "reason": "新版走不动了, 先退回去"})
        assert code == 200, body
        痕 = None
        for frame in frames:
            if frame.get("kind") == "release.force_rollback":
                痕 = frame
                break
    assert body["forced"] is True
    assert body["rolled_back_to"] == "2026-09-06-a3f9c1"
    assert len(回滚服务.重启记录) == 1
    assert 痕 is not None, "硬回滚没留痕"
    assert 痕["reason"] == "新版走不动了, 先退回去"
    assert 痕["rolled_back_to"] == "2026-09-06-a3f9c1"


def test_普通回滚不往事件流上撒东西(回滚服务):
    """留痕只给硬回滚。

    停着的机器换个版本跟事故无关,每次都往事件流上发一条只会让真正该看的
    那条淹掉 —— 值守屏上的每一行都是要人扫一眼的。

    **这条不走 SSE 走 ``emit`` 本身。** 要断的是"一条都没发",而 SSE 那边
    "读不到"和"没发出来"分不开 —— 没有东西发的时候那条连接就是干等到超时,
    等出来的绿是假的。
    """
    发过的: list[dict] = []
    原来的 = 回滚服务._hub.events.emit
    回滚服务._hub.events.emit = lambda payload: (发过的.append(payload),
                                                  原来的(payload))[1]
    assert _打(回滚服务, "/api/release/rollback")[0] == 200
    assert [p for p in 发过的 if p.get("kind") == "release.force_rollback"] == []


def test_回滚的请求体必须是个对象(回滚服务):
    """``{"force": true}`` 要读得出来,就得先确认它是个对象。

    发一个裸数组过来原来会一路走到 ``.get`` 上冒 500。
    """
    code, body = _打(回滚服务, "/api/release/rollback", [1, 2, 3])
    assert code == 400, body
