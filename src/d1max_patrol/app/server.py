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
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from d1max_patrol.app.alert_sources import AlertSources
from d1max_patrol.app.auth import (
    AUTH_PATH,
    CHALLENGE_PATH,
    CHANNEL_LAN,
    CHANNEL_LOCAL,
    NONCE_TTL_S,
    OPERATOR_NOTICE,
    OPERATOR_PATH,
    PROOF_ALG,
    TOKEN_IDLE_S,
    Denied,
    Guard,
    Session,
    bearer,
    normalize_pin,
)
from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.app.control import ControlDesk
from d1max_patrol.app.gridmap import GridError, from_frame, load_saved
from d1max_patrol.app.identity import (
    NICKNAME_ENV,
    SN_ENV,
    Identity,
    Operator,
    clean_operator,
    resolve,
    write_payload,
)
from d1max_patrol.app.mapping import (
    MappingConfig,
    MappingError,
    MappingOrchestrator,
)
from d1max_patrol.app.procs import ProcManager
from d1max_patrol.app.teleop import (
    MODES,
    PROFILES,
    REMOTE_CONFIRM,
    EmergencyStopFailed,
    Teleop,
    TeleopBusy,
)
from d1max_patrol.app.upload_pump import UploadPump
from d1max_patrol.app.video import CAMERAS, CameraFeed, RtspStill, VideoError
from d1max_patrol.app.watch import NO_UPLOADER, watch_summary
from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    BackendReconnected,
    BatteryEvent,
    ControlLostEvent,
    DeviceBackend,
    DeviceBackendError,
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
from d1max_patrol.engine import backup
from d1max_patrol.engine.alerts import AlertBook, AlertNotFound
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
    marker_path,
    plan_sync,
    read_sync_state,
    resolve_targets,
)
from d1max_patrol.engine.baselines import BASELINE_DIR_NAME, baselines_bytes
from d1max_patrol.engine.bundle import (
    BundleError,
    active_bundle,
    apply_bundle,
    denies,
    read_bundle_schedule,
    rollback_bundle,
    verify_bundle,
)
from d1max_patrol.engine.bundle import (
    read_state as read_bundle_state,
)
from d1max_patrol.engine.datadir import DATA_ROOT_ENV, resolve_paths
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
from d1max_patrol.engine.http_sink import HttpSink
from d1max_patrol.engine.lease import (
    AUDIT_MAX,
    AuditRecord,
    Holder,
    LeaseBusy,
    LeaseError,
    LeaseLost,
    LeaseState,
)
from d1max_patrol.engine.machine import EngineBusy, MissionEngine, SuspendPoint
from d1max_patrol.engine.mission import (
    Mission,
    MissionError,
    load_mission,
    parse_mission,
    save_mission,
)
from d1max_patrol.engine.preflight import CheckResult, PreflightReport, run_preflight
from d1max_patrol.engine.release import (
    Layout,
    ReleaseError,
    ReleaseManifest,
    activate,
    clear_pending,
    commit,
    current_name,
    installed,
    read_pending,
    rollback,
    stage,
)
from d1max_patrol.engine.release import (
    read_manifest as read_release_manifest,
)
from d1max_patrol.engine.removable import (
    DEFAULT_PROBE,
    DiskRole,
    Removable,
    RemovableProbe,
    scan_or_unknown,
)
from d1max_patrol.engine.retention import (
    UPLOADED_REL,
    RunInfo,
    apply_sweep,
    bytes_to_free,
    forecast,
    is_settled,
    mark_uploaded,
    plan_sweep,
    read_notice,
    scan_runs,
    unmark_uploaded,
    write_notice,
)
from d1max_patrol.engine.schedule import (
    Skew,
    clock_skew,
    decide,
    next_run,
)
from d1max_patrol.engine.selfcheck import (
    PrecheckInputs,
    RestartPlan,
    Verdict,
    mission_schema_floor,
    postcheck_verdict,
    precheck,
    restart_plan,
    run_postcheck,
)
from d1max_patrol.engine.upload_queue import UploadQueue, classify
from d1max_patrol.engine.uploader import Uploader
from d1max_patrol.inspect.judge import (
    judge_run,
    read_findings,
    read_reviews,
    save_review,
)
from d1max_patrol.inspect.report import write_reports
from d1max_patrol.protocol.nav_types import Pose

#: 写法照 ``recorder`` / ``backends.vendor_nav``:模块级 logger,不在这里配
#: handler、不动根 logger —— 配置是 ``cli`` 那一层的事。
log = logging.getLogger(__name__)

#: 静态文件目录。模块导入时就 resolve,后面判越界拿它当基准。
STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8095

#: 只听这些地址算"只给本机用"。别的地址都必须配 PIN。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 从环境变量里读 PIN。命令行上的 --pin 在 ``ps`` 里是明文可见的,同一台机器
#: 上的别的账号能直接看到;板载常驻的时候用环境变量更合适。
PIN_ENV = "D1MAX_PIN"

#: 回传服务器地址从哪儿来。**不给就是单机档** —— 证据全留在本机,靠 U 盘导出。
#: 这个名字只是个环境变量名,**具体地址一个字都不许写进代码或交付件**:
#: 每个客户的服务器都不一样,写死一个进去,下一家装机就带着上一家的地址上线。
CONSOLE_URL_ENV = "D1MAX_CONSOLE_URL"

#: 回传用的 token。跟 PIN 同一个道理走环境变量:命令行上的东西在 ``ps`` 里是
#: 明文。**它没有对应的命令行开关,这是故意的。**
CONSOLE_TOKEN_ENV = "D1MAX_CONSOLE_TOKEN"

#: 回传队列落在哪(相对 ``runs_root`` 的**兄弟**位置)。
#: **不许放进 ``runs/`` 里面** —— 放里面的话 ``retention`` 清盘那一趟会把队列
#: 本身当成一趟归档算进水位,更糟的是某天真删到它头上。
QUEUE_FILE_NAME = "queue.jsonl"

#: 请求体上限。这些接口收的都是任务 JSON,几十 KB 顶天了。
MAX_BODY = 4 * 1024 * 1024

#: 回退理由最长多少。挡的不是攻击,是「有人把整段日志粘进 landed.json」——
#: 跟 ``engine/bundle.py`` 的 ``MAX_SN_LEN`` 挡的是同一类事。
MAX_ROLLBACK_REASON_LEN = 500

#: 快照重建的节拍。链路通断这类"没有事件的变化"靠它发现。
_TICK_S = 0.5

#: 租约看门狗多久醒一次(§5.8)。
#:
#: **这个数就是"人走了"到"狗停下"之间的时间上界**,所以它不能按"屏幕刷新
#: 得快不快"来定 —— 那是 ``_TICK_S`` 的事。0.5 秒的理由:
#:
#: * 它是 ``LEASE_TTL_MS``(30 秒)的六十分之一。TTL 本身就有 30 秒的不确定
#:   性,在这个尺度上再纠结半秒毫无意义 —— 换句话说,把它调到 0.1 秒,现场
#:   感知不到任何差别。
#: * 再慢就开始有差别了:几秒一醒意味着人揣着手机走了之后,狗还可能在遥控
#:   档上举着最后一个动作好几秒,而 §5.8 要的是"停在原地"。
#: * 跟 ``_TICK_S`` 同一个数量级,现场排障时"多久一拍"是同一句话,不用记两
#:   个数;更快纯粹是白耗电,这条协程醒一次要读一次审计环、量一次盘。
#:
#: **它跟 ``_TICK_S`` 相等是巧合,不是同一个数。** 两者容忍度相反(显示晚
#: 一拍没关系,停车不能晚),所以各写各的常量:哪天要把屏幕调慢省电,不该
#: 顺手把闸门也调慢。
_LEASE_WATCH_PERIOD_S = 0.5

#: ``AlertSources.on_tick`` 那三条水位每多少拍看一次。60 拍 = 30 秒。
#:
#: **它跟 ``_LEASE_WATCH_PERIOD_S`` 是两码事,所以要分频。** 上面那个数论证
#: 的是"人走了到狗停下"的时间上界;而盘水位、任务包落差、钟偏的变化尺度是
#: 分钟到小时 —— 半秒看一次不会更早发现任何东西,却要在事件循环里做一次同步
#: ``shutil.disk_usage`` + 一趟目录遍历 + 一次读 ``landed.json``。盘慢或者
#: SD 卡将坏的时候,整个 server 的事件循环每半秒被拖一次,而拖的是那条最要紧
#: 的闸门自己。
#:
#: 30 秒的依据:这三件事里最急的是盘满(它会变成"没法记录证据"),而从 80%
#: 到写不进去以小时计,晚半分钟看见没有代价。再稀就开始有代价了 —— 值守屏
#: 上那几项是按秒刷的,人盯着屏等一分钟才看见变化会以为是屏卡了。
_WATER_EVERY = 60

#: 让开腿之后多久没人还回来,就升一条 P1(挂账 67a)。**先定 10 分钟,可改**
#: —— 跟聚合窗口那几个数一样是拍的,真机清单里要量、按现场手感调。
#:
#: 上界的依据是"人接管一段路"这件事本身:现场绕开一堆箱子、把狗从台阶上抱
#: 下来,几分钟是正常的;超过十分钟还挂着,压倒性的可能是人已经走开、忘了
#: 把腿还回来 —— 而这一趟从此永远停在那儿,屏幕上一切如常。
#:
#: 下界的依据是别把正常接管报成 P1:人很快就学会无视一个总在误报的 P1,
#: 而它误报一次的代价,是下一次真的没人管时没人当回事。
#:
#: **超时的处置只有报警。** 不自动 ``resume``、不自动 ``abort``:一只狗在
#: "最后已知状态是人正在接管"的情况下自己动起来,是这套系统里最不该发生
#: 的事(§5.8 同理)。
SUSPEND_STALE_MS = 10 * 60_000

#: 自动同步的巡查周期。镜像盘是"不依赖人"的那一路(spec §7.5),所以不能等人点。
#: 按分钟量级 —— 备份不是实时的,晚一分钟没有代价,而每分钟扫一次盘的开销
#: 可以忽略。
_AUTOSYNC_S = 60.0

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
    #: 这个请求是谁在发。没设 PIN 的部署上是 ``None`` —— 那种部署按定义只听
    #: 本机,没有"谁是谁"这个问题(见 ``check_exposure``)。
    session: Session | None = None

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


def _没这条告警(key: str) -> HttpError:
    """``ack``/``resolve`` 点到一条已经不在的告警 —— **404,不是 500**。

    ``AlertNotFound`` 是 ``engine/alerts.py`` 特意从裸 ``KeyError`` 里分出来
    的那一支:前端点了一条刚被别人解决掉的告警是正常的用户误操作,而
    ``raise_alert`` 撞上没登记的 kind 抛的裸 ``KeyError`` 是我们自己写错了
    代码,那个该冒成 500。这里只翻前者。
    """
    return HttpError(404, f"没有这条告警:{key}",
                     "多半是刚被别人确认或解决掉了。刷一下 /api/alerts。")


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


#: 租约被拒时统一附上的下一步。**每一条拒绝都要告诉人怎么往下走** —— 现场
#: 拿着手机的人看不到我们的代码。
_LEASE_DETAIL = ("控制权此刻在谁手上,GET /api/control 看得到。要接手就 POST "
                 "/api/control/takeover —— 对方同意、或者 15 秒不回应就归你;"
                 "紧急情况带上 force 和理由,那一下会进审计。")


def _lease_error(exc: LeaseError) -> HttpError:
    """租约层的拒绝翻成 HTTP。

    ``LeaseBusy``/``LeaseLost`` 是 **409**:"你发的没错,是现在这个状态下做
    不了"。客户端据此重试或者去接管;400 是"你发的东西不对",重试多少次都
    一样。分不开这两类,app 那头只能一律弹一个"失败了"。
    """
    status = 409 if isinstance(exc, LeaseBusy | LeaseLost) else 400
    return HttpError(status, str(exc), _LEASE_DETAIL)


def _checks_wire(report: PreflightReport) -> list[dict[str, Any]]:
    """起飞检查的结论,原样给页面。

    只给"过没过"是不够的:人站在狗旁边,要知道的是**哪一项**没过、当时是
    什么状态 —— 不然只能一项项自己试。
    """
    return [{"name": c.name, "ok": c.ok, "detail": c.detail}
            for c in report.checks]


def _check_results_wire(results: Sequence[CheckResult]) -> list[dict[str, Any]]:
    """一串检查结论,原样给页面。``CheckResult`` 没有 ``to_wire()``,补在这儿。"""
    return [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in results]


def _video_wire(ctx: AppContext) -> dict[str, bool]:
    """给 ``/api/state`` 那条常连 SSE 的视频段。**只放布尔。**

    详细的健康数字(``since_frame_s`` 之类)只出现在轮询的
    ``/api/video/health`` 上,不进这儿 —— 理由同 ``engine/lease.py`` 的
    ``每拍都变的键``:第 6 卷刚在租约的相对倒计时上栽过一次,握着控制权的
    时候那条流从"静止时安静"变成每半秒一整帧。``online`` 是个布尔,变一次
    才响一次,进这儿没问题。
    """
    return {name: (ctx.video[name].online if name in ctx.video else False)
            for name in CAMERAS}


def _run_id(run_dir: Path) -> str:
    """归档目录 -> URL 里的运行 id。见 :data:`RUN_SEP`。"""
    return f"{run_dir.parent.name}{RUN_SEP}{run_dir.name}"


#: 下载导出包时一次读多少。包可以有好几个 G,不整个读进内存。
_EXPORT_CHUNK = 1024 * 1024

#: 读不到盘上的标记时,容量那两个字段为什么是 ``null``。
_NO_MARKER_CAPACITY = (
    "读不到这块盘上的备份标记,所以容量报不出来:盘卸载之后挂载点常常作为一个"
    "空目录留在根文件系统上,照着那个路径量到的是根盘的容量 —— 把根盘的余量"
    "当成备份盘的余量,是这一屏能犯的最贵的一个错")


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


def _dir_size_mb(path: Path) -> float:
    """一棵目录树占多少 MB。落槽前算一版新包要占多少地方就靠它。"""
    return sum(p.stat().st_size for p in Path(path).rglob("*")
              if p.is_file()) / 1024 / 1024


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
    #: ``route()`` 收到的**原始模式串**(``/api/runs/<run_id>/judge`` 这种)。
    #:
    #: **留着它,不让人从 ``pattern.pattern`` 反推。** 编译过的正则里占位符
    #: 已经变成 ``(?P<run_id>[^/]+)``,想拿回"这条路由长什么样"就得写一段
    #: 反向扫描 —— 那是换了个马甲的源码文本扫描:脆,而且证明的是"正则源码
    #: 里长这样",不是"注册的时候写的是这条"。守着受控路由表那一组
    #: (``tests/app/test_control_gate.py``)读的就是这个字段。
    raw: str
    method: str
    pattern: re.Pattern[str]
    handler: Handler


def _compile(pattern: str) -> re.Pattern[str]:
    """``/api/runs/<run_id>/photos/<name>`` -> 正则。

    占位符**不跨斜杠**。这一条顺手堵住了一类穿越:``/api/runs/r1/photos/
    ..%2F..%2Fmanifest.json`` 解码之后带斜杠,于是根本匹配不上这条路由。

    **``<name*>`` 是唯一的例外,它跨斜杠**(贪婪的 ``.+``)。开这个口子是因为
    告警键(``engine/alerts.py`` 的 ``_key``)形如 ``robot/kind#seq``,斜杠是
    键本身的一部分:``/api/alerts/<key*>/ack`` 那一段收到的就是带斜杠的东西。
    转义解决不了 —— ``_dispatch`` 在匹配之前已经 ``unquote`` 过一道,``%2F``
    到这儿早变回斜杠了(这也正是上面那条穿越防线成立的原因)。

    **这个口子只许开在不落地的参数上。** 告警键唯一的去处是
    ``AlertBook`` 那本内存字典的一次 ``get``:不拼路径、不拼命令、不进
    子进程,上面那条穿越顾虑在它身上不成立。凡是会被拼进文件路径的段
    (``run_id``、``name``、``map_id``……)一律继续用不带星号的写法 —— 那些
    地方"不跨斜杠"就是防线本身。
    """
    parts = []
    for chunk in re.split(r"(<[a-z_]+\*?>)", pattern):
        if chunk.startswith("<") and chunk.endswith(">"):
            name = chunk[1:-1]
            if name.endswith("*"):
                parts.append(f"(?P<{name[:-1]}>.+)")
            else:
                parts.append(f"(?P<{name}>[^/]+)")
        else:
            parts.append(re.escape(chunk))
    return re.compile("^" + "".join(parts) + "$")


def _spawn_restart(plan: RestartPlan) -> None:
    """真去重启。**起了就不等** —— 等下去等的是自己的死。

    ``systemctl restart`` 会先把我们停掉,所以这个调用永远不会正常返回;
    ``Popen`` 之后立刻返回,让 HTTP 那一侧还来得及把响应写出去。
    """
    subprocess.Popen(list(plan.argv), start_new_session=True)


# ------------------------------------------------------------------ 上下文


def _wall_ms() -> int:
    """墙上时钟,UTC 毫秒。"""
    return int(time.time() * 1000)


def _归档钟() -> datetime:
    """归档那一摊(保留期、清盘、导出、镜像)专用的钟。**故意不走
    ``ctx.clock``,这不是漏注入。**

    这几条路由算的都是"这一趟归档存了多久":被减数是这里,减数是归档目录
    自己名字上那个时间戳,而那个时间戳是 ``engine/archive.py`` 的 ``_now()``
    盖的 —— 那是一个**外壳注不进去**的 ``datetime.now(timezone.utc)``
    (``engine/retention.py`` 的默认钟同理)。两个数必须来自同一口钟才减得
    出意义:注入钟一旦被拨(测试里拨、将来接了时间服务器之后校时拨),
    ``plan_sweep`` 算出来的"过期"就会横跨两个纪元 —— 而 ``plan_sweep`` 后面
    接的是 ``shutil.rmtree``。**宁可报表上的时间跟别处差几秒,也不能按一个
    错纪元去删盘。**

    要真把这摊改成可注入,得连 ``engine/archive.py`` 的 ``_now()`` 一起改,
    那不是这一层能单独完成的动作。在那之前,这个函数就是那条分界线:
    **``ctx.clock()`` 那一侧是"app 自己盖、app 自己读"的时间戳;这一侧是
    "engine 盖的、app 只能跟着读"的时间戳。**
    """
    return datetime.now(timezone.utc)


def _no_time_reference() -> tuple[int, str] | None:
    """默认没有外部时间参照。第 9 卷接上服务器之后换成真的。

    **回 ``None`` 而不是回当前时间。** 没有参照的时候,漂移是「不知道」,
    不是 0 —— 报 0 等于说「钟是准的」(§3.3 第 4 条)。
    """
    return None


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
    video: Mapping[str, CameraFeed] = field(default_factory=dict)
    #: 这是哪只狗。手机按它认机器、给归档分组 —— 每只 D1 Max 的内网地址
    #: 都一样,靠地址分不出谁是谁(见 ``app/identity.py``)。
    identity: Identity = field(default_factory=resolve)
    #: 版本目录摆在哪。**默认是真机上的路径**,测试传 tmp_path。
    release_root: Path = Path("/opt/d1max")
    #: 怎么执行重启。engine 只出方案,执行注入进来 —— 测试里换成一个记账的。
    restart: Callable[[RestartPlan], None] = _spawn_restart
    #: 「有没有上装」记在哪。``None`` = 用 ``identity.payload_path`` 的默认
    #: 解析顺序(环境变量,再默认路径)。
    payload_file: Path | None = None
    #: 任务包目录摆在哪。**默认是真机上的路径**,测试传 tmp_path。
    bundles_root: Path = Path("/opt/d1max/bundles")
    #: 现在几点(UTC 毫秒)。**注进来的**:这一整卷都是时间逻辑,测试里要能把
    #: 钟拨到 22:00 而不是等到 22:00(§8.5 第 2 条)。
    clock: Callable[[], int] = _wall_ms
    #: 外头的时间参照:``(毫秒, 来源)``,拿不到就 ``None``(断网时就是)。
    time_reference: Callable[[], tuple[int, str] | None] = _no_time_reference
    #: 告警簿(§5.2)。**跟 ``engine`` 同级挂在这儿,全进程只有这一份** ——
    #: 认事实的(``app/alert_sources.py``)、看板(卷 8 的值守屏)、四条告警
    #: 路由,问的必须是同一本簿子。各造各的,页面上确认掉的那条在别处还开
    #: 着,升级照样往下走,而现场看到的是"我明明点过确认了,它还在响"。
    alerts: AlertBook = field(default_factory=AlertBook)
    #: 回传那条后台线程。``None`` = **这台狗没装回传**(单机档,客户没买
    #: 服务器),不是「装了但是空着」—— 值守屏上那一格的 ``None`` 和 ``0``
    #: 差的就是这件事。装不装由 :func:`build_pump` 的调用方按有没有回传地址
    #: 决定;**测试夹具默认不装**,所以现有那一堆 app 测试看到的照旧是
    #: ``null``。
    upload: UploadPump | None = None

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


