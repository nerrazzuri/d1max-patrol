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
import os
import re
import sys
import threading
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from d1max_patrol.app.auth import (
    AUTH_PATH,
    Denied,
    Guard,
    normalize_pin,
)
from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.app.gridmap import GridError, from_frame, load_saved
from d1max_patrol.app.identity import NICKNAME_ENV, SN_ENV, Identity, resolve
from d1max_patrol.app.mapping import (
    MappingConfig,
    MappingError,
    MappingOrchestrator,
)
from d1max_patrol.app.procs import ProcManager
from d1max_patrol.app.teleop import DEFAULT_PULSE_S, Teleop, TeleopBusy
from d1max_patrol.app.video import CAMERAS, MjpegSource, RtspStill, VideoError
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
    MediaSource,
    NavBackend,
    NavBackendError,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.backends.map_bridge import MapBridgeClient
from d1max_patrol.engine.archive import (
    list_runs,
    read_events,
    read_manifest,
    read_state,
)
from d1max_patrol.engine.machine import EngineBusy, MissionEngine
from d1max_patrol.engine.mission import (
    Mission,
    MissionError,
    load_mission,
    parse_mission,
    save_mission,
)
from d1max_patrol.engine.preflight import PreflightReport, run_preflight
from d1max_patrol.inspect.judge import (
    judge_run,
    read_findings,
    read_reviews,
    save_review,
)
from d1max_patrol.inspect.report import write_reports

#: 静态文件目录。模块导入时就 resolve,后面判越界拿它当基准。
STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8095

#: 只听这些地址算"只给本机用"。别的地址都必须配 PIN。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 从环境变量里读 PIN。命令行上的 --pin 在 ``ps`` 里是明文可见的,同一台机器
#: 上的别的账号能直接看到;板载常驻的时候用环境变量更合适。
PIN_ENV = "D1MAX_PIN"

#: 请求体上限。这些接口收的都是任务 JSON,几十 KB 顶天了。
MAX_BODY = 4 * 1024 * 1024

#: 快照重建的节拍。链路通断这类"没有事件的变化"靠它发现。
_TICK_S = 0.5

#: 日志接口默认给多少字节的尾巴。够看清最近几百行,又不至于撑爆页面。
LOG_TAIL_BYTES = 64 * 1024

#: 进程名允许的形状。它要拼进文件名,松一点就是一条路径穿越。
_PROC_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

#: 会被拼进路径的 id 里许可的字符:字母、数字、下划线、横杠、点,加中文。
#: 现场的任务名和图名就是中文("1号配电室"),不放行等于逼人改用拼音。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-.一-鿿]+$")

#: 运行 id 里"任务名"和"时间戳"之间的分隔。归档目录是两层
#: (``runs/<任务名>/<时间戳>``),而 URL 的一段里放不下斜杠。
RUN_SEP = "__"

#: MJPEG 各帧之间的分隔串。内容里不可能出现它,所以固定死就够了。
_BOUNDARY = b"d1maxframe"


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
    #: 原始请求头,**键名已经归一成小写**(见 ``app/auth.py`` 的 ``bearer``)。
    headers: Mapping[str, str] = field(default_factory=dict)
    #: 请求从哪个 IP 来。PIN 试错限速按它记。
    client: str = ""

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
class ByteStream:
    """一条长连的**二进制**响应(MJPEG)。

    和 :class:`Stream` 的区别只在编码:那个把 dict 写成 ``data:`` 行,这个
    把字节原样写出去。收尾要求一模一样 —— 生成器必须能被 ``close()`` 打断,
    因为杀 ffmpeg 的逻辑只写在它的 ``finally`` 里。
    """

    chunks: Iterator[bytes]
    content_type: str


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


Handler = Callable[["Request"], "Response | Stream | ByteStream"]


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


def _safe_id(value: str, what: str) -> str:
    """把一个要拼进路径的 id 验一遍。

    白名单,不是黑名单。放行中文之后,"查一遍 ``..`` 和斜杠"这种做法就更靠不
    住了 —— 编码方式太多(``%2e%2e``、``....//``、全角点),补不完。只认列出来
    的那几类字符,别的一律不要。

    ``..`` 单独再挡一道:它每个字符都在白名单里,但拼出来就是往上一层走。
    """
    value = value.strip()
    if not value or ".." in value or not _SAFE_ID.match(value):
        raise HttpError(400, f"{what}不合法",
                        "只能用字母、数字、下划线、横杠、点和中文,"
                        f"给的是 {value!r}")
    return value


