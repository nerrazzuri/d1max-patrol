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
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
from d1max_patrol.engine.backup import (
    BackupError,
    TargetStatus,
    apply_sync,
    backup_notice,
    eject,
    init_target,
    plan_sync,
    resolve_targets,
)
from d1max_patrol.engine.backup import read_state as read_backup_state
from d1max_patrol.engine.baselines import BASELINE_DIR_NAME, baselines_bytes
from d1max_patrol.engine.export import (
    EXPORTS_DIR_NAME,
    ExportError,
    build_export,
    confirm_bundle,
    list_bundles,
    pick_runs,
    safe_name,
)
from d1max_patrol.engine.form import STANDALONE, Form
from d1max_patrol.engine.homing import HomeError, HomePoint, load_home, save_home
from d1max_patrol.engine.machine import EngineBusy, MissionEngine
from d1max_patrol.engine.mission import (
    Mission,
    MissionError,
    load_mission,
    parse_mission,
    save_mission,
)
from d1max_patrol.engine.preflight import PreflightReport, run_preflight
from d1max_patrol.engine.removable import (
    DEFAULT_PROBE,
    DiskRole,
    Removable,
    RemovableProbe,
    scan_or_unknown,
)
from d1max_patrol.engine.retention import (
    apply_sweep,
    bytes_to_free,
    forecast,
    plan_sweep,
    read_notice,
    scan_runs,
    write_notice,
)
from d1max_patrol.inspect.judge import (
    judge_run,
    read_findings,
    read_reviews,
    save_review,
)
from d1max_patrol.inspect.report import write_reports
from d1max_patrol.protocol.nav_types import Pose

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


#: 下载导出包时一次读多少。包可以有好几个 G,不整个读进内存。
_EXPORT_CHUNK = 1024 * 1024


def _disk(path: Path) -> tuple[int, int]:
    """(已用, 总量),字节。**目录还不存在就往上找。**

    第一次开机时 ``runs/`` 是不存在的 —— 而"这块盘还剩多少"在那时候照样
    有答案,不该因为还没跑过一趟就 500。
    """
    probe = Path(path).resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return usage.used, usage.total


def _day(raw: Any, which: str) -> datetime:
    """``YYYY-MM-DD`` -> 那天 UTC 零点。给导出区间用。"""
    if not isinstance(raw, str):
        raise HttpError(400, f"{which} 要是 YYYY-MM-DD 的字符串", repr(raw))
    try:
        day = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise HttpError(400, f"{which} 不是 YYYY-MM-DD",
                        f"{raw!r}: {exc}") from exc
    return day.replace(tzinfo=timezone.utc)


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

    @property
    def form(self) -> Form:
        """这台狗跑在哪一档。**ctx 自己不存,问引擎要。**

        引擎里那道 preflight 才是真拦住这一趟的那道。ctx 再存一份,
        就有了两个出处:今天两边都是单机档,谁也看不出来;等联网档落地,
        对不上的那个组合正好是没人跑过的那个(见 ``engine/form.py`` 开篇)。
        """
        return self.engine.form

    @property
    def removable(self) -> RemovableProbe:
        """怎么去认外插盘。**ctx 自己不存,问引擎要。**

        跟 ``form`` 一样:引擎里那道 preflight 才是真拦住这一趟的那道,
        ctx 再存一份,两份迟早不一样,而不一样的那天没有任何测试会红。
        """
        return self.engine.removable

    @property
    def baselines(self) -> Path:
        """基线集在哪。**在 ``runs_root`` 旁边,不在里面。**

        在里面它就活在那个被清扫器一遍遍扫过的目录树里 —— 今天 ``list_runs``
        只认两层目录、扫不到它,但那是巧合,不是约定。

        跟 ``form`` / ``removable`` 一样从别处推出来,**不另存一份字段**:
        存了就有两个出处,而对不上的那天没有任何测试会红。
        """
        return Path(self.runs_root).parent / BASELINE_DIR_NAME

    @property
    def exports(self) -> Path:
        """导出包放哪。理由同 :attr:`baselines`。"""
        return Path(self.runs_root).parent / EXPORTS_DIR_NAME