def _run_rel(key: str) -> str:
    """从队列的 key 里切出那一趟的目录(相对 ``runs_root``)。

    **key 的前两段就是一趟,后面全是 rel,而 rel 是会带斜杠的。**
    形状由 ``engine/uploader.py`` 的 ``scan()`` 定死:``<任务>/<时刻>/<rel>``,
    照片那一支的 rel 是 ``photos/<名字>.jpg``(``engine/archive.py`` 就写在
    那儿,``engine/upload_queue.py`` 也正是靠这个前缀认照片的优先级)。

    **所以不许用 ``rsplit("/", 1)[0]``。** 那个写法在 ``events.jsonl`` 上碰巧
    对,在照片上给出的是 ``<任务>/<时刻>/photos`` —— 而
    :func:`~d1max_patrol.engine.retention.mark_uploaded` 不做任何校验,拿到
    这么一个路径也照样 ``touch`` 成功。两个后果都不出声:那一趟永远进不了
    「可删」(``scan_runs`` 查的是 ``<run>/`` 底下那个标记),水位线于是扫不到
    东西可删,**真机上盘会满**;而「这一趟还剩几个没传完」的判据用的是同一个
    前缀,只看 photos 的话 ``events.jsonl`` 还在排队就会**提前**打上标记。
    """
    return "/".join(key.split("/")[:2])


def _该打可删了吗(runs_root: Path, queue: UploadQueue, run_rel: str) -> None:
    """两件事**同时**满足才打「可删」—— 而且只打标,**不删**(spec §4.4)。

    (a) **队列里这个 run 前缀下没有还没 ``done`` 的条目。**
        少了这一条,一趟里先传完的那张照片会把整趟判成可删,而
        ``events.jsonl`` 还在队里排着。

    (b) **这一趟已经收工**(:func:`is_settled`)。
        少了这一条,后果比 (a) 严重得多,而且**每一趟都会中招**:
        ``Uploader.scan()`` 的规矩是"不问这一趟结束了没有,谁在盘上谁就传"
        (spec §4.1),所以巡检**刚开跑**、盘上只有 ``manifest.json`` 和刚起头
        的 ``events.jsonl`` 时它们就进了队;pump 很快把这两个传完,此时 (a)
        成立 —— 于是一趟**正在跑**的巡检被打上了「可删」。而 ``.uploaded``
        只 ``touch``、**永不撤销**:等这一趟 settled 了(写了 summary,或者崩
        在半路被 ``SETTLE_HOURS`` 老化),``_deletable(..., has_upload=True)``
        认的就是 ``info.uploaded`` 这一个字段 —— 直接放行,水位线把整趟
        ``rmtree`` 掉,而照片可能一个字节都没传出去。最坏的一档恰好是崩溃那
        一趟:没有 summary,靠老化成 settled,而 §4.1 说它"恰恰是最需要看的
        一趟"。

    真删由水位线驱动(``engine/retention.py`` 那一套),不由上传驱动:一份
    传上去了的证据该留多久是保留期的事,跟"它传没传上去"是两个判据。

    **(b) 的判据一个字都不许在这儿重写**,整条走 :func:`is_settled` ——
    理由写在那个函数的文档串里。

    最前面还有一道**不是判据、是分辨**的门:run 目录还在不在。
    见下面那段注释 —— 它把"水位线删过了"跟"盘出事了"分开,
    这两件事在这儿必须分得开。
    """
    run = Path(runs_root) / run_rel
    # **「这个目录不在了」是一个预期之内的状态,不是错误。**
    #
    # 队列**按设计**永远留着已经 ``done`` 的条目(``UploadQueue._compact``:
    # 要留 offset/size 给追加流),所以水位线 ``rmtree`` 掉一趟归档之后,
    # 它那几条 key 还在队里 —— 而 ``.uploaded`` 跟着目录一起没了。于是这一
    # 趟每一拍都会走到这儿:条件 (a) 全 ``done`` 放行,条件 (b) 靠目录名老过
    # ``SETTLE_HOURS`` 也放行,最后拿一个**不存在的目录**去 ``touch`` ——
    # ``FileNotFoundError``。那一下会把 :func:`_补打可删` 的循环当场打断,
    # 排在后面的每一趟这一拍都轮不上;而那条死记录永不消失,于是**每一拍
    # 重演**,第二触发点从此永久失效 —— 水位线第一次动手就把它自己捅穿了。
    #
    # **所以这儿要的是一句「分辨」,不是一个 ``try``。** 目录不在就是没这一
    # 趟可标,安静走人;而"盘坏了、权限没了、只读挂载"那些**真的**
    # ``OSError`` 照旧原样往上抛 —— 把它们一起吞掉会把"循环会断"换成"永远
    # 不报错也永远不干活",那更糟,因为连 ``last_error`` 都不叫了。
    #
    # 归档盘短暂掉线时这儿同样回 ``False``(``runs_root`` 都读不到),方向跟
    # ``Uploader._missing()`` 第 1 分支一致:**不销账、不打标**,等盘回来。
    if not run.is_dir():
        return
    if any(i.key.startswith(f"{run_rel}/") and not i.done for i in queue.all()):
        # **有没传完的就不许挂着「可删」** —— 包括已经挂上去的。W02 让被原地
        # 改写的报告重新入队,这时标记若还在,水位线会在重传完成前把整趟删掉
        # (W03)。撤了之后全传完会走下面那条再打回来。
        unmark_uploaded(run)
        return
    if (run / UPLOADED_REL).exists():
        return
    if not is_settled(run):
        return
    mark_uploaded(run)


def _why_not_uploaded(run: Path, run_rel: str, queue: UploadQueue,
                      busy: Collection[Path] = ()) -> str | None:
    """这一趟**此刻**真的全在服务器上了吗。回 ``None`` 表示是;否则回一句原因。

    ``busy`` 是**正在判读**的那几趟(``AppServer._busy_runs``):判读要几分钟,
    模型调用不能放进协调锁,所以判读一开始就登记、提交或失败后撤销;登记期间
    这一趟不算已传 —— 不然清盘会把它正在读的照片和马上要写的 findings 一起删掉。

    「可删」的判据不能只看盘上那个 ``.uploaded`` 标记(它是异步打的、异步撤的),
    要对着**活队列和盘上的文件**重新算一遍(W03,外部审核的阻断项):

    * 队列里这一趟还有没 ``done`` 的 —— 不算。
    * 盘上该入队的文件(``classify`` 认的),队列里**没有**它 —— 说明是判读/复核
      刚写出来、pump 还没 scan 到的,不算。
    * 队列里有,但 ``size``/``mtime_ns`` 跟盘上对不上 —— 说明文件被原地改写了、
      pump 还没 scan 到(W02 的判据就是这两个数),不算。老条目 ``mtime_ns`` 为 0
      也不算 —— 下一拍 scan 补上就好,等得起。

    这三条一起,才把"文件改写 → pump scan"之间那段没人守的窗也盖住;光靠撤标
    只盖得住"scan 之后"。
    """
    if not run.is_dir():
        return "目录不在了"
    if run in busy:
        return "正在判读,结论还没写完"
    if not (run / UPLOADED_REL).exists():
        return "没有「可删」标记"
    if not is_settled(run):
        return "这一趟还没收工"
    prefix = f"{run_rel}/"
    by_key = {i.key: i for i in queue.all() if i.key.startswith(prefix)}
    if any(not i.done for i in by_key.values()):
        return "队列里还有没传完的"
    for path in run.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(run).as_posix()
        if classify(rel) is None:
            continue
        item = by_key.get(f"{run_rel}/{rel}")
        if item is None:
            return f"{rel} 还没入队(刚写出来,pump 还没扫到)"
        try:
            st = path.stat()
        except OSError:
            return f"{rel} 读不到"
        if item.mtime_ns == 0 or item.size != st.st_size or item.mtime_ns != st.st_mtime_ns:
            return f"{rel} 盘上跟队列记的对不上(改写过,pump 还没扫到)"
    return None


def _mask_uploaded_by_queue(runs: list[RunInfo], runs_root: Path,
                            queue: UploadQueue,
                            busy: Collection[Path] = ()) -> list[RunInfo]:
    """水位线拿方案之前,把 ``uploaded`` 按 :func:`_why_not_uploaded` 重算一遍:
    标记说传完了、活队列或盘上文件说没有,**听后者的**。"""
    root = Path(runs_root)
    out: list[RunInfo] = []
    for info in runs:
        if not info.uploaded:
            out.append(info)
            continue
        try:
            rel = info.path.relative_to(root).as_posix()
        except ValueError:
            out.append(replace(info, uploaded=False))
            continue
        out.append(replace(info, uploaded=False)
                   if _why_not_uploaded(info.path, rel, queue, busy) else info)
    return out


def _run_finished(runs_root: Path, queue: UploadQueue, key: str) -> None:
    """**快的那条触发**:一个文件对上了,顺手看看它那一趟够不够格打「可删」。

    它一个人不够 —— 见 :func:`_补打可删`。
    """
    _该打可删了吗(Path(runs_root), queue, _run_rel(key))


def _补打可删(runs_root: Path, queue: UploadQueue) -> None:
    """**补漏的那条触发**:回头把"传完了,但当时还没收工"的那几趟捡起来。

    **没有这一条,应修那个 bug 换个位置照样发生。** ``_run_finished`` 只在
    "有文件对上了"那一声上触发,而一趟的最后一个文件很可能在 ``summary``
    写下**之前**就传完了(照片传完 → 狗还在往回走 → 才写 summary)。那之后
    这一趟再也不会有第二声 ``done``,于是「可删」**永远打不上**、
    ``scan_runs()`` 永远看不到它、这一趟永远不可删 —— **真机上盘还是会满**,
    只不过这次是从另一头满的。

    挂在 pump 重扫 runs 目录那一拍上(``SCAN_EVERY_MS``),跟着它的节奏走:
    新文件是那一拍进的队,"收没收工"也该在那一拍重新问一遍。

    只看队列里出现过的那几趟 —— 没排过队的目录跟回传这条线没关系。

    **一趟出事不许拖垮同一拍里其余的趟。** 下面那个 ``try`` 不是用来消音的:
    接的是 ``OSError``(不是 ``Exception``,也没有 ``# noqa``),攒起来、跑完
    整圈之后**原样抛出去**,由 ``_喊一声扫过了`` 记 traceback 并写上值守屏。
    它只买一件事 —— 让排在出事那一趟**后面**的趟这一拍照样轮得上。
    "水位线删过了"根本进不到这个 ``try`` 里:那一支在
    :func:`_该打可删了吗` 最前面就被分辨出去了,不会留下假报错。
    """
    出事的: list[str] = []
    for run_rel in dict.fromkeys(_run_rel(i.key) for i in queue.all()):
        try:
            _该打可删了吗(runs_root, queue, run_rel)
        except OSError as exc:
            出事的.append(f"{run_rel}({type(exc).__name__}: {exc})")
    if 出事的:
        raise OSError("这几趟的「可删」没打上: " + "、".join(出事的))


def build_pump(ctx: AppContext, console_url: str, *, token: str = "") -> UploadPump:
    """**只有这一处知道怎么把那四个零件接起来。**

    队列落在 ``runs_root`` 的**兄弟位置**(见 :data:`QUEUE_FILE_NAME`)。

    ``on_done`` / ``on_scan`` 那两口是分层的关节:``app/upload_pump.py`` 只
    喊"这个文件对上了"和"我刚重扫过盘",打「可删」这件事在这儿做 ——
    ``engine/`` 一行都不用知道 ``retention`` 的存在。

    **两个触发点缺一不可**,理由分别写在 :func:`_run_finished` 和
    :func:`_补打可删` 上;两条都走同一道闸(:func:`_该打可删了吗`),
    所以判据仍然只有一份。
    """
    runs_root = Path(ctx.runs_root)
    queue = UploadQueue(runs_root.parent / QUEUE_FILE_NAME)
    sink = HttpSink(console_url, token=token)
    up = Uploader(ctx.runs_root, queue, sink, sn=ctx.identity.sn)
    return UploadPump(
        up, clock=ctx.clock,
        on_done=partial(_run_finished, runs_root, queue),
        on_scan=partial(_补打可删, runs_root, queue))


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


#: 会换归属的那几种留痕。别的(``takeover_asked`` 之类)只是打个招呼,不动
#: 租约本身,读顺序的时候要跳过 —— 不跳的话,一条"我想接管"就能把前面那条
#: ``expired`` 挡住。
_换手 = frozenset({"expired", "released", "acquired", "taken_over", "forced"})


def _该量水位了(拍: int) -> bool:
    """第 ``拍`` 拍该不该量一次水位。第 1 拍量,之后每 :data:`_WATER_EVERY` 拍一次。

    **起飞那一拍就量。** 盘满、包落好了没生效、钟偏,这三件事在服务起来的那
    一刻通常就已经成立了 —— 等半分钟才第一次去看,等于让值守屏在开机后的头
    30 秒里说一句它并不知道的"没事"。

    写成 ``(拍 - 1) % N`` 而不是 ``拍 % N == 1``:后者在 ``N == 1``(每拍都
    量)时永远是假,是一个只在改常量那天才现形的坑。
    """
    return (拍 - 1) % _WATER_EVERY == 0


def _挂起超时了(点: SuspendPoint | None, *, now_ms: int) -> bool:
    """让开腿之后没人还回来,超过 :data:`SUSPEND_STALE_MS` 了吗。

    **算的是 ``SuspendPoint.at_ms``,不是这一趟的 ``started_ms``。** 一趟任务
    可以跑一整天,人在最后一分钟才让开腿 —— 拿开跑那一刻算,人刚把手机掏出
    来就会挨一条 P1,而那种误报会很快教会人无视这条告警。这个函数只收一个
    ``SuspendPoint``,拿别的东西算这件事在这儿写不出来,这是故意的。

    ``None``(没让开腿)一律为假。**不许当成"很久以前"** —— 那样任务正常
    跑着也会报,而这条告警的全部意思就是"有人接管了却没还回来"。

    严格大于:正好卡在 :data:`SUSPEND_STALE_MS` 上还不算超时,再多一毫秒才
    算(跟 ``due_escalations`` 里那个 ``>`` 一个口径)。

    **``now_ms`` 必须是 ``MissionEngine.now_ms()``,不许是 ``ctx.clock()``。**
    这个式子里的三个数 —— ``now_ms``、``点.at_ms``、``点.prior_suspend_ms``
    —— 现在全出自引擎那一口钟:开跑时对一次墙钟当锚点,之后按可注入的单调钟
    往前推(见 ``engine/machine.py`` 的 ``_stamp_ms`` / ``now_ms``)。取数的
    地方只有一处,:meth:`_StateHub._判定挂起超时`。

    **``ctx.clock`` 不再参与这条判据,这是换过一次的。** 原来 ``at_ms`` 也是
    现问的墙钟,两边一起跳,差值反而是对的;``at_ms`` 换成"锚点 + 单调"之后
    它就不跟着跳了 —— 这时候再拿活墙钟去减,现场没有 NTP 的狗上墙钟往前跳
    一小时,就是"人刚让开腿就挨一条 P1"(正好撞上 :data:`SUSPEND_STALE_MS`
    注释里那段下界论证),往后跳则该报的永远不报,**两种都不会有任何一条
    测试红**。守卫断在测试那一侧:
    ``tests/app/test_suspend_e2e.py::test_这条P1的三个数必须同源``
    和 ``::test_墙钟往前跳不许因此报这条P1``。

    **算的是「这一趟返航累计挂了多久」,不是「这一次挂了多久」。**
    ``prior_suspend_ms`` 是引擎记的、同一趟返航里之前几次接管的总和,加上这
    一次已经挂着的时长才是判据。少了它,现场那一幕是这样的:狗在返航路上被
    拉开、放回、又被拉开,每一次都只有四五分钟 —— 这条 P1 一次都不报,而狗
    从电量到线那一刻起就一直没在往家走,最后耗到没电,全程零告警(引擎那边
    ``while True`` 每圈还会把 ``RETURN_TIMEOUT_S`` 重算,看门狗只在两次接管
    之间那几秒的缝里有机会开火)。

    **接管次数不封顶,只补可见性。** 封顶意味着在人最需要把狗拉到一边的时候
    拒绝他 —— 电量低正是现场最紧张的时候,那跟"返航途中也能让开腿"这件事
    存在的理由直接冲突。而且"次数超 N"要一个新阈值,N 只能等真机耗电率。
    时长复用已有的 :data:`SUSPEND_STALE_MS`,**不新增可调项**:语义变成
    "这条狗电量已经到返航线,却累计有十分钟没在往家走,因为一直有人在挪它"
    —— 现场的人读得懂,也知道该干什么。

    **两个加数现在同源,整条式子在一根轴上。** ``now_ms - 点.at_ms`` 是引擎
    那口钟走过的量,``prior_suspend_ms`` 是引擎拿同一个 ``clock`` 量出来的
    —— 不再是原来那种"相加的是两段时长,纪元不同也不影响"的将就,而是同一
    根时间轴上的加法。墙钟中途被校、或者压根没有 NTP,这三个数一个都不动。

    **剩下的真实缺口,就一条:锚点本身不准的时候,``at_ms`` 作为「几点几分」
    不准。** ``_stamp_ms`` 的锚点是 ``start()`` 那一刻读的真墙钟,归档和手机
    上显示的"让开腿的时刻"都按它算 —— 开跑那一刻墙钟偏多少,那个显示值就偏
    多少。但**这条判据只吃差值,锚点在减法里被抵掉了**,所以它偏多少都不影响
    ``suspend_stale`` 报不报。这个缺口不在这个函数的地盘里(要修是"开跑时对
    表对得准不准",属于钟偏那一摊),这里只是说清它存在、以及为什么它动不了
    这条 P1。
    """
    if 点 is None:
        return False
    return now_ms - 点.at_ms + 点.prior_suspend_ms > SUSPEND_STALE_MS


def _失主了(fresh: tuple[AuditRecord, ...]) -> bool:
    """这一批留痕看完,狗是不是没主了 —— **看最后一条换手的是不是到期**。

    ``LeaseBook`` 是一批一批地结算的:``_settle`` 在到期那一刻如果正好有人排
    着队,写完 ``expired`` 会紧接着写一条 ``taken_over``。所以"有没有
    ``expired``"这个问题本身问错了,该问的是"这一串换完手之后,还有没有人握
    着"。

    从后往前找第一条换手的:是 ``expired`` 就说明没人接着;是 ``acquired`` /
    ``taken_over`` / ``forced`` 说明有人接着了(排队接管,或者在看门狗这半秒
    的间隙里有人重新取了控制权);是 ``released`` 说明人自己交回来的,那本来
    就不该报。一条换手的都没有 —— 这一拍什么也没发生。
    """
    for rec in reversed(fresh):
        if rec.kind in _换手:
            return rec.kind == "expired"
    return False


