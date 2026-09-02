"""app 测试的公共零件:假后端、真服务、打真 HTTP 请求的小工具。

**这些测试起的是真服务、用 urllib 打真请求。** 直接调处理函数测不出这一层
真正会出问题的地方 —— 路径怎么解码、错误怎么变成响应体、SSE 到底有没有把
第一帧写出去、一个客户端卡住会不会拖住别人。那些全在 HTTP 这一侧。

假后端只实现 app 真正会碰的那几件事:``connected``、``capabilities``、以及
``EventEmitter`` 那套订阅。端口的完整性由 ``tests/backends`` 盯着,这里再写
一遍十几个 stub 只会让人看不清测的是什么。
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.app.procs import ProcManager
from d1max_patrol.app.server import AppContext, AppServer
from d1max_patrol.app.teleop import Teleop
from d1max_patrol.backends.base import EventEmitter
from d1max_patrol.engine.machine import MissionEngine


class FakeNav(EventEmitter):
    """假导航后端。``link`` / ``caps`` 直接改。

    ``asked`` 记下有没有人问过 ``nav_status()`` —— ``/api/state`` 的设计前提
    就是"读快照不碰链路",这个计数器是那句话的证据。
    """

    def __init__(self, caps: frozenset[str] | None = None) -> None:
        super().__init__()
        self.link = True
        self.caps = caps if caps is not None else frozenset(
            {"mapping", "map_admin", "reloc"})
        self.asked = 0

    @property
    def connected(self) -> bool:
        return self.link

    @property
    def capabilities(self) -> frozenset[str]:
        return self.caps

    async def nav_status(self):
        self.asked += 1
        return None

    async def loc_status(self):
        self.asked += 1
        return None


class FakeDevice(EventEmitter):
    """假设备后端。``estop`` 直接改;``walk_calls`` 记下每一拍。

    ``stop_calls`` 单独数"四个轴全零"的那种调用 —— 停车在这台机器上就是
    一次零控制量的 ``walk``,没有单独的 stop 接口。
    """

    def __init__(self) -> None:
        super().__init__()
        self.link = True
        self.estop = False
        self.walk_calls: list[tuple[float, float, float, float]] = []
        self.stop_calls = 0

    @property
    def connected(self) -> bool:
        return self.link

    async def emergency(self) -> bool:
        return self.estop

    async def walk(self, seconds: float, forward: float,
                   lateral: float = 0.0, yaw: float = 0.0) -> None:
        self.walk_calls.append((seconds, forward, lateral, yaw))
        if (forward, lateral, yaw) == (0.0, 0.0, 0.0):
            self.stop_calls += 1


class FakeMaps:
    """假地图桥客户端。app 只看它连没连上。"""

    def __init__(self) -> None:
        self.link = False

    @property
    def connected(self) -> bool:
        return self.link


async def _make_engine(nav, device, runs_root: Path) -> MissionEngine:
    return MissionEngine(nav, device, {}, runs_root)


@pytest.fixture
def bridge():
    b = LoopBridge()
    b.start()
    yield b
    b.stop()


async def _make_teleop(device, engine) -> Teleop:
    return Teleop(device, engine)


@pytest.fixture
def ctx(bridge, tmp_path) -> AppContext:
    nav, device = FakeNav(), FakeDevice()
    engine = bridge.call(lambda: _make_engine(nav, device, tmp_path / "runs"))
    teleop = bridge.call(lambda: _make_teleop(device, engine))
    return AppContext(
        bridge=bridge, engine=engine, nav=nav, device=device,
        maps=FakeMaps(), procs=ProcManager(tmp_path / "logs"), teleop=teleop,
        missions_dir=tmp_path / "missions", runs_root=tmp_path / "runs")


@pytest.fixture
def server(ctx):
    s = AppServer(ctx, port=0)
    s.start()
    yield s
    s.stop()


# ------------------------------------------------------------------ 打请求


def url(server: AppServer, path: str) -> str:
    return f"http://127.0.0.1:{server.port}{path}"


def request(server: AppServer, path: str, *, method: str = "GET",
            payload=None, raw: bytes | None = None, timeout: float = 5.0):
    """打一个请求,返回 (状态码, 响应体 bytes)。4xx/5xx 也照样返回,不抛。

    ``payload`` 会被 ``json.dumps`` 一道;``raw`` 是原样发出去的字节,给
    "发一段根本不是 JSON 的东西看服务怎么办"这种测试用。
    """
    body = raw if raw is not None else (
        None if payload is None else json.dumps(payload).encode("utf-8"))
    req = urllib.request.Request(url(server, path), data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read(), dict(exc.headers)


def status(server: AppServer, path: str, **kwargs) -> int:
    return request(server, path, **kwargs)[0]


def get_json(server: AppServer, path: str, **kwargs):
    code, body, _ = request(server, path, **kwargs)
    assert code == 200, f"{path} 给的是 {code}: {body!r}"
    return json.loads(body)


def get_err(server: AppServer, path: str, expect: int, **kwargs):
    code, body, _ = request(server, path, **kwargs)
    assert code == expect, f"{path} 给的是 {code} 不是 {expect}: {body!r}"
    return json.loads(body)


@contextlib.contextmanager
def sse(server: AppServer, path: str = "/api/events", timeout: float = 5.0):
    """开一条 SSE,拿一个逐帧吐 dict 的生成器。"""
    resp = urllib.request.urlopen(url(server, path), timeout=timeout)

    def frames():
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                yield json.loads(line[len("data:"):])

    try:
        yield frames()
    finally:
        resp.close()


def post(server: AppServer, path: str, payload=None) -> int:
    """打一个 POST,只要状态码。遥控那几个接口测的就是"收不收"。"""
    return request(server, path, method="POST", payload=payload or {})[0]