async def _preflight_with_scan(ctx: AppContext, mission: Mission,
                               home: HomePoint | None) -> PreflightReport:
    """扫外插盘 + 跑全项检查。**两件事都在循环线程里做。**

    扫盘要走 ``bridge`` 的原因不是它碰引擎状态(它不碰),而是 ``_call``
    收的是一个**协程工厂** —— 在 HTTP 线程上 await 它需要另一个事件循环。
    连在一个协程里交给桥,一次往返把两件事都办了。

    ``scan_or_unknown`` 和 ``robot_sn=`` 都跟引擎那道 preflight 用同一份 ——
    两条起飞路径判出不一样的结论,是这一卷最难查的一类错:页面上七项全绿,
    狗起来之后自己中止了。

    **不扫就不能说没插。** 这里漏传 ``removable=`` 的后果不是少查一项,
    是页面上白得一项绿的:插着取走盘时 HTTP 这条路照样放行,狗起来之后
    引擎自己那道 preflight 才拦住,整趟 ABORT —— 操作员看到的是
    "七项全绿,然后狗自己中止了"。
    """
    return await run_preflight(ctx.nav, ctx.device, mission, ctx.runs_root,
                               home=home, form=ctx.form,
                               removable=await scan_or_unknown(ctx.removable),
                               robot_sn=ctx.identity.sn)


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
        #: 哪几个挂载点上正跑着同步。**这是 HTTP 这一侧的状态**(哪几个请求
        #: 在飞),不是引擎状态 —— 引擎压根不知道备份这回事。服务是
        #: ThreadingHTTPServer,两个请求真的会同时进来,所以要锁。
        self._syncing: set[str] = set()
        #: 清盘正在跑。**跟 ``_syncing`` 共用一把锁,因为这两件事互斥** ——
        #: 清盘删的正是同步这一刻在读的那几棵目录树:拷到一半源目录被 rmtree
        #: 掉,盘上落的是残的一趟,而"只增不删"保证它再也不会被重拷。
        self._sweeping: bool = False
        self._sync_lock = threading.Lock()
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
        self.route("PUT", "/api/maps/<map_id>/home", self._map_home_put)
        self.route("GET", "/api/maps/<map_id>/home", self._map_home_get)
        self.route("POST", "/api/pose/initial", self._pose_initial)
        self.route("POST", "/api/pose/reset", self._pose_reset)
        self.route("GET", "/api/runs", self._runs)
        self.route("GET", "/api/runs/<run_id>", self._run_detail)
        self.route("GET", "/api/runs/<run_id>/photos/<name>", self._run_photo)
        self.route("GET", "/api/runs/<run_id>/report.<fmt>", self._run_report)
        self.route("POST", "/api/runs/<run_id>/judge", self._run_judge)
        self.route("POST", "/api/runs/<run_id>/review/<name>", self._run_review)
        self.route("GET", "/api/storage", self._storage)
        self.route("POST", "/api/storage/sweep", self._sweep)
        self.route("GET", "/api/exports", self._exports)
        self.route("POST", "/api/exports", self._export_new)
        self.route("GET", "/api/exports/<name>", self._export_get)
        self.route("POST", "/api/exports/<name>/confirm", self._export_confirm)
        self.route("GET", "/api/backup/targets", self._backup_targets)
        self.route("POST", "/api/backup/init", self._backup_init)
        self.route("POST", "/api/backup/sync", self._backup_sync)
        self.route("POST", "/api/backup/eject", self._backup_eject)

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

        检查结果连成功的一次也带回去:人在现场要的是"每一项现在都什么样",
        不是一个"好了"。
        """
        mission = self._load_mission(req.params["mid"])
        ctx = self._ctx
        try:
            home = load_home(ctx.mapping.maps_dir, mission.map_id)
        except HomeError:
            home = None      # 让起飞门槛去说这句话,别在这儿抢着报错
        report = self._call(
            lambda: _preflight_with_scan(ctx, mission, home),
            timeout_s=30.0)
        checks = _checks_wire(report)
        if not report.ok:
            return json_response({
                "error": "起飞检查没过",
                "detail": ";".join(f"{c.name}: {c.detail}"
                                    for c in report.failures),
                "checks": checks,
            }, status=409)
        self._call(lambda: ctx.engine.start(mission, home=home), timeout_s=30.0)
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

    def _map_home_put(self, req: Request) -> Response:
        """在这张图上标一个原点。**位姿由客户端给。**

        跟 ``POST /api/pose/initial`` 是同一个形状:手机 App 那边本来就显示着
        8091 那座桥推来的实时位姿,"就在这儿标一个"这个动作,客户端手上已经
        有那三个数了。狗自己问不出来 —— ``NavBackend`` 上没有"当前位姿"这个
        口子(位姿走的是另一条链路),为这一个端点去开那个口子要动两个后端、
        契约测试和两个 sim server。

        **没有这个端点,``save_home`` 就没有调用者**,而起飞门槛把原点当硬
        前置 —— 那意味着这台狗永远起不了飞。

        **不过 ``bridge._call``。** 跟 ``_mission_run`` 里那处 ``load_home``
        同一个道理:文件读写不是引擎状态,``maps_dir`` 只是 frozen config 上
        的一个属性,HTTP 线程直接做没有并发边界要守。
        """
        map_id = _safe_id(req.params["map_id"], "图名")
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体得是一个对象", type(body).__name__)
        missing = [k for k in ("x", "y", "yaw") if k not in body]
        if missing:
            # 缺字段不能按 0 补:(0, 0) 在地图里是一个真实存在的点,补出来的
            # 原点跟人标的那个看不出区别,而狗会一声不吭地走过去。
            raise HttpError(400, "原点少了字段", "缺 " + "、".join(missing))
        x, y, yaw = (_number(body, "x"), _number(body, "y"),
                     _number(body, "yaw"))
        note = body.get("note", "")
        if not isinstance(note, str):
            raise HttpError(400, "note 得是一个字符串", repr(note))
        home = HomePoint(map_id=map_id, pose=Pose.from_xy_yaw(x, y, yaw),
                         marked_at_ms=int(time.time() * 1000), note=note)
        try:
            save_home(self._ctx.mapping.maps_dir, home)
        except OSError as exc:
            raise HttpError(500, "原点没写下去", str(exc)) from exc
        return json_response(home.to_wire())

    def _map_home_get(self, req: Request) -> Response:
        """读回这张图上标的原点。**没标过是 404,不是一个零点。**"""
        map_id = _safe_id(req.params["map_id"], "图名")
        try:
            home = load_home(self._ctx.mapping.maps_dir, map_id)
        except HomeError as exc:
            raise HttpError(404, "这张图上没有可用的原点", str(exc)) from exc
        return json_response(home.to_wire())

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

        **判正常了才回写基线**(见 ``inspect.judge``)。判读通过的那张就是下一趟
        的比对基准;判异常的不回写 —— 回写了的话下一趟拿异常比异常,模型说
        "跟上次一样",异常就此静默转正。
        """
        run = self._run_dir(req.params["run_id"])
        try:
            findings = judge_run(run, history_root=self._ctx.runs_root,
                                 baselines_root=self._ctx.baselines)
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

    # --------------------------------------------------- 盘况、预告、导出

    def _storage(self, _req: Request) -> Response:
        """盘还剩多少,以及"再过不久这些就要到期了"。

        **落盘失败不报错,降级答完。** 见下面那个 ``except OSError``。

        **看一眼盘况就算预告过了。** 这一卷里没有常驻的巡盘任务,人打开这一屏
        是当前唯一一个"我们确实告诉过他"的时刻 —— 预告因此在这里落盘,7 天的
        钟从落盘那一刻起走。``write_notice`` 对已有的那几条只读不写,所以刷新
        页面不会把钟拨回去(拨得回去的话钟永远填不满,删除永远不会来,而那正好
        是这套东西看起来在工作、实际上什么也没做的样子)。

        **这一屏自己不碰引擎状态。** 唯一过桥的一跳是下面那句 ``_scan_targets``——
        过桥的理由跟磁盘无关:``scan_or_unknown`` 是个协程,在 HTTP 线程上
        await 它需要一个事件循环,单门不变量在这儿说不上话(见 ``_scan_targets``
        自己的 docstring,以及 ``_map_home_put`` 里那条一样的论证)。
        """
        ctx = self._ctx
        now = datetime.now(timezone.utc)
        used, total = _disk(ctx.runs_root)
        ratio = (used / total) if total > 0 else 0.0
        runs = scan_runs(ctx.runs_root, now=now)
        # 台账要在算预告之前读:那句"预计几天后开始删除"里的日子,是
        # ``max(到期日, 首次预告时刻 + 7 天)`` —— 拿不到台账就只能报到期日,
        # 而到期日那天什么也不会发生(见 ``retention.forecast``)。
        fc = forecast(runs, now=now, used_ratio=ratio,
                      noticed=read_notice(ctx.runs_root))
        notice_written = True
        notice_detail = ""
        try:
            # 第一次开机时 runs/ 还不存在,而预告要落在它下面。
            Path(ctx.runs_root).mkdir(parents=True, exist_ok=True)
            write_notice(ctx.runs_root, fc)
        except OSError as exc:
            # **落不了盘也照样把这一屏答完。** eMMC 写满(ENOSPC)或者出错之后
            # 被内核挂成只读(EROFS,Orin 上极常见)时,写什么都会失败 —— 而
            # 盘况页正是唯一一个在盘出事时必须还能显示的页面:用了多少、哪几趟
            # 要过期、"要留就先导出"那句话,全在这儿。整条接口 500 掉的话,
            # 操作员在最需要它的那一刻什么都看不到。
            #
            # 降级的方向是安全的:钟没开始走,就没有任何一趟因此变得可删。
            notice_written = False
            notice_detail = (f"预告没能记到盘上({exc})—— 盘可能已经写满,"
                             f"或者出错之后被挂成了只读。删除的钟因此没有开始走,"
                             f"不会有任何归档因为这次失败而被提前删掉;"
                             f"腾出空间之后再刷新一次这一屏。")
        # 备份这件事在这一屏上露一次面。**这里是"保留期快到期"那个时刻** ——
        # 人看盘况的时候,正是"马上要有东西被永久删掉"这句话最该被听见的时候
        # (spec §7.6 纪律 2)。另一个时刻(授权 OTA 之前)归第 4 卷。
        #
        # 扫盘炸了就当没配盘:这一屏在盘出事的时候必须还能显示,而降级的方向
        # 是安全的 —— 最坏是多说一句"未配备份盘"。
        targets, _why = self._scan_targets()
        # **那个 ``if not r.is_expired(now)`` 不能省。** ``days_left`` 把已经
        # 过期的 run **钳到 0.0**(负数会在排序和文案里到处冒出来),而"已经
        # 过期但还在盘上"是本仓有文档记载的正常状态 —— 清盘是按水位触发的,
        # 不是按到期。不过滤的那一边:狗上只要躺着一趟过期归档,``soonest``
        # 就永远是 0.0,``backup_notice`` 每次都判成"马上要删了",于是每一台
        # 没配镜像盘的狗从此常年顶着红色 PUSH,而实际上什么也没在被删 ——
        # 正是 spec §7.6 纪律 2 明令禁止的那种常年报警。规格原文是"保留期
        # **到期之前**",已经到期的不在其列。
        soonest = min((r.days_left(now) for r in runs if not r.is_expired(now)),
                      default=None)
        behind = 0
        for t in targets or ():
            if t.usable and t.role is DiskRole.MIRROR:
                # ``runs`` 是这一屏上面早算好的那份名单——传进去让 plan_sync
                # 不用再对 runs_root 重扫一遍(每一趟归档都是一次递归 stat)。
                behind = max(behind, plan_sync(ctx.runs_root, t.mount,
                                               robot_sn=ctx.identity.sn,
                                               runs=runs).behind)
        notice = backup_notice(targets or (), behind=behind,
                               days_left=soonest)
        return json_response({
            "used_bytes": used,
            "total_bytes": total,
            "used_ratio": ratio,
            "runs_total": len(runs),
            "runs_bytes": sum(r.size_bytes for r in runs),
            "baselines_bytes": baselines_bytes(ctx.baselines),
            "exports_bytes": sum(b.size_bytes for b in list_bundles(ctx.exports)),
            "forecast": fc.to_wire(),
            # 预告到底记下来没有。**这一条必须出现在响应里**:预告没落盘时
            # 页面上那句"再过几天开始删除"是说了不算的,人得知道。
            "notice_written": notice_written,
            "notice_detail": notice_detail,
            "backup": notice.to_wire(),
        })

    def _sweep(self, req: Request) -> Response:
        """按水位删。**默认只给方案,不动盘** —— 真删要显式写 ``{"apply": true}``。

        ``apply_sweep`` 里那行 ``shutil.rmtree`` 是全仓最危险的一行。这里做的
        两件事是它的前提:一是默认不删,人先看名单再点第二下;二是名单只可能
        来自 ``plan_sweep``,而 ``plan_sweep`` 从不把"还在写"的那一趟放进去。

        ``free_bytes`` 是"至少腾出这么多",不给就按水位线算。现场常见的是人
        自己知道要腾多少,而且给得出这个数 —— 这样清盘就不再取决于"这台机器
        此刻用了百分之几"。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "清盘要一个对象",
                            '形如 {"free_bytes": 2000000000, "apply": true}')
        ctx = self._ctx
        now = datetime.now(timezone.utc)
        used, total = _disk(ctx.runs_root)
        raw = body.get("free_bytes")
        if raw is None:
            need = bytes_to_free(used_bytes=used, total_bytes=total)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            need = raw
        else:
            raise HttpError(400, "free_bytes 要是个整数", repr(raw))
        sweep = plan_sweep(scan_runs(ctx.runs_root, now=now), now=now,
                           has_upload=ctx.form.has_upload, need_bytes=need,
                           noticed=read_notice(ctx.runs_root))
        applied = bool(body.get("apply"))
        deleted: tuple[Path, ...] = ()
        if applied:
            # **清盘和同步互斥。** 同步这一侧正一趟趟地读 runs_root 下的目录树,
            # 而这里那行 rmtree 删的就是它们。撞上的那次,盘上落的是残的一趟,
            # 而"只增不删"保证它再也不会被重新考虑 —— 归档两边都缺一块,
            # 屏上却写着有两份。``{"apply": false}`` 不受影响:出方案不动盘。
            with self._sync_lock:
                if self._syncing:
                    raise HttpError(
                        409, "正在同步备份盘,先等它跑完再清盘",
                        "同步正一趟趟地读这些目录,这时候删它们,备份盘上会落下"
                        "一趟残的归档 —— 而只增不删意味着它永远不会被重拷")
                self._sweeping = True
            try:
                deleted = apply_sweep(sweep, runs_root=ctx.runs_root)
            finally:
                with self._sync_lock:
                    self._sweeping = False
        return json_response({
            "applied": applied,
            "need_bytes": need,
            "deleted": [str(p) for p in deleted],
            "sweep": sweep.to_wire(),
        })

    def _bundle_path(self, name: str) -> Path:
        """URL 里的包名 -> 盘上的 zip。名字不合法 400,没有这个包 404。

        两者必须分得开:400 是"你这个名字写错了",404 是"名字没问题,东西
        不在了"。合成一个,人就查不出来是自己拼错了还是包被清掉了。
        """
        try:
            safe = safe_name(name)
        except ExportError as exc:
            raise HttpError(400, "包名不合法", str(exc)) from exc
        target = Path(self._ctx.exports) / safe
        if not target.is_file():
            raise HttpError(404, f"没有这个导出包:{safe}")
        return target

    def _exports(self, _req: Request) -> Response:
        """列出盘上还留着的导出包,**新的在前**。"""
        return json_response({
            "exports": [b.to_wire() for b in list_bundles(self._ctx.exports)]})

    def _export_new(self, req: Request) -> Response:
        """打一个包。**同步打完再回**,理由跟 :meth:`_run_judge` 一条一样:
        服务是多线程的,占住的只是这一条连接,而做成后台任务要多一套进度上报。

        区间**左闭右开**,两头都是 UTC 那一天的零点 —— 人会一个月一个月地导,
        左闭右开让相邻两个月既不重也不漏。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "导出要一个对象",
                            '形如 {"start": "2026-08-01", "end": "2026-09-01"}')
        start = _day(body.get("start"), "start")
        end = _day(body.get("end"), "end")
        if end <= start:
            raise HttpError(400, "这个区间是空的",
                            f"end({body.get('end')!r}) 不在 "
                            f"start({body.get('start')!r}) 后面")
        ctx = self._ctx
        picked = pick_runs(scan_runs(ctx.runs_root), start=start, end=end)
        try:
            bundle = build_export(picked, out_dir=ctx.exports,
                                  now=datetime.now(timezone.utc))
        except ExportError as exc:
            raise HttpError(400, "这个区间导不出来", str(exc)) from exc
        except OSError as exc:
            raise HttpError(500, "打包时读写失败", str(exc)) from exc
        return json_response(bundle.to_wire())

    def _export_get(self, req: Request) -> ByteStream:
        """下载一个包。**分块吐,不整个读进内存** —— 一个月的照片可以好几个 G。

        这条路上没有 ``Content-Length``(``ByteStream`` 不带),所以在 HTTP 这
        一层看不出下载有没有被截断。这正是"确认"那一步要客户端**自己重算**
        哈希的原因:截断的那半个包哈希一定对不上,而对不上就不放行。
        """
        target = self._bundle_path(req.params["name"])

        def chunks() -> Iterator[bytes]:
            with target.open("rb") as fh:
                while True:
                    block = fh.read(_EXPORT_CHUNK)
                    if not block:
                        return
                    yield block

        return ByteStream(chunks(), "application/zip")

    def _export_confirm(self, req: Request) -> Response:
        """客户端说它拿到了,并且自己重算的哈希是这个。**对上了才放行。**

        对上之后包里那几趟就带上了"别处还有一份"的标记,水位删除从此动得了
        它们。**这一步是自动删除的前提**(spec §4.6 第 2 条):没有导出通道的
        自动删除,等于系统在单方面销毁客户的资产。

        对不上抛 409 不抛 400:请求本身没写错,是包在路上掉了字节,重下一次
        就可能对上。400 会让人回去查自己的参数,查不出来。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "确认要一个对象", '形如 {"sha256": "..."}')
        ctx = self._ctx
        # 先确认包在:名字错了是 400、包没了是 404,都不该被后面那个 409 盖掉。
        self._bundle_path(req.params["name"])
        try:
            bundle = confirm_bundle(ctx.exports, req.params["name"],
                                    str(body.get("sha256", "")),
                                    runs_root=ctx.runs_root)
        except ExportError as exc:
            raise HttpError(409, "这个包对不上", str(exc)) from exc
        except OSError as exc:
            raise HttpError(500, "写确认失败", str(exc)) from exc
        return json_response(bundle.to_wire())

    def _scan_targets(self) -> tuple[list[TargetStatus] | None, str]:
        """狗现在认到哪些盘、各自能不能拿来备份。

        **过桥,但不是因为它碰引擎状态。** ``RemovableProbe.scan`` 是个协程,
        在 HTTP 线程上 await 它需要一个事件循环,而 ``LoopBridge.call``(桥上
        那个,不是本类的 ``_call``)收的正是一个**协程工厂** —— 理由跟
        ``_preflight_with_scan`` 一模一样,跟单门不变量无关。下面那三个纯
        文件 I/O 的处理器自己那部分**不过桥**,但都经 ``_pick`` 调用这一处,
        所以扫盘那一跳除外。

        回 ``(None, 一句话)`` 表示没扫成。认不出插着什么不等于这一屏该消失:
        人正是在盘出事的时候来看它的。
        """
        ctx = self._ctx
        disks: tuple[Removable, ...] | None = ctx.bridge.call(
            lambda: scan_or_unknown(ctx.removable))
        if disks is None:
            return None, ("认不出现在插着什么盘 —— 扫挂载点的时候出错了"
                          "(权限不对,或者有一个挂载点已经失效)。"
                          "备份不受影响:认不出来就什么也不写")
        return list(resolve_targets(disks, robot_sn=ctx.identity.sn)), ""

    def _pick(self, req: Request) -> TargetStatus:
        """从请求体里取挂载点,**必须是刚扫到的那几块之一**。

        这个 ``mount`` 来自外面。不核对的那一边,一个 POST 就能让狗往它文件
        系统上的任意路径写一个标记文件,或者对着任意目录跑一次"只增不删"的
        拷贝。核对是这一层唯一的边界。
        """
        raw = req.json()
        if not isinstance(raw, dict):
            raise HttpError(400, "要一个对象", '形如 {"mount": "/media/u1"}')
        want = raw.get("mount")
        if not isinstance(want, str) or not want:
            raise HttpError(400, "要带上 mount")
        targets, why = self._scan_targets()
        if targets is None:
            raise HttpError(503, why)
        for t in targets:
            if t.mount.as_posix() == want:
                return t
        raise HttpError(404, f"现在没认到 {want} 这块盘 —— 拔掉了,"
                             f"或者根本没挂上")

    def _backup_targets(self, _req: Request) -> Response:
        """狗现在认到哪些备份盘。**这一屏就是 spec §7.6 那句"app 看见的
        不是备份盘,是狗看见的备份盘"。**

        推论:**没有手机在场的时候同步照常进行。** 这几个接口都不是同步的
        发起条件,只是它的一个入口。

        **不过桥**,除了里面那一处扫盘(``_scan_targets`` 自己会过);算容量、
        读同步进度、排同步方案全是纯文件 I/O,不碰引擎状态。
        """
        ctx = self._ctx
        targets, why = self._scan_targets()
        if targets is None:
            return json_response({"robot_sn": ctx.identity.sn, "scanned": False,
                                  "targets": [], "detail": why})
        out = []
        for t in targets:
            used, total = _disk(t.mount)
            row = t.to_wire() | {"total_bytes": total,
                                 "free_bytes": max(0, total - used),
                                 "last_sync_ms": 0, "behind": 0,
                                 "behind_bytes": 0}
            if t.usable:
                state = read_backup_state(t.mount, robot_sn=ctx.identity.sn)
                plan = plan_sync(ctx.runs_root, t.mount,
                                 robot_sn=ctx.identity.sn)
                row |= {"last_sync_ms": state.last_sync_ms,
                        "behind": plan.behind,
                        "behind_bytes": plan.behind_bytes}
            out.append(row)
        return json_response({"robot_sn": ctx.identity.sn, "scanned": True,
                              "targets": out, "detail": ""})

    def _backup_init(self, req: Request) -> Response:
        """把一块盘认成这台狗的备份盘。

        本处理器自己那部分是纯文件 I/O,不过桥;扫盘那一跳除外 —— ``_pick``
        里调的 ``_scan_targets`` 会过(见它的 docstring)。
        """
        ctx = self._ctx
        target = self._pick(req)
        # ``_pick`` 已经验证过请求体是个 dict,这里是同一份 bytes 再解一遍。
        raw = req.json()
        want = raw.get("role")
        try:
            role = DiskRole(want)
        except ValueError:
            raise HttpError(400, f"role 只能是 mirror 或 transfer,给的是"
                                 f" {want!r}") from None
        label = raw.get("label", "")
        try:
            got = init_target(target.mount, robot_sn=ctx.identity.sn, role=role,
                              label=label if isinstance(label, str) else "",
                              now_ms=int(time.time() * 1000))
        except BackupError as exc:
            # 409 而不是 400:请求本身没毛病,是盘上已经有东西了。
            raise HttpError(409, str(exc)) from exc
        return json_response(got.to_wire())

    def _backup_sync(self, req: Request) -> Response:
        """同步一次。**默认只给方案,真跑要显式 ``{"apply": true}``** ——
        跟 ``/api/storage/sweep`` 同一条纪律:人先看名单,再点第二下。

        本处理器自己那部分是纯文件 I/O,不过桥;扫盘那一跳除外 —— ``_pick``
        里调的 ``_scan_targets`` 会过(见它的 docstring)。
        """
        ctx = self._ctx
        target = self._pick(req)
        if not target.usable:
            raise HttpError(409, target.detail)
        key = target.mount.as_posix()
        plan = plan_sync(ctx.runs_root, target.mount, robot_sn=ctx.identity.sn)
        # ``_pick`` 已经验证过请求体是个 dict,这里是同一份 bytes 再解一遍。
        applied = bool(req.json().get("apply"))
        if not applied:
            return json_response({"applied": False, "plan": plan.to_wire()})
        with self._sync_lock:
            if self._sweeping:
                raise HttpError(
                    409, "正在按水位清盘,先等它跑完再同步",
                    "清盘删的正是同步这一刻要读的那几棵目录树")
            if key in self._syncing:
                raise HttpError(409, "这块盘上已经有一轮同步在跑了")
            self._syncing.add(key)
        try:
            res = apply_sync(plan, now_ms=int(time.time() * 1000),
                             robot_sn=ctx.identity.sn)
        finally:
            with self._sync_lock:
                self._syncing.discard(key)
        return json_response({"applied": True, "plan": plan.to_wire(),
                              "result": res.to_wire()})

    def _backup_eject(self, req: Request) -> Response:
        """安全弹出。

        本处理器自己那部分是纯文件 I/O,不过桥;扫盘那一跳除外 —— ``_pick``
        里调的 ``_scan_targets`` 会过(见它的 docstring)。
        """
        target = self._pick(req)
        key = target.mount.as_posix()
        with self._sync_lock:
            busy = key in self._syncing
        return json_response(eject(target.mount, busy=busy).to_wire())

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
                       *, form: Form = STANDALONE,
                       removable: RemovableProbe = DEFAULT_PROBE,
                       ) -> MissionEngine:
    """在循环线程里造引擎 —— 它内部那个队列要绑在这条循环上。

    指纹里带着机身身份,这样每次运行的 ``manifest.json`` 自己就写明了是哪只
    狗跑的。**归档目录不按机器分层**:一台狗上只会有它自己的数据,多那一层
    下面永远只有一个兄弟;日后真要把几只狗的归档倒到一处,认的也是这个字段,
    不是目录名。
    """
    return MissionEngine(nav, device, media, runs_root,
                         fingerprint=dict(fingerprint or {}), form=form,
                         removable=removable)


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