class _StateHub:
    """把引擎、导航、设备三条事件流汇成一条,并且始终备着一份全量快照。

    **为什么要备快照,而不是请求来了现问后端。** ``nav_status()`` 这类方法在
    厂商后端上是一次真实的 WebSocket 往返:页面每秒问一次,就等于每秒往链路
    上压一条请求,而链路正忙着导航。快照是事件推出来的,读它不花一分钱。

    快照每次都**整个重建、整体替换**。HTTP 线程只读 ``snapshot`` 这一个引用,
    读到的要么是旧的一整份、要么是新的一整份,不会读到改了一半的。
    """

    def __init__(self, ctx: AppContext, control: ControlDesk) -> None:
        self._ctx = ctx
        self._control = control
        self.events: EventEmitter[dict[str, Any]] = EventEmitter()
        self._snapshot: dict[str, Any] = {}
        self._tasks: list[asyncio.Task[None]] = []
        # 下面这些量只在事件里出现,后端不提供只读属性,所以要自己记着。
        self._nav_status: str | None = None
        self._loc_status: str | None = None
        self._mapping: str | None = None
        self._alg_errors: tuple[str, ...] = ()
        self._link_down: str = ""
        #: 最近一次电量遥测:``(百分比, 收到的时刻毫秒)``,一次也没收到就是
        #: ``None``。**两件事存在一个字段里,不拆成两个** —— HTTP 线程要同时
        #: 用到它俩(值守屏那一格既报电量也报「这个数多老了」),拆成两个字段
        #: 的那一边,两次读之间夹进一个新事件,屏上就会出现「刚刚收到的」配着
        #: 上一拍的数值。一个字段一次赋值,读到的永远是配对的。
        self._battery_at: tuple[float, int] | None = None
        self._faults: tuple[str, ...] = ()
        self._control_lost: str = ""
        self._pose: dict[str, float] | None = None
        #: 把这三条汇流上的事实翻成告警(§5.2)。**只在这一处接**:这个类
        #: 已经是全进程唯一一个同时看得见三条流的地方,再开一份订阅就等于
        #: 多一条会跟快照说法不一致的路。判什么级不归这儿管,见
        #: ``app/alert_sources.py``。
        self._alerts = AlertSources(
            ctx.alerts, robot=ctx.identity.sn, clock_ms=ctx.clock,
            run_state=lambda: ctx.engine.snapshot.state,
            # 后面这三个是 ``on_tick`` 那几条周期事实的取值口(盘水位、任务
            # 包滞后、钟偏)。``disk=`` 传的就是 ``/api/storage`` 用的那一个
            # 函数 —— 一个口径,两个出口。
            disk=lambda: _disk(ctx.runs_root),
            bundles_root=ctx.bundles_root,
            time_reference=ctx.time_reference)
        #: 看门狗读到哪条留痕了。**自己一份游标,不借 ``ControlDesk.drain()``
        #: 那份** —— 那份是事件流的,借来读一次就把记录从 SSE 嘴里抢走了。
        #: ``LeaseBook.audit_since`` 是纯读,两个游标各走各的互不干扰。
        #:
        #: **从 0 起,前提是这本簿子是这个进程新建的。** 今天成立:
        #: ``LeaseBook`` 只活在内存里,0 就是"空历史"。哪天审计流水改成落盘
        #: (值守场景多半要),这一行就得跟着改成"从簿子当前末尾起" ——
        #: 否则重启后第一拍会把历史里所有 ``expired`` 一次读进来,只要那一刻
        #: 恰好没人握着租约,就凭空报一条 P1,而那些"到期"是上辈子的事。
        self._lease_cursor = 0
        #: 闸门醒过几拍。只给分频和测试用 —— 它是这条协程"还活着"的唯一外部
        #: 迹象(判到期本身是静默的:没到期就什么也不发生)。
        self._lease_ticks = 0
        #: 哪一次让开腿已经报过"没人还回来"了,存的是那一次的
        #: ``SuspendPoint.at_ms``;没报过就是 ``None``。
        #:
        #: **非有不可。** 闸门半秒醒一拍,而 ``raise_alert`` 在聚合窗口内会把
        #: 同一条 ``robot/kind`` 吸收成一条并把 ``count`` 往上加 —— 不记这一
        #: 笔的话,一次没人管的接管十分钟就能把 ``count`` 顶到一千二,值守屏上
        #: 那一行看起来像是出了一千两百件事。
        #:
        #: 存 ``at_ms`` 而不是一个布尔:接管结束又让开一次腿是**另一次**接管,
        #: 那一次该重新报。
        self._挂起报过: int | None = None
        #: "让位了却没盖点"这句 ERROR 喊过没有。**跟 ``_挂起报过`` 一个理由,
        #: 而且更急**:那件事是一个**会一直存在**的状态(不是瞬时竞态),而闸
        #: 门半秒醒一拍 —— 不记这一笔就是 2 条 ERROR/秒、通宵二十万条。
        #:
        #: 这在别的项目上只是吵,在这台狗上是**自伤**:这条日志刷的盘,正是同
        #: 一套值守在量 ``disk_used_ratio`` 的那块盘。一条诊断日志把自己的盘写
        #: 满、然后触发一条 P1,比不报还糟。
        #:
        #: 清账时机跟 ``_挂起报过`` 一致:回到正常状态(``not engine.yielding``)
        #: 时清掉,这样下一次真出问题还会再喊一声,不会喊过一次就永远闭嘴。
        #:
        #: **已知的一条缝,这一轮没修,记在这儿免得下一个人以为它管得住所有
        #: 情况:** 清账只挂在 ``yielding`` 落回假这一件事上,所以**同一次接管
        #: 之内**如果 ``suspended_at`` 走了一趟 ``None → 有 → None``,第二次
        #: 的 ``None`` 一声不吭 —— 账还记着,而 ``yielding`` 从头到尾没落过。
        #: 今天摆不出这一幕(引擎在一次让位里只盖一次点、不中途抹),后果也
        #: 有限(丢的是一条诊断日志,不是那条 P1 本身:``_挂起报过`` 是另一
        #: 笔账,该报还是会报)。真要堵,判据得从"``yielding`` 落过没有"换成
        #: "这一次没盖点跟上一次是不是同一段",那要往这条协程里再存一个状态,
        #: 这一轮不值。
        self._没盖点喊过 = False

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._rebuild(force=True)
        ctx = self._ctx
        self._tasks = [
            asyncio.create_task(self._watch(ctx.engine, self._on_run)),
            asyncio.create_task(self._watch(ctx.nav, self._on_nav)),
            asyncio.create_task(self._watch(ctx.device, self._on_device)),
            asyncio.create_task(self._tick()),
        ]
        # 闸门那条单独建,因为它要挂一个"你要是死了得有人知道"的回调。别的
        # 几条死掉是"屏幕不刷新了",人看得见;这一条死掉是**静默**的 ——
        # 页面照常、SSE 照常,只是从此再没有人去问那 30 秒过没过去。
        闸门 = asyncio.create_task(self._lease_watchdog())
        闸门.add_done_callback(self._看门狗塌了)
        self._tasks.append(闸门)

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

    @property
    def battery_at(self) -> tuple[float, int] | None:
        """最近一次电量遥测:``(百分比, 收到的时刻毫秒)``,没收到过就 ``None``。

        **一次读到的是配对的两个数**,理由见 ``_battery_at`` 那一行。
        """
        return self._battery_at

    # ------------------------------------------------------------ 事件

    async def _watch(self, emitter: Any,
                     on_event: Callable[[Any], None] | None = None) -> None:
        """跟一条事件流。引擎那条不用往快照里记什么 —— 它的状态在
        ``engine.snapshot`` 里;但告警要认它,所以它也带了个回调。"""
        with emitter.subscription() as inbox:
            while True:
                event = await inbox.get()
                if on_event is not None:
                    on_event(event)
                self._rebuild()

    async def _tick(self) -> None:
        """定期重建,顺便结算控制权。

        链路断没断、子进程还在不在,这些是**属性**不是事件,没人会推给我们。
        租约到期也一样:没有人会来通知"那 30 秒过去了"。只有变了才发,所以静
        止的时候这条 SSE 是安静的。

        **这一拍只管显示和事件流,闸门不靠它。** 这里的 ``sweep()`` 是为了让
        ``/api/state`` 和 SSE 上看到的控制权状态别拖太久;真正拦请求的
        ``ControlDesk.require()`` 自己会在每次判定时调 ``sweep()``
        (见 ``app/control.py``),不依赖这条 tick 有没有按时跑。也就是说,
        就算这个协程因为某种原因卡住甚至死掉,也不会出现"租约早过期了,
        但闸门还认"这种事——顶多是屏幕上的数字晚一拍才更新。
        """
        while True:
            await asyncio.sleep(_TICK_S)
            self._control.sweep(now_ms=self._ctx.clock())
            for rec in self._control.drain():
                self.events.emit({"kind": "control", "event": rec.to_wire()})
            self._rebuild()

    async def _lease_watchdog(self) -> None:
        """租约到期:停 + 升 P1,**绝不自动续跑**(§5.8)。

        ``LeaseBook`` 是惰性结算的——没人问它就"还没过期"。而 §5.8 要的
        恰恰是**没人在的时候**它自己动作,所以必须有一条协程主动去问。

        **不并进 ``_tick``**:那条协程自己的 docstring 写着"这一拍只管显示
        和事件流,闸门不靠它"——显示晚一拍没关系,停车不能晚,两条相反的
        容忍度不该落在同一条协程上。

        顺手也把 ``AlertSources.on_tick`` 那三条周期事实(盘水位、任务包滞
        后、钟偏)带上:它们跟租约到期是同一类东西 —— **没有任何一条流会推
        给我们,只能自己隔一会儿去看一眼**。

        **兜的是 ``OSError`` 和 ``ValueError`` 这两类,别的照样会把这条协程
        掀翻。** 这是有意的,而且必须说清楚它兜不住什么:``raise_alert`` 撞上
        没登记的 ``kind`` 抛的是 ``KeyError``,那是 ``alerts.py`` 特意设的
        闸——"不许猜一个级别顶上";拓宽成 ``except Exception`` 会把那道闸和
        一堆真 bug 一起吞掉(房规里 ``BLE`` 禁的就是这个)。
        ``asyncio.CancelledError`` 更不能进网(它是 ``BaseException``),不然
        ``stop()`` 取消不掉这条协程。

        **所以死掉这件事本身要有人接着。** 这条协程死掉的后果是静默的:页面
        照常、SSE 照常,只是从此再没有人去问那 30 秒过没过去 —— 而它存在的
        全部理由就是没人在场。``self._tasks`` 只在 ``stop()`` 时才被 await,
        异常在那之前一个字都不会打出来。补法是 ``start()`` 里给它挂的那个
        ``add_done_callback``,见 ``_看门狗塌了``。
        """
        while True:
            await asyncio.sleep(_LEASE_WATCH_PERIOD_S)
            self._lease_ticks += 1
            try:
                await self._lease_once(水位=_该量水位了(self._lease_ticks))
            except (OSError, ValueError) as exc:
                log.warning("租约看门狗这一拍出错了:%s", exc, exc_info=True)

    def _看门狗塌了(self, task: asyncio.Task[None]) -> None:
        """闸门那条协程结束了。**除了被取消,没有一种结束是正常的。**

        它是个 ``while True``:正常情况下只有 ``stop()`` 的取消能让它出来。
        剩下的路只有一条 —— 抛了一个不在兜底网里的异常。那一刻起租约到期
        没人处置,而屏幕上一切如常,所以这里既要 ``log.error``,也要在簿子上
        留一条 P1:日志没人盯着,告警栏有人盯着。

        **回调自己不许再抛。** 它跑在事件循环的 ``call_soon`` 上,抛出去只会
        进 loop 的异常处理器,又是一次静默。``raise_alert`` 那几条已知的抛法
        (没登记的 kind、级别对不上、簿子写不动)都在这条 suppress 里。
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        log.error("租约看门狗协程死了,从此没人再问 TTL 过没过去:%s", exc,
                  exc_info=exc)
        with contextlib.suppress(KeyError, ValueError, OSError):
            self._ctx.alerts.raise_alert(
                kind="watchdog_died", robot=self._ctx.identity.sn,
                title="值守闸门那条协程死了",
                detail=f"{type(exc).__name__}: {exc} —— 租约到期从此没人处置,"
                       "遥控走了也不会自动停。重启服务才能恢复(§5.8)。",
                now_ms=self._ctx.clock())

    async def _lease_once(self, *, 水位: bool = True) -> None:
        """看一眼:租约过期了没有;每 :data:`_WATER_EVERY` 拍再看一眼水位。

        **判"到期"靠审计流水,不靠 ``holder`` 由有变无。** 人自己按了释放,
        ``holder`` 也是由有变无 —— 靠它分不开"走了"和"交回来了",而把一次
        正常交接报成 P1,人很快就学会无视 P1。``AuditRecord.kind`` 里
        ``released`` / ``expired`` / ``dropped`` 是分得清清楚楚的。

        **还要"到期之后没有人接着"** —— 判据见 :func:`_失主了`,读的是这一
        批留痕里的先后顺序,不是"读完之后当下 holder 是谁"。后者有两个窟窿:
        到期那一刻有人排着队会被 ``taken_over`` 立刻接走(正常换人,狗没有失
        主);而在这半秒的间隙里有人重新 ``acquire``,那条 ``expired`` 已经被
        游标吃掉了,下一拍再也读不到 —— 处置就这么被跳过。按顺序读这一批,
        两个窟窿一起堵上。

        **排队接管那条路上是故意不停腿的。** 前一个人的手机已经走了,可他那
        一档遥控没有在这里被 ``stop()``;兜底的是 ``Teleop`` 自己那个 0.6 秒
        的守死人开关(``HEARTBEAT_TIMEOUT_S``)—— 心跳一断它自己把 ``active``
        落下来并发一次零速。在这儿顺手停一下不是更保险:接管的那个人这一秒
        很可能已经在推摇杆了,一条来自"上一任到期"的零速会把他的动作打断,
        而他看到的是狗莫名其妙顿了一下。
        """
        now_ms = self._ctx.clock()
        self._control.sweep(now_ms=now_ms)
        self._lease_cursor, fresh = self._control.book.audit_since(
            self._lease_cursor)
        if _失主了(fresh):
            await self._lease_gone(now_ms)
        self._判定挂起超时(now_ms=now_ms)
        # **这一行的副作用就是它存在的理由,返回值是故意丢掉的。**
        # ``due_escalations`` 会把该升档的告警的 ``escalated`` 就地前移,而
        # ``Alert.channel`` 是由 ``escalated`` 直接算出来的 property、
        # ``to_wire()`` 两个都上线 —— 也就是说"该出声了"这件事一路推到手机
        # 上,靠的全是这次调用改掉的那个整数,不需要在这儿再搭一条投递。
        # 任务 12 之前**全仓没有一处生产代码调它**:一条没人确认的 P1 永远停
        # 在 ``escalated=0`` / ``channel=screen``,声音一次也没响过,而
        # engine 那一组测试全绿。看着像死代码就把它"顺手清掉"的话,线上会
        # 静悄悄地退回那个样子(端到端的守卫见
        # ``tests/app/test_alert_routes.py::test_升级真的有人在驱动``)。
        self._ctx.alerts.due_escalations(now_ms=now_ms)
        # 上面这两条都在 ``if 水位:`` **外面**,这是有意的:分频是为盘水位那
        # 几件 I/O 活儿设的(见 :data:`_WATER_EVERY`),而这两条一条是读引擎
        # 快照、一条是扫一本内存字典,半秒一次的开销可以忽略。挂进去等于把
        # "人接管没还回来"和"P1 该出声了"的时机焊死在盘水位的轮询节拍上。
        if 水位:
            self._alerts.on_tick()

    def _判定挂起超时(self, *, now_ms: int) -> None:
        """人点了「让开腿」接管,接管完忘了还回来 —— 报一条 P1(挂账 67a)。

        **只报警,不动狗。** 这里没有、也不许有任何一句 ``resume`` / ``abort``:
        一只狗在"最后已知状态是人正在接管"的情况下自己动起来,是这套系统里
        最不该发生的事(理由跟 :meth:`_lease_gone` 那一条一样)。人还在现场,
        腿是他的。

        判据整个交给 :func:`_挂起超时了`,这儿只负责取数和记账。取的是引擎
        自己的 ``yielding``,不在外壳里比一遍状态表 —— "哪些状态算让位"是
        引擎的词汇,复刻一份迟早跟正主不一样。

        **这一拍手里有两口钟,各管各的,不许互相顶替。**

        * 判据那一口是 ``engine.now_ms()``:它跟 ``点.at_ms`` /
          ``点.prior_suspend_ms`` 出自同一个锚点、同一口单调钟,墙钟怎么跳都
          不影响这个差(为什么必须这样,见 :func:`_挂起超时了`)。
        * 形参 ``now_ms``(调用方给的 ``ctx.clock()``,墙钟)只用来给告警**盖
          时刻**:``AlertBook`` 的聚合窗口和 ``due_escalations`` 的升档(2 分钟
          没人确认就 push、5 分钟出声)全按这口钟算,而"什么时候该出声"是要
          跟现场的人对表的 —— 那一头必须留在墙钟上。拿引擎那口钟去盖告警,
          一趟没在跑的时候引擎回的是墙钟、在跑的时候回的是锚点推出来的值,
          升档的节奏会跟着开跑那一刻的钟偏走。

        **``engine.now_ms()`` 在这儿一定拿得到锚点,所以不加防御。** 走到这
        一句时 ``engine.yielding`` 已经为真,而 ``yielding`` 就是
        ``state is RunState.SUSPENDED``(``engine/machine.py``);那个状态只有
        ``_suspend`` 翻得进去,而 ``_suspend`` 跑在 ``start()`` 起的那条任务
        协程里 —— ``start()`` 里 ``_epoch_ms`` / ``_epoch_at`` / ``_live`` 是
        挨着的三行,而 ``_live`` 全文件再没有第二处被写回 ``None``(只有
        ``__init__`` 里那句带类型标注的初值)。补一句 ``if _live is None`` 的
        兜底,只会在这儿造一条永远走不到的分支,而永远走不到的分支没有任何
        测试守得住。
        """
        engine = self._ctx.engine
        if not engine.yielding:
            # 接管结束(或者压根没让开腿)。**记账要清掉** —— 不清的话,下一
            # 次让开腿如果凑巧撞上同一个 ``at_ms``,那一次就不会报了。
            self._挂起报过 = None
            # 底下那句 ERROR 的账也在这儿清:回到正常状态之后再出一次问题,
            # 值得再喊一声。见 :attr:`_没盖点喊过`。
            self._没盖点喊过 = False
            return
        点 = engine.snapshot.suspended_at
        if 点 is None:
            # **这是不该发生的状态,不许跟上面那条走同一个静默出口。**
            # "``yielding`` 的每一种成因都会往快照上盖一个 ``suspended_at``"
            # 是这条 P1 的隐含前提,而它没有编译器守着:任务 13 刚加了一种新
            # 的让位(``RETURNING`` 途中挂起),下一种还会有。哪一种忘了盖点,
            # 这条告警就整个哑掉 —— 狗在返航路上被拉到一边、然后没人管一整
            # 夜,``suspend_stale`` 一条不报、屏上一切如常(挂账 67a 原样复发,
            # 换了个入口)。合并进上面那个 ``return`` 的话,这件事连一行日志
            # 都不会留下。
            #
            # **只喊一次。** 这是一个会一直存在的状态,而闸门半秒醒一拍 ——
            # 不节流就是 2 条 ERROR/秒、一夜二十万条,而且刷的正是同一套值守
            # 在量 ``disk_used_ratio`` 的那块盘(见 :attr:`_没盖点喊过`)。
            if not self._没盖点喊过:
                log.error("引擎说自己在让位(yielding),快照上却没有 suspended_at ——"
                          "挂起超时这条 P1 在这种状态下是哑的。state=%s",
                          engine.snapshot.state)
                self._没盖点喊过 = True
            self._挂起报过 = None
            return
        # **判据那一口钟从引擎拿,不是形参那个 ``now_ms``。** 理由见上面的
        # docstring 和 :func:`_挂起超时了`:``点.at_ms`` 已经是"开跑对表 +
        # 单调钟"盖的,再拿活墙钟去减它,墙钟一跳判据就跟着跳。
        if (self._挂起报过 == 点.at_ms
                or not _挂起超时了(点, now_ms=engine.now_ms())):
            return
        self._挂起报过 = 点.at_ms
        self._ctx.alerts.raise_alert(
            kind="suspend_stale", robot=self._ctx.identity.sn,
            title="人接管着没还回来,这趟一直挂着",
            # **正文里那个数是真除不是整除。** ``//`` 的话
            # :data:`SUSPEND_STALE_MS` 一旦改成不整除 60 秒的数(它已经排进真
            # 机清单要量,改是排好期的),正文会说"1 分钟"而阈值其实是 1.5
            # 分钟 —— 人照着正文去复现("才过了 1 分 10 秒怎么没报"),得到
            # 一个查不出来的结论。``:g`` 让 10 还是"10"、10.5 就是"10.5"。
            # **说"累计",不说"这一次"。** 反复短接管那一幕里,每一次都只有
            # 四五分钟,而正文如果写"已经超过十分钟没人点继续",现场的人会
            # 照着去对表,发现对不上,然后把这条 P1 当误报。
            detail=f"让开腿的理由是「{点.reason}」,这一趟里累计已经超过 "
                   f"{SUSPEND_STALE_MS / 60_000:g} 分钟没在跑(可能是被反复"
                   "接管了好几次)。"
                   "**狗停在原地没有自己动**(§5.8)—— 回去点「继续」接着"
                   "跑,或者中止这一趟。",
            now_ms=now_ms)

    async def _lease_gone(self, now_ms: int) -> None:
        """人不在了。**停遥控,并且只停遥控**(§5.8)。

        这里没有、也不许有任何一句 ``resume``:一只狗在"最后已知状态是人正
        在接管"的情况下自己动起来,是这套系统里最不该发生的事(理由跟 §3.4
        那个不对称一样)。引擎停在哪一档就留在哪一档,等人回来自己决定。

        **停在前、报在后,但报不许被停的失败吃掉。** 停腿是要紧的那一半,
        所以先做;而它要是抛了(链路正好断了),更得有人知道 —— 那时候屏幕
        上一条告警都没有,才是真正的静默失败。

        **告警的文案分三套写,这不是措辞讲究。**
        上面那句 ``teleop.stop()`` 只停遥控那一档,**它不停引擎正在跑的那趟
        任务** —— 而"取控制权 → 起一趟巡检 → 人走开 → TTL 到期"是一串完全
        正常的动作,现场随时会发生。那种局面下 ``teleop.stop()`` 基本是空
        动作:引擎照走点位、照拍照,狗在动。原来那句"遥控租约到期,狗已停在
        原地"这时候就是一句假话,而现场的人会据此判断狗是静止的,然后走过去
        —— 这是会伤人的那种假话,不是文案问题。

        判"在不在跑"、判"是不是让开腿了"都用引擎自己的属性
        ``engine.running`` / ``engine.yielding``,**不在外壳里比一遍状态
        表**(理由跟 ``loc_lost`` 那条一样:哪些状态算"在跑"是引擎的词汇,
        外壳复刻一份迟早跟正主不一样)。``yielding`` 也正是
        :meth:`_判定挂起超时` 那道闸读的那个属性,不是这里另造的判据。

        **为什么 ``running`` 一位不够,非得再读一次 ``yielding``。**
        ``running`` 为真里头还套着 ``SUSPENDED`` 那一档,而那一档上"引擎会
        自己接着走"是**假的**:让开腿之后引擎就停在那儿等外面喂 resume,
        谁都不喂它就一直挂着(``tests/app/test_lease_expiry.py::
        test_人揣着手机走了_遥控停下而且任务不自己接着跑`` 钉的就是这件事)。
        在那一格上写"不管它的话它会自己把这趟跑完",人就会**什么都不做直接
        离开**,这一趟巡检从此挂死,屏上只剩 ``suspend_stale`` 隔一阵一条。
        反过来也不许把那一格整个说成"已经停下了" —— 停是停了,可这趟还没
        完,人同样会以为不用管。所以三格是:

        * **没在跑** —— 原来那句话是对的,留着。
        * **在跑而且让开了腿**(人按过暂停/接管,然后走了)—— **spec §5.8
          描述的正是这一格**("人接管完,手机往兜里一揣走了"),所以它那两句
          承诺"停在原地"、"绝不自动续跑"在这里是**真话,必须留着**;要补的
          是 §5.8 没写的那件事:这趟是"挂着"不是"结束了",不回来它就一直
          挂着,收尾得回来取控制权再在"继续 / 中止"里选一个。
        * **在跑而且没让开腿**(人起了一趟就走了)—— 引擎照走点位、照拍照,
          **不许默认它是静止的**。

        三格都把 ``snapshot.state`` 摆进正文让人自己看。宁可让人多警惕一次,
        也不能让人照着一句"已停在原地"走过去、或者照着一句"它会自己跑完"
        转身就走。
        """
        try:
            await self._ctx.teleop.stop()
        except (OSError, ValueError) as exc:
            log.warning("租约到期后停遥控失败:%s", exc, exc_info=True)
        engine = self._ctx.engine
        档位 = engine.snapshot.state.value
        断线 = "TTL 到了没人续租,遥控那一档已经断开(发过零速、手松了)。"
        if not engine.running:
            标题 = "遥控租约到期,狗已停在原地"
            正文 = ("TTL 到了没人续租。已经停下,**没有自动续跑** —— "
                    "要接着干,回来重新取一次控制权(§5.8)。")
        elif engine.yielding:
            标题 = "遥控租约到期,狗已停在原地,但这趟任务还挂着"
            正文 = (f"{断线}**狗已停在原地,没有自动续跑**(§5.8)—— 引擎挂"
                    f"在 {档位} 上,不会自己发任何一条运动指令。**但这趟巡检"
                    f"是「挂着」不是「结束了」**:既没跑完、也没被中止。不"
                    f"回来处理,它就一直这么挂着,再过 "
                    f"{SUSPEND_STALE_MS / 60_000:g} 分钟屏上会开始刷「接管着"
                    f"没还回来」那条 P1。要收尾,回来重新取一次控制权,然后"
                    f"「继续」或者「中止」,二选一。")
        else:
            # **这一格故意不挂 §5.8。** §5.8 的结论是"绝不自动续跑",管的是
            # 上面那一格(被接管挂起的那一趟);这一格根本没挂起过,"它会自己
            # 把这趟跑完"是本实现的事实,不是 §5.8 的承诺 —— 把 §5.8 摆在
            # 这句话后面,读者照着去查会看到一句意思正好相反的规格原文。
            标题 = "遥控租约到期,但这趟任务没有跟着停"
            正文 = (f"{断线}**但这趟巡检任务还活着,引擎一个字都没停** —— "
                    f"它现在在 {档位} 上,该走点位的时候会自己接着走、自己"
                    f"拍照。**不许默认它是静止的,别就这么走过去。** 要让它"
                    f"真停下来,回来重新取一次控制权,再按暂停或者中止;不管"
                    f"它的话它会自己把这趟跑完。")
        self._ctx.alerts.raise_alert(
            kind="lease_expired", robot=self._ctx.identity.sn,
            title=标题, detail=正文, now_ms=now_ms)

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
        self._alerts.on_nav(event)

    def _on_device(self, event: Any) -> None:
        if isinstance(event, BatteryEvent):
            # 收到的时刻要一起记下来:链路断了这个字段不会自己变回 None,
            # 只会一直停在最后一个读数上,而一个不再更新的数比 None 更危险。
            self._battery_at = (event.percent, self._ctx.clock())
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
        self._alerts.on_device(event)

    def _on_run(self, snapshot: Any) -> None:
        """引擎快照。这儿**只**喂告警 —— 快照该长什么样由 ``_build`` 现问
        ``engine.snapshot``,不在这条回调里抄一份。"""
        self._alerts.on_run(snapshot)

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
                # 快照里**只放数值,不放接收时刻**:``_rebuild`` 靠
                # ``snap == self._snapshot`` 去重,时刻一进来每一拍电量事件都
                # 会重建快照、再往 SSE 上推一帧,哪怕百分比一个数没变。要时刻
                # 的那一处(值守屏)直接读 :attr:`battery_at`。
                "battery": self.battery_at[0] if self.battery_at else None,
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
            "control": self._control.snapshot(now_ms=ctx.clock()),
            "video": _video_wire(ctx),
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
                 port: int = DEFAULT_PORT, pin: str | None = None,
                 postcheck_sleep: Callable[[float], Awaitable[None]] | None = None,
                 auth_clock: Callable[[], float] | None = None,
                 ) -> None:
        check_exposure(host, pin)
        self._ctx = ctx
        #: 重启后自检里那几处等待走谁。**默认真 sleep,测试注入假的**(§8.5)。
        #:
        #: ``run_postcheck`` 有两处有界重试(``grab_control`` 抢会话、
        #: ``_check_bridges`` 探三个桥),两处的间隔都是秒级。原来这儿不传
        #: ``sleep=``,走的是真 ``asyncio.sleep`` —— 套件之所以不慢,只因为
        #: ``tests/app/conftest.py`` 里 ``FakeDevice.control = True`` 让抢会话
        #: 第一次就成、``FakeNav`` 三个桥也第一次就通。那是个会突然咬人的隐式
        #: 依赖:哪天有人把假件改成"第二次才通",套件立刻慢下来,而且慢得
        #: 没有出处。开一个口子把它变成显式的。
        self._postcheck_sleep = postcheck_sleep
        #: 鉴权那口钟(单调秒)。**默认真 monotonic, 测试注入假的。**
        #: token 的闲置期是 12 小时、热点上 30 分钟, 绝对期 24 小时 ——
        #: 这几条边界拿真钟一条都测不着, 而"过期了还按不按得动急停"正是
        #: 第 8 卷值守那一段的成败所在(见 ``auth.IDLE_EXEMPT_PATHS``)。
        #: 跟上面 ``postcheck_sleep`` 是同一个路数:把隐式依赖变成显式的口子。
        self._auth = Guard(pin) if auth_clock is None else Guard(
            pin, clock=auth_clock)
        #: 此刻记着的自报姓名(§6.3)。**这是署名,不是登录态。**
        #:
        #: 换的时候整个换掉这个引用(``Operator`` 是 frozen dataclass),换引用
        #: 本身是原子的 —— 服务是 ``ThreadingHTTPServer``,两条请求真的会同时
        #: 进来,读到的要么是旧的一整份要么是新的一整份,不会读到改了一半的。
        #: 写法照 ``_payload_put`` 换 ``ctx.identity`` 那一处。
        self._operator = Operator()
        #: L1 控制权。**时刻一律由调用方读一次传进去**(``now_ms=ctx.clock()``),
        #: 这个类自己不读钟 —— 理由见 ``app/control.py`` 的模块文档。
        self._control = ControlDesk(self._auth)
        self._host = host
        self._want_port = port
        self._httpd: _HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._hub = _StateHub(ctx, self._control)
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
        #: 正在判读的那几趟(W03)。只在 ``_coord()`` 锁里增删;清盘的判据读它。
        self._busy_runs: set[Path] = set()
        #: 自动同步那条后台协程。**活在循环线程上**,由 :meth:`start` 经桥
        #: 建起来、:meth:`stop` 经桥收掉,写法照 ``_StateHub``。
        self._autosync: asyncio.Task[None] | None = None
        self._register_routes()

    # ------------------------------------------------------------ 生命周期

    def start(self, *, postcheck: bool = True) -> None:
        """起服务。``postcheck=False`` 时不在这儿跑重启后自检。

        默认 ``True``:绝大多数调用方(全仓每一条 HTTP 测试、绝大多数用法)
        起完服务就该把自检带上,没有理由让调用方每次都记着补一句。

        ``main()`` 是唯一的例外,而且是有意的:见下面 ``self._boot_postcheck()``
        那一句原来的位置(现在挪到了 ``main()`` 里连上三个后端**之后**)——
        理由写在那边,别在这儿重复。
        """
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
        self._ctx.bridge.call(self._autosync_start)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="d1max-http", daemon=True)
        self._thread.start()
        # **先 HTTP 后 pump。** 反过来的话,起 HTTP 这一步要是炸了(端口被占),
        # 已经起来的上传线程没人收 —— 它会一直往 queue.jsonl 里写到进程结束。
        if self._ctx.upload is not None:
            self._ctx.upload.start()
        # **不能早于这儿**:自检第一项"进程起来了"的可观测定义就是端口在听。
        # 放到 serve_forever 那个线程起来之前,第一项就永远是假失败,于是
        # 每一次升级都会回滚。
        #
        # **但 ``main()`` 还要把它推得更晚。** 这儿说的只是"HTTP 起没起来",
        # 不代表 nav/device/maps 三个后端已经连上了——``main()`` 里
        # ``server.start()`` 排在那三个 ``connect()`` 之前(先把端口起来,
        # 免得连接慢的时候整个进程看着像卡死)。真机上一旦有在途升级,这四
        # 项里的 ``control``/``bridges`` 两项问的正是这三个后端;后端没连
        # 上时问它们几乎必然假失败,一版好版本就这么被误判回滚——而 §7.3
        # 说自动回滚只有"重启后自检没过"这一个触发条件,不该被"我们自己
        # 后端还没连上"这种开机时序意外触发。所以 ``main()`` 传
        # ``postcheck=False``,等三个 ``connect()`` 都跑完了才补一次
        # ``self._boot_postcheck()``。测试起服务时后端(假件)天生就是连着
        # 的,不受这条时序影响,默认值留 ``True`` 就够了。
        if postcheck:
            self._boot_postcheck()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        if self._ctx.upload is not None:
            # **先停它,再停 HTTP。** 它在往 queue.jsonl 里写,而那份文件下次
            # 开机要重放。反过来的话,HTTP 已经不收请求了而 pump 还在写队列,
            # 关服务的最后一秒钟就是一段没人看得见的时间;那会儿被掐断写出来
            # 的半行,重放那一头兜得住,但兜得住不等于该发生。
            self._ctx.upload.stop(timeout_s=5.0)
        if self._ctx.bridge.running:
            with contextlib.suppress(Exception):
                self._ctx.bridge.call(self._autosync_stop)
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

    @property
    def auth(self) -> Guard:
        """鉴权闸门。测试和控制权那几条路由要它。"""
        return self._auth

    @property
    def control(self) -> ControlDesk:
        """L1 控制权台。"""
        return self._control

    @property
    def alerts(self) -> AlertBook:
        """告警簿(§5.2)。**转手 ``ctx`` 那一份,不另存** —— 存了就有两个
        出处,而对不上的那天没有任何测试会红。"""
        return self._ctx.alerts

    # ------------------------------------------------------------ 路由

    def route(self, method: str, pattern: str, handler: Handler) -> None:
        """挂一条路由。后面几个模块就是靠它把业务接口接进来的。"""
        self._routes.append(
            _Route(pattern, method.upper(), _compile(pattern), handler))

    def _register_routes(self) -> None:
        self.route("GET", "/", self._index)
        self.route("POST", AUTH_PATH, self._auth_unlock)
        self.route("GET", CHALLENGE_PATH, self._auth_challenge)
        self.route("POST", "/api/auth/logout", self._auth_logout)
        self.route("GET", "/api/sessions", self._sessions)
        self.route("GET", "/api/control", self._control_get)
        self.route("POST", "/api/control/acquire", self._control_acquire)
        self.route("POST", "/api/control/heartbeat", self._control_beat)
        self.route("POST", "/api/control/release", self._control_release)
        self.route("POST", "/api/control/takeover", self._control_takeover)
        self.route("POST", "/api/control/takeover/approve",
                   self._control_approve)
        self.route("GET", "/api/control/audit", self._control_audit)
        self.route("GET", OPERATOR_PATH, self._operator_get)
        self.route("PUT", OPERATOR_PATH, self._operator_put)
        self.route("GET", "/api/identity", self._identity)
        self.route("GET", "/api/state", self._state)
        self.route("GET", "/api/events", self._events)
        self.route("POST", "/api/teleop", self._teleop)
        self.route("POST", "/api/teleop/heartbeat", self._teleop_beat)
        self.route("POST", "/api/teleop/mode", self._teleop_mode)
        self.route("POST", "/api/estop", self._estop)
        self.route("POST", "/api/estop/release", self._estop_release)
        self.route("GET", "/api/mapping", self._mapping_state)
        self.route("POST", "/api/mapping/record/start", self._record_start)
        self.route("POST", "/api/mapping/record/stop", self._record_stop)
        self.route("POST", "/api/mapping/rebuild", self._rebuild)
        self.route("GET", "/api/procs/<name>/log", self._proc_log)
        # **顺序有讲究。** ``_dispatch()`` 按登记顺序找第一条匹配的路由,
        # ``/api/video/<name>`` 的 ``<name>`` 是个万能段,会先吃掉
        # ``/api/video/health`` —— 必须登记在它前面。
        self.route("GET", "/api/video/health", self._video_health)
        self.route("GET", "/api/video/<name>", self._video)
        self.route("GET", "/api/missions", self._missions)
        self.route("GET", "/api/missions/<mid>", self._mission_get)
        self.route("PUT", "/api/missions/<mid>", self._mission_put)
        self.route("POST", "/api/missions/<mid>/run", self._mission_run)
        self.route("POST", "/api/run/pause", self._run_pause)
        self.route("POST", "/api/run/resume", self._run_resume)
        self.route("POST", "/api/run/abort", self._run_abort)
        self.route("POST", "/api/run/suspend", self._run_suspend)
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
        self.route("GET", "/api/release", self._release)
        self.route("POST", "/api/release/install", self._release_install)
        self.route("POST", "/api/release/activate", self._release_activate)
        self.route("POST", "/api/release/rollback", self._release_rollback)
        self.route("GET", "/api/selfcheck", self._selfcheck)
        self.route("PUT", "/api/identity/payload", self._payload_put)
        self.route("GET", "/api/bundle", self._bundle)
        self.route("POST", "/api/bundle/apply", self._bundle_apply)
        self.route("POST", "/api/bundle/rollback", self._bundle_rollback)
        self.route("GET", "/api/schedule", self._schedule)
        self.route("GET", "/api/watch/summary", self._watch_summary)
        self.route("GET", "/api/upload", self._upload_get)
        self.route("GET", "/api/alerts", self._alerts_open)
        self.route("GET", "/api/alerts/all", self._alerts_all)
        # ``<key*>`` 跨斜杠(见 ``_compile``)。两条都以 ``/ack``、
        # ``/resolve`` 收尾,吃不到上面那两条 GET。
        self.route("POST", "/api/alerts/<key*>/ack", self._alert_ack)
        self.route("POST", "/api/alerts/<key*>/resolve", self._alert_resolve)

    def handle(self, method: str, path: str, query: Mapping[str, str],
               body: bytes, headers: Mapping[str, str] | None = None,
               client: str = "") -> Response | Stream:
        """路由一个已经解码好的请求。找不到就抛 ``HttpError``。

        **鉴权就在这一处。** 所有请求都从这儿过,放在这里不会漏 —— 而白名单
        式的"给某几个接口加检查"迟早会漏掉新加的那个,偏偏新加的往往最危险。

        **L1 控制权的闸也在这儿,而且在路由匹配之前。** 它是**按路径**判的,
        不是按处理函数判的: 放到匹配之后就得在十一个处理函数里各写一遍,而
        漏写一个不会有任何报错 —— 只会安静地少一道闸。放在这里,
        ``control.CONTROLLED`` 那张表就是唯一的真相, 读一处就知道哪些接口要
        控制权。代价是遥控每一拍(5 Hz)多走一次 ``sweep``: 一把锁、一次
        ``frozenset`` 比较、几个整数比较, 跟同一条请求里已经有的
        ``TokenStore.info``(也上一把锁)是同一个量级。
        """
        headers = headers or {}
        try:
            session = self._auth.gate(method, path, headers, query)
            self._control.require(session, method, path,
                                  now_ms=self._ctx.clock())
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
                headers=headers, client=client, session=session))
        if others:
            raise HttpError(405, f"{path} 不支持 {method}",
                            "支持的方法:" + "、".join(sorted(others)))
        raise HttpError(404, f"没有这个接口:{path}",
                        "页面在 /,状态在 /api/state,事件在 /api/events")

    # ------------------------------------------------------------ 处理函数

    def _index(self, _req: Request) -> Response:
        return self._static("index.html")

    def _auth_unlock(self, req: Request) -> Response:
        """PIN 换 token,或者质询-应答换 token。

        **这是唯一一个不要 token 的写接口。** 两条路:带 ``nonce``/``proof``
        的走质询-应答(§6.5 措施 2,PIN 从不上网);带 ``pin`` 的是老路,在狗
        的热点上只换得到只读凭证 —— 那条通道上报文人人可解。

        ``operator`` 是操作人自己报的名字。**狗记下来,但不核实**(§6.3),所
        以每一次响应都把 ``operator_verified: false`` 和 ``notice`` 一起发回
        去 —— 界面上必须照着说,不许含糊成"已登录:张三"。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象",
                            '形如 {"pin": "..."},或者 '
                            '{"nonce": "...", "proof": "..."}')
        client = req.client or "?"
        operator = body.get("operator", "")
        try:
            if "nonce" in body or "proof" in body:
                token = self._auth.unlock_proof(body.get("nonce"),
                                                body.get("proof"), client,
                                                operator=operator)
            else:
                token = self._auth.unlock(body.get("pin"), client,
                                          operator=operator)
        except Denied as exc:
            raise HttpError(exc.status, exc.error, exc.detail) from None
        sess = self._auth.session_of(token)
        return json_response({
            "token": token,
            "operator": sess.operator if sess else "",
            "operator_verified": False,
            "channel": sess.channel if sess else CHANNEL_LAN,
            "readonly": bool(sess and sess.readonly),
            "idle_s": sess.idle_limit_s if sess else TOKEN_IDLE_S,
            "notice": OPERATOR_NOTICE,
        })

    def _auth_challenge(self, req: Request) -> Response:
        """取一个一次性质询。**这条不要 token** —— 要了就没人换得到 token。

        配额按 ``req.client``(TCP 对端地址)分桶:这条路不要 token,不分桶的
        话一个未鉴权的人连发几十次就能把别人还没用掉的质询挤光,把人降级回
        明文 PIN(见 ``auth.NonceStore``)。桶满了回 429。
        """
        try:
            nonce = self._auth.challenge(req.client)
        except Denied as exc:
            raise HttpError(exc.status, exc.error, exc.detail) from None
        return json_response({"nonce": nonce, "ttl_s": NONCE_TTL_S,
                              "alg": PROOF_ALG})

    def _auth_logout(self, req: Request) -> Response:
        """交回这个会话的名额。**顺带把租约还了**(§6.4)。

        没有这条,一个装完就卸载的 app 会占着 1/3 的名额到闲置期走完 —— 在局
        域网上那是 12 小时。
        """
        token = bearer(req.headers)
        if not token:
            raise HttpError(401, "要先解锁", "退出也得说清是谁在退。")
        was_live = self._auth.logout(token)
        self._control.sweep(now_ms=self._ctx.clock())
        return json_response({"ok": True, "was_live": was_live})

    def _sessions(self, req: Request) -> Response:
        """3 个名额现在谁占着(§3.6)。**只给指纹,不给 token 本身。**

        **``max_sessions`` 只约束 ``remote_sessions``。** 回环(``local``)走的
        是自己那个池 —— 它不占那条无线上行,所以不占那 3 个名额(见
        ``app/auth.py`` 的 ``Guard._issue``)。行是全给的,每行都带 ``channel``;
        但界面上**不许**拿 ``len(sessions)`` 去跟 ``max_sessions`` 比,那是两个
        分母,会渲染出"6 条会话,上限 3"这种话。要比就比
        ``remote_sessions`` / ``max_sessions``。
        """
        mine = req.session.ref if req.session is not None else ""
        rows = [{**s.to_wire(), "mine": s.ref == mine}
                for s in self._auth.sessions()]
        本机 = sum(1 for r in rows if r["channel"] == CHANNEL_LOCAL)
        return json_response({"sessions": rows,
                              "remote_sessions": len(rows) - 本机,
                              "local_sessions": 本机,
                              "max_sessions": self._auth.max_sessions,
                              "max_local_sessions": self._auth.max_local_sessions,
                              "notice": OPERATOR_NOTICE})

    # ---------------------------------------------------------- L1 控制权

    def _need_session(self, req: Request) -> Session:
        """要一个会话,没有就说清为什么没有。"""
        if req.session is None:
            raise HttpError(
                400, "这台没设 PIN",
                "没有会话就没有「谁在控制」这回事:启动时不给 --pin 的部署按"
                "定义只听本机(见 check_exposure),L1 仲裁没有对象。")
        return req.session

    def _control_wire(self, state: LeaseState, req: Request) -> Response:
        """控制权报文。

        ``mine`` 和 ``challenging`` 只在这儿有,不在 ``/api/state`` 的
        ``control`` 段里 —— 那一段是所有人共用的一份快照,塞一个"是不是我"
        进去就得按人分份,SSE 那条广播路子立刻塌掉。

        **``challenging`` 是 ``mine`` 的对称写法,同一个理由。** 它俩都不是
        租约本身的状态,而是"这一份报文发给谁"的属性,所以两个都算在这儿、
        两个都不进 ``LeaseState.to_wire()``:进去了的话,广播快照里会出现一个
        对所有人都一样的 ``challenging``,那是错的。

        **``challenging`` 不叫 ``challenger_mine``。** ``mine`` 说的是"东西
        在我手上",``challenging`` 说的是"我正在要" —— 两个不同的动作,用两个
        不同的词,比前缀套娃在界面代码里读起来清楚。

        它解的是挂账 61:``challenger`` 只带 ``{ref, operator}``,现场两台手机
        的操作人同名(都报"张三")时,屏上一模一样,看的人分不出"在要接管的
        那个人就是我"。名字分不开,``ref`` 分得开 —— 而 ``ref`` 只有狗这头有。

        ``remote_sessions``/``max_sessions`` 从 ``ControlDesk.seats()`` 取,
        不在这里自己数 —— 那两个数只许有一处定义(见 ``app/control.py`` 的
        ``seats()`` docstring),不然这条接口和 ``/api/state`` 会在同一时刻
        对同一件事报出两个不同的答案。

        **这个整数叫 ``remote_sessions``,不叫 ``sessions``。** 手机端两条接口
        都要读,而 ``GET /api/sessions`` 里的 ``sessions`` 是一个**数组**;同一
        个名字两种类型是最容易写出偶发崩溃的客户端的那种设计。那条接口早就把
        这个整数叫 ``remote_sessions`` 了,这里对齐它 —— 一个概念一个名字。
        """
        mine = req.session.ref if req.session is not None else ""
        sessions, max_sessions = self._control.seats()
        return json_response({
            **state.to_wire(),
            "mine": state.holder is not None and state.holder.ref == mine,
            "challenging": (state.challenger is not None
                            and state.challenger.ref == mine),
            "remote_sessions": sessions,
            "max_sessions": max_sessions,
            "notice": OPERATOR_NOTICE,
        })

    def _control_get(self, req: Request) -> Response:
        """控制权此刻在谁手上。**只读,永远不要控制权**(§3.5 规则 1)。"""
        return self._control_wire(
            self._control.sweep(now_ms=self._ctx.clock()), req)

    def _control_acquire(self, req: Request) -> Response:
        """取一份租约。自己已经拿着时是续,不是错。"""
        sess = self._need_session(req)
        now = self._ctx.clock()
        self._control.sweep(now_ms=now)
        try:
            state = self._control.book.acquire(sess.ref, sess.operator,
                                               now_ms=now)
        except LeaseError as exc:
            raise _lease_error(exc) from None
        return self._control_wire(state, req)

    def _control_beat(self, req: Request) -> Response:
        """续租。**每 10 秒一次,30 秒不续自动到期**(§6.4)。"""
        sess = self._need_session(req)
        now = self._ctx.clock()
        self._control.sweep(now_ms=now)
        try:
            state = self._control.book.renew(sess.ref, now_ms=now)
        except LeaseError as exc:
            raise _lease_error(exc) from None
        return self._control_wire(state, req)

    def _control_release(self, req: Request) -> Response:
        """交回。**幂等** —— app 退出时无脑发一次就行。"""
        sess = self._need_session(req)
        state = self._control.book.release(sess.ref,
                                           now_ms=self._ctx.clock())
        return self._control_wire(state, req)

    def _control_takeover(self, req: Request) -> Response:
        """接管。默认是礼貌的;``force`` 是硬夺,**必须写理由**(§3.5 规则 3)。

        为什么强制这条必须存在:持有者可能已经不在了 —— 手机没电、人走了、
        网断了。没有它,一只狗会被一个不存在的会话占到 TTL 走完为止,而现场
        正等着有人把它从带电设备旁边挪开。
        """
        sess = self._need_session(req)
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象",
                            '形如 {"force": true, "reason": "..."}')
        now = self._ctx.clock()
        self._control.sweep(now_ms=now)
        reason = body.get("reason", "")
        try:
            if body.get("force"):
                state = self._control.book.force(
                    sess.ref, sess.operator, now_ms=now,
                    reason=reason if isinstance(reason, str) else "")
            else:
                state = self._control.book.ask_takeover(
                    sess.ref, sess.operator, now_ms=now)
        except LeaseError as exc:
            raise _lease_error(exc) from None
        return self._control_wire(state, req)

    def _control_approve(self, req: Request) -> Response:
        """当前持有者同意移交。立刻交,不等宽限期。"""
        sess = self._need_session(req)
        now = self._ctx.clock()
        self._control.sweep(now_ms=now)
        try:
            state = self._control.book.approve(sess.ref, now_ms=now)
        except LeaseError as exc:
            raise _lease_error(exc) from None
        return self._control_wire(state, req)

    def _control_audit(self, _req: Request) -> Response:
        """控制权换过几次手(§3.5 规则 3)。**只有指纹,没有 token。**

        这是个环,只留最近 200 条 —— 够值守屏翻一页,不够当档案。要长期存档
        是服务器的活(§5.9)。
        """
        self._control.sweep(now_ms=self._ctx.clock())
        return json_response({
            "audit": [r.to_wire() for r in self._control.book.audit],
            "max": AUDIT_MAX,
        })

    # ------------------------------------------------------------ 现在是谁

    def _operator_wire(self) -> Response:
        """自报姓名的报文。**两条路由同一个出口**,免得一边改了另一边没跟。

        ``notice`` 跟解锁、会话表、控制权那几条一样,发的是狗自己那句原话
        (``auth.OPERATOR_NOTICE``):``operator_verified`` 只说得出"核没核",
        说不出"为什么不核";手机上硬编一句的话,狗改了措辞手机不跟。
        """
        return json_response({**self._operator.to_wire(),
                              "notice": OPERATOR_NOTICE})

    def _operator_get(self, _req: Request) -> Response:
        """**这台狗上一次被谁报过名字**。这不是登录状态(§6.3)。

        **它不是「现在是谁在开这只狗」的真相来源**(挂账 93)。这个
        ``self._operator`` 是服务器级的一份,而每个会话自己还有一份
        ``Session.operator``:三台手机连着的时候,这条接口回的是**最后一个报过
        名字的人**,不是此刻拿着控制权的那个人,两者长期是两份。要「现在谁在
        开」,读 ``GET /api/control`` 的 ``holder.operator``;要事后对账,翻
        ``operator_changed`` 那个审计环 —— 那是唯一带时刻、删不掉的一份。

        **不要 token 之外的任何东西,也不要控制权**(§3.5 规则 1:看永远不要)。
        """
        return self._operator_wire()

    def _operator_put(self, req: Request) -> Response:
        """换人。狗**收下并留痕,但从不核实**(§6.3)。

        **不在 ``control.CONTROLLED`` 里,这是想清楚了的。** 换署名不改变这只
        狗正在做什么(§3.5 规则 4);而且下一个要接手的人正是在拿到控制权
        **之前**报名字的 —— 要控制权才准改名,等于让人只能顶着前一个人的名字
        去接管,那条审计链上就再也分不出人。

        **产出是审计环里那一条,不是返回值。** 挂账 65 接受的代价是"甲忘了
        切、乙的操作签在甲名下";正因为接受了它,这条带时刻、带新名字、删不掉
        的记录才是事后唯一能翻的东西。

        **换的不只是这台狗记着的那个名字,还有当前这个会话的署名**(见下面
        ``rename_ref`` 那一段)—— 不然这次会话后面每一条留痕都还签在上一个
        人名下。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象", '形如 {"name": "老王"}')
        name = clean_operator(body.get("name"))
        if not name:
            # **拒得干净:一个字节都不写。** 拒了却把已经记着的名字冲成空的,
            # 比收下更坏 —— 屏上那个署名变成空白,而请求回的是 400,人以为
            # 什么都没发生。
            raise HttpError(400, "名字不能是空的",
                            "空名字在账上是「未具名」,事后谁也说不清那一趟"
                            "是谁开的。狗不核实这个名字,但要求你报一个。")
        now = self._ctx.clock()
        self._operator = Operator(name=name, at_ms=now)
        # 留痕里 ``ref`` 是 token 指纹;没设 PIN 的部署上没有会话,那就是空串
        # —— **空串不许拿去顶替成别的什么**,"没有指纹"和"某个指纹"是两件事。
        sess = req.session
        if sess is not None:
            # **这一次会话后面每一条留痕也得跟着换名字。**
            #
            # 别的留痕(``acquired``/``taken_over``/``mode_switched``)署的是
            # ``req.session.operator`` —— 那个名字是解锁那一刻定下的。不在这里
            # 一起换的话,小李点了 chip、屏上写着小李、审计里也刚记下一条"换成
            # 小李",可他接着抢控制权、切远程档产生的每一条留痕**仍然签老王**:
            # 交接班查账时两份记录互相矛盾,而没有任何一处提示发生过什么。这比
            # 挂账 65 接受的代价重一档 —— 那笔账认的是"甲忘了切",这里是人记得
            # 切、系统也记下了,账还是错的。
            #
            # **换的是会话上那个名字,不是让留痕改从 ``self._operator`` 取。**
            # ``self._operator`` 是**服务器级**的一份,两台手机连一只狗时它只是
            # "最后报到的那个人";让留痕去读它,甲手机的抢控制权就会签上乙手机
            # 刚报的名字 —— 那是把一个人的错名扩散成所有人的错名,比现在更坏。
            # 会话是每台手机各一份,按 ``ref`` 换才只影响报名字的那一台。
            #
            # **也不是一次隐式 unlock。** 没有任何凭证被重新校验,token、
            # readonly、通道、闲置计时全不动(见 ``Guard.rename_ref``);
            # ``operator_verified`` 照旧恒为 ``False`` —— 狗只记,不核。
            self._auth.rename_ref(sess.ref, name)
        self._control.book.note(
            at_ms=now, kind="operator_changed",
            who=Holder(ref=sess.ref if sess is not None else "", operator=name),
            detail="从这一刻起账记在这个名字下(狗不核实)")
        return self._operator_wire()

    def _identity(self, _req: Request) -> Response:
        """这是哪只狗。手机拿它认机器、给拉回去的归档分组。

        **放在 PIN 后面。** 认狗这件事手机用热点的 BSSID 就够了 —— 连上之前
        就看得见,不用问服务端。既然如此,没必要让射程之内任何人白拿到机身
        序列号和一串网卡地址。
        """
        return json_response(self._ctx.identity.to_wire())

    def _state(self, _req: Request) -> Response:
        return json_response(self._hub.snapshot)

    def _events(self, req: Request) -> Stream:
        """SSE。**第一帧就是当前全量状态**。

        不这么做的话,页面打开之后要一直等到下一次状态变化才知道现在是什么
        情况 —— 机器停着不动的时候,那就是永远。

        **这条长连上每推一帧, 就把这个会话的闲置计时刷一次。** 闸
        (``Guard.gate``)只在建连那一刻过一次, 之后这条连接挂多久都不会再
        有第二次 ``tokens.info()`` —— 于是值守屏上"人一直盯着"会被判成
        "这个 token 一直没人用", 半小时后热点凭证就作废了。人在看着屏幕,
        把它算成闲置本身就是错的。

        刷的只有**闲置**计时。绝对期(``auth.TOKEN_ABS_S``)推不动 —— 一条
        挂着不放的长连不该换来一张永不过期的凭证。

        **注意这条救不了"狗停着不动"的场景**:快照一样就不推帧
        (见 ``_StateHub._rebuild``), 没帧就没有活动可算。急停在那种情形下
        按得动, 靠的是 ``auth.IDLE_EXEMPT_PATHS`` 那条豁免, 不是这里。
        """
        hub = self._hub
        bridge = self._ctx.bridge
        auth = self._auth
        #: 没设 PIN 的部署上没有会话, 那种部署也没有"谁是谁"这个问题。
        ref = req.session.ref if req.session is not None else ""

        def gen() -> Iterator[dict[str, Any]]:
            stream = bridge.subscribe(hub.events)
            try:
                # 第一帧不用刷:建连时 gate() 刚刚续过一次。
                yield {"kind": "state", **hub.snapshot}
                for event in stream:
                    if event.get("kind") == "bye":
                        return
                    if ref:
                        auth.touch_ref(ref)
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
        seconds = None if body.get("seconds") is None else _number(body, "seconds")
        name = body.get("profile", "roam")
        if not isinstance(name, str) or name not in PROFILES:
            raise HttpError(400, "不认识的节奏档",
                            f"只有 {'、'.join(PROFILES)},给的是 {name!r}")
        profile = PROFILES[name]
        teleop = self._ctx.teleop
        self._call(lambda: teleop.pulse(fwd, lat, yaw, seconds, profile=profile))
        return json_response({"ok": True})

    def _teleop_beat(self, _req: Request) -> Response:
        """续命。同步的 —— 它只是记一个时间戳,没必要过桥。"""
        self._ctx.teleop.heartbeat()
        return json_response({"ok": True})

    def _teleop_mode(self, req: Request) -> Response:
        """切现场档 / 远程档(§7.8)。**同步的** —— 它不碰腿。

        规格不硬禁远程遥控,要的是留痕:「每次都弹同一个框,第三次就被条件
        反射点掉了」。所以这里真正的产出是**审计环里那一条**,不是返回值。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体得是一个对象", type(body).__name__)
        name = body.get("mode")
        if name not in MODES:
            raise HttpError(400, "不认识的档",
                            f"只有 {'、'.join(MODES)},给的是 {name!r}")
        now = self._ctx.clock()
        state = self._control.book.state(now_ms=now)
        who = state.holder
        # **没设 PIN 的部署上,``require()`` 对 ``sess is None`` 直接放行**
        # (只听本机,没有"谁是谁"这个问题)——这条路由虽然在 ``CONTROLLED``
        # 里,但那只保证过了闸,不保证有租约持有者。切档这件事的唯一产出就
        # 是审计环里那条署名记录,没有身份就没法署名;正确做法是**拒绝**,
        # 不是放行后悄悄记一条匿名留痕(也不能用 ``assert``——``python -O``
        # 下会被整个删掉,那时候会在 ``who.ref`` 上炸成没人看得懂的
        # ``AttributeError``,连"切回现场"这个安全方向都会被一起炸掉)。
        if who is None:
            raise HttpError(
                409, "先取控制权",
                "这台狗没设 PIN,只听本机,没有'谁是谁'这回事;"
                "切远程档要留名,留不了名就不许切。")
        # 档挂在**这一次授予**上,不挂在 token 指纹上(§7.8 S-3):同一个
        # ``ref`` 放手之后被别人拿走又要回来,那已经是新的一段作业,该重新
        # 确认一次、重新留一条痕 —— 不能因为 ref 相等就以为还在接着上一段。
        # 组合出来的身份只喂给 ``Teleop``(它天生就是不透明字符串),留痕的
        # ``who=`` 仍然传干净的 ``Holder``,内部序号绝不混进审计记录。
        holder_grant = f"{who.ref}#{state.grant_seq}"
        if name == "remote" and self._ctx.teleop.mode(holder_grant) != "remote":
            # **没确认就切是 409 不是 400。** 发的东西完全正确,缺的是一次
            # 确认 —— 分错了,手机上只能显示一句「请求格式错误」,人不知道
            # 该点什么。
            if body.get("confirmed") is not True:
                raise HttpError(409, "切远程遥控要先确认一次", REMOTE_CONFIRM)
            self._control.book.note(
                at_ms=now, kind="mode_switched", who=who,
                detail="切到远程遥控")
        # 切回现场不用确认:往安全的方向走不该设卡,多设一道人就懒得切回来。
        self._ctx.teleop.set_mode(name, holder_grant)
        return json_response({"mode": name, "confirm": REMOTE_CONFIRM})

    #: 急停这一趟最多要等多久:软急停、停车各有 ``URGENT_ACK_TIMEOUT_S``(5s)
    #: 的回执上限,再加打断任务。给默认的 10s 会在两步都慢的时候先超时,
    #: 报成 504 却说不清是哪一步没成。
    ESTOP_TIMEOUT_S = 15.0

    def _estop(self, _req: Request) -> Response:
        """红按钮:软急停、停遥控,并打断正在跑的任务。

        **只有三步都做到了才回 200。** 以前停车的错误被吞、软急停从来没发,
        这里照样回 ``{"ok": true}``,页面就写「已急停」—— 人以为停了,狗没停。
        现在任何一步没成都回 502,把哪一步没成原样带给页面。
        """
        teleop = self._ctx.teleop
        try:
            self._call(lambda: teleop.emergency_stop("页面按了急停"),
                       timeout_s=self.ESTOP_TIMEOUT_S)
        except EmergencyStopFailed as exc:
            raise HttpError(502, "急停没有全部做到,狗可能还在动", str(exc)) from exc
        return json_response({"ok": True})

    def _estop_release(self, _req: Request) -> Response:
        """解除软急停。要控制权(见 ``control.CONTROLLED``)。

        解除之后遥控和任务都**不会**自己接着动,要人重新推杆或重新起任务。
        """
        teleop = self._ctx.teleop
        try:
            self._call(teleop.release_emergency_stop)
        except DeviceBackendError as exc:
            raise HttpError(502, "解除急停没成功", str(exc)) from exc
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

        **这条路由是有破坏性的,不是"离线活儿"。** ``mapping.rebuild()`` 干的
        第一件事是 ``forget_home(map_id)`` —— 把这张图的原点删掉。原点是起飞
        门槛的硬前置,删完这只狗用这张图就起不了飞了,而唯一的恢复办法是有人
        物理走到原点上用手机重标一次。所以这里有两道闸,拦的是两拨人:

        * ``control.CONTROLLED`` 里那一条拦的是**没有控制权的人**;
        * 下面这道 ``engine.running`` 拦的是**握着控制权的人自己手滑** ——
          正巡检着把脚下这张图的原点删了。

        另外补一条**留痕**:谁、什么时候、删的哪张图,走 ``self._hub.events``
        (跟 ``release.*`` 那几条同一条流),值守屏和第 8 卷的告警都读得到。
        """
        body = req.json()
        bag_name = _text(body, "bag")
        map_id = _text(body, "map_id")
        if self._ctx.engine.running:
            raise HttpError(
                409, "这一趟还在跑,不能重建地图",
                "重建会把这张图的原点一起删掉,删完这只狗下一趟起不了飞。"
                "先 POST /api/run/abort 把这一趟收掉,或者等它自己跑完。")
        mapping = self._ctx.mapping
        bag = self._call(lambda: mapping.plan_rebuild(bag_name, map_id))
        # 留痕在 spawn **之前**:重建本身是异步的、可能失败,但"这张图的原点
        # 要没了"在参数验过之后就是既成事实,值守屏得立刻看得见。事后再补
        # 一条的话,中间那几分钟正是最需要知道"是谁动的"的那几分钟。
        sess = req.session
        who = sess.operator if sess else ""
        self._hub.events.emit({"kind": "mapping.rebuild_started",
                               "map_id": map_id, "bag": bag.name,
                               "operator": who,
                               "session": sess.ref if sess else "",
                               "at_ms": self._ctx.clock(),
                               "home_forgotten": True})
        log.warning("重建地图 %s(用包 %s,发起人 %s):这张图的原点已作废,"
                    "重建完要有人重新标一次原点才能起飞",
                    map_id, bag.name, who or "未署名")
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

    def _video_health(self, _req: Request) -> Response:
        """每一路相机现在在不在线。**这一屏是轮询的,数字放这儿。**

        每拍都变的量(``since_frame_s``)只出现在这条轮询接口上,不进
        ``/api/state`` 那条常连的 SSE —— 第 6 卷在这上面栽过一次,理由见
        ``engine/lease.py`` 的 ``每拍都变的键``。
        """
        cams: dict[str, Any] = {}
        for name in CAMERAS:
            feed = self._ctx.video.get(name)
            if feed is None:
                cams[name] = {"online": False, "viewers": 0,
                              "since_frame_s": None,
                              "detail": f"{name} 相机没配地址 —— "
                                        f"起 app 时用 --camera-host 指到推流的那台机器"}
            else:
                cams[name] = feed.health()
        return json_response({"cameras": cams})

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

    def _run_suspend(self, req: Request) -> Response:
        """让开腿:接下来这段路人拿手机亲自开(§5.10)。

        **不是暂停。** 暂停是狗停着没人碰;挂起期间人正在用遥控开它,
        所以 ``TeleopBusy`` 那道闸看的是 ``yielding``,不是 ``paused``。
        鉴权、控制权、错误码、返回体形状一律跟 ``/api/run/pause`` 一族一致。
        """
        body = req.json()
        raw = body.get("reason", "") if isinstance(body, dict) else ""
        reason = raw.strip() if isinstance(raw, str) and raw.strip() else "页面上点了让开腿"
        return self._run_cmd(lambda: self._ctx.engine.suspend(reason))

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
                         marked_at_ms=self._ctx.clock(), note=note)
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
                with self._coord():
                    write_reports(run)
                    # 新写出来的报告要传;这一趟若已标「可删」,标记此刻就得撤,
                    # 不能等 pump 下一拍才发现(W03)。
                    unmark_uploaded(run)
            except (OSError, ValueError) as exc:
                raise HttpError(500, "生成报告失败", str(exc)) from exc
        ctype = ("text/markdown" if fmt == "md" else "text/html")
        return Response(200, path.read_bytes(), f"{ctype}; charset=utf-8")

    def _coord(self):
        """上传协调锁(见 ``UploadPump.coord_lock``);单机档没有 pump 就是空的。"""
        up = self._ctx.upload
        return up.coord_lock if up is not None else contextlib.nullcontext()

    def _stale_reports(self, run: Path) -> None:
        """把已经生成的报告删掉,让下次取报告时重新出一份;**并同步撤掉「可删」**。

        报告是**结论的快照**:判读或复核改了之后,躺在目录里的那份就过时了,
        而 :meth:`_run_report` 只在文件不存在时才生成。删掉是最省事的失效
        方式 —— 报告本来就随时能从归档重建。

        撤标为什么在这儿而不是等 pump:判读刚写了 ``findings.json``、复核刚写了
        ``review.json``,到 pump 下一拍 scan 之间最长 15 秒,这段里 ``.uploaded``
        还挂着、队列里还全是 done —— 清盘看这两样会把整趟删掉,而新结论一个
        字节都还没传(W03,外部审核的阻断项)。调用方在 :meth:`_coord` 锁里调。
        """
        for name in ("report.md", "report.html"):
            with contextlib.suppress(OSError):
                (run / name).unlink(missing_ok=True)
        unmark_uploaded(run)

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
        # **判读期间这一趟不许被清盘删。** 模型调用要几分钟,不能握着协调锁;
        # 所以先登记"正在判读",清盘的判据见到它就不算已传;提交或失败后在锁里
        # 撤登记、撤「可删」、删旧报告 —— 这三件事跟写 findings 之间没有窗。
        #
        # **同一趟同时只许一个判读。** 登记是个集合,数不了引用:两个并发请求
        # 一个先结束就把另一个的保护撤了,清盘随即可能把它正在读的照片删掉。
        # 双击、客户端超时重试、代理重放都能造出两个并发调用 —— 拒绝第二个。
        # **登记是资源,必须 finally 释放**:judge_run 抛任何异常都不能把这一趟
        # 永久锁成"正在判读"(那样它以后永远清不了盘,只能重启服务)。
        with self._coord():
            if run in self._busy_runs:
                raise HttpError(409, "这一趟正在判读", "等它结束再点")
            self._busy_runs.add(run)
        succeeded = False
        try:
            findings = judge_run(run, history_root=self._ctx.runs_root,
                                 baselines_root=self._ctx.baselines)
            succeeded = True
        except OSError as exc:
            raise HttpError(500, "判读时读写归档失败", str(exc)) from exc
        finally:
            with self._coord():
                self._busy_runs.discard(run)
                if succeeded:
                    # 提交、撤标、删旧报告在同一个锁区间里,中间没有窗。
                    self._stale_reports(run)
        return json_response({"findings": [f.to_wire() for f in findings]})

    def _run_review(self, req: Request) -> Response:
        """记一条人工复核。碰不到模型的结论 —— 见 ``inspect.judge``。"""
        run = self._run_dir(req.params["run_id"])
        payload = req.json()
        if not isinstance(payload, dict):
            raise HttpError(400, "复核要一个对象", "形如 {verdict, note}")
        # 复核很短:写 review.json 和撤「可删」放进同一把协调锁,中间没有窗。
        try:
            with self._coord():
                save_review(run, req.params["name"],
                            str(payload.get("verdict", "")),
                            str(payload.get("note", "")))
                self._stale_reports(run)
        except ValueError as exc:
            raise HttpError(400, "这条复核记不下来", str(exc)) from exc
        except OSError as exc:
            raise HttpError(500, "写复核失败", str(exc)) from exc
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
        now = _归档钟()
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
        behind, full = 0, False
        for t in targets or ():
            if t.usable and t.role is DiskRole.MIRROR:
                # ``runs`` 是这一屏上面早算好的那份名单——传进去让 plan_sync
                # 不用再对 runs_root 重扫一遍(每一趟归档都是一次递归 stat)。
                plan = plan_sync(ctx.runs_root, t.mount,
                                 robot_sn=ctx.identity.sn, runs=runs)
                behind = max(behind, plan.behind)
                # 任何一块镜像盘满了都要顶出来(spec §7.6)。满盘不删旧的,
                # 于是同步就此停住 —— 不说的话,值守屏上只会看到"落后"在涨。
                full = full or plan.full
        notice = backup_notice(targets or (), behind=behind,
                               days_left=soonest, full=full)
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
        now = _归档钟()
        used, total = _disk(ctx.runs_root)
        raw = body.get("free_bytes")
        if raw is None:
            need = bytes_to_free(used_bytes=used, total_bytes=total)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            need = raw
        else:
            raise HttpError(400, "free_bytes 要是个整数", repr(raw))
        applied = bool(body.get("apply"))
        deleted: tuple[Path, ...] = ()
        skipped: list[dict[str, str]] = []
        # **拿方案、重验、真删,全在上传协调锁里。** 方案是队列和盘的一次快照;
        # 不握锁的话,快照到 rmtree 之间 pump 可以重开一条、判读可以改写一份,
        # 验证过的结论在删之前就过期了(W03 外部审核复现的检查后使用竞态)。
        # 单机档没有 pump 也没有 .uploaded,``_coord()`` 就是空的。
        with self._coord():
            runs = scan_runs(ctx.runs_root, now=now)
            if ctx.upload is not None:
                runs = _mask_uploaded_by_queue(runs, Path(ctx.runs_root), ctx.upload.queue,
                                               self._busy_runs)
            sweep = plan_sweep(runs, now=now,
                               has_upload=ctx.form.has_upload, need_bytes=need,
                               noticed=read_notice(ctx.runs_root))
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
                    if ctx.upload is not None:
                        # 删之前对每一趟再验一次:方案里的 uploaded 是刚算的,但
                        # "刚算的"跟"删的那一刻"之间还是要对一次盘和队列。只验
                        # 那些**凭 uploaded 进方案**的趟;凭导出/预告进来的不归这里管。
                        ok: list[RunInfo] = []
                        root = Path(ctx.runs_root)
                        for info in sweep.delete:
                            rel = info.path.relative_to(root).as_posix()
                            why = (_why_not_uploaded(info.path, rel, ctx.upload.queue,
                                                     self._busy_runs)
                                   if info.uploaded else None)
                            if why:
                                skipped.append({"path": str(info.path), "reason": why})
                            else:
                                ok.append(info)
                        sweep = replace(sweep, delete=tuple(ok))
                    deleted = apply_sweep(sweep, runs_root=ctx.runs_root)
                finally:
                    with self._sync_lock:
                        self._sweeping = False
        return json_response({
            "applied": applied,
            "skipped": skipped,
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
                                  now=_归档钟())
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
        在 HTTP 线程上 await 它需要一个事件循环,而桥收的正是一个**协程
        工厂** —— 理由跟 ``_preflight_with_scan`` 一模一样,跟单门不变量无关。
        下面那三个纯文件 I/O 的处理器自己那部分**不过桥**,但都经 ``_pick``
        调用这一处,所以扫盘那一跳除外。

        **走的是本类的 ``_call``,不是 ``ctx.bridge.call``。** 后者不经异常
        翻译:探针卡住时它抛的 ``TimeoutError``、桥停了抛的 ``RuntimeError``
        会绕过 ``_call`` 那段翻译一路穿到兜底,变成一个光秃秃的 500 —— 而这
        一跳恰好挂在唯一两个"盘出毛病时必须还能打开"的页面上(盘况页、备份盘
        页)。走 ``_call`` 拿到的是 504 加一句人话。

        回 ``(None, 一句话)`` 表示没扫成。认不出插着什么不等于这一屏该消失:
        人正是在盘出事的时候来看它的。
        """
        ctx = self._ctx
        disks: tuple[Removable, ...] | None = self._call(
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

    # ------------------------------------------------------------ 自动同步

    async def _autosync_start(self) -> None:
        """把自动同步那条协程建起来。**在循环线程里跑**(经 ``bridge.call``
        进来),写法照 ``_StateHub.start``。"""
        if self._autosync is None:
            self._autosync = asyncio.create_task(self._autosync_loop())

    async def _autosync_stop(self) -> None:
        """收掉那条协程。**停得掉协程,停不掉正在拷的那条线程。**

        ``cancel()`` 之后 ``await`` 是为了等这条协程真的收尾,而不是发完取消
        就走 —— 那样它还可能在下一拍才醒过来。但**等到的只是协程**:实测
        (py3.10.11)协程正卡在 ``asyncio.to_thread`` 里的时候,
        ``cancel()`` + ``await`` 会在 0.00 秒返回,而线程池里那条正在拷贝的
        线程照跑不误 —— ``to_thread`` 底下是 ``run_in_executor``,取消的是
        等待,不是已经交出去的那份活。

        **这样是可以接受的**,因为拷贝本身是原子改名的(``_copy_file`` 先落
        临时名再 ``replace``):那条线程被进程退出打断,盘上要么是完整的那份,
        要么什么也没有,不会留下半个文件;而"拷过了"要等 ``apply_sync`` 落账
        才算数,没落账的下次重拷(重拷是浪费,不是损坏)。

        **要一个"连线程也停下来"的语义,得另加一条协作式的取消标志**,而这一
        卷没有 —— 别照着这段 docstring 以为 ``stop()`` 回来之后盘上就没人写了。
        """
        task, self._autosync = self._autosync, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    async def _autosync_loop(self) -> None:
        """每 :data:`_AUTOSYNC_S` 秒巡一次镜像盘。

        **先睡后跑**:进程刚起来那一刻后端在连、引擎在建,那不是抢盘 I/O 的
        好时候,而备份晚一分钟没有任何代价。

        **这里没有兜底,兜底在 ``_autosync_once`` 里** —— 它整趟都包着
        ``except (OSError, BackupError)``,所以这条 ``while True`` 转不出去。
        兜底放在那一层而不是这一层,是为了让"哪块盘出的事"还在手边:这一层
        除了"又炸了一次"什么也说不出。
        """
        while True:
            await asyncio.sleep(_AUTOSYNC_S)
            await self._autosync_once()

    async def _autosync_once(self) -> None:
        """巡一趟:认盘 -> 只挑镜像盘 -> 排方案 -> 拷。

        **这一条才是"镜像盘不依赖人"那句话的兑现**(spec §7.5 那张表里镜像盘
        那一列写着"依赖人:否",§7.6 写着"没有手机在场时,同步照常进行")。
        四条 HTTP 路由都只是入口,不是同步的发起条件。

        **不走 ``ctx.bridge.call``。** 这个协程本来就跑在循环线程上,而桥是给
        HTTP 线程用的那道门 —— 在循环线程里调它,提交进去的活要等这条循环
        转下一圈才轮得到,而这条循环正卡在等它,当场死锁。直接 ``await``。

        **``plan_sync`` / ``apply_sync`` 必须 ``to_thread``。** 它们是阻塞的
        文件 I/O,一趟 GB 级归档要跑几分钟。占住事件循环的后果不是"慢一点":
        SSE、全部 HTTP、遥控看门狗全在这一条循环上(同样的理由见
        ``video.py`` 里 ``RtspStill.grab``)。

        **只碰镜像盘。** 交付盘是人插上来手动取走的,自动往上写违反 §7.5 ——
        人拔走的那份会比他以为的多。

        **引擎在跑就整趟跳过。** 巡检的时候不跟它抢盘 I/O:归档正在往
        ``runs_root`` 里写,而这边要递归 stat 整棵树再拷几百兆。

        **这一趟从头到尾都在兜底里,而且出错要留下痕迹。** 取 ``engine.running``、
        扫盘、认盘这几步在兜底外面的那一边:任何一个抛出来,``_autosync_loop``
        整条就没了 —— 没有异常回溯、没有日志、页面上什么也不变,而镜像盘从此
        再也不同步。**那正是这一卷立志要消灭的那种静默失败**(一块坏掉的镜像盘
        本身就是个完美的静默失败)。所以捕获 ``(OSError, BackupError)`` 并
        ``log.warning`` 一行说得出是哪块盘的话。

        **``asyncio.CancelledError`` 必须让它穿过去。** 3.10 里它不是
        ``Exception`` 的子类,写 ``except (OSError, BackupError)`` 本来就碰不到
        它 —— 这里记一笔是提醒后来人:别为了"稳"把它改成 ``except Exception``
        或 ``BaseException``,吞了它 ``_autosync_stop()`` 就停不掉这条协程。
        """
        ctx = self._ctx
        try:
            if ctx.engine.running:
                return
            disks = await scan_or_unknown(ctx.removable)
            if disks is None:
                # 认不出插着什么就什么也不写 —— 跟 ``_scan_targets`` 同一条纪律。
                return
            targets = resolve_targets(disks, robot_sn=ctx.identity.sn)
        except (OSError, BackupError):
            # 还没轮到具体某块盘,所以这一行说不出挂载点 —— 它说的是"这一圈
            # 连认盘都没认成",而下一圈会照常再来一次。
            log.warning("自动同步:这一圈认盘没认成,跳过", exc_info=True)
            return
        for t in targets:
            if not (t.usable and t.role is DiskRole.MIRROR):
                continue
            key = t.mount.as_posix()
            with self._sync_lock:
                # 手动那条路由正在这块盘上跑,或者清盘在跑:让路,下一圈再说。
                if self._sweeping or key in self._syncing:
                    continue
                self._syncing.add(key)
            try:
                plan = await asyncio.to_thread(
                    plan_sync, ctx.runs_root, t.mount,
                    robot_sn=ctx.identity.sn)
                # **没有新东西就不写盘。** ``apply_sync`` 空计划也写进度是它的
                # 本分(人点一下要看到"上次同步"往前走),但这条循环每分钟醒
                # 一次:照写的话就是每分钟一次 fsync,一块机械镜像盘从此永远
                # 不休眠。"这条循环还活着"由 ``behind`` 说 —— 它不回落到 0
                # 就是没在同步。
                if plan.items:
                    await asyncio.to_thread(
                        apply_sync, plan, now_ms=ctx.clock(),
                        robot_sn=ctx.identity.sn)
            except (OSError, BackupError):
                # **一块坏盘不能让这条循环死掉** —— 死了之后没有任何人会发现,
                # 因为备份本来就是那个"平时看不见它在不在工作"的东西。
                # ``asyncio.CancelledError`` 是 BaseException,不在这里被吞掉:
                # 吞了 ``stop()`` 就停不掉。
                #
                # **但"不死"不等于"不说话"。** 悄悄跳过跟循环死掉一样不可观测,
                # 所以留一行说得出是哪块盘的日志;真正顶到人脸上的那句话由
                # ``backup_notice`` 按 behind 出(见 ``/api/storage``)。
                log.warning("自动同步:%s 这一圈没跑成,下一圈再试", key,
                            exc_info=True)
            finally:
                with self._sync_lock:
                    self._syncing.discard(key)

    def _backup_targets(self, _req: Request) -> Response:
        """狗现在认到哪些备份盘。**这一屏就是 spec §7.6 那句"app 看见的
        不是备份盘,是狗看见的备份盘"。**

        **没有手机在场的时候同步照常进行** —— 兑现它的是 ``_autosync_once``:
        一条每 :data:`_AUTOSYNC_S` 秒醒一次的后台协程,只碰 ``usable`` 的
        镜像盘(交付盘是人插上来手动取走的),引擎在跑的那几圈整趟让路,
        跟这条路由共用 ``_sync_lock`` / ``_syncing``。这几个接口都不是同步的
        发起条件,只是它的一个入口。

        **不过桥**,除了里面那一处扫盘(``_scan_targets`` 自己会过);算容量、
        读同步进度、排同步方案全是纯文件 I/O,不碰引擎状态。

        **容量报得出来的前提是盘上那个标记文件读得到。** ``_disk`` 在路径不
        存在时会一路往上走到父目录(第一次开机 ``runs/`` 还没建时它要的就是
        这个),而一块刚被拔掉的盘,挂载点常常作为空目录留在根文件系统上 ——
        照着量,报出来的是**根文件系统**的容量,而屏上写着这是备份盘的余量。
        读不到标记就把两个容量字段报成 ``null`` 并在 ``detail`` 里说清楚:
        少一个数好过多一个假数。

        **一块坏盘不许把整条路由掀翻。** ``OSError`` 就地接住,那一块标成
        不可用,边上那几块好盘照列 —— 人正等着看那份列表决定往哪块盘上拷。
        """
        ctx = self._ctx
        targets, why = self._scan_targets()
        if targets is None:
            return json_response({"robot_sn": ctx.identity.sn, "scanned": False,
                                  "targets": [], "detail": why})
        now = _归档钟()
        # **扫一遍就够。** 每块盘各扫一遍的话,``scan_runs`` 会对整个 runs_root
        # 递归 stat 一次(``retention._size_bytes`` 是 rglob("*")),而这条
        # 路由挂在 HTTP 请求路径上,归档一多这个代价不是可以忽略的。
        runs = scan_runs(ctx.runs_root, now=now)
        out = []
        for t in targets:
            row = t.to_wire() | {"total_bytes": None, "free_bytes": None,
                                 "last_sync_ms": 0, "behind": 0,
                                 "behind_bytes": 0}
            try:
                if marker_path(t.mount).is_file():
                    _used, total = _disk(t.mount)
                    row["total_bytes"] = total
                    # **剩余空间跟 ``plan_sync`` 取同一个数(bavail)。**
                    # 屏上那个 ``total - used`` 是 bfree:ext4 默认给 root 留
                    # 5%,同一块 2T 盘上两个数能差出几十个 G,现场会看到
                    # "剩余 100 GB"紧挨着"盘上只剩 0 字节,拷不下"。
                    row["free_bytes"] = backup._free_bytes(t.mount)
                else:
                    row["detail"] = ";".join(x for x in (
                        t.detail, _NO_MARKER_CAPACITY) if x)
                if t.usable:
                    state = read_sync_state(t.mount, robot_sn=ctx.identity.sn)
                    plan = plan_sync(ctx.runs_root, t.mount,
                                     robot_sn=ctx.identity.sn, now=now,
                                     runs=runs)
                    row |= {"last_sync_ms": state.last_sync_ms,
                            "behind": plan.behind,
                            "behind_bytes": plan.behind_bytes}
            except OSError as exc:
                row |= {"usable": False, "total_bytes": None,
                        "free_bytes": None,
                        "detail": f"这块盘读不了({exc})—— 现在不会往它上面写。"
                                  f"盘可能已经拔掉了,或者挂载点已经失效"}
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
                              now_ms=ctx.clock())
        except BackupError as exc:
            # 409 而不是 400:请求本身没毛病,是盘上已经有东西了。
            raise HttpError(409, str(exc)) from exc
        return json_response(got.to_wire())

    def _backup_sync(self, req: Request) -> Response:
        """同步一次。**默认只给方案,真跑要显式 ``{"apply": true}``** ——
        跟 ``/api/storage/sweep`` 同一条纪律:人先看名单,再点第二下。

        本处理器自己那部分是纯文件 I/O,不过桥;扫盘那一跳除外 —— ``_pick``
        里调的 ``_scan_targets`` 会过(见它的 docstring)。

        **进 ``_syncing`` 要赶在 ``plan_sync`` 之前。** ``_pick``(要扫盘)和
        ``plan_sync``(要把整个 runs_root 递归 stat 一遍)是这条路由最慢的两步,
        而这段时间里这块盘其实已经"要被动了"。记晚了的那一边,并发进来的
        ``/api/backup/eject`` 会照着一份空的 ``_syncing`` 回一句"可以拔了" ——
        人真拔了,下一秒这边就开始往一个已经不在的挂载点上拷。
        """
        ctx = self._ctx
        target = self._pick(req)
        if not target.usable:
            raise HttpError(409, target.detail)
        key = target.mount.as_posix()
        # ``_pick`` 已经验证过请求体是个 dict,这里是同一份 bytes 再解一遍。
        if not bool(req.json().get("apply")):
            # 出方案不动盘,所以不占 ``_syncing``:跟 ``{"apply": false}`` 的
            # 清盘同一条纪律 —— 光看名单的人不该把弹出按钮也一起锁住。
            plan = plan_sync(ctx.runs_root, target.mount, robot_sn=ctx.identity.sn)
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
            plan = plan_sync(ctx.runs_root, target.mount, robot_sn=ctx.identity.sn)
            res = apply_sync(plan, now_ms=ctx.clock(),
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

    def _backup_ages(self) -> float | None:
        """距上次备份多少天。**取不到就是 None,不是 0**。

        0 意味着「刚同步过」,而这里的「不知道」和「很新」得是两句不一样的话
        —— 升级前自检那一项(``backup``)照 ``None``/一个数走两条不同的措辞。
        跟 ``_scan_targets`` 一样:没扫成、或者一块能用的盘都没有,都算「不知道」。
        """
        targets, _why = self._scan_targets()
        if not targets:
            return None
        ctx = self._ctx
        best_ms: int | None = None
        for t in targets:
            if not t.usable:
                continue
            state = read_sync_state(t.mount, robot_sn=ctx.identity.sn)
            if state.last_sync_ms and (best_ms is None or state.last_sync_ms > best_ms):
                best_ms = state.last_sync_ms
        if best_ms is None:
            return None
        return max(0.0, (ctx.clock() - best_ms) / 1000 / 86400)

    # ------------------------------------------------------------ 版本

    def _layout(self) -> Layout:
        return Layout(root=Path(self._ctx.release_root))

    def _release(self, _req: Request) -> Response:
        """盘上现在是什么局面。装机验收和值守屏都读这一条。"""
        layout = self._layout()
        pending = read_pending(layout)
        return json_response({
            "root": str(layout.root),
            "current": current_name(layout),
            "installed": list(installed(layout)),
            "pending": pending.to_wire() if pending is not None else None,
        })

    def _release_install(self, req: Request) -> Response:
        """把一个包落进 releases/。**只落槽,不切换。**"""
        raw = req.json()
        if not isinstance(raw, dict):
            raise HttpError(400, "请求体要是个对象", '形如 {"package": "..."}')
        package = raw.get("package")
        if not isinstance(package, str) or not package:
            raise HttpError(400, f"package 要是一个路径字符串,给的是 {package!r}")
        try:
            manifest = stage(self._layout(), Path(package),
                             now_ms=self._ctx.clock())
        except ReleaseError as exc:
            # 409 而不是 400:请求本身没毛病,是那个包有毛病。
            raise HttpError(409, str(exc)) from exc
        except OSError as exc:
            raise HttpError(409, f"落槽失败: {exc}") from exc
        return json_response(manifest.to_wire())

    def _gather_precheck(self, manifest: ReleaseManifest, *,
                         auto: bool) -> PrecheckInputs:
        """把升级前自检要的事实一次取齐。**取值都在这儿,判断不在这儿。**"""
        ctx = self._ctx
        layout = self._layout()
        used_b, total_b = _disk(layout.root)          # 回的是(已用, 总量),字节
        free_mb = (total_b - used_b) / 1024 / 1024
        need_mb = _dir_size_mb(layout.releases / manifest.name)
        battery = self._call(ctx.device.battery)
        payload = ctx.identity.payload
        targets = self._backup_ages()
        return PrecheckInputs(
            package_ok=True, package_detail="落槽时已经对过哈希",
            requires_mission_schema=manifest.requires_mission_schema,
            mission_schema_floor=mission_schema_floor(ctx.missions_dir),
            free_mb=free_mb, need_mb=need_mb,
            battery_pct=float(battery),
            engine_running=ctx.engine.running,
            # 用 sweep() 而不是 book.state():两者都会结算过期,但 sweep()
            # 还会把 token 已经没了的租约释放掉(keep_only(live_refs))——
            # 操作员手机掉线、token 死了,不该让那份租约再挡 30 秒 TTL 才放行
            # (见 docs/第2卷待办.md 第 50 条)。``ControlDesk.require()`` 判
            # 「有没有人握着控制权」用的就是这同一个 sweep(),这里跟它对齐,
            # 免得同一个文件里出现两套判据。
            lease_active=self._control.sweep(
                now_ms=ctx.clock()).holder is not None,
            payload_recorded=payload.recorded, has_payload=payload.has,
            auto=auto, backup_age_days=targets)

    def _release_activate(self, req: Request) -> Response:
        """切到某一版。**先自检,过了才换链。**"""
        ctx = self._ctx
        raw = req.json()
        if not isinstance(raw, dict):
            raise HttpError(400, "请求体要是个对象", '形如 {"name": "..."}')
        name = raw.get("name")
        if not isinstance(name, str):
            raise HttpError(400, f"name 要是版本名,给的是 {name!r}")
        auto = bool(raw.get("auto", False))
        layout = self._layout()
        try:
            manifest = read_release_manifest(layout.release_dir(name))
        except ReleaseError as exc:
            raise HttpError(404, str(exc)) from exc

        report = precheck(self._gather_precheck(manifest, auto=auto))
        if not report.ok:
            raise HttpError(409, "升级前自检没过",
                            json.dumps(report.to_wire(), ensure_ascii=False))

        # ``recorded`` 在这条路上其实到不了 False —— precheck 的 payload 那一项
        # 就拦着「没记过」的机器。**照样传对**:一个只在某些调用点传的参数,
        # 下一个人读到时说不清它到底是不是可省的,而漏传的后果是一台装了上装
        # 但没登记的机器被只重启服务(= 放掉 SDK 会话,§7.1)。
        plan = restart_plan(has_payload=ctx.identity.payload.has,
                            recorded=ctx.identity.payload.recorded)
        try:
            pending = activate(layout, name, now_ms=ctx.clock(),
                               auto=auto, sn=ctx.identity.sn)
        except ReleaseError as exc:
            raise HttpError(409, str(exc)) from exc
        ctx.restart(plan)
        return json_response({"precheck": report.to_wire(),
                              "pending": pending.to_wire(),
                              "restart": plan.to_wire()})

    def _release_rollback(self, req: Request) -> Response:
        """人工回滚。**跟自动回滚走同一个 ``release.rollback``。**

        **巡检途中默认不许回滚。** 回滚这条路最后一定落在 ``ctx.restart()``
        上,重启的那几秒到几十秒里 ``POST /api/estop`` 这条路由是**不通的**
        —— 服务没起来,谁都软急停不了。狗这时候还在走。所以这里跟
        ``_release_activate`` 的自检对齐:``engine_running`` 为真就 409。

        **为什么是这里加闸而不是把它放进 ``control.CONTROLLED``:** 那张表拦
        的是"没有控制权的人",而回滚常常正是一次恢复性操作 —— 现场刚升完一版
        发现不对,这时候上一个会话很可能已经掉线了,让一份死租约挡住回滚是把
        机器锁死。判据在这儿不一样:不该按"有没有人握方向盘"拦,该按"这只狗
        现在在不在走"拦。

        **留了硬回滚的口子**,写法跟 ``_control_takeover`` 的 ``force`` 一模
        一样 —— ``{"force": true, "reason": "..."}``,**理由是必填的**(§3.5
        规则 3)。理由:新装的这一版本身就可能是"狗停不下来"的原因,那种时候
        ``/api/run/abort`` 也未必好使,不给口子等于把人逼去拔电。硬回滚会在
        事件流上留一条 ``release.force_rollback``。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象",
                            '不给内容就是普通回滚;硬回滚形如 '
                            '{"force": true, "reason": "..."}')
        force = bool(body.get("force"))
        reason = body.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        reason = reason.strip()
        if self._ctx.engine.running:
            if not force:
                raise HttpError(
                    409, "这一趟还在跑,不能回滚版本",
                    "回滚要重启服务,重启那几十秒里软急停是打不通的,而狗还在"
                    "走。先 POST /api/run/abort 收掉这一趟;真要带着跑的活儿"
                    '硬回滚,传 {"force": true, "reason": "为什么"}。')
            if not reason:
                raise HttpError(400, "硬回滚必须写理由",
                                "带着一趟没跑完的活儿重启服务是要留痕的。")
        sess = req.session
        layout = self._layout()
        try:
            back = rollback(layout, now_ms=self._ctx.clock())
        except ReleaseError as exc:
            raise HttpError(409, str(exc)) from exc
        if force:
            # 只有硬回滚才留痕。普通回滚是"停着的时候换个版本",跟事故无关;
            # 硬回滚是"带着一趟没跑完的活儿把服务重启了",事后一定有人要查。
            self._hub.events.emit({"kind": "release.force_rollback",
                                   "rolled_back_to": back, "reason": reason,
                                   "operator": sess.operator if sess else "",
                                   "session": sess.ref if sess else "",
                                   "at_ms": self._ctx.clock()})
            log.warning("巡检途中硬回滚到 %s(发起人 %s,理由:%s)",
                        back, (sess.operator if sess else "") or "未署名", reason)
        payload = self._ctx.identity.payload
        plan = restart_plan(has_payload=payload.has, recorded=payload.recorded)
        self._ctx.restart(plan)
        return json_response({"rolled_back_to": back, "restart": plan.to_wire(),
                              "forced": force})

    # ------------------------------------------------------- 任务包与排程

    def _bundle(self, _req: Request) -> Response:
        """狗手上是哪一版包。**心跳报的就是这一条**(§3.2)。

        包被改过的时候 ``manifest`` 是 ``None``,但 ``state`` 照样答得出来 ——
        「current 指着谁」正是出事之后第一个要看的东西。
        """
        root = self._ctx.bundles_root
        # ``current`` 链悬空(那个槽被 prune 掉了 / bundles_root 被搬过 /
        # 人手删了槽目录)也算不出「那一版的内容」,但不该让整条路由跟着炸 ——
        # `state` 用的是 ``read_bundle_state``,它直接读链名,不管链指的目录
        # 在不在,所以「current 指着谁」这句话在这个分支里照样答得出来。
        try:
            where = active_bundle(root)
        except BundleError:
            where = None
        manifest = None
        if where is not None:
            with contextlib.suppress(BundleError):
                manifest = verify_bundle(where).to_wire()
        return json_response({"root": str(root),
                              "state": read_bundle_state(root).to_wire(),
                              "manifest": manifest})

    def _bundle_apply(self, req: Request) -> Response:
        """让某一版生效。

        **换链之前 engine 会先校验**(槽名那道闸、§3.1 那个目标 SN 那道闸都在
        里头),坏包、越界的槽名、发错机器的包都换不过去。所以这儿不再验一遍
        —— 验两遍就有两份判据,迟早分岔。

        本机 SN 从 ``self._ctx.identity.sn`` 来。**SN 是服务端自己填的,不收
        请求体里的** —— 让调用方报「我是谁」,这道闸就等于不存在(定夺 13)。

        **``force`` 是这条路上唯一一扇「明确强推」的门**(评审复评 finding 5)。
        交付文档对客户写着「退过的那一版默认不许再上,**除非人明确强推**」,
        而在这之前产品里根本没有那个开关:排障确认崩因是环境(SD 卡没挂上)
        之后想把上一版装回去,只能重打一个版号更高的同内容包。

        **必须是字面量布尔 ``true``。** 字符串 ``"true"``、``1``、``"1"`` 一律
        不算,非布尔直接 400 —— 跟同一个类里 ``reason`` 那道类型校验同源,而
        理由更硬:这扇门后面是「装一个已知会崩的版本」,一个被 JSON 编码器
        随手转成字符串的 truthy 值不该顶开它。(``isinstance(True, int)``
        是真的,反过来 ``isinstance(1, bool)`` 是假的,所以这道闸写成
        ``isinstance(force, bool)`` 正好只放行字面量布尔。)

        强推会在 ``landed.json`` 的 ``forced`` 历史里留一笔(时刻同样是**服务端
        盖的**,跟回退记录一个道理),而且**不动 ``denied``**:下一次不带
        ``force`` 的 apply 照样被拒。

        **响应里回一个 ``overrode_denied``**(评审复评第 3 轮 N4):这一次
        ``force`` 到底有没有真顶开黑名单。留痕**只在真顶开那一次才记**
        (不然这份历史会被噪音冲掉),于是「传了 force、200 OK、``forced``
        里什么也没多」这件事在调用方看来无从分辨 —— 那正是上一轮挂账的
        F8「静默降级」换了个位置。这个布尔把它变回看得见的。判据本身不在这儿
        写第二遍:调的是 engine 那个 ``denies()``,跟 ``apply_bundle`` 的闸
        同一份。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象", '形如 {"slot": "..."}')
        slot = body.get("slot")
        if not isinstance(slot, str) or not slot:
            raise HttpError(400, "要 slot", "就是包目录名,形如 site-kl-7")
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise HttpError(400, "force 要是字面量布尔 true/false",
                            f'给的是 {type(force).__name__} —— 字符串 "true"、'
                            "1、\"1\" 都不算:这扇门后面是装一个已知会崩的版本")
        at = datetime.fromtimestamp(
            self._ctx.clock() / 1000, tz=timezone.utc).isoformat()
        # 换链之前问一次「这一版现在是不是被拉黑的」。换完再问就晚了 ——
        # 那时候 ``denied`` 一个字没变(强推不洗白),看不出这一次顶开过什么。
        try:
            顶开了 = denies(read_bundle_state(self._ctx.bundles_root), slot)
        except (OSError, ValueError, TypeError):
            # 状态读不出来只影响这个布尔的准头,不该让一次 apply 失败 ——
            # 真读不出来的话底下 ``apply_bundle`` 自己会给出一个像样的错。
            顶开了 = False
        try:
            state = apply_bundle(self._ctx.bundles_root, slot,
                                 sn=self._ctx.identity.sn, force=force, at=at)
        except BundleError as exc:
            if "退过的那一版" in str(exc):
                # 409,不是 400:请求本身没毛病(槽名、SN 都对),是这台机器
                # 现在的状态不允许 —— 跟 `_bundle_rollback` 那边同一个判据。
                # 评审定夺:同一件事在相邻两条路由上不该给两个不同的答案。
                raise HttpError(409, str(exc)) from None
            raise HttpError(400, str(exc)) from None
        return json_response({"state": state.to_wire(),
                              "overrode_denied": 顶开了})

    def _bundle_rollback(self, req: Request) -> Response:
        """退回上一版。

        **时刻是服务端盖的。** 请求里带的 ``at`` 一律不收 —— 手机的钟可以是
        任何值,而这条记录是事后追责用的:一条能被客户端随便写的时间戳,
        追责的时候等于没有。

        **连点两次是安全的**(评审 F1):第二次的 ``previous`` 正是第一次刚
        拉黑的那一版,``rollback_bundle`` 会拒,这儿翻成 409。这条闸在 engine
        里,不在这儿 —— 跟 ``_bundle_apply`` 一样,两扇门共用同一份判据。
        """
        body = req.json()
        raw_reason = body.get("reason") if isinstance(body, dict) else None
        if raw_reason is not None and not isinstance(raw_reason, str):
            raise HttpError(400, "reason 要是个字符串",
                            f"给的是 {type(raw_reason).__name__}")
        reason = raw_reason or ""
        if len(reason) > MAX_ROLLBACK_REASON_LEN:
            # 挡的不是攻击,是「有人把整段日志粘进 landed.json」——跟
            # ``engine/bundle.py`` 的 ``MAX_SN_LEN`` 挡的是同一类事。
            raise HttpError(400, f"reason 太长了,上限 {MAX_ROLLBACK_REASON_LEN} 字",
                            f"给的是 {len(reason)} 字")
        at = datetime.fromtimestamp(
            self._ctx.clock() / 1000, tz=timezone.utc).isoformat()
        try:
            state = rollback_bundle(self._ctx.bundles_root, at=at,
                                    reason=reason or "手工回退")
        except BundleError as exc:
            # 409:请求本身没毛病,是这台机器现在的状态不允许(没有 previous)。
            # 手机上该弹「现在退不了」,不是「参数错了」。
            raise HttpError(409, str(exc)) from None
        return json_response({"state": state.to_wire()})

    def _schedule(self, _req: Request) -> Response:
        """包里的排程,加上每条此刻的决定。值守屏读这一条。

        **这条路由是「看」,不是「跑」。** ``decide()`` 说 due 了,它也只是把
        这句话答出来。

        **到点自动出发这件事,整个仓库里没有人做。** ``engine/`` 那边只有
        ``decide()`` 这个判据,没有任何一条循环去轮询它、更没有谁拿它的结论
        去调 ``engine.start()``;这条路由是 ``decide()`` 唯一的调用点,而它只
        把结论写进响应体给值守屏看。以前这段话写的是「真起跑是第 8 卷排程器
        的事」—— 第 8 卷就是现在,那个排程器没有被做出来。**这里不把话再往
        后推一卷**,只把现状写清楚:今天要让一趟任务出发,唯一的办法是有人
        (或者手机)去 ``POST /api/missions/<id>/run``。

        ``last_started_ms`` 一律传 ``None``:「上一次真起跑是什么时候」要从 run
        归档里回查,那份回查同样没做。这里按「从没跑过」答,**在当下不是
        漏了** —— 没有任何东西会照这个结论去起跑,重复起跑这件事在这儿不存在。
        要哪天真接上自动起跑,这两条(轮询循环、``last_started_ms`` 回查)得
        一起补:只补前一条会变成「到了点每一轮都判 due,于是每一轮起一趟」。
        """
        # 一次请求里只读一次墙上时钟:真机上 ``now`` 和漂移用不同瞬间的读数
        # 是一处可以省掉的糊涂账,读一次存下来,全程用它。
        local_ms = self._ctx.clock()
        clock = self._clock_skew(local_ms).to_wire()
        try:
            where = active_bundle(self._ctx.bundles_root)
        except BundleError as exc:
            # 链悬空(那个槽被 prune 掉了 / bundles_root 被搬过 / 人手删了槽
            # 目录)也是「取不到包」的一种,同样翻成 409 —— 不能让它冒成 500:
            # 现场的人看到 500 只能猜,而这台狗此刻在一个没有网的地方。
            raise HttpError(409, str(exc)) from None
        if where is None:
            return json_response({"timezone": "", "now": "", "entries": [],
                                  "clock": clock})
        try:
            schedule = read_bundle_schedule(where)
        except BundleError as exc:
            # 409 而不是 500:现场的人看到 500 只能猜,看到「缺 timezone」能
            # 自己修 —— 而这台狗此刻在一个没有网的地方。
            raise HttpError(409, str(exc)) from None

        # **用包里那个时区,不读系统时区**(§3.3 第 1 条)。裸 datetime 会被
        # 按系统时区解释,而且不报错,只是悄悄算错。
        now = datetime.fromtimestamp(local_ms / 1000, tz=schedule.tz())
        entries = []
        for entry in schedule.entries:
            下一轮 = next_run(entry, now=now)
            entries.append(entry.to_wire() | {
                "decision": decide(entry, now=now,
                                   last_started_ms=None).to_wire(),
                "next_run": 下一轮.isoformat() if 下一轮 is not None else "",
            })
        return json_response({"timezone": schedule.timezone,
                              "now": now.isoformat(),
                              "entries": entries, "clock": clock})

    def _clock_skew(self, local_ms: int) -> Skew:
        """本地钟跟外头差多少。**没有参照就是「不知道」,不是 0。**

        ``local_ms`` 由调用方传进来,不在这儿现读 ``ctx.clock()`` —— 一次
        请求只读一次墙上时钟,``now`` 和漂移用的是同一个瞬间。
        """
        ref = self._ctx.time_reference()
        if ref is None:
            return clock_skew(local_ms=local_ms, reference_ms=None, source="")
        reference_ms, source = ref
        return clock_skew(local_ms=local_ms, reference_ms=reference_ms,
                          source=source)

    # ------------------------------------------------------------ 值守屏

    def _watch_summary(self, _req: Request) -> Response:
        """值守屏那六项(§5.1)。**只读** —— 判据全在 ``app/watch.py``。

        这儿只干一件事:把那三样 ``watch.py`` 自己够不着的事实取来递进去。

        * 盘水位传的是 ``_disk`` 本人,不是模块 —— ``app/watch.py`` import
          回这个模块就是一个环。跟 ``AlertSources`` 那个 ``disk=`` 同一个口径。
        * 电量取 ``_StateHub`` 手上那份 ``battery_at``,**不现问后端**:厂商
          后端上问一次电量是一次真实的链路往返,而这一屏是按秒刷的;那份是
          事件推出来的,读它不花一分钱。**连同接收时刻一起取** —— 链路断了
          它不会自己变回 ``None``,只会停在最后一个读数上,把时刻一并报上去
          才轮得到看的人自己判断这个数多老了。
        * 扫盘那一跳要过桥(``_scan_targets`` 的 docstring 说了为什么)。

        **探针卡住不许把另外五项一起带走。** 值守屏是最后一块必须还能亮的
        玻璃 —— 那五项里有盘水位和电量,正是盘出事、电快没了的时候人要看的。
        扫不成就把镜像盘那一档报成「不知道」,这一屏照样答完。
        """
        ctx = self._ctx
        try:
            targets, _why = self._scan_targets()
        except HttpError:
            log.warning("扫盘没成,镜像盘那一档按不知道答", exc_info=True)
            targets = None
        battery_pct, battery_as_of_ms = self._hub.battery_at or (None, None)
        return json_response(watch_summary(
            ctx, now_ms=ctx.clock(),
            disk=lambda: _disk(ctx.runs_root),
            battery_pct=battery_pct, battery_as_of_ms=battery_as_of_ms,
            targets=targets,
            # 没装回传就是 ``None``,不是 ``UploadStats()`` —— 后者的
            # ``backlog`` 是 0,而 0 在这一格上的意思是「查过了没积压」。
            upload=ctx.upload.stats() if ctx.upload is not None else None))

    # ------------------------------------------------------------ 回传

    def _upload_get(self, _req: Request) -> Response:
        """回传这一档现在什么样。**只读 —— 这条路由按不动任何东西。**

        没装回传的时候 ``backlog`` 是 ``None`` 不是 ``0``,口径跟
        ``/api/watch/summary`` 上那一格完全一致(见 ``watch.NO_UPLOADER``)。

        **它不进 ``OPEN_PATHS``,要 token。** 积压条数会泄露这台狗最近跑了
        多少趟、传没传出去 —— 跟值守屏同一个口径。**也不进 ``CONTROLLED``**:
        它不改狗,进了那张表就等于别人握着控制权的时候值守的人连看都看不了。
        """
        pump = self._ctx.upload
        if pump is None:
            return json_response({
                "enabled": False, "backlog": None, "last_step": "",
                "last_ok_ms": 0, "last_error": "", "sent_files": 0,
                "detail": NO_UPLOADER})
        st = pump.stats()
        return json_response({
            "enabled": True, "backlog": st.backlog,
            "last_step": st.last_step, "last_ok_ms": st.last_ok_ms,
            "last_error": st.last_error, "sent_files": st.sent_files})

    # ------------------------------------------------------------ 告警

    def _alerts_open(self, _req: Request) -> Response:
        """未解决的告警,P1 在最上面(§5.2)。值守屏的主表读这一条。

        **顺序是 ``AlertBook.open()`` 给的,这儿不再排一遍。** 排法(先级别
        再 ``last_ms`` 倒序)是判据的一部分,判据只许在 ``engine/`` 里说一
        次;在这里再排一遍就是同一件事有两个出处,而对不上的那天,屏幕第一行
        显示的不是该起身的那件事。
        """
        return json_response(
            {"alerts": [a.to_wire() for a in self.alerts.open()]})

    def _alerts_all(self, _req: Request) -> Response:
        """连已确认、已解决的一起给 —— 交接班要看的是这一张。

        **不做分页、不做截断。** 这本簿子活在内存里,一次开机的量级是几十
        条;真要长到需要分页,那本身就是一件该被看见的事,不该被一条悄悄
        截断的接口掩过去。
        """
        return json_response(
            {"alerts": [a.to_wire() for a in self.alerts.all()]})

    def _alert_ack(self, req: Request) -> Response:
        """记名确认:"我看见了,我在处理"(§5.3)。

        **记名是这条接口存在的理由。** 确认会把升级链停下来(``ack`` 之后
        ``due_escalations`` 不再看这一条),匿名确认等于任何人都能把声音关
        掉而没人负责 —— 所以空姓名是 400,不是"先记下来再说"。

        ``who`` 只认请求体里传上来的。任务 11 负责让手机自动带上落盘的操作
        员姓名;在那之前手机得自己填,这一层不去猜、也不拿会话上的
        ``operator`` 顶替 —— 那个名字狗记下来但**不核实**(§6.3),拿它当
        "谁确认的"会让屏幕上出现一个看着像被核实过的名字。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象", '形如 {"who": "老王"}')
        who = body.get("who")
        who = who.strip() if isinstance(who, str) else ""
        if not who:
            raise HttpError(
                400, "确认要记名",
                '形如 {"who": "老王"}。确认会把升级停下来 —— 没有名字就没有'
                "人负责,那条升级链就白做了(§5.3)。")
        try:
            alert = self.alerts.ack(req.params["key"], who=who,
                                    now_ms=self._ctx.clock())
        except AlertNotFound:
            raise _没这条告警(req.params["key"]) from None
        return json_response({"alert": alert.to_wire()})

    def _alert_resolve(self, req: Request) -> Response:
        """这件事没了。**解决记名,但不冒充确认。**

        **一个名字都不许往 ``acked_*`` 里填。** 解决了不等于有人看见过 ——
        "没人看见"正是 §5.3 要暴露出来的那个事实(升级只看有没有人确认),
        在这里悄悄补一个确认,交接班那张表上就再也看不出这一班到底有没有人
        在盯屏幕,而升级链会被一次"我顺手点了解决"整个关掉。所以名字落的是
        ``resolved_by`` 这个独立字段,``acked_by`` / ``acked_ms`` 原样不动。

        **记名是可选的,这一点跟 ``ack`` 不一样。** 空姓名的确认会把升级关
        掉,所以那边空了就是 400;而空姓名的解决只是交接班那张表上少一个名
        字 —— 拿它去挡住"这件事没了",代价大得多(手机端至今发的就是空体,
        见 ``mobile/lib/net/patrol_client.dart`` 的 ``resolveAlert``)。

        **但类型错了要说出来,不许静默当成没记名。** ``{"who": 123}`` 是前端
        会犯的错:悄悄按"没记名"办,那次解决会安安静静地成功,而表上少的那
        个名字谁也不知道少在哪儿。也不该 500 —— 那是把我们的锅甩给一个只是
        拼错了类型的人。
        """
        body = req.json()
        if not isinstance(body, dict):
            raise HttpError(400, "请求体要是个对象",
                            '形如 {"who": "老王"};不记名就发个空体。')
        raw = body.get("who", "")
        if not isinstance(raw, str):
            raise HttpError(
                400, "名字要是一串字",
                f'"who" 给的是 {type(raw).__name__}。形如 {{"who": "老王"}};'
                "不记名就别带这个字段 —— 但别拿一个不是字的东西当名字,"
                "那样交接班那张表上会少一个名字而没人知道少在哪儿。")
        try:
            alert = self.alerts.resolve(req.params["key"], who=raw.strip(),
                                        now_ms=self._ctx.clock())
        except AlertNotFound:
            raise _没这条告警(req.params["key"]) from None
        return json_response({"alert": alert.to_wire()})

    # ------------------------------------------------------------ 起来之后那一遍

    def _boot_postcheck(self) -> dict[str, Any] | None:
        """起来之后跑一遍四项自检,按结果坐实或退回(§7.2、§7.3)。

        **它是同步的,而且在 HTTP 端口起来之后才跑。** 顺序不能反:自检的第一
        项就是"进程起来了",而在这套代码里"进程起来了"的可观测定义就是端口
        在听。先自检后监听的话,第一项永远假失败,于是每一次升级都会回滚。

        返回 ``None`` 表示这次开机根本不在途 —— 这是绝大多数开机的情况;也可能
        是这次自检整个没跑起来(比如线程桥没在跑),那种情况下宁可当没在途处
        理,也不能让异常从这个方法冒出去把 ``start()``/``main()`` 带崩。

        **跑判据、落盘、发事件三件事挤在同一次 ``ctx.bridge.call`` 里一起过桥。**
        ``EventEmitter.emit()`` 按 ``bridge.py`` 里写的约定只能在事件循环那根
        线程上碰——订阅者列表没上锁,而且有等待者的时候 ``put_nowait`` 的唤醒
        路径走的是非线程安全的 ``loop.call_soon()``。落盘(``commit``/
        ``clear_pending``/``rollback``)跟着一起挪过去,不是因为它本身非得在
        循环线程上跑,而是不想把"跑判据""落盘""发事件"这三件事拆到两次过桥
        里——拆开以后中间状态更难想清楚(比如落盘成功了但因为在另一根线程上
        发事件时抛了异常,调用方却看不出落盘到底成没成)。
        """
        layout = self._layout()
        pending = read_pending(layout)
        if pending is None:
            return None

        ctx = self._ctx

        async def _run() -> dict[str, Any]:
            try:
                # **直接跑,不走 ``self._call``。** ``_call`` 的活是把业务异常翻成
                # HTTP 状态码,而这儿根本没有一个请求在等回话——让它把
                # ``HttpError`` 抛出来只会把开机流程搅乱。这里要的恰恰相反:任何
                # 异常都只是"这一项没过"。
                results: Sequence[CheckResult] = await run_postcheck(
                    ctx.nav, ctx.device,
                    want_sn=pending.sn or ctx.identity.sn,
                    got_sn=ctx.identity.sn,
                    sleep=self._postcheck_sleep)
            except Exception as exc:  # 自检自己炸了 ≠ 自检通过
                # 一个坏到连自检都跑不完的版本,正是最该被退回去的那种。
                log.exception("重启后自检自己炸了,按没过处理")
                results = [CheckResult("process", False, f"自检崩了: {exc}")]
                # 只有一项——``postcheck_verdict`` 见到项数不全就判回滚,正合适。

            verdict = postcheck_verdict(results)
            wire = {"verdict": verdict.value,
                    "checks": _check_results_wire(results),
                    "name": pending.to, "previous": pending.src}

            try:
                if verdict is Verdict.KEEP:
                    commit(layout)
                    self._hub.events.emit({"kind": "release.kept", **wire})
                    log.info("重启后自检四项全过,坐实 %s", pending.to)
                    return wire

                if not pending.src:
                    # 退无可退。**不回滚**——回滚到"没有"会把机器变成一块砖。
                    # 清掉在途标记免得下次开机再回滚一次,然后把话喊出来。
                    clear_pending(layout)
                    self._hub.events.emit({"kind": "release.stuck", **wire})
                    log.error("重启后自检没过,但没有上一版可退。留着 %s,请人来看",
                             pending.to)
                    return wire

                back = rollback(layout, now_ms=ctx.clock())
                self._hub.events.emit({"kind": "release.rolled_back",
                                      "rolled_back_to": back, **wire})
                log.error("重启后自检没过,退回 %s", back)
                ctx.restart(restart_plan(
                    has_payload=ctx.identity.payload.has,
                    recorded=ctx.identity.payload.recorded))
                return wire
            except Exception:  # 落盘/发事件炸了也不能把过桥的调用方带崩
                # **别把 pending 标记清掉去图好看。** 上面这一段(坐实/回滚/发事
                # 件/重启)没走完,标记就该照原样留着——``boot_guard`` 会在下一
                # 次开机按 ``pending.attempts`` 接着判,这正是两层回滚设计里的
                # 第二层,专门给"第一层自己没走完"兜底用的。
                log.exception("重启后自检坐实/回滚落盘或发事件时炸了,"
                              "标记留着等下次开机 boot_guard 兜底")
                return wire

        try:
            return ctx.bridge.call(_run, timeout_s=30.0)
        except Exception:  # 这一路任何异常都不能从这个方法冒出去
            log.exception("重启后自检整个没跑起来(比如线程桥没在跑)")
            return None

    def _selfcheck(self, _req: Request) -> Response:
        """随时重跑那四项。**只看不动** —— 不坐实也不回滚。

        人在值守屏上点这一下,意思是"现在还好吗",不是"我确认这版能用"。
        把它做成会清在途标记的,等于给了一条绕开自动回滚的路。
        """
        ctx = self._ctx
        pending = read_pending(self._layout())
        want = pending.sn if pending is not None and pending.sn else ctx.identity.sn
        results = self._call(partial(run_postcheck, ctx.nav, ctx.device,
                                     want_sn=want, got_sn=ctx.identity.sn,
                                     sleep=self._postcheck_sleep))
        return json_response({"verdict": postcheck_verdict(results).value,
                              "checks": _check_results_wire(results),
                              "pending": pending.to_wire() if pending else None})

    def _payload_put(self, req: Request) -> Response:
        """改「这台有没有装上装」。**改完立刻生效**,不用重启。

        身份是个 frozen dataclass,所以这里换的是 ``ctx.identity`` 这个引用
        本身 —— 换引用是原子的,HTTP 线程读到的要么是旧的一整份要么是新的
        一整份,不会读到改了一半的。
        """
        ctx = self._ctx
        raw = req.json()
        if not isinstance(raw, dict):
            raise HttpError(400, "请求体要是个对象")
        try:
            written = write_payload(has=bool(raw.get("has_payload")),
                                    by=str(raw.get("by", "")),
                                    now_ms=ctx.clock(),
                                    path=ctx.payload_file,
                                    confirm=str(raw.get("confirm", "")))
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        except OSError as exc:
            raise HttpError(500, f"记不下来: {exc}") from exc
        ctx.identity = replace(ctx.identity, payload=written)
        return json_response(ctx.identity.to_wire())

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

        ``VideoError`` 也在这个元组里:RTSP 流可能在已经发出第一帧**之后**
        才断(源头见 ``app/video.py`` 的 ``_stream()``),那时异常是从
        ``stream.chunks`` 这个生成器里,在下面这个 ``for`` 循环里冒出来的。
        漏了它,连接照样会关,但每个观众都会在 stderr 刷一份 traceback,把
        真正该看的报错淹掉。
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
        except (BrokenPipeError, ConnectionResetError, OSError, VideoError):
            pass                    # 客户端走了,或者流断了 —— 都是正常收尾
        finally:
            with contextlib.suppress(Exception):
                stream.chunks.close()   # type: ignore[attr-defined]


# ------------------------------------------------------------------ 入口


def _warn_if_data_in_slot(cwd: Path, env: Mapping[str, str]) -> str | None:
    """D1MAX_DATA_ROOT 没设、而进程跑在版本槽里 —— 多半是 OTA 升上来、单元文件
    没更新。返回要打的警告,没事回 None。拆成纯函数是为了不起服务也能测。
    """
    if env.get(DATA_ROOT_ENV) is not None:
        return None
    if "/releases/" not in str(cwd.resolve()) + "/":
        return None
    return (f"{DATA_ROOT_ENV} 没设,而当前目录在版本槽里:{cwd}。巡检数据会落在槽里,"
            "升级两次会被 prune 拦下但不会被搬走。这台机器多半是 OTA 升上来、"
            "单元文件没更新 —— 重跑一次 deploy/install.sh")


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
    # **数据根。** 服务单元设 D1MAX_DATA_ROOT=/var/lib/d1max,四个数据目录就都
    # 落到版本槽外面;开发机不设,还是仓库里的相对路径。理由见 engine/datadir.py。
    paths = resolve_paths(os.environ.get(DATA_ROOT_ENV))
    p.add_argument("--maps-dir", default=str(paths.maps_dir), help="自建地图目录")
    p.add_argument("--bags-dir", default=str(paths.bags_dir), help="录包落盘目录")
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
    p.add_argument("--missions-dir", default=str(paths.missions_dir))
    p.add_argument("--runs-root", default=str(paths.runs_root),
                   help=f"巡检数据根目录。默认按环境变量 {DATA_ROOT_ENV} 推:"
                        f"<数据根>/runs;没设就是相对路径 runs")
    p.add_argument("--log-dir", help="子进程日志目录(默认 <runs-root>/logs)")
    p.add_argument("--console-url", default=os.environ.get(CONSOLE_URL_ENV),
                   help=f"回传服务器地址(也可以用环境变量 {CONSOLE_URL_ENV})。"
                        f"不给就是单机档:证据全留在本机,靠 U 盘导出。"
                        f"token 只走环境变量 {CONSOLE_TOKEN_ENV} —— 命令行上的"
                        f"东西在 ps 里是明文的")
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


async def _make_teleop(device: DeviceBackend, engine: MissionEngine,
                       video: Mapping[str, CameraFeed]) -> Teleop:
    return Teleop(device, engine, video_gate=_video_gate(video))


def _video_gate(video: Mapping[str, CameraFeed]) -> Callable[[], str]:
    """§5.9 那道闸。**任何一路在线就算看得见。**

    要求两路都在线,后相机掉一路就把整条狗锁死 —— 而人开着走的时候看的是
    前面。要求「前相机在线」又太死:装机时可以只接一路,或者两路的名字换了。
    所以判据是「至少有一路有画面」,拦的是**一张画面都没有**那种情况,那才是
    §5.9 说的盲开。
    """
    def gate() -> str:
        live = [name for name, feed in video.items() if feed.online]
        if live:
            return ""
        return ("一路画面都没有 —— 打开视频再开狗;"
                "网差到没有画面,那就走过去挪")
    return gate


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
    if (msg := _warn_if_data_in_slot(Path.cwd(), os.environ)):
        log.warning(msg)
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
    # 相机源现在就造好,但一条 ffmpeg 都不起 —— 真起是在有人打开画面的时候。
    # 挪到遥控构造之前:video_gate(§5.9)要闭包引用它。
    video = {name: CameraFeed(f"rtsp://{args.camera_host}:8554/{name}",
                              ffmpeg=args.ffmpeg)
             for name in CAMERAS}
    # 遥控要在循环线程里造:它一上来就往引擎上挂检查,还会起后台看门狗。
    teleop = bridge.call(lambda: _make_teleop(device, engine, video))

    mapping = MappingOrchestrator(procs, MappingConfig(
        bags_dir=Path(args.bags_dir), maps_dir=Path(args.maps_dir),
        params_template=Path(args.params_file)))

    ctx = AppContext(bridge=bridge, engine=engine, nav=nav, device=device,
                     maps=maps, procs=procs, teleop=teleop, mapping=mapping,
                     missions_dir=Path(args.missions_dir), runs_root=runs_root,
                     video=video, identity=who)
    # **装不装回传就看这一处。** 没配地址 => ``ctx.upload`` 留 ``None`` =>
    # 一条上传线程都不起,值守屏上那一格是「没装回传」而不是 0。
    if args.console_url:
        ctx.upload = build_pump(ctx, args.console_url,
                                token=os.environ.get(CONSOLE_TOKEN_ENV, ""))
    else:
        log.info("没配回传地址,这台狗按单机档跑 —— 证据全留在本机,"
                 "靠 U 盘导出(见《值守与告警》)。配 %s 就会开始回传。",
                 CONSOLE_URL_ENV)
    server = AppServer(ctx, host=args.host, port=args.port, pin=args.pin)
    # postcheck=False:重启后自检要等三个后端都连上(或者连不上也试过了)才
    # 跑——不然 control/bridges 两项问的正是这三个后端,后端还没连的时候问
    # 它们几乎必然假失败,一版好版本就这么被误判回滚。理由详见
    # ``AppServer.start()`` 里那段注释。
    server.start(postcheck=False)
    print(f"app 起来了:{server.url}")
    for what, opener in (("导航", nav.connect), ("旁路进程", device.connect),
                         ("地图桥", maps.connect)):
        try:
            bridge.call(opener, timeout_s=10.0)
        except Exception as exc:    # noqa: BLE001 - 连不上不影响 app 起来
            print(f"{what}没连上({type(exc).__name__}: {exc}),页面上会显示未连接")
    # 三个后端都试过了(连没连上不重要——后端没连上本身也是一种"这一版有
    # 问题"的信号,该走自检的判据决定留还是退)。**现在**才是重启后自检该
    # 跑的时刻。``_boot_postcheck()`` 内部已经把自己的异常都收住了,这里再
    # 套一层纯粹是保险——跟上面三个 ``connect()`` 一个待遇,绝不能有任何
    # 意外让它把 main() 带崩。
    try:
        server._boot_postcheck()
    except Exception as exc:    # noqa: BLE001 - 重启后自检不能有机会终止 main()
        print(f"重启后自检没跑起来({type(exc).__name__}: {exc}),留给下次开机兜底")

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
           "Response", "Stream", "build_pump", "json_response", "main",
           "shutdown"]