def _checks_wire(report: PreflightReport) -> list[dict[str, Any]]:
    """起飞检查的结论,原样给页面。

    只给"过没过"是不够的:人站在狗旁边,要知道的是**哪一项**没过、当时是
    什么状态 —— 不然只能一项项自己试。
    """
    return [{"name": c.name, "ok": c.ok, "detail": c.detail}
            for c in report.checks]


def _run_id(run_dir: Path) -> str:
    """归档目录 -> URL 里的运行 id。见 :data:`RUN_SEP`。"""
    return f"{run_dir.parent.name}{RUN_SEP}{run_dir.name}"


#: MJPEG 的响应头。``<img>`` 认这个,一帧一换。
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={_BOUNDARY.decode()}"


def _close(it: Iterator[Any]) -> None:
    """关掉一个生成器。关不上也不能让它盖住真正的错误。"""
    with contextlib.suppress(Exception):
        it.close()      # type: ignore[attr-defined]


def _multipart(first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    """把一串 JPEG 包成 ``multipart/x-mixed-replace``。

    第一帧是单独传进来的:调用方已经先取了它,用来判断这条流到底起没起来。

    ``finally`` 里必须把 ``rest`` 关掉 —— 人关掉标签页时,杀 ffmpeg 的逻辑
    就挂在那个生成器的 ``finally`` 上。
    """
    def part(jpg: bytes) -> bytes:
        return (b"--" + _BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                + jpg + b"\r\n")

    try:
        yield part(first)
        for jpg in rest:
            yield part(jpg)
    finally:
        _close(rest)


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
    #: 相机名 -> 那一路 RTSP 流。没配的相机在页面上是一句"没配地址",
    #: 而不是一个转不出来的图标。
    video: Mapping[str, MjpegSource] = field(default_factory=dict)
    #: 这是哪只狗。手机按它认机器、给归档分组 —— 每只 D1 Max 的内网地址
    #: 都一样,靠地址分不出谁是谁(见 ``app/identity.py``)。
    identity: Identity = field(default_factory=resolve)


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


def check_exposure(host: str, pin: str | None) -> None:
    """绑到局域网就必须有 PIN。不合规直接 ``SystemExit``。

    **是拒绝启动,不是打条警告。** 警告没人看,而这一条看漏的后果是:狗热点的
    密码是 12345678、还印在我们自己的手册里,射程之内任何人都能把机器开走,
    也能把变电站里拍的照片整包拉走。

    提成函数是为了让 ``main()`` 在连后端之前就能查一次 —— 不然要等到把线程桥
    和三个后端都拉起来之后才报错,那时候看到的是一串莫名其妙的连接超时。
    """
    if host not in _LOCAL_HOSTS and not pin:
        raise SystemExit(
            f"拒绝启动:--host {host} 会把接口暴露到网络上,而这些接口能让机器"
            f"狗走起来、能把现场照片整包拉走。加上 --pin(或设环境变量 "
            f"{PIN_ENV})再来。只想自己本机用就别改 --host。")
    if pin is not None:
        try:
            normalize_pin(pin)
        except ValueError as exc:
            raise SystemExit(f"--pin 不合规:{exc}") from None


class _HTTPServer(ThreadingHTTPServer):
    #: 端口刚被上一次 app 释放时不至于起不来。
    allow_reuse_address = True
    #: SSE 那些线程可能正卡在写上,不能让它们拖住退出。
    daemon_threads = True

    app: AppServer


class AppServer:
    """HTTP 外壳。路由表由自己和后面几个模块一起填。"""

    def __init__(self, ctx: AppContext, *, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT, pin: str | None = None) -> None:
        check_exposure(host, pin)
        self._ctx = ctx
        self._auth = Guard(pin)
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
            print(f"app 正听在 {self._host}:{self.port},已启用 PIN。"
                  f"这些接口能让机器狗走起来 —— 别把它暴露到公网。")
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
        self.route("POST", AUTH_PATH, self._auth_unlock)
        self.route("GET", "/api/identity", self._identity)
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
        self.route("GET", "/api/video/<name>", self._video)
        self.route("GET", "/api/missions", self._missions)
        self.route("GET", "/api/missions/<mid>", self._mission_get)
        self.route("PUT", "/api/missions/<mid>", self._mission_put)
        self.route("POST", "/api/missions/<mid>/run", self._mission_run)
        self.route("POST", "/api/run/pause", self._run_pause)
        self.route("POST", "/api/run/resume", self._run_resume)
        self.route("POST", "/api/run/abort", self._run_abort)
        self.route("GET", "/api/maps", self._maps)
        self.route("POST", "/api/maps/load", self._map_load)
        self.route("GET", "/api/maps/<map_id>/grid", self._map_grid)
        self.route("POST", "/api/pose/initial", self._pose_initial)
        self.route("POST", "/api/pose/reset", self._pose_reset)
        self.route("GET", "/api/runs", self._runs)
        self.route("GET", "/api/runs/<run_id>", self._run_detail)
        self.route("GET", "/api/runs/<run_id>/photos/<name>", self._run_photo)
        self.route("GET", "/api/runs/<run_id>/report.<fmt>", self._run_report)
        self.route("POST", "/api/runs/<run_id>/judge", self._run_judge)
        self.route("POST", "/api/runs/<run_id>/review/<name>", self._run_review)

    def handle(self, method: str, path: str, query: Mapping[str, str],
               body: bytes, headers: Mapping[str, str] | None = None,
               client: str = "") -> Response | Stream:
        """路由一个已经解码好的请求。找不到就抛 ``HttpError``。

        **鉴权就在这一处。** 所有请求都从这儿过,放在这里不会漏 —— 而白名单
        式的"给某几个接口加检查"迟早会漏掉新加的那个,偏偏新加的往往最危险。
        """
        headers = headers or {}
        try:
            self._auth.gate(method, path, headers, query)
        except Denied as exc:
            raise HttpError(exc.status, exc.error, exc.detail) from None
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
            return route.handler(Request(
                method, path, match.groupdict(), query, body,
                headers=headers, client=client))
        if others:
            raise HttpError(405, f"{path} 不支持 {method}",
                            "支持的方法:" + "、".join(sorted(others)))
        raise HttpError(404, f"没有这个接口:{path}",
                        "页面在 /,状态在 /api/state,事件在 /api/events")

    # ------------------------------------------------------------ 处理函数

    def _index(self, _req: Request) -> Response:
        return self._static("index.html")

    def _auth_unlock(self, req: Request) -> Response:
        """PIN 换 token。**这是唯一一个不要 token 的 /api/ 接口。**"""
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象", '形如 {"pin": "..."}')
        try:
            token = self._auth.unlock(body.get("pin"), req.client or "?")
        except Denied as exc:
            raise HttpError(exc.status, exc.error, exc.detail) from None
        return json_response({"token": token})

    def _identity(self, _req: Request) -> Response:
        """这是哪只狗。手机拿它认机器、给拉回去的归档分组。

        **放在 PIN 后面。** 认狗这件事手机用热点的 BSSID 就够了 —— 连上之前
        就看得见,不用问服务端。既然如此,没必要让射程之内任何人白拿到机身
        序列号和一串网卡地址。
        """
        return json_response(self._ctx.identity.to_wire())

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

    # ---------------------------------------------------------------- 画面

    def _video(self, req: Request) -> ByteStream:
        """一路 MJPEG,直接喂 ``<img src="/api/video/front">``。

        **先取一帧再发响应头。** ffmpeg 装没装、流拉不拉得到、观众超没超上限,
        这三件事都要到第一帧才见分晓;头一旦发出去就只能是 200,那时再出错,
        页面上看到的就是一个不动的破图标而不是一句人话。
        """
        name = req.params["name"]
        if name not in CAMERAS:
            raise HttpError(404, "不认识的相机",
                            f"只有 {'、'.join(CAMERAS)},给的是 {name!r}")
        source = self._ctx.video.get(name)
        if source is None:
            raise HttpError(503, f"{name} 相机没配地址",
                            "起 app 时用 --camera-host 指到推流的那台机器")
        frames = source.stream()
        try:
            first = next(frames)
        except VideoError as exc:
            _close(frames)
            raise HttpError(503, str(exc)) from exc
        except StopIteration as exc:
            _close(frames)
            raise HttpError(503, f"拉不到 {name} 相机的画面",
                            "ffmpeg 起来了但一帧没出") from exc
        return ByteStream(_multipart(first, frames), MJPEG_CONTENT_TYPE)

    # ---------------------------------------------------------------- 任务

    def _mission_path(self, mid: str) -> Path:
        return self._ctx.missions_dir / f"{_safe_id(mid, '任务名')}.yaml"

    def _load_mission(self, mid: str) -> Mission:
        """按 id 读一份任务。读不出来的原因原样往上抛。

        文件坏了给的是 400 不是 500:任务文件是**人手写的**(不引数据库的全部
        理由就是这个),而"缺少 map_id"这种话得原样出现在页面上,包在一个
        "服务器内部错误"里没人查得动。
        """
        path = self._mission_path(mid)
        if not path.is_file():
            raise HttpError(404, f"没有这个任务:{path.stem}",
                            f"任务放在 {self._ctx.missions_dir}")
        try:
            return load_mission(path)
        except MissionError as exc:
            raise HttpError(400, f"任务定义不合法:{exc}", str(path)) from exc

    def _missions(self, _req: Request) -> Response:
        """任务列表。只给 id —— 详情一份份取,列表页不需要全文。"""
        root = self._ctx.missions_dir
        names = sorted(p.stem for p in root.glob("*.yaml")) if root.is_dir() else []
        return json_response({"missions": names})

    def _mission_get(self, req: Request) -> Response:
        return json_response(self._load_mission(req.params["mid"]).to_wire())

    def _mission_put(self, req: Request) -> Response:
        """存一份任务。**先验后写**。

        验不过的任务一个字节都不落盘:页面上编到一半点了保存,写进去的半成品
        会在下一次"起任务"时才炸,而那时人已经在外面等着了。
        """
        mid = _safe_id(req.params["mid"], "任务名")
        raw = req.json()
        if not isinstance(raw, dict):
            raise HttpError(400, "任务定义得是一个对象", type(raw).__name__)
        raw = dict(raw)
        # 文件名就是任务名。对不上就直说 —— 悄悄以哪一个为准都会让人找不到
        # 自己刚存的东西(照片按任务名归档,文件按 id 存)。
        name = raw.setdefault("mission", mid)
        if name != mid:
            raise HttpError(400, "任务名和文件名对不上",
                            f"路径里是 {mid},定义里是 {name!r}")
        try:
            mission = parse_mission(raw)
        except MissionError as exc:
            raise HttpError(400, f"任务定义不合法:{exc}") from exc
        save_mission(mission, self._ctx.missions_dir / f"{mid}.yaml")
        return json_response(mission.to_wire())

    def _mission_run(self, req: Request) -> Response:
        """起一趟巡检。**起飞检查没过就不起**,并且把没过的项原样交给页面。

        检查结果连成功的一次也带回去:人在现场要的是"这五项现在都什么样",
        不是一个"好了"。
        """
        mission = self._load_mission(req.params["mid"])
        ctx = self._ctx
        report = self._call(
            lambda: run_preflight(ctx.nav, ctx.device, mission, ctx.runs_root),
            timeout_s=30.0)
        checks = _checks_wire(report)
        if not report.ok:
            return json_response({
                "error": "起飞检查没过",
                "detail": ";".join(f"{c.name}: {c.detail}"
                                    for c in report.failures),
                "checks": checks,
            }, status=409)
        self._call(lambda: ctx.engine.start(mission), timeout_s=30.0)
        return json_response({"run": ctx.engine.snapshot.to_wire(),
                              "checks": checks})

    # ---------------------------------------------------------------- 运行

    def _run_pause(self, _req: Request) -> Response:
        return self._run_cmd(self._ctx.engine.pause)

    def _run_resume(self, _req: Request) -> Response:
        return self._run_cmd(self._ctx.engine.resume)

    def _run_abort(self, req: Request) -> Response:
        body = req.json()
        raw = body.get("reason", "") if isinstance(body, dict) else ""
        reason = raw.strip() if isinstance(raw, str) and raw.strip() else "页面上点了中止"
        return self._run_cmd(lambda: self._ctx.engine.abort(reason))

    def _run_cmd(self, factory: Callable[[], Any]) -> Response:
        """暂停 / 继续 / 中止 共用的一段。

        没任务在跑就是 409,不是静默成功:页面上点了"暂停"却什么都没发生,
        人会以为已经停了。
        """
        if not self._ctx.engine.running:
            raise HttpError(409, "现在没有任务在跑")
        self._call(factory)
        return json_response(self._ctx.engine.snapshot.to_wire())

    # ---------------------------------------------------------------- 地图

    def _maps(self, _req: Request) -> Response:
        """地图列表,**带着当前后端的能力集**。

        "建图"、"改名"、"重定位"这几个按钮该不该能点,取决于接的是哪条路线的
        后端(厂商导航 / 自建导航),不该由页面自己猜。
        """
        nav = self._ctx.nav
        maps = self._call(nav.list_maps, timeout_s=15.0)
        return json_response({"maps": list(maps),
                              "caps": sorted(nav.capabilities)})

    def _map_load(self, req: Request) -> Response:
        map_id = _safe_id(_text(req.json(), "map_id"), "图名")
        self._call(lambda: self._ctx.nav.load_map(map_id), timeout_s=60.0)
        return json_response({"map_id": map_id})

    def _map_grid(self, req: Request) -> Response:
        """一张图的占据栅格,给页面画底图用。

        **先找盘上存好的,再退回建图桥推的那一帧。** 存好的图是按名字取的,
        取到的一定是这一张;桥推的那帧是"现在正在长的那张",建图还没存盘时
        只有它。响应里的 ``source`` 说明这次给的是哪一种,页面照着标。
        """
        map_id = _safe_id(req.params["map_id"], "图名")
        try:
            grid = load_saved(self._ctx.mapping.maps_dir, map_id)
        except GridError as exc:
            frame = self._ctx.maps.latest
            if frame is None:
                raise HttpError(404, f"画不出 {map_id} 的底图", str(exc)) from exc
            grid = from_frame(frame)
        return json_response(grid.to_wire())

    def _pose_initial(self, req: Request) -> Response:
        """给定位一个初始猜测。见 ``docs/建图定位与巡检管线.md`` §4。

        这是**自建路线**的重定位方式:往 ``/initialpose`` 发一条位姿。厂商
        路线上等价的动作是 :meth:`_pose_reset`。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体得是一个对象", type(body).__name__)
        x, y, yaw = (_number(body, "x"), _number(body, "y"),
                     _number(body, "yaw"))
        mapping = self._ctx.mapping
        self._call(lambda: mapping.publish_initial_pose(x, y, yaw),
                   timeout_s=60.0)
        return json_response({"x": x, "y": y, "yaw": yaw})

    def _pose_reset(self, _req: Request) -> Response:
        """让后端自己重定位。**能力集里没有就当场拒**。

        没有这个能力还照调不误,后端只会抛一个它自己的错,页面上看到的是
        "设备拒绝了"——像是机器出了问题,其实是这条路线本来就没有这个动作。
        """
        nav = self._ctx.nav
        if "reloc" not in nav.capabilities:
            raise HttpError(409, "这条路线的后端不支持自动重定位",
                            "自建导航要人给一个初始位姿:POST /api/pose/initial")
        self._call(nav.reset_localization, timeout_s=60.0)
        return json_response({"ok": True})

    # ---------------------------------------------------------------- 归档

    def _run_dir(self, run_id: str) -> Path:
        """URL 里的运行 id -> 归档目录。

        换回去不是拼路径,是在 ``list_runs`` 列出来的目录里**找**:候选全部来
        自文件系统本身,所以拼不出一条通往目录外的路,任务名里有什么字符都
        不影响。
        """
        run_id = _safe_id(run_id, "运行 id")
        for run in list_runs(self._ctx.runs_root):
            if _run_id(run) == run_id:
                return run
        raise HttpError(404, f"没有这次运行:{run_id}")

    def _runs(self, _req: Request) -> Response:
        """历史运行,**新的在前**(``list_runs`` 按时间戳倒序)。

        只给列表要的那几项。事件流和照片名去 ``/api/runs/<id>`` 取 —— 几十次
        运行的事件全塞进列表响应,页面第一屏就要等好几秒。
        """
        out = []
        for run in list_runs(self._ctx.runs_root):
            manifest = read_manifest(run) or {}
            summary = manifest.get("summary") or {}
            out.append({
                "id": _run_id(run),
                "mission": run.parent.name,
                "started_at": manifest.get("started_at", run.name),
                "state": summary.get("state", ""),
                "succeeded": summary.get("succeeded", 0),
                "failed": summary.get("failed", 0),
                "total": summary.get("total", 0),
            })
        return json_response({"runs": out})

    def _run_detail(self, req: Request) -> Response:
        run = self._run_dir(req.params["run_id"])
        return json_response({
            "id": _run_id(run),
            "manifest": read_manifest(run),
            "state": read_state(run),
            "events": read_events(run),
            "photos": sorted(p.name for p in (run / "photos").glob("*.jpg")),
            # 两层结论一起给:页面上每张照片旁边要同时显示"模型说什么"和
            # "人改成了什么",分两次请求只会让那一屏闪一下。
            "findings": [f.to_wire() for f in read_findings(run)],
            "reviews": read_reviews(run),
        })

    def _run_photo(self, req: Request) -> Response:
        """取一张照片。

        判越界和 :meth:`_static` 一样:``resolve()`` 之后看它在不在 photos 里。
        照片名是**点位名拼出来的**,而点位名是人写的中文,过 ``_safe_id`` 会把
        带空格的点位名连带挡掉 —— 所以这里只挡 ``..``,越界交给路径解析判。
        """
        run = self._run_dir(req.params["run_id"])
        name = req.params["name"]
        if ".." in name:
            raise HttpError(400, "照片名不合法", name)
        photos = (run / "photos").resolve()
        try:
            target = (photos / name).resolve()
        except OSError as exc:      # 路径里有非法字符时 Windows 会抛
            raise HttpError(400, "照片名不合法", str(exc)) from exc
        if not target.is_relative_to(photos) or not target.is_file():
            raise HttpError(404, f"没有这张照片:{name}")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Response(200, target.read_bytes(), ctype)

    def _run_report(self, req: Request) -> Response:
        """报告。**没生成过就当场生成一份**。

        报告本来是运行结束时顺手写的,但"跑到一半被强杀"和"目录是从别的机器
        拷过来的"都会留下没有报告的归档 —— 而人来翻报告的时候,十有八九就是
        这两种情况。
        """
        run = self._run_dir(req.params["run_id"])
        fmt = req.params["fmt"]
        if fmt not in ("md", "html"):
            raise HttpError(404, f"报告只有 md 和 html 两种,不是 {fmt}")
        path = run / f"report.{fmt}"
        if not path.is_file():
            try:
                write_reports(run)
            except (OSError, ValueError) as exc:
                raise HttpError(500, "生成报告失败", str(exc)) from exc
        ctype = ("text/markdown" if fmt == "md" else "text/html")
        return Response(200, path.read_bytes(), f"{ctype}; charset=utf-8")

    def _stale_reports(self, run: Path) -> None:
        """把已经生成的报告删掉,让下次取报告时重新出一份。

        报告是**结论的快照**:判读或复核改了之后,躺在目录里的那份就过时了,
        而 :meth:`_run_report` 只在文件不存在时才生成。删掉是最省事的失效
        方式 —— 报告本来就随时能从归档重建。
        """
        for name in ("report.md", "report.html"):
            with contextlib.suppress(OSError):
                (run / name).unlink(missing_ok=True)

    def _run_judge(self, req: Request) -> Response:
        """判读一趟的照片。**同步跑完再回**。

        判读是人点一下"判读"按钮触发的(工程版,手动触发),几十张照片要
        分钟级 —— 服务是多线程的,占住的只是这一条连接。做成后台任务要多一
        套进度上报,而现在还没有第二个人会同时点它。

        没配密钥不是错:照片会全标 ``pending``,页面照样列得出来,人自己看。
        """
        run = self._run_dir(req.params["run_id"])
        try:
            findings = judge_run(run, history_root=self._ctx.runs_root)
        except OSError as exc:
            raise HttpError(500, "判读时读写归档失败", str(exc)) from exc
        self._stale_reports(run)
        return json_response({"findings": [f.to_wire() for f in findings]})

    def _run_review(self, req: Request) -> Response:
        """记一条人工复核。碰不到模型的结论 —— 见 ``inspect.judge``。"""
        run = self._run_dir(req.params["run_id"])
        payload = req.json()
        if not isinstance(payload, dict):
            raise HttpError(400, "复核要一个对象", "形如 {verdict, note}")
        try:
            save_review(run, req.params["name"],
                        str(payload.get("verdict", "")),
                        str(payload.get("note", "")))
        except ValueError as exc:
            raise HttpError(400, "这条复核记不下来", str(exc)) from exc
        except OSError as exc:
            raise HttpError(500, "写复核失败", str(exc)) from exc
        self._stale_reports(run)
        return json_response({"reviews": read_reviews(run)})

    def _call(self, factory: Callable[[], Any], timeout_s: float = 10.0) -> Any:
        """过桥,并且把业务异常翻成 HTTP 状态。

        翻译只在这一处:业务层抛的是它自己的词(``TeleopBusy``、
        ``EngineBusy``),不该为了 HTTP 去背状态码。
        """
        try:
            return self._ctx.bridge.call(factory, timeout_s=timeout_s)
        except ValueError as exc:
            raise HttpError(400, "参数不对", str(exc)) from exc
        except (TeleopBusy, EngineBusy, MappingError, NavRequestError) as exc:
            raise HttpError(409, "现在做不了这件事", str(exc)) from exc
        except NavBackendError as exc:
            # 连不上、超时:机器那边的事,不是这次请求的错。502 而不是 500 ——
            # 500 会让人去翻 app 的日志,而该翻的是链路。
            raise HttpError(502, "导航后端没答应", str(exc)) from exc
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
            # 头名归一成小写:HTTP 头名大小写不敏感,而归一放在边界上做一次,
            # 比让每个取值的地方各自"两种都试试"可靠。
            headers = {k.lower(): v for k, v in self.headers.items()}
            result = app.handle(method, path, query, self._read_body(),
                                headers, self.client_address[0])
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
        elif isinstance(result, ByteStream):
            self._send_bytes(result)
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

    def _send_bytes(self, stream: ByteStream) -> None:
        """把生成器吐的字节块原样写出去(MJPEG 走这条)。

        和 :meth:`_send_stream` 一样没有 ``Content-Length``,写完就关。人关掉
        标签页时这里会撞上 ``BrokenPipeError`` —— 那是正常收尾,但一定要走到
        ``finally``,ffmpeg 才会被杀掉。
        """
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", stream.content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in stream.chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                    # 客户端走了,正常收尾
        finally:
            with contextlib.suppress(Exception):
                stream.chunks.close()   # type: ignore[attr-defined]


# ------------------------------------------------------------------ 入口


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max-app", description="D1 Max 巡检 app")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="监听地址。默认只听本机 —— 换成别的就必须给 --pin")
    p.add_argument("--pin", default=os.environ.get(PIN_ENV),
                   help=f"解锁用的 PIN,至少 4 位。也可以用环境变量 {PIN_ENV} "
                        f"给(命令行上的 --pin 在 ps 里是明文的)")
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
    p.add_argument("--camera-host", default="192.168.234.1",
                   help="推 RTSP 的那台机器。前后广角是 8554/{front,back}")
    p.add_argument("--ffmpeg", default="ffmpeg",
                   help="ffmpeg 可执行文件。板载装的不在 PATH 里时用它指过去")
    p.add_argument("--sn", default=os.environ.get(SN_ENV),
                   help=f"这只狗的机身序列号。厂商接口不报这个字段,查不到的"
                        f"时候就靠这里人填(也可以用环境变量 {SN_ENV})")
    p.add_argument("--nickname", default=os.environ.get(NICKNAME_ENV),
                   help=f"给这只狗起的名,现场喊着方便(环境变量 {NICKNAME_ENV})")
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
                       media: Mapping[str, MediaSource],
                       runs_root: Path,
                       fingerprint: Mapping[str, str] | None = None,
                       ) -> MissionEngine:
    """在循环线程里造引擎 —— 它内部那个队列要绑在这条循环上。

    指纹里带着机身身份,这样每次运行的 ``manifest.json`` 自己就写明了是哪只
    狗跑的。**归档目录不按机器分层**:一台狗上只会有它自己的数据,多那一层
    下面永远只有一个兄弟;日后真要把几只狗的归档倒到一处,认的也是这个字段,
    不是目录名。
    """
    return MissionEngine(nav, device, media, runs_root,
                         fingerprint=dict(fingerprint or {}))


async def _make_teleop(device: DeviceBackend, engine: MissionEngine) -> Teleop:
    return Teleop(device, engine)


def shutdown(server: AppServer, ctx: AppContext) -> None:
    """把 app 收干净。**顺序是有讲究的,而且每一步都不许让下一步跑不成。**

    先停 HTTP:再有请求进来,后面几步就是在往正在关的链路上写。然后停遥控
    (它挂着一条看门狗协程),再关三条链路,最后杀子进程 —— 子进程是 app
    起的,app 走了它们不能留,留下就占着 ROS 话题和端口,下次起 app 会撞上。

    **这里面没有 patrol_agent。** 旁路进程不是 app 起的:它在 app 之前就跑
    着、在 app 之后还得跑着。控制权一旦被上装拿走就交接不回来(清单
    #46/#47),唯一的恢复手段是重启运控主机 —— app 每关一次就顺手把旁路进程
    带走,等于每关一次 app 都要重启一次机器狗。

    每一步都吞异常:收尾路上一步失败不能挡住后面的,那会漏下最难收的东西
    (子进程)。
    """
    server.stop()
    bridge = ctx.bridge
    with contextlib.suppress(Exception):
        bridge.call(ctx.teleop.aclose, timeout_s=5.0)
    for closer in (ctx.maps.close, ctx.nav.close, ctx.device.close):
        with contextlib.suppress(Exception):
            bridge.call(closer, timeout_s=5.0)
    with contextlib.suppress(Exception):
        bridge.call(ctx.procs.stop_all, timeout_s=15.0)
    bridge.stop()


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
    # 先查这一条,再去连任何东西:不然要等三个后端都超时完才报错。
    check_exposure(args.host, args.pin)
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
    # 拍照用的取图源。跟实时画面同一路 RTSP,但抓完就把 ffmpeg 收掉 ——
    # 取舍见 app.video。没有它,所有拍照点位都会明确判失败。
    photo: dict[str, MediaSource] = {
        name: RtspStill(f"rtsp://{args.camera_host}:8554/{name}",
                        ffmpeg=args.ffmpeg)
        for name in CAMERAS}
    who = resolve(args.sn, args.nickname)
    engine = bridge.call(
        lambda: _make_engine(nav, device, photo, runs_root, who.fingerprint()))
    # 遥控要在循环线程里造:它一上来就往引擎上挂检查,还会起后台看门狗。
    teleop = bridge.call(lambda: _make_teleop(device, engine))

    mapping = MappingOrchestrator(procs, MappingConfig(
        bags_dir=Path(args.bags_dir), maps_dir=Path(args.maps_dir),
        params_template=Path(args.params_file)))

    # 相机源现在就造好,但一条 ffmpeg 都不起 —— 真起是在有人打开画面的时候。
    video = {name: MjpegSource(f"rtsp://{args.camera_host}:8554/{name}",
                               ffmpeg=args.ffmpeg)
             for name in CAMERAS}

    ctx = AppContext(bridge=bridge, engine=engine, nav=nav, device=device,
                     maps=maps, procs=procs, teleop=teleop, mapping=mapping,
                     missions_dir=Path(args.missions_dir), runs_root=runs_root,
                     video=video, identity=who)
    server = AppServer(ctx, host=args.host, port=args.port, pin=args.pin)
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
        shutdown(server, ctx)
    return 0


if __name__ == "__main__":      # pragma: no cover - 入口
    sys.exit(main())


__all__ = ["AppContext", "AppServer", "ByteStream", "HttpError", "Request",
           "Response", "Stream", "json_response", "main", "shutdown"]
