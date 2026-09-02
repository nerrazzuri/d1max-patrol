"""app 的 HTTP 外壳:一份状态快照、一条事件流、一堆静态文件。

**为什么是 ``http.server`` 而不是 FastAPI。** 这个 app 要能在机器狗自带的
Orin 上、在客户现场没有外网的机器上直接跑起来。多一个第三方依赖,就多一次
"现场装不上"的可能。页面就一个人看,并发是个位数,标准库足够。

**为什么是 ``ThreadingHTTPServer``。** SSE 是一条**长连**:一个客户端会占着
一条连接不放。单线程的 ``HTTPServer`` 上,第一个打开页面的人会把服务堵死,
后面谁都连不上。一客户端一线程,这个问题就不存在了。

**线程与 asyncio 的分界。** 每个请求都在自己的线程里跑,而引擎和后端全是
asyncio 的。分界线只有一条:``ctx.bridge``(见 ``app/bridge.py``)。这个模块
里**没有一处**直接 ``await`` 后端,也没有一处在 HTTP 线程里碰引擎的内部状态
—— 要么读 ``_StateHub`` 备好的快照,要么经 ``bridge.call`` 回到循环线程。

**为什么默认只听 127.0.0.1。** 这些接口能让机器狗走起来,而且**没有任何
认证**。绑到 0.0.0.0 等于把遥控器放在网上,所以那需要显式指定,并且会打印
一行警告。

本模块只管外壳:路由、错误体、静态文件、状态快照、事件流。业务路由由后面
的模块往 ``AppServer.route`` 上挂。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import mimetypes
import re
import sys
import threading
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.app.mapping import (
    MappingConfig,
    MappingError,
    MappingOrchestrator,
)
from d1max_patrol.app.procs import ProcManager
from d1max_patrol.app.teleop import DEFAULT_PULSE_S, Teleop, TeleopBusy
from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    BackendReconnected,
    BatteryEvent,
    ControlLostEvent,
    DeviceBackend,
    DevicePoseEvent,
    EventEmitter,
    FaultEvent,
    LocStatusEvent,
    MappingStatusEvent,
    NavBackend,
    NavStatusEvent,
)
from d1max_patrol.backends.map_bridge import MapBridgeClient
from d1max_patrol.engine.machine import EngineBusy, MissionEngine

#: 静态文件目录。模块导入时就 resolve,后面判越界拿它当基准。
STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8095

#: 只听这些地址算"只给本机用"。别的地址一律警告。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 请求体上限。这些接口收的都是任务 JSON,几十 KB 顶天了。
MAX_BODY = 4 * 1024 * 1024

#: 快照重建的节拍。链路通断这类"没有事件的变化"靠它发现。
_TICK_S = 0.5

#: 日志接口默认给多少字节的尾巴。够看清最近几百行,又不至于撑爆页面。
LOG_TAIL_BYTES = 64 * 1024

#: 进程名允许的形状。它要拼进文件名,松一点就是一条路径穿越。
_PROC_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


# ------------------------------------------------------------------ 请求与响应


class HttpError(Exception):
    """一个能直接变成响应体的错误。

    ``error`` 是给人看的一句话,``detail`` 是给排查用的补充。**堆栈永远不进
    响应体** —— 它进服务端日志。
    """

    def __init__(self, status: int, error: str, detail: str = "") -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.detail = detail

    def to_wire(self) -> dict[str, str]:
        return {"error": self.error, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Request:
    """一个已经解析好的请求。处理函数只跟它打交道。"""

    method: str
    path: str
    #: 路径里 ``<name>`` 占位符捕获到的值。
    params: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        """请求体当 JSON 解。空体算空对象 —— 很多 POST 本来就没内容。"""
        if not self.body.strip():
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, "请求体不是合法 JSON", str(exc)) from exc


@dataclass(frozen=True, slots=True)
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Stream:
    """一条长连响应(SSE)。生成器吐一个 dict,就是一帧 ``data:``。

    生成器**必须**能被 ``close()`` 打断:客户端随时会关标签页,那时它正卡在
    某个 ``next()`` 上,收尾逻辑只能写在 ``finally`` 里。
    """

    events: Iterator[dict[str, Any]]


def json_response(payload: Any, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(status, body)


def _error_response(exc: HttpError) -> Response:
    return json_response(exc.to_wire(), exc.status)


Handler = Callable[["Request"], "Response | Stream"]


def _number(body: Mapping[str, Any], name: str, default: float = 0.0) -> float:
    """从 JSON 体里取一个数。取不到就是 400,不是 500。

    ``bool`` 单独挡掉:它是 ``int`` 的子类,``{"fwd": true}`` 会静默变成 1.0,
    也就是"闷头往前冲"。
    """
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HttpError(400, f"{name} 得是一个数", repr(value))
    return float(value)


def _text(body: Any, name: str) -> str:
    """从 JSON 体里取一个非空字符串。"""
    if not isinstance(body, dict):
        raise HttpError(400, "请求体得是一个对象", type(body).__name__)
    value = body.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise HttpError(400, f"{name} 得是一个非空字符串", repr(value))
    return value.strip()


@dataclass(frozen=True, slots=True)
class _Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler


def _compile(pattern: str) -> re.Pattern[str]:
    """``/api/runs/<run_id>/photos/<name>`` -> 正则。

    占位符**不跨斜杠**。这一条顺手堵住了一类穿越:``/api/runs/r1/photos/
    ..%2F..%2Fmanifest.json`` 解码之后带斜杠,于是根本匹配不上这条路由。
    """
    parts = []
    for chunk in re.split(r"(<[a-z_]+>)", pattern):
        if chunk.startswith("<") and chunk.endswith(">"):
            parts.append(f"(?P<{chunk[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(chunk))
    return re.compile("^" + "".join(parts) + "$")


# ------------------------------------------------------------------ 上下文


@dataclass
class AppContext:
    """app 手里的全部零件。

    这些对象的生死**不归 app 管**:谁造的谁负责关。app 只是把它们接到 HTTP
    上。这样测试里换成假件、现场换成真件,都不用动这个模块。
    """

    bridge: LoopBridge
    engine: MissionEngine
    nav: NavBackend
    device: DeviceBackend
    maps: MapBridgeClient
    procs: ProcManager
    teleop: Teleop
    mapping: MappingOrchestrator
    missions_dir: Path
    runs_root: Path


# ------------------------------------------------------------------ 状态汇总


class _StateHub:
    """把引擎、导航、设备三条事件流汇成一条,并且始终备着一份全量快照。

    **为什么要备快照,而不是请求来了现问后端。** ``nav_status()`` 这类方法在
    厂商后端上是一次真实的 WebSocket 往返:页面每秒问一次,就等于每秒往链路
    上压一条请求,而链路正忙着导航。快照是事件推出来的,读它不花一分钱。

    快照每次都**整个重建、整体替换**。HTTP 线程只读 ``snapshot`` 这一个引用,
    读到的要么是旧的一整份、要么是新的一整份,不会读到改了一半的。
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self.events: EventEmitter[dict[str, Any]] = EventEmitter()
        self._snapshot: dict[str, Any] = {}
        self._tasks: list[asyncio.Task[None]] = []
        # 下面这些量只在事件里出现,后端不提供只读属性,所以要自己记着。
        self._nav_status: str | None = None
        self._loc_status: str | None = None
        self._mapping: str | None = None
        self._alg_errors: tuple[str, ...] = ()
        self._link_down: str = ""
        self._battery: float | None = None
        self._faults: tuple[str, ...] = ()
        self._control_lost: str = ""
        self._pose: dict[str, float] | None = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._rebuild(force=True)
        ctx = self._ctx
        self._tasks = [
            asyncio.create_task(self._watch(ctx.engine)),
            asyncio.create_task(self._watch(ctx.nav, self._on_nav)),
            asyncio.create_task(self._watch(ctx.device, self._on_device)),
            asyncio.create_task(self._tick()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks = []
        # 让还挂在 SSE 上的那些 HTTP 线程有机会收尾,而不是等进程被杀。
        self.events.emit({"kind": "bye"})

    @property
    def snapshot(self) -> dict[str, Any]:
        return self._snapshot

    # ------------------------------------------------------------ 事件

    async def _watch(self, emitter: Any,
                     on_event: Callable[[Any], None] | None = None) -> None:
        """跟一条事件流。引擎那条不用记什么 —— 它的状态在 ``engine.snapshot``。"""
        with emitter.subscription() as inbox:
            while True:
                event = await inbox.get()
                if on_event is not None:
                    on_event(event)
                self._rebuild()

    async def _tick(self) -> None:
        """定期重建。

        链路断没断、子进程还在不在,这些是**属性**不是事件,没人会推给我们。
        只有变了才发,所以静止的时候这条 SSE 是安静的。
        """
        while True:
            await asyncio.sleep(_TICK_S)
            self._rebuild()

    def _on_nav(self, event: Any) -> None:
        if isinstance(event, NavStatusEvent):
            self._nav_status = event.status.name
        elif isinstance(event, LocStatusEvent):
            self._loc_status = event.status.name
        elif isinstance(event, MappingStatusEvent):
            self._mapping = event.status.name
        elif isinstance(event, AlgErrorEvent):
            self._alg_errors = tuple(str(item) for item in event.items)
        elif isinstance(event, BackendDisconnected):
            self._link_down = event.reason
        elif isinstance(event, BackendReconnected):
            self._link_down = ""

    def _on_device(self, event: Any) -> None:
        if isinstance(event, BatteryEvent):
            self._battery = event.percent
        elif isinstance(event, FaultEvent):
            self._faults = tuple(event.items)
        elif isinstance(event, ControlLostEvent):
            # 控制权拿不回来(清单 #46/#47),所以这条不会自己消失,只会被
            # 下一次抢到控制权覆盖掉。页面要一直显眼地摆着它。
            self._control_lost = event.reason
        elif isinstance(event, DevicePoseEvent):
            pose = event.pose
            self._pose = {"x": pose.position.x, "y": pose.position.y,
                          "yaw": pose.yaw}

    # ------------------------------------------------------------ 快照

    def _rebuild(self, *, force: bool = False) -> None:
        snap = self._build()
        if not force and snap == self._snapshot:
            return
        self._snapshot = snap
        self.events.emit({"kind": "state", **snap})

    def _build(self) -> dict[str, Any]:
        ctx = self._ctx
        return {
            "run": ctx.engine.snapshot.to_wire(),
            "nav": {
                "connected": ctx.nav.connected,
                "nav_status": self._nav_status,
                "loc_status": self._loc_status,
                "mapping": self._mapping,
                "alg_errors": list(self._alg_errors),
                "link_down": self._link_down,
            },
            "device": {
                "connected": ctx.device.connected,
                "battery": self._battery,
                "faults": list(self._faults),
                "control_lost": self._control_lost,
                "pose": self._pose,
            },
            "backend": {
                "nav": type(ctx.nav).__name__,
                "device": type(ctx.device).__name__,
                "map_bridge": ctx.maps.connected,
                "procs": ctx.procs.running(),
            },
            "caps": sorted(ctx.nav.capabilities),
        }


# ------------------------------------------------------------------ 服务


class _HTTPServer(ThreadingHTTPServer):
    #: 端口刚被上一次 app 释放时不至于起不来。
    allow_reuse_address = True
    #: SSE 那些线程可能正卡在写上,不能让它们拖住退出。
    daemon_threads = True

    app: AppServer


class AppServer:
    """HTTP 外壳。路由表由自己和后面几个模块一起填。"""

    def __init__(self, ctx: AppContext, *, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT) -> None:
        self._ctx = ctx
        self._host = host
        self._want_port = port
        self._httpd: _HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._hub = _StateHub(ctx)
        self._routes: list[_Route] = []
        self._register_routes()

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._httpd is not None:
            return
        if not self._ctx.bridge.running:
            raise RuntimeError("线程桥没在跑,先 bridge.start()")
        self._httpd = _HTTPServer((self._host, self._want_port), _RequestHandler)
        self._httpd.app = self
        if self._host not in _LOCAL_HOSTS:
            print(f"警告:app 正听在 {self._host},这些接口没有任何认证,"
                  f"而且能让机器狗走起来。别把它暴露到公网或不受控的网段。")
        self._ctx.bridge.call(self._hub.start)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="d1max-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        if self._ctx.bridge.running:
            with contextlib.suppress(Exception):
                self._ctx.bridge.call(self._hub.stop)
        httpd.shutdown()
        httpd.server_close()
        if self._thread is not None:
            self._thread.join(5.0)
            self._thread = None

    def __enter__(self) -> AppServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("还没启动")
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def ctx(self) -> AppContext:
        return self._ctx

    @property
    def hub(self) -> _StateHub:
        return self._hub

    # ------------------------------------------------------------ 路由

    def route(self, method: str, pattern: str, handler: Handler) -> None:
        """挂一条路由。后面几个模块就是靠它把业务接口接进来的。"""
        self._routes.append(_Route(method.upper(), _compile(pattern), handler))

    def _register_routes(self) -> None:
        self.route("GET", "/", self._index)
        self.route("GET", "/api/state", self._state)
        self.route("GET", "/api/events", self._events)
        self.route("POST", "/api/teleop", self._teleop)
        self.route("POST", "/api/teleop/heartbeat", self._teleop_beat)
        self.route("POST", "/api/estop", self._estop)
        self.route("GET", "/api/mapping", self._mapping_state)
        self.route("POST", "/api/mapping/record/start", self._record_start)
        self.route("POST", "/api/mapping/record/stop", self._record_stop)
        self.route("POST", "/api/mapping/rebuild", self._rebuild)
        self.route("GET", "/api/procs/<name>/log", self._proc_log)

    def handle(self, method: str, path: str, query: Mapping[str, str],
               body: bytes) -> Response | Stream:
        """路由一个已经解码好的请求。找不到就抛 ``HttpError``。"""
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        others: set[str] = set()
        for route in self._routes:
            match = route.pattern.match(path)
            if match is None:
                continue
            if route.method != method:
                others.add(route.method)
                continue
            return route.handler(
                Request(method, path, match.groupdict(), query, body))
        if others:
            raise HttpError(405, f"{path} 不支持 {method}",
                            "支持的方法:" + "、".join(sorted(others)))
        raise HttpError(404, f"没有这个接口:{path}",
                        "页面在 /,状态在 /api/state,事件在 /api/events")

    # ------------------------------------------------------------ 处理函数

    def _index(self, _req: Request) -> Response:
        return self._static("index.html")

    def _state(self, _req: Request) -> Response:
        return json_response(self._hub.snapshot)

    def _events(self, _req: Request) -> Stream:
        """SSE。**第一帧就是当前全量状态**。

        不这么做的话,页面打开之后要一直等到下一次状态变化才知道现在是什么
        情况 —— 机器停着不动的时候,那就是永远。
        """
        hub = self._hub
        bridge = self._ctx.bridge

        def gen() -> Iterator[dict[str, Any]]:
            stream = bridge.subscribe(hub.events)
            try:
                yield {"kind": "state", **hub.snapshot}
                for event in stream:
                    if event.get("kind") == "bye":
                        return
                    yield event
            finally:
                stream.close()  # type: ignore[attr-defined]

        return Stream(gen())

    # -------------------------------------------------------------- 遥控

    def _teleop(self, req: Request) -> Response:
        """走一拍。

        参数校验放在这一层是有意的:``Teleop`` 收的是数,HTTP 收的是 JSON,
        "fwd 传了个字符串"是 HTTP 这一层的问题,不该让业务层去认识它。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体得是一个对象", type(body).__name__)
        fwd = _number(body, "fwd")
        lat = _number(body, "lat")
        yaw = _number(body, "yaw")
        seconds = _number(body, "seconds", DEFAULT_PULSE_S)
        teleop = self._ctx.teleop
        self._call(lambda: teleop.pulse(fwd, lat, yaw, seconds))
        return json_response({"ok": True})

    def _teleop_beat(self, _req: Request) -> Response:
        """续命。同步的 —— 它只是记一个时间戳,没必要过桥。"""
        self._ctx.teleop.heartbeat()
        return json_response({"ok": True})

    def _estop(self, _req: Request) -> Response:
        """红按钮:停遥控,并打断正在跑的任务。"""
        teleop = self._ctx.teleop
        self._call(lambda: teleop.emergency_stop("页面按了急停"))
        return json_response({"ok": True})

    # -------------------------------------------------------------- 建图

    def _mapping_state(self, _req: Request) -> Response:
        mapping = self._ctx.mapping
        return json_response(self._call(mapping.snapshot))

    def _record_start(self, req: Request) -> Response:
        name = _text(req.json(), "name")
        mapping = self._ctx.mapping
        bag = self._call(lambda: mapping.start_record(name), timeout_s=60.0)
        return json_response({"bag": bag.name})

    def _record_stop(self, _req: Request) -> Response:
        """停录。超时给得比别的接口长 —— rosbag2 要把 mcap 的索引写完才算完,
        中途掐断的包放不回去,而那一段路是走不回来的。"""
        mapping = self._ctx.mapping
        bag = self._call(mapping.stop_record, timeout_s=60.0)
        return json_response({"bag": bag.name})

    def _rebuild(self, req: Request) -> Response:
        """起一趟离线重建。**立刻返回 202**。

        这件事要跑几分钟到几十分钟,压在一个 HTTP 请求里必然超时。所以只
        同步验一遍参数(名字填错了要当场知道),然后丢到循环线程去跑。
        进展去 ``GET /api/mapping`` 看 ``phase``,失败原因看 ``last_error``。
        """
        body = req.json()
        bag_name = _text(body, "bag")
        map_id = _text(body, "map_id")
        mapping = self._ctx.mapping
        bag = self._call(lambda: mapping.plan_rebuild(bag_name, map_id))
        self._ctx.bridge.spawn(lambda: mapping.rebuild(bag, map_id))
        return json_response({"bag": bag.name, "map_id": map_id}, status=202)

    def _proc_log(self, req: Request) -> Response:
        """一个子进程日志的**尾巴**。

        只给尾巴不给全文:建图跑一小时的日志有几十兆,整个塞进响应体会把
        页面和这台机器一起拖垮。想看全的人手边就有那个文件。

        这里直接读文件、不过桥:``log_path`` 是纯拼路径,读文件也不碰循环
        线程里的任何状态 —— 子进程自己往里写,我们只是另开一个句柄读。
        """
        name = req.params["name"]
        if not _PROC_NAME.match(name):
            raise HttpError(400, "进程名不合法", name)
        path = self._ctx.procs.log_path(name)
        if not path.is_file():
            raise HttpError(404, f"没有这个进程的日志:{name}",
                            "它可能还没起过")
        raw_limit = req.query.get("bytes") or str(LOG_TAIL_BYTES)
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise HttpError(400, "bytes 得是一个整数", raw_limit) from exc
        limit = max(1, min(limit, MAX_BODY))
        with path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - limit))
            raw = fh.read()
        # ROS 的 C++ 节点不吃 PYTHONIOENCODING,日志里混进别的编码是常事。
        # 容错解码:看日志的人要的是内容,不是一个 500。
        return Response(200, raw.decode("utf-8", errors="replace").encode("utf-8"),
                        "text/plain; charset=utf-8")

    def _call(self, factory: Callable[[], Any], timeout_s: float = 10.0) -> Any:
        """过桥,并且把业务异常翻成 HTTP 状态。

        翻译只在这一处:业务层抛的是它自己的词(``TeleopBusy``、
        ``EngineBusy``),不该为了 HTTP 去背状态码。
        """
        try:
            return self._ctx.bridge.call(factory, timeout_s=timeout_s)
        except ValueError as exc:
            raise HttpError(400, "参数不对", str(exc)) from exc
        except (TeleopBusy, EngineBusy, MappingError) as exc:
            raise HttpError(409, "现在做不了这件事", str(exc)) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise HttpError(504, "后端没在规定时间内回话", str(exc)) from exc

    def _static(self, rel: str) -> Response:
        """取 static 目录下的文件。

        判越界用的是 ``resolve()`` 之后的 ``is_relative_to`` —— 自己拼字符串
        查 ``..`` 是永远补不完的,符号链接、``%2e%2e``、``....//`` 各有各的
        绕法,而解析成绝对路径之后只剩一个问题:它到底在不在这个目录里。
        """
        if not rel or rel.endswith("/"):
            raise HttpError(404, f"没有这个文件:{rel}")
        try:
            target = (STATIC_DIR / rel).resolve()
        except OSError as exc:      # 路径里有非法字符时 Windows 会抛
            raise HttpError(400, "路径不合法", str(exc)) from exc
        if not target.is_relative_to(STATIC_DIR):
            raise HttpError(403, "只能取 static 目录里的文件", rel)
        if not target.is_file():
            raise HttpError(404, f"没有这个文件:{rel}")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        return Response(200, target.read_bytes(), ctype)


# ------------------------------------------------------------------ HTTP 细节


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "d1max-app"
    sys_version = ""

    def do_GET(self) -> None:       # 方法名是基类定的
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认实现把每个请求都打到 stderr。页面一开就是刷屏,关掉。

        真正该留的是出错信息,那个在 ``_dispatch`` 里单独打。
        """

    # ------------------------------------------------------------ 分发

    def _dispatch(self, method: str) -> None:
        app: AppServer = self.server.app     # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        try:
            path = unquote(parsed.path)
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            result = app.handle(method, path, query, self._read_body())
        except HttpError as exc:
            self._send(_error_response(exc))
            return
        except Exception as exc:    # noqa: BLE001 - 兜底:一个请求出错不能带走服务
            traceback.print_exc()   # 堆栈只进服务端日志,不进响应体
            self._send(_error_response(HttpError(
                500, "服务端出错了,详情见 app 日志",
                f"{type(exc).__name__}: {exc}")))
            return
        if isinstance(result, Stream):
            self._send_stream(result)
        else:
            self._send(result)

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if not raw:
            return b""
        try:
            length = int(raw)
        except ValueError as exc:
            raise HttpError(400, "Content-Length 不是数字", raw) from exc
        if length > MAX_BODY:
            raise HttpError(413, "请求体太大", f"{length} > {MAX_BODY}")
        return self.rfile.read(length) if length > 0 else b""

    def _send(self, resp: Response) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            for name, value in resp.headers:
                self.send_header(name, value)
            self.end_headers()
            if resp.body:
                self.wfile.write(resp.body)

    def _send_stream(self, stream: Stream) -> None:
        """把生成器吐的 dict 一帧帧写成 SSE。

        没有 ``Content-Length``,所以这条连接写完就关(``Connection: close``)。
        写不出去(浏览器关了标签页)是正常收尾,不是错误 —— 但一定要走到
        ``finally`` 把生成器关掉,否则那条订阅会一直挂在事件源上。
        """
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for payload in stream.events:
                data = json.dumps(payload, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                    # 客户端走了,正常收尾
        finally:
            with contextlib.suppress(Exception):
                stream.events.close()   # type: ignore[attr-defined]


# ------------------------------------------------------------------ 入口


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max-app", description="D1 Max 巡检 app")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="监听地址。默认只听本机 —— 换成别的会打印警告")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--nav", choices=("vendor", "local"), default="vendor",
                   help="导航后端:vendor=厂商导航(甲路线),local=自建(乙路线)")
    p.add_argument("--nav-url", help="厂商导航 WebSocket 地址")
    p.add_argument("--agent-host", default="127.0.0.1", help="旁路进程地址")
    p.add_argument("--agent-port", type=int, default=8090)
    p.add_argument("--pose-bridge", default="127.0.0.1:8091",
                   help="自建导航用的定位桥 host:port")
    p.add_argument("--map-bridge", default="127.0.0.1:8092",
                   help="地图桥 host:port")
    p.add_argument("--maps-dir", default="runs/slam", help="自建地图目录")
    p.add_argument("--bags-dir", default="runs/bags", help="录包落盘目录")
    p.add_argument("--params-file", default="config/params/mapper_3d.yaml",
                   help="建图用的 slam_toolbox 参数模板")
    p.add_argument("--missions-dir", default="missions")
    p.add_argument("--runs-root", default="runs")
    p.add_argument("--log-dir", help="子进程日志目录(默认 <runs-root>/logs)")
    return p


def _hostport(text: str, what: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"{what} 要写成 host:port,收到的是 {text!r}")
    return host, int(port)


async def _make_engine(nav: NavBackend, device: DeviceBackend,
                       runs_root: Path) -> MissionEngine:
    """在循环线程里造引擎 —— 它内部那个队列要绑在这条循环上。"""
    # 相机源还没接(Task 16 的 RTSP 抽帧)。拍照点位现在会**明确判失败**,
    # 而不是悄悄跳过 —— 见 MissionEngine 的 media 说明。
    return MissionEngine(nav, device, {}, runs_root)


async def _make_teleop(device: DeviceBackend, engine: MissionEngine) -> Teleop:
    return Teleop(device, engine)


def main(argv: Sequence[str] | None = None) -> int:
    """``d1max-app`` 的入口。

    **后端连不上不算失败。** app 要能在机器狗关着的时候起来 —— 看历史报告、
    编任务、查日志都不需要机器。连不上只是页面上少一块绿灯。
    """
    from d1max_patrol.backends.local_nav import LocalNavBackend
    from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
    from d1max_patrol.backends.vendor_nav import VendorNavBackend
    from d1max_patrol.config.models import NavConfig

    args = _build_parser().parse_args(argv)
    runs_root = Path(args.runs_root)
    log_dir = Path(args.log_dir) if args.log_dir else runs_root / "logs"
    map_host, map_port = _hostport(args.map_bridge, "--map-bridge")
    pose_host, pose_port = _hostport(args.pose_bridge, "--pose-bridge")

    bridge = LoopBridge()
    bridge.start()
    device = SidecarDeviceBackend(args.agent_host, args.agent_port)
    if args.nav == "vendor":
        nav_cfg = NavConfig(url=args.nav_url) if args.nav_url else NavConfig()
        nav: NavBackend = VendorNavBackend(nav_cfg)
    else:
        nav = LocalNavBackend(device, maps_dir=args.maps_dir,
                              pose_host=pose_host, pose_port=pose_port)
    maps = MapBridgeClient(map_host, map_port)
    procs = ProcManager(log_dir)
    engine = bridge.call(lambda: _make_engine(nav, device, runs_root))
    # 遥控要在循环线程里造:它一上来就往引擎上挂检查,还会起后台看门狗。
    teleop = bridge.call(lambda: _make_teleop(device, engine))

    mapping = MappingOrchestrator(procs, MappingConfig(
        bags_dir=Path(args.bags_dir), maps_dir=Path(args.maps_dir),
        params_template=Path(args.params_file)))

    ctx = AppContext(bridge=bridge, engine=engine, nav=nav, device=device,
                     maps=maps, procs=procs, teleop=teleop, mapping=mapping,
                     missions_dir=Path(args.missions_dir), runs_root=runs_root)
    server = AppServer(ctx, host=args.host, port=args.port)
    server.start()
    print(f"app 起来了:{server.url}")
    for what, opener in (("导航", nav.connect), ("旁路进程", device.connect),
                         ("地图桥", maps.connect)):
        try:
            bridge.call(opener, timeout_s=10.0)
        except Exception as exc:    # noqa: BLE001 - 连不上不影响 app 起来
            print(f"{what}没连上({type(exc).__name__}: {exc}),页面上会显示未连接")

    stop = threading.Event()
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C,收尾中……")
    finally:
        server.stop()
        with contextlib.suppress(Exception):
            bridge.call(teleop.aclose, timeout_s=5.0)
        for closer in (maps.close, nav.close, device.close):
            with contextlib.suppress(Exception):
                bridge.call(closer, timeout_s=5.0)
        # 子进程是 app 起的,app 走了它们不能留 —— 留下就占着话题和端口。
        with contextlib.suppress(Exception):
            bridge.call(procs.stop_all, timeout_s=15.0)
        bridge.stop()
    return 0


if __name__ == "__main__":      # pragma: no cover - 入口
    sys.exit(main())


__all__ = ["AppContext", "AppServer", "HttpError", "Request", "Response",
           "Stream", "json_response", "main"]
