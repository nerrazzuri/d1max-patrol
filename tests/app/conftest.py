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
from d1max_patrol.app.mapping import MappingConfig, MappingOrchestrator
from d1max_patrol.app.procs import ProcManager
from d1max_patrol.app.server import AppContext, AppServer
from d1max_patrol.app.teleop import Teleop
from d1max_patrol.backends.base import (
    EventEmitter,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.engine.machine import MissionEngine
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus


class FakeNav(EventEmitter):
    """假导航后端。``link`` / ``caps`` / ``nav`` / ``loc`` 直接改。

    ``asked`` 记下有没有人问过 ``nav_status()`` —— ``/api/state`` 的设计前提
    就是"读快照不碰链路",这个计数器是那句话的证据。

    起飞检查那几项要问 ``nav_status`` / ``loc_status``,所以这两个默认回的是
    "能起飞"的那组值;想测检查没过的路径,把 ``nav`` 或 ``loc`` 改掉即可。
    """

    def __init__(self, caps: frozenset[str] | None = None) -> None:
        super().__init__()
        self.link = True
        self.caps = caps if caps is not None else frozenset(
            {"mapping", "map_admin", "reloc"})
        self.asked = 0
        self.nav = NavStatus.STANDBY
        self.loc = LocStatus.CONTINUOUS_LOC
        self.maps = ["map_test", "map_b"]
        self.current_map = ""
        self.reloc_calls = 0
        self.goto_calls: list[object] = []

    @property
    def connected(self) -> bool:
        return self.link

    @property
    def capabilities(self) -> frozenset[str]:
        return self.caps

    async def nav_status(self):
        self.asked += 1
        return self.nav

    async def loc_status(self):
        self.asked += 1
        return self.loc

    async def list_maps(self) -> list[str]:
        return list(self.maps)

    async def load_map(self, map_id: str) -> None:
        if map_id not in self.maps:
            raise NavRequestError("load_map", f"没有这张图: {map_id}")
        self.current_map = map_id

    async def reset_localization(self) -> None:
        self.reloc_calls += 1

    async def goto(self, pose) -> None:
        """只记下,不发"到了"。任务因此会停在"走着"上 —— 测接口正需要这样。"""
        self.goto_calls.append(pose)

    async def stop(self) -> None: ...

    async def return_home(self) -> None:
        self.emit(NavStatusEvent(NavStatus.SUCCEED))


class FakeDevice(EventEmitter):
    """假设备后端。``estop`` / ``batt`` / ``control`` 直接改;``walk_calls`` 记下每一拍。

    ``stop_calls`` 单独数"四个轴全零"的那种调用 —— 停车在这台机器上就是
    一次零控制量的 ``walk``,没有单独的 stop 接口。
    """

    def __init__(self) -> None:
        super().__init__()
        self.link = True
        self.estop = False
        self.batt = 88.0
        self.control = True
        self.walk_calls: list[tuple[float, float, float, float]] = []
        self.stop_calls = 0

    @property
    def connected(self) -> bool:
        return self.link

    async def emergency(self) -> bool:
        return self.estop

    async def battery(self) -> float:
        return self.batt

    async def has_control(self) -> bool:
        return self.control

    async def walk(self, seconds: float, forward: float,
                   lateral: float = 0.0, yaw: float = 0.0) -> None:
        self.walk_calls.append((seconds, forward, lateral, yaw))
        if (forward, lateral, yaw) == (0.0, 0.0, 0.0):
            self.stop_calls += 1

    async def set_light(self, on: bool) -> None: ...

    async def set_gimbal(self, pitch: float, yaw: float) -> None: ...


class FakeMaps:
    """假地图桥客户端。app 只看它连没连上。"""

    def __init__(self) -> None:
        self.link = False
        #: 桥上最近推来的那一帧。没接真机时是 None,页面画底图会退回盘上存好的图。
        self.latest = None

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


def make_ctx(bridge, tmp_path: Path, nav=None, device=None) -> AppContext:
    """拼一份上下文。``nav`` / ``device`` 留空就是默认的那对假件。"""
    nav = nav if nav is not None else FakeNav()
    device = device if device is not None else FakeDevice()
    engine = bridge.call(lambda: _make_engine(nav, device, tmp_path / "runs"))
    teleop = bridge.call(lambda: _make_teleop(device, engine))
    procs = ProcManager(tmp_path / "logs")
    # 真的 ProcManager 配真的编排器:这一层测的是接口的形状,不是 ROS ——
    # 没有一条测试会走到"真起进程"那一步。
    mapping = MappingOrchestrator(procs, MappingConfig(
        bags_dir=tmp_path / "bags", maps_dir=tmp_path / "maps",
        params_template=tmp_path / "mapper_3d.yaml",
        work_dir=tmp_path / "work"))
    return AppContext(
        bridge=bridge, engine=engine, nav=nav, device=device,
        maps=FakeMaps(), procs=procs, teleop=teleop, mapping=mapping,
        missions_dir=tmp_path / "missions", runs_root=tmp_path / "runs")


@pytest.fixture
def ctx(bridge, tmp_path) -> AppContext:
    c = make_ctx(bridge, tmp_path)
    yield c
    # 有测试会把任务真起起来。不收掉的话,引擎那条协程会活到桥停为止,
    # 而它中途还会往归档目录里写 —— 写到一个 pytest 正在删的临时目录里。
    with contextlib.suppress(Exception):
        bridge.call(c.engine.aclose, timeout_s=10.0)


@pytest.fixture
def server(ctx):
    s = AppServer(ctx, port=0)
    s.start()
    yield s
    s.stop()


@pytest.fixture
def server_local_backend(bridge, tmp_path):
    """自建路线那一侧的服务:后端一项可选能力都没有。

    ``LocalNavBackend.capabilities`` 返回的就是空集(建图是 ROS 侧离线做的、
    删改图是文件系统的事、重定位要人给初始位姿),这里照着它摆。
    """
    c = make_ctx(bridge, tmp_path, nav=FakeNav(caps=frozenset()))
    s = AppServer(c, port=0)
    s.start()
    yield s
    s.stop()
    with contextlib.suppress(Exception):
        bridge.call(c.engine.aclose, timeout_s=10.0)


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
