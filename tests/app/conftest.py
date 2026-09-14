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
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.app.identity import resolve
from d1max_patrol.app.mapping import MappingConfig, MappingOrchestrator
from d1max_patrol.app.procs import ProcManager
from d1max_patrol.app.server import AppContext, AppServer, build_pump
from d1max_patrol.app.teleop import Teleop
from d1max_patrol.backends.base import (
    DeviceBackendError,
    EventEmitter,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.backends.sidecar_device import MAX_WALK_SECONDS
from d1max_patrol.engine.homing import HomePoint, save_home
from d1max_patrol.engine.machine import MissionEngine
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

from ..conftest import NoDisks


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

    ``stop_calls`` 数 ``halt()``。停车**不是**零控制量的 ``walk`` —— 真后端拒掉
    非正时长,这个桩也拒。
    """

    def __init__(self) -> None:
        super().__init__()
        self.link = True
        self.estop = False
        self.batt = 88.0
        self.control = True
        self.walk_calls: list[tuple[float, float, float, float]] = []
        #: 数的是 ``halt()``。
        self.stop_calls = 0
        #: 每次 ``emergency_stop(on)`` 的 on。
        self.estop_calls: list[bool] = []
        #: 设了就让对应的调用抛它 —— 急停链路"一步失败不挡下一步"靠它测。
        self.halt_error: Exception | None = None
        self.estop_error: Exception | None = None

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
        # 跟真后端(``SidecarDeviceBackend.walk``)一样拒掉非正时长。以前这个桩
        # 把 ``walk(0,0,0,0)`` 当停车照单全收,真狗上停车一直被拒,测试却是绿的。
        if not 0 < seconds <= MAX_WALK_SECONDS:
            raise DeviceBackendError(
                f"行走时长要在 (0, {MAX_WALK_SECONDS}] 秒内,收到 {seconds}")
        self.walk_calls.append((seconds, forward, lateral, yaw))

    async def halt(self) -> None:
        if self.halt_error is not None:
            raise self.halt_error
        self.stop_calls += 1

    async def emergency_stop(self, on: bool = True) -> None:
        if self.estop_error is not None:
            raise self.estop_error
        self.estop_calls.append(on)

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


#: 起飞门槛把原点当成前置条件(preflight §home)。这些测试关心的是接口的
#: 形状,不是原点本身,默认给个跟 ``map_test``(任务默认的那张图)对得上的
#: 原点,免得每个用 ``ctx``/``server`` 的用例都要单独标一次。
_HOME = HomePoint(map_id="map_test", pose=Pose.from_xy_yaw(0.0, 0.0),
                  marked_at_ms=1_757_000_000_000)


async def _make_engine(nav, device, runs_root: Path,
                       removable=None, engine_clock=None) -> MissionEngine:
    # removable 传假探针:默认的 DEFAULT_PROBE 扫真机上的 /media、/mnt,
    # 结果就取决于跑测试的这台机器上此刻插着什么盘(见 tests/conftest.py)。
    #
    # ``engine_clock`` 是引擎那口**单调钟**(秒)。留空就是引擎自己的缺省
    # ``time.monotonic``,现有那一堆 app 测试一个字都不用改。要它是因为
    # ``SuspendPoint.at_ms`` / ``prior_suspend_ms`` / ``MissionEngine.now_ms()``
    # 三个数全从它推(见 ``engine/machine.py`` 的 ``_stamp_ms``),而
    # ``suspend_stale`` 那条 P1 判的正是这三个数 —— 不注进来就只能靠真的等
    # 十分钟(见 ``tests/app/test_suspend_e2e.py``)。
    return MissionEngine(nav, device, {}, runs_root,
                         removable=removable if removable is not None
                         else NoDisks(),
                         **({"clock": engine_clock}
                            if engine_clock is not None else {}))


@pytest.fixture
def bridge():
    b = LoopBridge()
    b.start()
    yield b
    b.stop()


async def _make_teleop(device, engine) -> Teleop:
    # video_gate=lambda: "":这层测的不是 §5.9 那道闸(那是 test_teleop.py
    # 自己的一组用例),给个永远放行的闸,免得每个用到这份 ctx 的既有测试
    # 都要顺带布一份 CameraFeed。
    return Teleop(device, engine, video_gate=lambda: "")


def make_ctx(bridge, tmp_path: Path, nav=None, device=None,
             removable=None, release_root=None, payload_file=None,
             bundles_root=None, clock=None, time_reference=None,
             engine_clock=None, console_url=None) -> AppContext:
    """拼一份上下文。``nav`` / ``device`` / ``removable`` 留空就是默认的假件。

    ``identity`` 显式传 ``resolve(...)``:``AppContext.identity`` 的默认工厂
    在开发机上会去读 ``/etc/d1max/payload.json``,不传的话真机上那个文件
    会串进测试。

    ``clock`` 和 ``engine_clock`` 是**两口不同的钟,别混**:前者是 app 那口
    墙钟(``ctx.clock``,毫秒,给告警盖时刻、给租约判 TTL),后者是引擎那口
    单调钟(秒,见 :func:`_make_engine`)。两个都留空就全走各自的缺省。

    ``console_url`` 留空(默认)就是**单机档**:``ctx.upload is None``,一条
    上传线程都不起,值守屏上那一格照旧是 ``null``。**这个默认是承重的** ——
    全仓那一大堆 app 测试(含 ``test_wire_fixtures`` 里
    ``assert 份["upload_backlog"] is None`` 那条)都靠它一个字不用改。
    """
    nav = nav if nav is not None else FakeNav()
    device = device if device is not None else FakeDevice()
    engine = bridge.call(lambda: _make_engine(nav, device, tmp_path / "runs",
                                              removable, engine_clock))
    teleop = bridge.call(lambda: _make_teleop(device, engine))
    procs = ProcManager(tmp_path / "logs")
    # 真的 ProcManager 配真的编排器:这一层测的是接口的形状,不是 ROS ——
    # 没有一条测试会走到"真起进程"那一步。
    mapping = MappingOrchestrator(procs, MappingConfig(
        bags_dir=tmp_path / "bags", maps_dir=tmp_path / "maps",
        params_template=tmp_path / "mapper_3d.yaml",
        work_dir=tmp_path / "work"))
    # 起飞门槛真正读的是盘上那份(见 app/server.py 的 _mission_run):每次起飞
    # 都会用它把引擎里的原点换一遍,构造时给的那份不写盘的话会被换成 None。
    save_home(mapping.maps_dir, _HOME)
    c = AppContext(
        bridge=bridge, engine=engine, nav=nav, device=device,
        maps=FakeMaps(), procs=procs, teleop=teleop, mapping=mapping,
        missions_dir=tmp_path / "missions", runs_root=tmp_path / "runs",
        release_root=release_root if release_root is not None
        else tmp_path / "opt",
        payload_file=payload_file,
        bundles_root=bundles_root if bundles_root is not None
        else tmp_path / "bundles",
        # 留空就用 AppContext 自己的默认值(真墙上时钟、没有参照)——
        # 现有那一堆 app 测试一个字都不用改。
        **({"clock": clock} if clock is not None else {}),
        **({"time_reference": time_reference}
           if time_reference is not None else {}),
        identity=resolve(sn="D1M-TEST", files=(), net_root=tmp_path / "无",
                         payload_file=payload_file, host="test"))
    if console_url is not None:
        c.upload = build_pump(c, console_url)
    return c


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


# --------------------------------------------------------------- 假的 ffmpeg


@pytest.fixture
def ffdir(tmp_path_factory) -> Path:
    """假 ffmpeg 的落脚处。

    **不能用 tmp_path**:它的名字里带测试函数名,而这里的测试名是中文。
    Windows 上 cmd 按当前代码页(GBK)读 ``.bat``,路径里的中文到子进程手上
    就成了乱码,脚本直接打不开。``mktemp`` 给的目录名是纯 ASCII。
    """
    return tmp_path_factory.mktemp("ff")


def _fake(where: Path, body: str, tag: str) -> str:
    """把一段 Python 包成一个能直接执行的"ffmpeg"。

    包一层壳而不是直接把 ``.py`` 交给 ``Popen``:``.py`` 能不能直接执行取决于
    系统上的文件关联,而 ``.bat`` / 带 shebang 的 ``.sh`` 到哪儿都能跑。
    """
    script = where / f"{tag}.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        shim = where / f"{tag}.bat"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                        encoding="utf-8")
    else:
        shim = where / f"{tag}.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                        encoding="utf-8")
        shim.chmod(0o755)
    return str(shim)


# ------------------------------------------------------------------ 打请求


def url(server: AppServer, path: str) -> str:
    return f"http://127.0.0.1:{server.port}{path}"


def request(server: AppServer, path: str, *, method: str = "GET",
            payload=None, raw: bytes | None = None, timeout: float = 5.0,
            headers: dict[str, str] | None = None):
    """打一个请求,返回 (状态码, 响应体 bytes)。4xx/5xx 也照样返回,不抛。

    ``payload`` 会被 ``json.dumps`` 一道;``raw`` 是原样发出去的字节,给
    "发一段根本不是 JSON 的东西看服务怎么办"这种测试用;``headers`` 给鉴权
    那组测试带 ``Authorization`` 和 ``Host``。
    """
    body = raw if raw is not None else (
        None if payload is None else json.dumps(payload).encode("utf-8"))
    req = urllib.request.Request(url(server, path), data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
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
