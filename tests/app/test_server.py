"""HTTP 外壳:路由、错误体、静态文件、状态快照、事件流。

这些测试**故意不是 async 的**:HTTP 客户端就是同步的,测试也该是。真服务、
真端口、真 urllib 请求 —— 直接调处理函数的话,这一层真正会出问题的地方
(路径怎么解码、错误怎么变成响应体、SSE 有没有把第一帧真写出去)一个都测
不到。
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from d1max_patrol.app.server import (
    AppServer,
    HttpError,
    Response,
    json_response,
)
from d1max_patrol.backends.base import BatteryEvent, NavStatusEvent
from d1max_patrol.engine.machine import RunState
from d1max_patrol.protocol.nav_types import NavStatus
from tests.app.conftest import FakeNav, get_err, get_json, request, sse, status


async def _emit(emitter, event) -> None:
    emitter.emit(event)


def _next_frame(frames, predicate, limit: int = 20):
    """一直读到满足条件的那帧。

    不写"跳过 N 帧":帧是异步来的,数着跳迟早会跳错一帧然后偶发失败。
    """
    for _ in range(limit):
        frame = next(frames)
        if predicate(frame):
            return frame
    raise AssertionError("等不到满足条件的帧")


def _until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ 页面


def test_根路径给的是页面(server):
    code, body, headers = request(server, "/")
    assert code == 200
    assert "text/html" in headers["Content-Type"]
    assert b"<title>" in body


def test_静态文件拿得到(server):
    code, _, headers = request(server, "/static/index.html")
    assert code == 200 and "text/html" in headers["Content-Type"]


def test_静态文件出不去static目录(server):
    """路径穿越 —— 这个服务默认只听 127.0.0.1,但还是不能有这个洞。"""
    for evil in ("/static/../server.py", "/static/..%2Fserver.py",
                 "/static/....//server.py", "/static/%2e%2e/server.py"):
        assert status(server, evil) in (400, 403, 404), evil


def test_没有的静态文件给404(server):
    assert status(server, "/static/nope.js") == 404


# ------------------------------------------------------------------ 状态


def test_state给全量快照(server):
    d = get_json(server, "/api/state")
    assert set(d) >= {"run", "nav", "device", "backend", "caps"}
    assert d["run"]["state"] == "IDLE"


def test_state读的是快照不去问后端(server, ctx):
    """``/api/state`` 会被页面反复拉。它要是每次都问一遍链路,导航正忙的
    时候就是在给链路加塞 —— 快照存在的全部理由就是这一条。
    """
    for _ in range(5):
        get_json(server, "/api/state")
    assert ctx.nav.asked == 0, "快照不该去问后端"


def test_caps跟着后端走(ctx):
    """自建那条路线三项可选能力一项都没有,页面据此把按钮画成灰的。"""
    ctx.nav = FakeNav(caps=frozenset())
    srv = AppServer(ctx, port=0)
    srv.start()
    try:
        assert get_json(srv, "/api/state")["caps"] == []
    finally:
        srv.stop()


def test_快照里带着后端是哪一路(server):
    backend = get_json(server, "/api/state")["backend"]
    assert backend["nav"] == "FakeNav"
    assert backend["procs"] == []


def test_链路断了快照里看得出来(server, ctx):
    ctx.nav.link = False
    assert _until(lambda: not get_json(server, "/api/state")["nav"]["connected"]), \
        "链路通断没有事件,只能靠定期重建发现 —— 这条挂了说明那个节拍没在跑"


# ------------------------------------------------------------------ 错误


def test_不存在的路径给404而且是人话(server):
    err = get_err(server, "/api/nope", 404)
    assert err["error"] and "堆栈" not in err["error"]
    assert "Traceback" not in err["error"] + err["detail"]


def test_错误体统一是error加detail(server):
    for path, expect in (("/api/nope", 404), ("/static/../server.py", 403)):
        err = get_err(server, path, expect)
        assert set(err) == {"error", "detail"}, path
        assert isinstance(err["error"], str) and err["error"]


def test_方法不对给405而且说清楚支持什么(server):
    err = get_err(server, "/api/state", 405, method="POST", payload={})
    assert "GET" in err["detail"]


def test_处理函数炸了给500但不吐堆栈(server):
    def boom(_req):
        raise ValueError("内部炸了")

    server.route("GET", "/api/boom", boom)
    err = get_err(server, "/api/boom", 500)
    assert "Traceback" not in err["error"] and "Traceback" not in err["detail"]
    assert "ValueError" in err["detail"], "类型和消息要留着,不然没法查"


def test_一个请求出错不会把服务带走(server):
    def boom(_req):
        raise ValueError("内部炸了")

    server.route("GET", "/api/boom", boom)
    get_err(server, "/api/boom", 500)
    assert get_json(server, "/api/state")["run"]["state"] == "IDLE"


def test_请求体不是JSON给400(server):
    def echo(req):
        return json_response(req.json())

    server.route("POST", "/api/echo", echo)
    code, _, _ = request(server, "/api/echo", method="POST", payload={"a": 1})
    assert code == 200
    err = get_err(server, "/api/echo", 400, method="POST", raw=b"{ this is not json")
    assert set(err) == {"error", "detail"}
    assert status(server, "/api/echo", method="POST") == 200, "空体算空对象"


def test_路径参数不跨斜杠(server):
    """``/api/runs/r1/photos/..%2F..%2Fx`` 解码之后带斜杠,于是根本匹配不上。"""
    server.route("GET", "/api/runs/<run_id>/photos/<name>",
                 lambda req: json_response(dict(req.params)))
    assert get_json(server, "/api/runs/r1/photos/a.jpg") == {
        "run_id": "r1", "name": "a.jpg"}
    assert status(server, "/api/runs/r1/photos/..%2F..%2Fmanifest.json") == 404


def test_后面的模块能往上挂路由(server):
    server.route("PUT", "/api/thing/<name>",
                 lambda req: Response(201, req.params["name"].encode()))
    code, body, _ = request(server, "/api/thing/x", method="PUT", payload={})
    assert (code, body) == (201, b"x")


# ------------------------------------------------------------------ 事件流


def test_events是sse而且先推一帧当前状态(server):
    """页面刚打开就得知道现在什么情况,不能等下一次变化。"""
    code, _, headers = request(server, "/api/state")
    assert code == 200
    with sse(server) as frames:
        first = next(frames)
    assert first["kind"] == "state"
    assert set(first) >= {"run", "nav", "device", "backend", "caps"}


def test_events的content_type是eventstream(server):
    with sse(server) as frames:
        next(frames)     # 头是随第一帧一起到的
    # 头已经在上面那次读里验过了,这里再显式确认一次
    import urllib.request

    from tests.app.conftest import url
    with urllib.request.urlopen(url(server, "/api/events"), timeout=5) as resp:
        assert "text/event-stream" in resp.headers["Content-Type"]


def test_events收得到后来的变化(server, ctx):
    with sse(server) as frames:
        next(frames)
        ctx.bridge.spawn(lambda: _emit(ctx.device, BatteryEvent(42.0)))
        frame = _next_frame(frames, lambda f: f["device"]["battery"] == 42.0)
    assert frame["kind"] == "state"


def test_导航状态变化也推得出来(server, ctx):
    with sse(server) as frames:
        next(frames)
        ctx.bridge.spawn(
            lambda: _emit(ctx.nav, NavStatusEvent(NavStatus.ACTIVE)))
        _next_frame(frames, lambda f: f["nav"]["nav_status"] == "ACTIVE")


def test_没变化就不刷屏(server):
    """节拍是 0.5 秒一次,要是每次都发,页面上的日志一天能滚出几十万行。"""
    with sse(server) as frames:
        next(frames)
        time.sleep(1.5)
        ctx_free = server.ctx
        ctx_free.bridge.spawn(lambda: _emit(ctx_free.device, BatteryEvent(7.0)))
        frame = next(frames)
    assert frame["device"]["battery"] == 7.0, "中间那几拍不该发出来"


def test_一个sse客户端卡住不影响另一个(server, ctx):
    """ThreadingHTTPServer 一客户端一线程,这条是它的存在理由。"""
    with sse(server) as stuck:      # 开着但一帧都不读
        assert stuck is not None
        with sse(server) as frames:
            assert next(frames)["kind"] == "state"


def test_客户端断了就退订(server, ctx):
    """不退订的话,每关一个标签页就留一条永远没人读的队列。

    断开是客户端那边的事,服务端只有在**写不动**的时候才发现 —— 而第一次
    写往往还能塞进内核缓冲区里成功返回。所以这里持续推事件,等它真的写不动。
    """
    with sse(server) as frames:
        next(frames)

    def _poke() -> bool:
        ctx.bridge.spawn(
            lambda: _emit(ctx.device, BatteryEvent(time.monotonic() % 100)))
        return len(server.hub.events._subscribers) == 0

    assert _until(_poke), "断了的 SSE 客户端还留在订阅名单上"


def test_服务停了SSE线程也收得了尾(ctx):
    """不收尾的话,那条 HTTP 线程会永远挂在 next() 上。"""
    srv = AppServer(ctx, port=0)
    srv.start()
    with sse(srv) as frames:
        next(frames)
        srv.stop()
        assert list(frames) == [], "停服之后这条流应该干净地结束"


# ------------------------------------------------------------------ 只读与监听


def test_GET不改任何状态(server, ctx):
    before = get_json(server, "/api/state")
    for path in ("/api/state", "/api/maps", "/api/missions", "/api/runs"):
        status(server, path)
    assert ctx.engine.snapshot.state is RunState.IDLE
    assert get_json(server, "/api/state") == before


def test_默认只听本地(server):
    assert server._httpd.server_address[0] == "127.0.0.1"


def test_听0000会打印警告(ctx, capsys):
    srv = AppServer(ctx, host="0.0.0.0", port=0)   # 测的就是这个地址
    try:
        srv.start()
        assert "公网" in capsys.readouterr().out
    finally:
        srv.stop()


def test_桥没起来就拒绝启动(ctx):
    """起了却什么都干不了,比起不来更难查。"""
    ctx.bridge.stop()
    with pytest.raises(RuntimeError, match="桥"):
        AppServer(ctx, port=0).start()


def test_没启动问端口会说清楚(ctx):
    with pytest.raises(RuntimeError, match="还没启动"):
        _ = AppServer(ctx, port=0).port


# ------------------------------------------------------------------ 分层


def test_引擎和判读都不import_http():
    """全局约束的自动化证据:业务逻辑不许知道 HTTP 存在。

    反过来是允许的 —— app 依赖引擎。这一条要是破了,引擎就再也不能在没有
    HTTP 的地方(命令行、仿真、真机现场脚本)单独跑了。
    """
    import d1max_patrol.engine
    import d1max_patrol.inspect

    for pkg in (d1max_patrol.engine, d1max_patrol.inspect):
        for py in Path(pkg.__file__).parent.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            for banned in ("import http", "socketserver", "d1max_patrol.app"):
                assert banned not in src, f"{py.name} 里出现了 {banned}"


def test_外壳自己不碰后端的内部(server):
    """HTTP 线程碰后端的唯一通道是 bridge。这条查的是"有没有第二条路"。"""
    import inspect as pyinspect

    from d1max_patrol.app import server as mod

    tree = ast.parse(pyinspect.getsource(mod))
    awaits = [n for n in ast.walk(tree)
              if isinstance(n, ast.Await)]
    inside_async = {n for fn in ast.walk(tree)
                    if isinstance(fn, ast.AsyncFunctionDef)
                    for n in ast.walk(fn)}
    assert all(node in inside_async for node in awaits), \
        "同步路径上不该有 await"


def test_错误类型自己就能变成响应(server):
    exc = HttpError(409, "已经在跑了", "先 abort")
    assert exc.to_wire() == {"error": "已经在跑了", "detail": "先 abort"}
