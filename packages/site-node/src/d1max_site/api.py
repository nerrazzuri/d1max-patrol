"""站点 API 的最小版(W00c 设计决定四 A:标准库 ``ThreadingHTTPServer``)。

路由(除登录外都要 ``Authorization: Bearer <令牌>``):

- ``POST /api/login`` ``{"name","password"}`` → ``{"token","name","role"}``
- ``POST /api/logout``
- ``GET  /api/robots`` → ``{"robots":[…]}``
- ``GET  /api/robots/<id>`` → 视图 + 最近事件 + 最近命令
- ``POST /api/robots/<id>/goto`` ``{"target":MapPose,"max_speed_mps"?}``(优先级由站点定,W00c2b)
- ``POST /api/robots/<id>/abort`` ``{"task_id"}``
- ``POST /api/robots/<id>/patrol`` ``{"mission_id"}``:从当前任务包里起一趟(W00c2a)
- ``POST /api/bundles`` ``{"path"}``:导入站点主机上的一个任务包目录,并进路线库(W00c2a;按名字替换,
  回摘要 ``summary``)
- 路线库(2026-10-14,``routes.py``,**站点存的路线、排程是唯一真理源**;看 ``view``,改 ``manage``):
  ``GET /api/routes[?map_id=&map_version=]``、``GET /api/routes/<名>``(带逐点校验 ``problems``)、
  ``POST /api/routes/<名>``(整条存,带 ``base_revision``,新建 0)、``POST /api/routes/<名>/delete``;
  ``POST /api/route-timezone``;``GET /api/route-schedules``、``POST /api/route-schedules/<id>``、
  ``…/<id>/delete``;``POST /api/route-export``(导成任务包放在站点主机的 ``route-exports/``)
- ``GET/POST /api/robots/<id>/standby``、``POST /api/robots/<id>/standby/return``:待命点(W00c2b);
  GET 也带这台狗的原点(``homes``,W13a);``standby/return`` 可带 ``name`` 回指定的那一个;
  ``POST /api/robots/<id>/standby/here``:在当前位置设待命点(W13a,不动原点)
- ``POST /api/incidents``:外部事件(**不要登录,要签名**,见 ``incidents.py``;W00c2c)
- ``GET  /api/incidents``、``POST /api/intercepts``、``POST /api/zones``:
  事件账、拦截点、防区(W00c2c)
- ``GET/POST /api/arming/schedule``、``POST /api/arming/exceptions``、``…/{id}/remove``:布防时间表
  (W44 第二期);``GET/POST /api/duty-settings``:交班时间、多久没巡过
- ``GET/POST /api/accounts``、``POST /api/accounts/<name>``、``POST /api/me/password``、
  ``GET /api/audit``:账号、角色、审计(W00c3)
- ``GET  /api/schedule``:路线库的排程,每条下一轮何时、最近一次去向与结果(W00c2a)
- ``GET  /api/events`` SSE:第一帧全量快照,之后是派遣器的 status/event/ack/reconcile;
  订阅者跟不上时补发一帧全量快照(``lagged``),不悄悄丢

状态码:派遣条件不满足 409(带理由)、等回执超时 504、未登录 401、角色不够 403、锁定 429、
请求体超过 64 KB 413、JSON 坏 400。每条路由要什么权限见 ``permissions.py``;改动类请求(非 GET)
一律进审计(``audit.py``)。

**绑到非本机地址必须带 TLS**(站点服务证书):账号口令不能在局域网上明文走。手机信任
站点 CA 的事归 W00c4。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from d1max_contract import SCHEMA as CONTRACT_SCHEMA
from d1max_contract.dispatch import DispatchTimeout
from d1max_site.accounts import WATCH_PATHS, Accounts, AuthError, LockedOut
from d1max_site.audit import AuditLog
from d1max_site.ca import SAFE_ID
from d1max_site.dispatcher import Dispatcher, DispatchRefused, Unsupported
from d1max_site.loop import LoopThread
from d1max_site.permissions import (
    ABORT,
    ARM,
    DISPATCH,
    EXPORT,
    HANDLE_ALERTS,
    MAINTAIN,
    MANAGE,
    MANAGE_ACCOUNTS,
    PATROL,
    REVIEW,
    SCHEDULE,
    SET_MODE,
    TELEOP,
    VIEW,
    VIEW_AUDIT,
    allowed,
)
from d1max_site.priorities import MANUAL
from d1max_site.tlsserve import TlsHandlerMixin, TlsThreadingServer

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
SSE_HEARTBEAT_S = 15.0
#: 一个请求(含读请求头)多久没动静就断:慢速攻击(slowloris)不能一直占着线程。
REQUEST_TIMEOUT_S = 30.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ACCOUNT = re.compile(r"^/api/accounts/([A-Za-z0-9._-]{1,64})$")
_ACCOUNT_RM = re.compile(r"^/api/accounts/([A-Za-z0-9._-]{1,64})/remove$")
#: 防区多边形(商业化 B1c)、名单(时段 / 人员授权)。
#: 路线库:路线 ``/api/routes/<名>[/delete]``、排程 ``/api/route-schedules/<id>[/delete]``。
_ADHOC = re.compile(r"^/api/adhoc/([^/]{1,32})/cancel$")
_ROUTE = re.compile(r"^/api/routes/([^/]{1,64})(/delete)?$")
_ROUTE_SCHED = re.compile(r"^/api/route-schedules(?:/([^/]{1,64})(/delete)?)?$")
_AREAS = re.compile(r"^/api/maps/([^/]{1,64})/([^/]{1,32})/areas$")
_HIST = re.compile(r"^/api/layout/history(?:/(\d{1,12})/restore)?$")
_ARM_EX = re.compile(r"^/api/arming/exceptions/(\d{1,9})/remove$")
_AUTHZ = re.compile(r"^/api/lists/authorizations(?:/(\d{1,9})/(remove|end))?$")
#: 网页上登记 / 出开通码 / 吊销机器狗(商业化 B1b,管理员)。
_ADMIN_ROBOT = re.compile(r"^/api/admin/robots(?:/([^/]{1,64})/(code|revoke|name))?$")
#: W42:机器人轮询自己的绑定申请;站点上的人批准 / 拒绝。
_BIND_POLL = re.compile(r"^/api/bind/request/([0-9a-f]{1,32})$")
_ACCT_REQ = re.compile(r"^/api/account-requests(?:/([0-9a-f]{1,32})/(approve|reject))?$")
_ADMIN_BIND = re.compile(r"^/api/admin/bind(?:/([0-9a-f]{1,32})/(approve|reject))?$")
#: W42 §6:确认 / 不同意机器人上提的换 WiFi。
_ROBOT_WIFI = re.compile(r"^/api/robots/([^/]{1,64})/wifi$")
#: ``GET /api/alerts?all=1&limit=`` 的上限。
ALERTS_LIMIT_MAX = 2000
#: 告警键 ``robot/kind#seq`` 里有 ``/`` 与 ``#``:客户端整个键编码成一段(``%2F``、``%23``)。
_ALERT = re.compile(r"^/api/alerts/([^/]{1,256})/(ack|resolve|shelve|unshelve)$")
#: W00c5c:``/api/robots/<id>/teleop``(WebSocket)与 ``/api/robots/<id>/halt``。
_TELEOP = re.compile(r"^/api/robots/([^/]{1,64})/"
                     r"(teleop|halt|resume|supervise|relocalize|home/here|deter|service)$")
#: W00c5b:``/api/robots/<id>/video/<front|back|health>``。
_VIDEO = re.compile(r"^/api/robots/([^/]{1,64})/video/([a-z]{1,16})$")
#: W00c5d:运行记录与导出。
#: 固定摄像头的实时画面(W19)。
_CAM = re.compile(r"^/api/cameras/([A-Za-z0-9._-]{1,64})/(live|health)$")
#: 分级驱离(W22):``/api/deterrence/<狗>/level``、``/release``。
_DETER = re.compile(r"^/api/deterrence/([^/]{1,64})/(level|release|start)$")
_RANGE = re.compile(r"^bytes=(\d{0,18})-(\d{0,18})$")


def byte_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """``Range`` 头里要的那一段 ``(头, 尾)``(都含)。没带、不是 ``bytes=``、一次要好几段:``None``
    (整份发,规范允许不理);写得对但够不着(头在文件外、``bytes=-0``、空文件):``False``(416)。"""
    m = _RANGE.match((header or "").strip())
    if m is None or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):                                  # bytes=-N:最后 N 个字节
        n = int(m.group(2))
        return False if n == 0 or size == 0 else (max(0, size - n), size - 1)
    start = int(m.group(1))
    if start >= size:
        return False
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    return None if end < start else (start, end)


#: 连续录像(W18):一段 ``/api/recordings/<id>/video``、标留着 ``/api/recordings/<id>/keep``。
_REC = re.compile(r"^/api/recordings/(\d{1,12})/(video|keep)$")
#: 充电桩(W13):``/api/chargers/<robot_id>``。
_CHARGER = re.compile(r"^/api/chargers/([^/]{1,128})$")
#: 看证据的审计(W30):同一个人看同一样东西多久记一次(毫秒)。
VIEW_AUDIT_EVERY_MS = 600_000
_RUN = re.compile(r"^/api/runs/(\d{1,12})(?:/(judge|photos|review|keep)(?:/([^/]{1,300}))?)?$")
_EXPORT = re.compile(r"^/api/exports/([^/]{1,128})$")
#: W00c5d 第二部分:给狗下发图、录包、重建。
_MAPCMD = re.compile(r"^/api/robots/([^/]{1,64})/(map|mapping|map_build|outbox_retry)$")
#: W00c6h:建好的图的栅格预览(坐标换算 + 待命点;PNG);地图显示改进:平面图 ``plan.png``。
_MAPVIEW = re.compile(
    r"^/api/maps/([^/]{1,64})/([^/]{1,32})/(preview|preview\.png|plan\.png)$")
#: W10:``/api/maps/<id>/<版本>/zones``(看、整份改)与 ``…/zones/confirm``(发布前人工确认);
#: R2:``…/zones/proposal``(丢掉从上一版带过来、待核的建议)。
_ZONES = re.compile(r"^/api/maps/([^/]{1,64})/([^/]{1,32})/zones(/confirm|/proposal)?$")
#: W48:``/api/maps/<id>/<版本>/route_net``(看、整份改)。
_ROUTENET = re.compile(r"^/api/maps/([^/]{1,64})/([^/]{1,32})/route_net$")
#: W00c6g:狗上建图进程日志的列表、某一个的尾巴(``?bytes=``)。
_LOGS = re.compile(r"^/api/robots/([^/]{1,64})/logs(?:/([^/]{1,64}))?$")
_OPS = re.compile(r"^/api/robots/([^/]{1,64})/ops/([a-z_-]{1,32})$")      # W55 远程运维
_LOG_NAME = re.compile(r"[a-z0-9_.-]{1,48}")
#: W00c6h:录包时的轨迹(``?since=`` 上次拿到的点数)。
#: 录包轨迹(W00c6h)、建图预览(W09f):都是管理员经只读命令现取现给
_TRAIL = re.compile(r"^/api/robots/([^/]{1,64})/mapping/(trail|preview)$")
#: W00c5d 第三部分:给狗装、切、退版本。
_RELCMD = re.compile(r"^/api/robots/([^/]{1,64})/release$")
#: W44:某台机器人一段时间的位置轨迹。
_TRACK = re.compile(r"^/api/robots/([^/]{1,64})/track$")
#: 保安的临时路线最多几个点(W44)。
TEMP_ROUTE_MAX = 20
#: 轨迹一次最多问多长(毫秒)。
TRACK_SPAN_MAX_MS = 86_400_000
_ROBOT = re.compile(
    r"^/api/robots/([^/]+)"
    r"(?:/(goto|abort|patrol|patrol-plan|standby|standby/return|standby/here|intercept/here))?$")


def _cam_frames(v: Any) -> Iterator[bytes]:
    """固定摄像头的画面(``CctvView.frames`` 外面包一层):第一帧之前出的错换成 :class:`HttpError`
    (504 没出画面、502 拉不下来,带摄像头报的原因);之后断了就结束。"""
    frames = v.frames()
    first = True
    try:
        while True:
            try:
                f = next(frames)
            except TimeoutError as exc:
                raise HttpError(504, f"摄像头未出画面:{v.error or exc}") from exc
            except StopIteration:
                if first:
                    raise HttpError(502, f"摄像头画面拉不下来:{v.error}") from None
                return
            first = False
            yield f
    finally:
        frames.close()


class HttpError(Exception):
    def __init__(self, status: int, message: str, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        #: 跟 ``error`` 一起回给客户端的其他字段(比如标原点登记没成时狗上的位置)。
        self.extra = extra or {}


def check_exposure(host: str, tls: tuple[Path, Path] | None) -> None:
    if host not in LOCAL_HOSTS and tls is None:
        raise SystemExit(f"拒绝启动:站点 API 绑到 {host} 会把账号密码明文暴露在网络上。"
                         "加 --tls-cert/--tls-key(站点服务证书),或只绑 127.0.0.1。")


async def _sync(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """在事件循环线程上跑一个同步函数:告警台与派遣器的内存状态只在那条线程上改、读。"""
    return fn(*args, **kw)


class SiteApi:
    def __init__(self, *, host: str, port: int, loop: LoopThread, dispatcher: Dispatcher,
                 accounts: Accounts, tls: tuple[Path, Path] | None = None,
                 scheduler: Any = None, standby: Any = None, incidents: Any = None,
                 alerts: Any = None, video: Any = None, teleop: Any = None,
                 runs: Any = None, backup: Any = None, maps: Any = None,
                 releases: Any = None, supervision: Any = None, zones: Any = None,
                 now_ms: Callable[[], int] | None = None,
                 request_timeout_s: float = REQUEST_TIMEOUT_S,
                 sse_recheck_s: float = SSE_HEARTBEAT_S) -> None:
        check_exposure(host, tls)
        #: SSE 长连多久重查一次令牌:注销、过期之后流要断。
        self.sse_recheck_s = sse_recheck_s
        self.loop = loop
        self.dispatcher = dispatcher
        self.accounts = accounts
        self.scheduler = scheduler
        self.standby = standby
        self.incidents = incidents
        self.alerts = alerts
        self.video = video
        self.teleop = teleop
        #: W00c5d:运行记录台(看、判读、复核、导出)与站点自己的备份。
        self.runs = runs
        self.backup = backup
        #: W00c5d 第二部分:地图目录。
        self.maps = maps
        #: W00c5d 第三部分:发布目录。
        self.releases = releases
        #: W18:连续录像(站点主程序接上)。
        self.recordings: Any = None
        #: 个人数据(W30,``privacy.PrivacyDesk``):运行记录标「留着」。
        self.privacy: Any = None
        #: 自动回充(W13,``charging.ChargeDesk``):充电桩登记、回充进度。
        self.charge: Any = None
        #: 谁看过什么(W30,PDPA):``(账号, 东西) → 上次记审计的时刻``;
        #: 10 分钟内同一个人看同一样只记一次。
        self._viewed: dict[tuple[str, str], int] = {}
        self._viewed_lock = threading.Lock()
        #: W19:固定摄像头(站点主程序接上);实时画面每台一个 ``CctvView``,观众共用。
        self.cctv: Any = None
        #: 布防模式(W20,``modes.ArmingDesk``)。站点主程序接上;没接的站点 /api/mode 回 404。
        self.arming: Any = None
        #: 分级驱离(W22,``deterrence.DeterrenceDesk``)。
        self.deterrence: Any = None
        #: 天气(W29,``weather.WeatherDesk``)。没接的站点 /api/weather 回 404。
        self.weather: Any = None
        #: P1 推到手机(商业化 A6,``push.PushDesk``)。没接的站点 /api/push/devices 回 404。
        self.push: Any = None
        #: 运营可视化:规划路径 / 轨迹 / 被挡(``runviz.RunViz``)、
        #: 定位历史(``loc_history.LocHistory``)。
        self.runviz: Any = None
        self.loc_history: Any = None
        #: 体检、诊断(商业化 A1,``health.HealthDesk``)。没接的站点 /healthz 只回好。
        self.health: Any = None
        #: 站点目录(商业化 A4:狗领证书包从 ``<站点目录>/ca/issued/`` 拿)。站点主程序接上。
        self.home: Path | None = None
        #: W42 绑定申请台(``binding.BindDesk``;有站点目录时第一次用现建)。
        self.bind: Any = None
        #: W53 临时巡逻台(``adhoc.AdhocDesk``);站点主程序接上,没接的第一次用时现建。
        self.adhoc: Any = None
        #: W52 账号申请台(``account_requests.AccountRequests``);第一次用时现建。
        self.account_requests: Any = None
        #: W42:解绑后重启 broker(让它重读吊销名单);站点主程序接上,回做没做到。
        self.broker_restart: Callable[[], bool] | None = None
        #: 网页值班台的静态文件目录(商业化 B1;None = 包里带的 web/,测试换成别的)。
        self.web_dir: Path | None = None
        #: 给人看的站点名(商业化 B1,网页顶栏;站点主程序从 site.json 接)。
        self.site_name = ""
        #: 防区多边形、名单(商业化 B1c;站点主程序接上)。
        self.areas: Any = None
        self.authz: Any = None
        #: W44:位置轨迹(录像页那一刻在哪、地图页多久没巡过;站点主程序接上)。
        self.pose_track: Any = None
        #: W44:按时刻从录像抓一帧(报警记录的抓拍);第一次用时按录像库现建。
        self.frames: Any = None
        #: W55 远程运维(站点主程序接上):特权执行器(``privops``;没装是 None)、证书自动续签、
        #: App 与网页的错误记录、App 安装包分发、站点 VPN。
        self.priv: Any = None
        self.cert_renewer: Any = None
        self.client_errors: Any = None
        self.apps: Any = None
        self.vpn: Any = None
        #: 有「待生效」的 VPN 配置就装上(``Server.vpn_sync``,main 接上;外审 W55-2 I1)。
        #: 回空串 = 好了。
        self.vpn_sync: Callable[[], str] = lambda: "站点未接上 VPN 同步"
        self._cctv_views: dict[str, Any] = {}
        self._cctv_lock = threading.Lock()
        #: 造 ``CctvView`` 的(测试换成假 ffmpeg 的)。
        self.cctv_view_factory: Callable[[Any], Any] | None = None
        self._now = now_ms or (lambda: int(__import__("time").time() * 1000))
        #: W44:交班留言(报警记录页)。
        from d1max_site.shift_notes import ShiftNotes
        self.shift_notes = (ShiftNotes(dispatcher.db, now_ms=self._now)
                            if dispatcher is not None else None)
        #: W44 第二期:站点的零散设置(交班时间、多久没巡过)。
        from d1max_site.site_settings import SiteSettings
        self.settings = SiteSettings(dispatcher.db) if dispatcher is not None else None
        #: W44 第四期:地图的显示设置(摆正、底图、区域名、特殊地面)。
        from d1max_site.map_display import MapDisplay
        self.map_display = MapDisplay(dispatcher.db, now_ms=self._now) \
            if dispatcher is not None else None
        #: W44 第四期:等充完电、空闲时再换的升级。
        from d1max_site.release_queue import ReleaseQueue

        def _activate_payload(name: str) -> dict[str, Any]:
            from d1max_site.catalog import active_bundle_schema
            cur = active_bundle_schema(dispatcher.db)
            return {"name": name, **({"mission_schema": cur[1]} if cur is not None else {})}
        self.release_queue = (ReleaseQueue(dispatcher.db, dispatcher, now_ms=self._now,
                                           payload=_activate_payload)
                              if dispatcher is not None else None)
        #: W44 第三期:「布置」的历史版本。
        from d1max_site.layout_history import LayoutHistory
        self.layout_history = (LayoutHistory(dispatcher.db, now_ms=self._now)
                               if dispatcher is not None else None)
        #: W10:禁行区、限速区。有地图目录就有(测试台子不传也现建一个,派遣器共用这一份)。
        if zones is None and maps is not None:
            from d1max_site.nav_zones import NavZones
            zones = getattr(dispatcher, "zones", None) or NavZones(dispatcher.db, now_ms=self._now)
        #: 路线库(2026-10-14):站点存的路线、排程。校验点用地图目录,没地图目录(测试台子)就不查可达。
        from d1max_site.routes import RouteStore
        self.routes = RouteStore(dispatcher.db, maps=maps, now_ms=self._now) \
            if dispatcher is not None else None
        if self.routes is not None and maps is not None and zones is not None:
            # W44 第三期:路线存之前逐段查走不走得通(跟机器人同一份规划,带禁行区)
            from d1max_site.intercept_reach import InterceptReach
            self.routes.reach = InterceptReach(dispatcher.db, maps, zones)
        self.zones = zones
        if zones is not None and getattr(dispatcher, "zones", None) is None:
            dispatcher.zones = zones
        #: W48:路网。跟区域一样有地图目录就有,派遣器共用这一份(补发)。
        from d1max_site.route_nets import RouteNets
        self.route_nets: Any = None
        if dispatcher is not None and maps is not None:
            self.route_nets = getattr(dispatcher, "route_nets", None) \
                or RouteNets(dispatcher.db, now_ms=self._now)
            if getattr(dispatcher, "route_nets", None) is None:
                dispatcher.route_nets = self.route_nets
        #: 监护心跳(W00c6i):站点主程序会传同一个给待命点管理器;不传就自己建一个(测试)。
        from d1max_site.supervision import SupervisionDesk
        self.supervision = supervision if supervision is not None else \
            SupervisionDesk(dispatcher, loop, now_ms=self._now)
        self.audit = AuditLog(dispatcher.db, now_ms=self._now) if dispatcher is not None else None
        #: 基站改正数据转发(W09e,``RtcmRelay``);没配是 None。主程序接上。
        self.rtk: Any = None
        self._stopping = threading.Event()
        api = self

        class Handler(_Handler):
            site = api
            timeout = request_timeout_s          # StreamRequestHandler:套接字超时

        ctx = None
        self._scheme = "http"
        if tls is not None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(tls[0]), str(tls[1]))
            self._scheme = "https"
        # TLS 握手放在每条连接自己的线程里、带超时(W00c5d):包监听套接字的老办法在唯一那条
        # 接连接的线程里握手,一个不发 ClientHello 的连接就能让手机全都连不上。
        self.httpd = TlsThreadingServer((host, port), Handler, ctx=ctx)
        self._thread: threading.Thread | None = None

    def drop_cctv_view(self, name: str) -> None:
        """这台摄像头删了、改了配置(W19 复查):正在看的旧画面当场关,不等下一次有人打开。删了的再也
        打不开;改了的下一次打开按新配置起。"""
        with self._cctv_lock:
            v = self._cctv_views.pop(name, None)
        if v is not None:
            v.close()

    def close_cctv_views(self) -> None:
        """站点收尾:摄像头画面的 ffmpeg 全收掉(有观众的、在空闲等待里的都算)。"""
        with self._cctv_lock:
            views, self._cctv_views = list(self._cctv_views.values()), {}
        for v in views:
            v.close()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"{self._scheme}://{host}:{port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="d1max-site-http",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            # 没 start 过就 shutdown() 会永远等一个从没跑起来的 serve_forever。
            self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(10)

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    # ------------------------------------------------------------ 业务

    def snapshot(self) -> dict[str, Any]:
        return {"kind": "snapshot", "robots": self.dispatcher.robots_view()}

    def dispatch(self, fn: Callable[[], Any]) -> Any:
        try:
            return self.loop.call(fn, timeout_s=self.dispatcher.ack_timeout_s + 5)
        except Unsupported as exc:
            raise HttpError(409, str(exc), extra={"unsupported": True}) from exc
        except DispatchRefused as exc:
            from d1max_site.refusals import extra_for
            raise HttpError(409, str(exc), extra=extra_for(str(exc))) from exc   # W53:带代号
        except (DispatchTimeout, TimeoutError) as exc:
            raise HttpError(504, f"等机器人的回执超时: {exc}") from exc


class _Handler(TlsHandlerMixin):
    site: SiteApi
    protocol_version = "HTTP/1.1"
    server_version = "d1max-site"

    def log_message(self, fmt: str, *args: Any) -> None:     # 走 logging,别打 stderr
        log.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ 输入输出

    def _write_json(self, status: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in getattr(self, "_extra_headers", ()):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise HttpError(400, "Content-Length 不是数") from exc
        if n < 0:
            raise HttpError(400, "Content-Length 不能是负的")
        if n > MAX_BODY:
            raise HttpError(413, f"请求体超过 {MAX_BODY} 字节")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(400, f"JSON 无法解析: {exc}") from exc
        if not isinstance(d, dict):
            raise HttpError(400, "请求体要是 JSON 对象")
        return d

    def _token(self) -> str | None:
        """``Authorization: Bearer``(手机、命令行);没有就看网页的 cookie(商业化 B1)。"""
        h = self.headers.get("Authorization") or ""
        if h.startswith("Bearer "):
            self._via_cookie = False
            return h[7:].strip()
        from d1max_site.webui import cookie_token
        tok = cookie_token(self.headers.get("Cookie"))
        self._via_cookie = tok is not None
        return tok

    def _csrf_ok(self) -> None:
        """靠 cookie 认的、要改东西的请求,得带 ``X-D1Max-Web`` 头(别的网站加不了)。"""
        from d1max_site.webui import CSRF_HEADER
        if getattr(self, "_via_cookie", False) and self._method != "GET" \
                and self.headers.get(CSRF_HEADER) != "1":
            raise HttpError(403, "网页请求少了防伪造的头")

    def _user(self) -> str:
        name = self.site.accounts.check(self._token())
        if name is None:
            raise HttpError(401, "未登录或登录已过期")
        self._csrf_ok()
        self._actor = name
        return name

    def _need(self, user, perm: str) -> None:
        if not allowed(getattr(user, "role", ""), perm):
            raise HttpError(403, f"{getattr(user, 'role', '?')} 没有 {perm} 权限")

    def _send_json(self, status: int, body: Any) -> None:
        self._status = status
        self._resp = body if isinstance(body, dict) else {}
        self._write_json(status, body)

    def _handle(self, method: str) -> None:
        self._actor, self._status, self._resp = "", 0, {}
        self._audit_target, self._audit_detail = "", {}
        self._method, self._via_cookie, self._extra_headers = method, False, []
        #: 这一次请求不进审计(W00c6i:续着的监护心跳每秒一条,只记开始与结束)。
        self._skip_audit = False
        path = self.path.split("?", 1)[0]
        try:
            self._route(method, path)
        finally:
            # 未登录的乱请求(401/413/400,不是登录也不是事件回调)不进审计:谁都能发,
            # 记下来只会把表撑满(内部评审)。登录失败、事件回调照记。
            if method != "GET" and self.site.audit is not None and self._actor \
                    and not self._skip_audit:
                detail = {k: self._resp[k] for k in ("command_id", "task_id", "error", "outcome")
                          if k in self._resp} | self._audit_detail
                try:
                    self.site.audit.record(actor=self._actor, action=f"{method} {path}",
                                           target=self._audit_target, status=self._status,
                                           detail=detail, remote=self.client_address[0])
                except Exception:
                    log.exception("审计写不进去")

    def _route(self, method: str, path: str) -> None:
        try:
            if method == "GET" and path == "/healthz":
                return self._healthz()             # 商业化 A1:探活不用登录,只回好不好
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)          # 商业化 B1:网页值班台(不用登录;数据走 /api)
            if method == "POST" and path == "/api/login":
                return self._login()
            if method == "POST" and path == "/api/enroll/claim":
                return self._enroll_claim()          # 商业化 A4:狗领证书包,开通码就是凭证
            if path.startswith("/api/bind/"):
                return self._bind_robot(method, path)  # W42:机器人挂绑定申请、轮询、问证书还算不算
            if method == "POST" and path == "/api/account-requests":
                return self._account_apply()          # W52:App 上申请账号(不用登录)
            if method == "POST" and path == "/api/incidents":
                return self._incident_in()          # 摄像头不登录:验签
            if method == "POST" and path == "/api/logout":
                # A 阶段外审 I2:令牌闲置过期了也能退出(连同这次登录登记的手机一起不推);
                # 拿得出令牌就是那部手机,不用它还有效
                tok = self._token()
                self._csrf_ok()
                self.site.accounts.logout(tok or "")
                from d1max_site.webui import clear_cookie
                self._extra_headers.append(("Set-Cookie", clear_cookie()))  # B1:清 cookie
                return self._send_json(200, {"ok": True})
            user = self._user()
            if getattr(user, "scope", "") == "watch" and (method, path) not in WATCH_PATHS:
                raise HttpError(403, "值守令牌只能看告警")
            if method == "POST" and path == "/api/watch/token":
                # W17:手机后台值守领一个只能看告警的长期令牌(普通会话 12 小时就到期)
                self._need(user, VIEW)
                token, expires = self.site.accounts.issue_watch_token(str(user))
                return self._send_json(200, {"token": token, "expires_at": expires})
            if method == "GET" and path == "/api/me":
                # 商业化 B1:网页刷新以后靠 cookie 认人,要知道是谁、什么角色、站点叫什么
                from d1max_contract.compat import MIN_APP_API_LEVEL, SITE_API_LEVEL
                self._need(user, VIEW)
                from d1max_site.setup import read_guide
                return self._send_json(200, {
                    "name": str(user), "role": getattr(user, "role", ""),
                    "display_name": self.site.accounts.display_name(str(user)),
                    "site_name": self.site.site_name, "api_level": SITE_API_LEVEL,
                    "min_app_level": MIN_APP_API_LEVEL,
                    # W53:上手向导看过哪一版、开站清单收没收起来
                    "guide": read_guide(self.site.accounts, str(user))})
            if method == "POST" and path == "/api/me/guide":
                from d1max_site.setup import GuideError, write_guide
                self._need(user, VIEW)
                try:
                    return self._send_json(200, write_guide(self.site.accounts, str(user),
                                                            self._body()))
                except GuideError as exc:
                    raise HttpError(400, str(exc)) from exc
            if method == "GET" and path == "/api/setup":
                from d1max_site.setup import read_guide, setup_view
                self._need(user, MANAGE)
                skip = set(read_guide(self.site.accounts, str(user))["skip"])
                disp = self.site.dispatcher
                return self._send_json(200, self.site.loop.call(lambda: _sync(
                    setup_view, disp.db, disp, skip=skip)))
            if path == "/api/map-wizard":
                return self._map_wizard(method, user)       # W54 建图向导
            if method == "GET" and path == "/api/pairing":
                self._need(user, VIEW)
                return self._pairing()
            if method == "POST" and path == "/api/me/password":
                return self._change_own_password(user)
            m = _AREAS.match(path)
            if m is not None:
                return self._areas(method, unquote(m.group(1)), unquote(m.group(2)), user)
            m = _AUTHZ.match(path)
            if m is not None:
                return self._authz(method, m.group(1), user, m.group(2))
            m = _ADMIN_ROBOT.match(path)
            if m is not None:
                self._need(user, MANAGE)
                return self._admin_robot(method, m.group(1), m.group(2), user)
            m = _ADMIN_BIND.match(path)
            if m is not None:
                self._need(user, MANAGE)
                return self._admin_bind(method, m.group(1), m.group(2), user)
            m = _ACCT_REQ.match(path)
            if m is not None:
                self._need(user, MANAGE_ACCOUNTS)
                return self._account_requests(method, m.group(1), m.group(2), user)
            m = _ROBOT_WIFI.match(path)
            if m is not None and method == "POST":
                self._need(user, MANAGE)
                return self._robot_wifi(unquote(m.group(1)), user)
            if path == "/api/accounts" or _ACCOUNT.match(path) or _ACCOUNT_RM.match(path):
                self._need(user, MANAGE_ACCOUNTS)
                return self._accounts(method, path, user)
            if method == "GET" and path == "/api/audit":
                self._need(user, VIEW_AUDIT)
                q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                actor = (q.get("actor") or [None])[0]
                return self._send_json(200, {"audit": self.site.audit.list(actor=actor)})
            if method == "GET" and path == "/api/robots":
                self._need(user, VIEW)
                return self._send_json(200, {"robots": self.site.dispatcher.robots_view()})
            if method == "GET" and path == "/api/video/ws":
                return self._video_ws(user)               # W51:网页的多路画面走一条 WebSocket
            if method == "GET" and path == "/api/robots/overview":
                # W44 第二期:机器人卡片(今天几趟、毛病、电量)。
                # W60:经理也看(以看为主;改动的接口照旧要 manage)
                if not allowed(getattr(user, "role", ""), SCHEDULE):
                    self._need(user, MANAGE)
                from d1max_site.robot_overview import overview
                from d1max_site.sitetime import site_tz
                disp = self.site.dispatcher
                return self._send_json(200, {"robots": overview(
                    disp, disp.db, now_ms=self.site._now(), tz=site_tz(disp.db))})
            if method == "GET" and path == "/api/events":
                self._need(user, VIEW)
                return self._sse()
            if method == "GET" and path == "/api/watch/events":
                # W17 外审:只有告警的流(后台值守用;值守令牌只能走这一条)
                self._need(user, VIEW)
                return self._sse(alerts_only=True)
            if method == "GET" and path == "/api/rtk":
                # 自建基站的改正数据(W09e):源连没连上、多久没来数据、收了哪些报文、基站坐标
                self._need(user, VIEW)
                rtk = self.site.rtk
                return self._send_json(200, {"configured": False} if rtk is None
                                       else {"configured": True, **rtk.stats()})
            if method == "GET" and path == "/api/schedule":
                self._need(user, VIEW)
                if self.site.scheduler is None:
                    raise HttpError(404, "此站点未启用排程")
                return self._send_json(200, self.site.scheduler.view())
            if method == "GET" and path == "/api/patrols/today":
                self._need(user, VIEW)
                from d1max_site.today import today_view
                desk = self._adhoc()
                return self._send_json(200, self.site.loop.call(lambda: _sync(
                    today_view, self.site.dispatcher.db, now_ms=self.site._now(), adhoc=desk)))
            m = _ADHOC.match(path)
            if m is not None:
                return self._adhoc_cancel(method, unquote(m.group(1)), user)
            if path == "/api/shift-notes":
                return self._shift_notes(method, user)
            if path == "/api/duty-settings":
                return self._duty_settings(method, user)
            m = _HIST.match(path)
            if m is not None:
                return self._layout_history(method, m.group(1), user)
            m = _TRACK.match(path)
            if m is not None or path == "/api/coverage":
                return self._track(method, unquote(m.group(1)) if m else None, user)
            if path == "/api/alerts" or path == "/api/watch/summary" or _ALERT.match(path):
                return self._alerts(method, path, user)
            if method == "POST" and path == "/api/bundles":
                self._need(user, MANAGE)
                return self._import_bundle(user)
            if path in ("/api/routes", "/api/route-timezone", "/api/route-export") \
                    or _ROUTE.match(path) or _ROUTE_SCHED.match(path):
                return self._routes(method, path, user)
            if path == "/api/deterrence" or _DETER.match(path):
                return self._deterrence(method, path, user)
            if path in ("/api/mode", "/api/mode/zones"):
                return self._mode(method, path, user)
            if path in ("/api/arming/schedule", "/api/arming/exceptions") or _ARM_EX.match(path):
                return self._arming_schedule(method, path, user)
            if path == "/api/weather":
                return self._weather(method, user)
            if method == "GET" and path == "/api/versions":
                self._need(user, VIEW)
                from d1max_site.compat import versions
                got = self.site.loop.call(lambda: _sync(versions, self.site.dispatcher))
                if allowed(getattr(user, "role", ""), MANAGE):
                    # C-B4:谁在用哪一版 App 登录着(带账号名,只给管理员)
                    got["clients"] = self.site.accounts.clients()
                return self._send_json(200, got)
            if path in ("/api/health", "/api/metrics", "/api/support-bundle"):
                return self._health(method, path, user)
            if path in ("/api/push/devices", "/api/push/devices/remove"):
                return self._push_devices(method, path, user)
            if path == "/api/chargers" or _CHARGER.match(path):
                return self._chargers(method, path, user)
            if path in ("/api/incidents", "/api/intercepts", "/api/zones"):
                self._need(user, VIEW if method == "GET" else MANAGE)
                return self._incident_admin(method, path)
            if path == "/api/runs" or path.startswith(("/api/runs/", "/api/exports")):
                return self._runs(method, path, user)
            if path in ("/api/reports/daily", "/api/reports/summary"):
                return self._report(method, user, path)
            if path in ("/api/recordings", "/api/recordings/frame") or _REC.match(path):
                return self._recordings(method, path, user)
            from d1max_site import api_runviz
            if api_runviz.handles(path):               # 运营可视化:路径轨迹、定位历史、摄像头登记
                return api_runviz.route(self, method, path, user)
            if path == "/api/cameras" or _CAM.match(path):
                return self._cameras(method, path, user)
            if path == "/api/admin/maps" or path.startswith("/api/admin/maps/"):
                return self._maps_admin(method, path, user)     # 地图管理页
            if path == "/api/maps" or _MAPCMD.match(path) or _MAPVIEW.match(path) \
                    or _ZONES.match(path) or _ROUTENET.match(path):
                return self._maps(method, path, user)
            if method == "POST" and path == "/api/releases/upload":
                return self._release_upload(user)
            if path == "/api/releases" or _RELCMD.match(path):
                return self._releases(method, path, user)
            m = _TELEOP.match(path)
            if m is not None:
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "机器人不存在")
                if m.group(2) == "halt" and method == "POST":
                    return self._halt(robot_id, user)
                if m.group(2) == "resume" and method == "POST":
                    return self._resume(robot_id, user)
                if m.group(2) == "service" and method == "POST":
                    return self._service(robot_id, user)
                if m.group(2) == "supervise" and method == "POST":
                    return self._supervise(robot_id, user)
                if m.group(2) == "relocalize" and method == "POST":
                    return self._relocalize(robot_id, user)
                if m.group(2) == "deter" and method == "POST":
                    return self._deter(robot_id, user)
                if m.group(2) == "home/here" and method == "POST":
                    return self._mark_home(robot_id, user)
                if m.group(2) == "teleop" and method == "GET":
                    return self._teleop_ws(robot_id, user)
                raise HttpError(404, f"没有 {method} {path}")
            m = _TRAIL.match(path)
            if m is not None and method == "GET":
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "机器人不存在")
                return self._mapping_read(robot_id, m.group(2), user)
            m = _OPS.match(path)
            if m is not None:
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "机器人不存在")
                return self._robot_ops(method, robot_id, m.group(2), user)
            if path == "/api/client-errors":
                return self._client_errors(method, user)
            if path.startswith("/api/system/"):
                return self._system(method, path, user)
            if path == "/api/app-update" or path.startswith("/api/app-update/"):
                return self._app_update(method, path, user)
            m = _LOGS.match(path)
            if m is not None and method == "GET":
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "机器人不存在")
                return self._proc_logs(robot_id, m.group(2), user)
            m = _VIDEO.match(path)
            if m is not None and method == "GET":
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "机器人不存在")
                return self._video(robot_id, m.group(2), user)
            if path == "/api/robots/nearest":
                return self._nearest(method, user)
            m = _ROBOT.match(path)
            if m is not None:
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "机器人不存在")
                return self._robot(method, robot_id, m.group(2), user)
            raise HttpError(404, f"没有 {method} {path}")
        except HttpError as exc:
            self.close_connection = True          # 请求体可能没读完(413),连接不能复用
            self._send_json(exc.status, {**exc.extra, "error": exc.message})
        except Exception:                                    # 最后一道:别把栈回给客户端
            log.exception("站点 API 处理 %s %s 炸了", method, self.path)
            self._send_json(500, {"error": "站点内部错误"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    # ------------------------------------------------------------ 路由

    def _alerts(self, method: str, path: str, user) -> None:
        """告警与值守(W00c5a)。看:``view``;确认、解决:``handle_alerts``,人取登录账号。"""
        if self.site.alerts is None:
            raise HttpError(404, "此站点未启用告警")
        from d1max_site.alerts import AlertNotFound
        if method == "GET" and path == "/api/alerts":
            self._need(user, VIEW)
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if q.get("all") != ["1"]:
                rows = self.site.loop.call(lambda: _sync(self.site.alerts.open))
                return self._send_json(200, {"alerts": rows})
            # 交接班那一张:最近的在前,**截多少条写在回包里**(不许悄悄截)。
            raw = (q.get("limit") or ["200"])[0]
            if not raw.isdigit() or not 1 <= int(raw) <= ALERTS_LIMIT_MAX:
                raise HttpError(400, f"limit 要是 1–{ALERTS_LIMIT_MAX} 的整数")
            n = int(raw)
            rows = self.site.loop.call(lambda: _sync(self.site.alerts.recent, n))
            return self._send_json(200, {"alerts": rows, "limit": n,
                                         "truncated": len(rows) >= n})
        if method == "GET" and path == "/api/watch/summary":
            self._need(user, VIEW)
            from d1max_site.watch import watch_summary
            return self._send_json(200, self.site.loop.call(lambda: _sync(
                watch_summary, self.site.dispatcher, self.site.alerts,
                now_ms=self.site._now(), scheduler=self.site.scheduler,
                backup=self.site.backup, push=self.site.push)))
        m = _ALERT.match(path)
        if method != "POST" or m is None:
            raise HttpError(404, f"没有 {method} {path}")
        self._need(user, HANDLE_ALERTS)
        key = unquote(m.group(1))
        self._audit_target = key[:256]
        d = self._body()                                # 请求体里的 who 不信,人取登录账号
        try:
            if m.group(2) == "shelve":
                # B1:P2 搁置(ISA-18.2):要原因、到点自动回来
                until = d.get("until_ms")
                if not isinstance(until, int) or isinstance(until, bool):
                    raise HttpError(400, "要 until_ms(整数毫秒)")
                reason = str(d.get("reason") or "")
                self._audit_detail = {"until_ms": until, "reason": reason[:200]}
                a = self.site.loop.call(lambda: _sync(
                    self.site.alerts.shelve, key, who=str(user), until_ms=until, reason=reason))
            elif m.group(2) == "unshelve":
                a = self.site.loop.call(lambda: _sync(self.site.alerts.unshelve, key))
            elif m.group(2) == "ack":
                a = self.site.loop.call(lambda: _sync(self.site.alerts.ack, key, who=str(user)))
            else:
                # W44:结论(确认有人 / 误报 / 处理好了);不给 = 空(老客户端照旧能用),别的值 400
                verdict = d.get("verdict", "")
                if not isinstance(verdict, str):
                    raise HttpError(400, "verdict 要是字符串")
                self._audit_detail = {"verdict": verdict}
                a = self.site.loop.call(
                    lambda: _sync(self.site.alerts.resolve, key, who=str(user), verdict=verdict))
        except AlertNotFound as exc:
            raise HttpError(404, "没有此条告警") from exc
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        return self._send_json(200, {"alert": a.to_wire()})

    def _track(self, method: str, robot_id: str | None, user) -> None:
        """位置轨迹(W44):``GET /api/robots/{id}/track?since=&until=``(最多 24 小时)、
        ``GET /api/coverage?map_id=&map_version=&since=``(每格最后一次有机器人到过的时刻)。"""
        if method != "GET":
            raise HttpError(405, "只支持 GET")
        self._need(user, VIEW)
        pt = self.site.pose_track
        if pt is None:
            raise HttpError(404, "此站点未启用位置轨迹")
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

        def num(k: str) -> int:
            v = (q.get(k) or [""])[0]
            if not v.isdigit():
                raise HttpError(400, f"{k} 要是非负整数(毫秒)")
            return int(v)
        if robot_id is not None:
            since, until = num("since"), num("until")
            if until <= since or until - since > TRACK_SPAN_MAX_MS:
                raise HttpError(400, "until 要比 since 大,最多问 24 小时")
            from d1max_site.pose_track import TRACK_LIMIT
            raw = pt.track(robot_id, since, until, limit=TRACK_LIMIT + 1)
            pts = pt.track(robot_id, since, until) if len(raw) > TRACK_LIMIT else raw
            return self._send_json(200, {"points": pts, "thinned": len(raw) > TRACK_LIMIT})
        map_id = (q.get("map_id") or [""])[0]
        version = (q.get("map_version") or [""])[0]
        if not map_id or not version:
            raise HttpError(400, "要 map_id、map_version")
        from d1max_site.pose_track import CELL_M
        cells = pt.last_visit(map_id, version, num("since"))
        return self._send_json(200, {"cell_m": CELL_M, "cells": cells})

    def _shift_notes(self, method: str, user) -> None:
        """交班留言(W44):``GET`` 最近一条(``view``);``POST {"text"}`` 写一条(``handle_alerts``,
        作者取登录账号)。"""
        notes = self.site.shift_notes
        if notes is None:
            raise HttpError(404, "此站点未启用交班留言")
        if method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, {"latest": notes.latest()})
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        self._need(user, HANDLE_ALERTS)
        d = self._body()
        try:
            note = notes.add(str(user), d.get("text"))
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        self._audit_detail = {"chars": len(note["text"])}
        return self._send_json(200, {"note": note})

    # ------------------------------------------------------------ 布置的历史版本(W44 第三期)

    def _snapshot(self, kind: str, map_id: str, version: str, item: str) -> Any:
        """这一样现在的样子(记历史用)。摄像头不带口令、地址里的账号口令抹掉(``list_cameras``)。"""
        if kind == "zones":
            return [{"zone": a["zone"], "points": a["points"]}
                    for a in self.site.areas.list(map_id, version)]
        if kind == "nogo":
            return [z.to_wire() for z in self.site.zones.current(map_id, version).zones]
        if kind == "route":
            r = self.site.routes.get(item)
            return {k: r[k] for k in ("map_id", "map_version", "waypoints", "policy")}
        if kind == "standby":
            return [{k: p[k] for k in ("name", "x", "y", "yaw", "default")}
                    for p in self.site.standby.list(item)
                    if p["map_id"] == map_id and p["map_version"] == version]
        if kind == "cameras":
            from d1max_site import cctv_admin
            keep = ("name", "zone", "onvif_url", "rtsp_url", "username", "motion", "placement")
            return [{k: c[k] for k in keep}
                    for c in cctv_admin.list_cameras(self.site.dispatcher.db)]
        raise ValueError(kind)

    _NO_BODY = object()

    def _hist(self, kind: str, map_id: str, version: str, item: str, user, *,
              body: Any = _NO_BODY, note: str = "") -> None:
        """保存成功以后记一版。记不下来不挡这次保存(只记日志)。"""
        h = self.site.layout_history
        if h is None:
            return
        try:
            snap = self._snapshot(kind, map_id, version, item) if body is self._NO_BODY else body
            h.record(kind, map_id, version, item, snap, by=str(user), note=note)
        except Exception:
            log.exception("布置的历史版本记不下来:%s %s:%s %s", kind, map_id, version, item)

    def _layout_history(self, method: str, hid: str | None, user) -> None:
        """``GET /api/layout/history?kind=&map_id=&version=&item=``;
        ``POST …/{id}/restore`` 退回到那一版
        (``manage``;走原来的保存路径)。"""
        from d1max_site.layout_history import HistoryError
        h = self.site.layout_history
        if h is None:
            raise HttpError(404, "此站点未启用历史版本")
        self._need(user, MANAGE)
        if hid is None:
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
            try:
                rows = h.list(one("kind"), one("map_id"), one("version"),
                              item=(q.get("item") or [None])[0])
            except HistoryError as exc:
                raise HttpError(400, str(exc)) from exc
            return self._send_json(200, {"history": rows})
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        body = self._body()
        row = h.get(int(hid))
        if row is None:
            raise HttpError(404, "版本不存在")
        # 外审 R3:请求要说清是哪一层、哪张图的哪一版(人在页面上看的那个);跟这一条对不上就拒绝,
        # 不按编号悄悄去改别的图
        exp = body.get("expect") if isinstance(body, dict) else None
        if not isinstance(exp, dict) or (exp.get("kind"), exp.get("map_id"), exp.get("version")) \
                != (row["kind"], row["map_id"], row["map_version"]):
            raise HttpError(409, "此版本不是你现在看的那张图、那一层的:请刷新历史后再回退")
        self._audit_target = f"{row['kind']}:{row['map_id']}:{row['map_version']}/{row['item']}"
        self._audit_detail = {"restore": row["id"]}
        warnings = self._restore(row, user)
        self._hist(row["kind"], row["map_id"], row["map_version"], row["item"], user,
                   note=f"restored #{row['id']}")
        return self._send_json(200, {"ok": True, "warnings": warnings})

    def _restore(self, row: dict[str, Any], user) -> list[dict[str, str]]:
        """退回:走各自原来的保存路径(规矩不变)。回给人看的提醒。"""
        from d1max_site.areas import AreaError
        from d1max_site.nav_zones import ZonesConflict, ZonesError
        from d1max_site.routes import RouteConflict, RouteError, RouteNotFound
        kind, mid, ver, item, body = (row["kind"], row["map_id"], row["map_version"],
                                      row["item"], row["body"])
        warnings: list[dict[str, str]] = []
        try:
            if kind == "zones":
                book = self.site.areas
                want = {a["zone"]: a["points"] for a in body or []}
                for a in book.list(mid, ver):
                    if a["zone"] not in want:
                        book.remove(mid, ver, a["zone"])
                for z, pts in want.items():
                    book.set(mid, ver, z, pts, by=str(user))
            elif kind == "nogo":
                zones = self.site.zones
                cur = zones.current(mid, ver)
                zs, tighten = zones.put(mid, ver, body or [], base_revision=cur.revision,
                                        by=str(user))
                self.site.dispatch(lambda: self.site.dispatcher.push_zones(
                    zs, tighten=tighten, issued_by=str(user)))
            elif kind == "route":
                store = self.site.routes
                try:
                    base = store.get(item)["revision"]
                except RouteNotFound:
                    base = 0
                if body is None:
                    if base:
                        store.delete_route(item, base_revision=base, by=str(user))
                else:
                    store.put_route(item, dict(body) | {"base_revision": base}, by=str(user))
            elif kind == "standby":
                stb = self.site.standby
                want = {p["name"]: p for p in body or []}
                for p in stb.list(item):
                    if p["map_id"] == mid and p["map_version"] == ver and p["name"] not in want:
                        stb.remove(item, p["name"])
                for p in sorted(want.values(), key=lambda p: bool(p.get("default"))):
                    stb.set(item, p["name"], map_id=mid, map_version=ver, x=p["x"], y=p["y"],
                            yaw=p["yaw"], default=bool(p.get("default")))
            elif kind == "cameras":
                warnings = self._restore_cameras(body or [])
        except (AreaError, ZonesError, RouteError, ValueError) as exc:
            code = 409 if isinstance(exc, (ZonesConflict, RouteConflict)) else 400
            raise HttpError(code, f"无法回退:{exc}") from exc
        return warnings

    def _restore_cameras(self, body: list[dict[str, Any]]) -> list[dict[str, str]]:
        """摄像头:按名字换回去;口令用现在那一台的(历史里没有口令),没有这台了就空着、提醒重填。
        地址:快照里的是抹掉账号口令的,跟现在那一台抹掉以后一样就用现在的原文。"""
        from d1max_site import cctv_admin
        from d1max_site.cctv import scrub
        db = self.site.dispatcher.db
        now = {r["name"]: dict(r) for r in db.query("SELECT * FROM cameras")}
        want = {c["name"]: c for c in body}
        warnings: list[dict[str, str]] = []
        for name in now:
            if name not in want:
                cctv_admin.remove(db, name)
        for name, c in want.items():
            old = now.get(name)
            d: dict[str, Any] = {k: c[k] for k in ("name", "zone", "onvif_url", "rtsp_url",
                                                   "username", "motion")}
            d["placement"] = c.get("placement")
            if old is not None:
                for k in ("onvif_url", "rtsp_url"):
                    if "@" in old[k] and scrub(old[k]) == c[k]:
                        d[k] = old[k]
                d["edit"] = True
            else:
                d["password"] = ""
                warnings.append({"camera": name, "problem": "password_missing"})
            cctv_admin.save(db, d, now_ms=self.site._now())
        return warnings

    def _duty_settings(self, method: str, user) -> None:
        """值班设置(W44 第二期):交班时间、多久没巡过。``GET`` 谁都要读(``view``,保安页面用);
        ``POST`` 改(``manage``)。"""
        from d1max_site.site_settings import SettingsError
        st = self.site.settings
        if st is None:
            raise HttpError(404, "此站点未启用设置")
        if method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, st.duty())
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        self._need(user, MANAGE)
        d = self._body()
        self._audit_detail = {k: d.get(k) for k in ("shift_hours", "stale_hours") if k in d}
        try:
            return self._send_json(200, st.set_duty(d, by=str(user)))
        except SettingsError as exc:
            raise HttpError(400, str(exc)) from exc

    def _login(self) -> None:
        d = self._body()
        name, pw = d.get("name"), d.get("password")
        if not isinstance(name, str) or not isinstance(pw, str):
            raise HttpError(400, "要 name 与 password 两个字符串")
        if d.get("web") is True:
            # B 阶段外审 I4:网页登录(会种 cookie)也要防跨站 —— 别的网站的表单设不了自定义头、
            # 也发不了 application/json(要预检,站点不答应跨域)。在建会话、种 cookie 之前拒。
            from d1max_site.webui import CSRF_HEADER
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if self.headers.get(CSRF_HEADER) != "1" or ctype != "application/json":
                raise HttpError(403, "网页登录少了防伪造的头")
        self._actor = f"login:{name[:64]}"
        try:
            # C-B4:App 报自己是哪一版;网页登录记成 web(不信它自己报的类别)
            web = d.get("web") is True
            client = {"kind": "web"} if web else d.get("client")
            if not web and isinstance(client, dict) and client.get("kind") == "web":
                client = None
            token = self.site.accounts.login(name, pw, client)
        except LockedOut as exc:
            raise HttpError(429, str(exc)) from exc
        except AuthError as exc:
            hint = self._acct_desk().login_hint(name)      # W52:申请了还没批 / 被拒了
            raise HttpError(401, hint or str(exc), extra={"apply": hint} if hint else None) \
                from exc
        who = self.site.accounts.check(token)
        from d1max_contract.compat import MIN_APP_API_LEVEL, SITE_API_LEVEL
        if d.get("web") is True:
            # 商业化 B1:网页登录种 HttpOnly cookie,回复里**不给令牌**(页面脚本拿不到)
            from d1max_site.webui import session_cookie
            self._extra_headers.append(("Set-Cookie", session_cookie(token)))
            token = ""
        self._send_json(200, {"token": token, "name": name,
                              "role": getattr(who, "role", ""),
                              "display_name": self.site.accounts.display_name(name),
                              # 商业化 A7:App 看这两个数,配不上的当场说「升级 App」「升级站点」
                              "api_level": SITE_API_LEVEL, "min_app_level": MIN_APP_API_LEVEL})

    def _robot(self, method: str, robot_id: str, action: str | None, user: str) -> None:
        disp = self.site.dispatcher
        if method == "GET":
            self._need(user, VIEW)
        elif action == "abort":
            self._need(user, ABORT)
        elif action == "standby":
            self._need(user, MANAGE)
        elif action == "patrol":
            self._need(user, PATROL)              # W53:经理也能派巡逻(别的派单不能)
        else:
            self._need(user, DISPATCH)
        if action is None:
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            view = disp.robot_view(robot_id)
            if view is None:
                raise HttpError(404, f"没有登记过 {robot_id}")
            view["events"] = disp.recent_events(robot_id)
            view["commands"] = disp.commands(robot_id)
            return self._send_json(200, view)
        if action in ("standby", "standby/return"):
            return self._standby(method, robot_id, action, user)
        if action == "standby/here":
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            return self._standby_here(robot_id, user)
        if action == "intercept/here":
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            return self._intercept_here(robot_id, user)
        if action == "patrol-plan":
            return self._patrol_plan(method, robot_id, user)
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        d = self._body()
        if action in ("goto", "patrol"):
            # 站点这一道关(W00c6i 内审):要人监护的狗没人监护就不派 —— 回滚到旧版代理时狗自己不查。
            why = self.site.supervision.refusal(robot_id)
            if why:
                from d1max_site.refusals import extra_for
                raise HttpError(409, why, extra=extra_for(why))
        if action == "goto":
            speed = d.get("max_speed_mps")
            if speed is not None and (isinstance(speed, bool)
                                      or not isinstance(speed, (int, float))
                                      or not math.isfinite(speed) or speed <= 0):
                raise HttpError(400, "max_speed_mps 要是正的有限数")
            # 请求体里的 priority 不认:手动派单一律 MANUAL(W00c2b 设计决定一)。
            target = d.get("target")
            if not isinstance(target, dict):
                raise HttpError(400, "要 target(MapPose 对象)")
            # 网页、手机发的目标点只有图号、版本、坐标(2026-10-15 点「去这里」被拒「缺 schema」):
            # 契约版本站点补上;给了别的版本照旧按契约拒。发给狗的报文照旧带 schema。
            target = {"schema": CONTRACT_SCHEMA, **target}
            result = self.site.dispatch(lambda: disp.goto(
                robot_id, target, speed, issued_by=str(user), priority=MANUAL))
        elif action == "patrol":
            # W53:手动派的巡逻都是临时巡逻 —— 估时、撞日程就排在日程后面、通知经理和管理员
            wire, label = self._patrol_wire(d)
            if not d.get("points"):
                self._audit_detail = {"mission": label}
            desk = self._adhoc()
            result = self.site.dispatch(lambda: desk.submit(robot_id, wire, label=label,
                                                            by=str(user)))
        else:
            task_id = d.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise HttpError(400, "要 task_id")
            result = self.site.dispatch(lambda: disp.abort(robot_id, task_id, issued_by=str(user)))
        self._send_json(200, result)

    def _adhoc(self) -> Any:
        """临时巡逻台(W53)。站点主程序接了就用它的(排队的每拍由它派);测试里第一次用时现建。"""
        site = self.site
        if site.adhoc is None:
            from d1max_site.adhoc import AdhocDesk
            site.adhoc = AdhocDesk(site.dispatcher.db, site.dispatcher, now_ms=site._now,
                                   alerts=site.alerts,
                                   refusal=getattr(site.supervision, "refusal", None))
        return site.adhoc

    def _patrol_wire(self, d: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """派巡逻的请求体 → (整份任务, 给人看的名字)。``points`` 是临时路线,否则 ``mission_id``。"""
        if "points" in d:
            m = self._temp_mission(d)
            self._audit_detail = {"temp": True, "points": len(m.waypoints), "mission": m.mission}
            return m.to_wire(), f"临时路线 {len(m.waypoints)} 个点"
        mid = d.get("mission_id")
        if not isinstance(mid, str) or not mid:
            raise HttpError(400, "要 mission_id")
        from d1max_site.catalog import active_bundle
        act = active_bundle(self.site.dispatcher.db)
        if act is None or mid not in act.missions:
            raise HttpError(404, f"路线库里没有路线 {mid!r}")
        return act.missions[mid].to_wire(), mid

    def _adhoc_cancel(self, method: str, aid: str, user) -> None:
        """取消排队中的临时巡逻(W53):派的人自己、经理、管理员。"""
        from d1max_site.adhoc import AdhocError
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        self._need(user, PATROL)
        self._audit_target = aid
        desk = self._adhoc()
        others = allowed(getattr(user, "role", ""), SCHEDULE)
        try:
            self.site.loop.call(lambda: _sync(desk.cancel, aid, by=str(user), may_others=others))
        except AdhocError as exc:
            raise HttpError(409 if "不在排队" in str(exc) else 404, str(exc)) from exc
        except PermissionError as exc:
            raise HttpError(403, str(exc)) from exc
        self._send_json(200, {"ok": True})

    def _patrol_plan(self, method: str, robot_id: str, user) -> None:
        """派之前先看(W53):``GET ?mission_id=`` → 估时、能不能马上走、撞了哪趟日程。"""
        if method != "GET":
            raise HttpError(405, "只支持 GET")
        self._need(user, PATROL)
        if robot_id not in self.site.dispatcher.clients:
            raise HttpError(404, f"没有登记过 {robot_id}")
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        wire, label = self._patrol_wire({"mission_id": (q.get("mission_id") or [""])[0]})
        desk = self._adhoc()
        got = self.site.loop.call(lambda: _sync(desk.plan, robot_id, wire))
        self._send_json(200, {**got, "label": label})

    def _temp_mission(self, d: dict[str, Any]) -> Any:
        """保安的临时路线(W44,用户选的):按顺序的 1–20 个点,走一趟,**不进路线库**。校验跟存路线同一套
        (点成不成形、在不在图上、走不走得到);朝向:每个点朝下一个点,最后一个跟前一个一样。"""
        from d1max_site.routes import RouteError
        pts = d.get("points")
        if not isinstance(pts, list) or not 1 <= len(pts) <= TEMP_ROUTE_MAX:
            raise HttpError(400, f"points 要是 1–{TEMP_ROUTE_MAX} 个点")
        if self.site.routes is None:
            raise HttpError(404, "此站点未启用路线库")
        flat: list[dict[str, Any]] = []
        for i, p in enumerate(pts):
            q = p if isinstance(p, dict) else {}
            flat.append({"name": f"P{i + 1}", "x": q.get("x"), "y": q.get("y")})
        for i, p in enumerate(flat):
            nxt = flat[i + 1] if i + 1 < len(flat) else None
            prv = flat[i - 1] if i > 0 else None
            a, b = (p, nxt) if nxt else (prv, p) if prv else (None, None)
            try:
                p["yaw"] = (math.atan2(float(b["y"]) - float(a["y"]),
                                       float(b["x"]) - float(a["x"])) if a and b else 0.0)
            except (TypeError, ValueError):
                p["yaw"] = 0.0                            # 点不成形:下面逐点报
        name = f"temp-{self.site._now()}"
        body = {"map_id": d.get("map_id"), "map_version": d.get("map_version"), "waypoints": flat}
        try:
            return self.site.routes.build_mission(name, body)
        except RouteError as exc:
            raise HttpError(400, str(exc), extra={"errors": exc.errors}) from exc

    def _nearest(self, method: str, user) -> None:
        """让最近的机器人过去看看(W44):``GET ?map_id=&map_version=&x=&y=`` → 谁、多少米、约几秒。
        候选:在线、数据新鲜、没被手动接管、不在驱离、加载的就是这一版图、有可信位姿。"""
        if method != "GET":
            raise HttpError(405, "只支持 GET")
        self._need(user, DISPATCH)
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        map_id = (q.get("map_id") or [""])[0]
        version = (q.get("map_version") or [""])[0]
        if not map_id or not version:
            raise HttpError(400, "要 map_id、map_version")
        try:
            x, y = float((q.get("x") or [""])[0]), float((q.get("y") or [""])[0])
        except ValueError as exc:
            raise HttpError(400, "x、y 要是数字(米)") from exc
        if not (math.isfinite(x) and math.isfinite(y)):
            raise HttpError(400, "x、y 要是有限数")
        disp = self.site.dispatcher
        deter = self.site.deterrence

        def gather() -> list[tuple[str, str, str, float, float]]:
            busy = deter.busy() if deter is not None else set()
            out = []
            for rid, c in disp.clients.items():
                t = c.telemetry
                if t is None or t.pose is None or c.status is None or not c.status.online:
                    continue
                if not disp._fresh(c) or disp.held(rid) or rid in busy:
                    continue
                out.append((rid, t.pose.map_id, t.pose.map_version,
                            float(t.pose.x), float(t.pose.y)))
            return out
        cands = [c for c in self.site.loop.call(lambda: _sync(gather))
                 if c[1] == map_id and c[2] == version]
        reach = getattr(self.site.incidents, "reach", None)
        if not cands:
            raise HttpError(404, "此地图上没有能派的机器人")
        if reach is None:
            raise HttpError(404, "站点没有此地图的规划栅格,无法确定最近的机器人")
        from d1max_site.nearest import nearest
        got = nearest(reach, cands, map_id, version, x, y)
        if got is None:
            raise HttpError(404, "此地图上没有能派的机器人")
        return self._send_json(200, got)

    def _standby(self, method: str, robot_id: str, action: str, user: str) -> None:
        from d1max_site.standby import StandbyError
        stb = self.site.standby
        if stb is None:
            raise HttpError(404, "此站点未启用待命点")
        if action == "standby/return":
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            why = self.site.supervision.refusal(robot_id)
            if why:
                from d1max_site.refusals import extra_for
                raise HttpError(409, why, extra=extra_for(why))
            d = self._body() or {}
            name = d.get("name") if isinstance(d, dict) else None
            if name is not None and (not isinstance(name, str) or not SAFE_ID.match(name)):
                raise HttpError(400, f"名字只许 ASCII 字母、数字、. _ -:{name!r}")
            result = self.site.dispatch(lambda: stb.return_to(robot_id, issued_by=str(user),
                                                              name=name))
            return self._send_json(200, result)
        if method == "GET":
            return self._send_json(200, {"points": stb.list(robot_id),
                                         "homes": stb.homes(robot_id)})
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        d = self._body()
        try:
            if d.get("remove") is True:
                was = [p for p in stb.list(robot_id) if p["name"] == d.get("name")]
                stb.remove(robot_id, d.get("name"))
                maps = {(p["map_id"], p["map_version"]) for p in was}
            else:
                stb.set(robot_id, d.get("name"), map_id=d.get("map_id"),
                        map_version=d.get("map_version"), x=d.get("x"), y=d.get("y"),
                        yaw=d.get("yaw", 0.0), default=d.get("default"))
                maps = {(d.get("map_id"), d.get("map_version"))}
        except StandbyError as exc:
            raise HttpError(400, str(exc)) from exc
        for mid, ver in maps:
            self._hist("standby", str(mid), str(ver), robot_id, user)
        self._send_json(200, {"points": stb.list(robot_id)})

    def _confirm(self, user, d: dict[str, Any]) -> None:
        """敏感操作再输一次密码(W44 第二期,设计 §5.2 第 4 条):开工程师账号、解绑机器人……
        跟登录同一个失败计数;不对回 403,锁了回 429。"""
        pw = d.get("confirm_password")
        if not isinstance(pw, str) or not pw:
            raise HttpError(403, "这一步要再输一次你的密码")
        try:
            ok = self.site.accounts.verify(str(user), pw)
        except LockedOut as exc:
            raise HttpError(429, str(exc)) from exc
        if not ok:
            raise HttpError(403, "密码错误")

    def _accounts(self, method: str, path: str, user=None) -> None:
        acc = self.site.accounts
        if path == "/api/accounts/engineer":
            # W44 第二期:给工程师开临时账号(4 / 24 小时);口令只在这一次回包里
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            d = self._body()
            self._audit_detail = {"engineer": str(d.get("display_name", ""))[:64],
                                  "hours": d.get("hours")}
            self._confirm(user, d)
            try:
                name, pw, until = acc.add_engineer(d.get("display_name"), d.get("hours"),
                                                   by=str(user))
            except AuthError as exc:
                raise HttpError(400, str(exc)) from exc
            self._audit_target = name
            out = {"name": name, "password": pw, "expires_ms": until, "accounts": acc.list()}
            if d.get("vpn") is True:
                # W55:同时给一份 VPN 配置(同样到点失效),能 SSH 到机器人、站点主机
                out["vpn"] = self._engineer_vpn(name, until)
                self._audit_detail["vpn"] = bool(out["vpn"].get("conf"))
            return self._send_json(200, out)
        m = _ACCOUNT_RM.match(path)
        if m is not None:
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            name = m.group(1)
            self._audit_target = name
            if name == str(user):
                raise HttpError(400, "不能删自己")
            try:
                acc.remove(name)
            except AuthError as exc:
                raise HttpError(404 if "没有账号" in str(exc) else 400, str(exc)) from exc
            return self._send_json(200, {"accounts": acc.list()})
        if path == "/api/accounts":
            if method == "GET":
                return self._send_json(200, {"accounts": acc.list()})
            if method != "POST":
                raise HttpError(405, "只支持 GET/POST")
            d = self._body()
            self._audit_target = str(d.get("name", ""))[:64]
            self._audit_detail = {"new_account": self._audit_target,
                                  "role": str(d.get("role", "guard"))[:16]}
            try:
                acc.add(d.get("name"), d.get("password"), role=d.get("role", "guard"),
                        display_name=d.get("display_name", ""))
            except AuthError as exc:
                raise HttpError(400, str(exc)) from exc
            return self._send_json(200, {"accounts": acc.list()})
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        name = _ACCOUNT.match(path).group(1)
        d = self._body()
        self._audit_target = name
        self._audit_detail = ({"role": str(d["role"])[:16]} if "role" in d else {}) | (
            {"disabled": d["disabled"]} if isinstance(d.get("disabled"), bool) else {}) | (
            {"password_reset": True} if "password" in d else {}) | (
            {"display_name": str(d["display_name"])[:64]} if "display_name" in d else {})
        if "password" in d and name == str(self._actor):
            # 管理员的令牌被偷了,也不能靠这条路不验旧口令就把自己的账号改走。
            raise HttpError(400, "改自己的密码走 /api/me/password(要旧密码)")
        try:
            acc.update(name, role=d.get("role"), disabled=d.get("disabled"),
                       password=d.get("password"), display_name=d.get("display_name"))
        except AuthError as exc:
            raise HttpError(400 if "没有账号" not in str(exc) else 404, str(exc)) from exc
        self._send_json(200, {"accounts": acc.list()})

    def _areas(self, method: str, map_id: str, version: str, user) -> None:
        """防区画在地图上(B1c):``GET`` 这张图这一版上的防区多边形(``view``);``POST
        {zone, points}`` 新建或改、``POST {zone, remove: true}`` 删(``manage``)。"""
        from d1max_site.areas import AreaError
        book = self.site.areas
        if book is None:
            raise HttpError(404, "此站点未启用防区")
        if method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, {"areas": book.list(map_id, version)})
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        self._need(user, MANAGE)
        d = self._body()
        zone = d.get("zone")
        self._audit_target = f"{map_id}:{version}/{str(zone)[:64]}"
        try:
            if d.get("remove") is True:
                if not book.remove(map_id, version, str(zone)):
                    raise HttpError(404, "此地图上没有此防区")
            else:
                book.set(map_id, version, zone, d.get("points"), by=str(user))
        except AreaError as exc:
            raise HttpError(400, str(exc)) from exc
        self._hist("zones", map_id, version, "", user)
        return self._send_json(200, {"areas": book.list(map_id, version)})

    def _authz(self, method: str, entry_id: str | None, user, action: str | None = None) -> None:
        """名单(B1c,时段 / 人员授权):``GET`` 看(``view``);``POST`` 加一条、``POST …/{id}/remove``
        删(``manage``)。"""
        from d1max_site.authz import AuthzError
        book = self.site.authz
        if book is None:
            raise HttpError(404, "此站点未启用名单")
        if method == "GET" and entry_id is None:
            self._need(user, VIEW)
            return self._send_json(200, {"authorizations": book.list(with_active=True),
                                         "active": book.active(None)})
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        self._need(user, MANAGE)
        d = self._body()
        if entry_id is not None and action == "end":
            # W44 第二期:正在生效的那一段提前结束(下一段照常)
            self._audit_target = f"authorization/{entry_id}"
            if not book.end_now(int(entry_id), by=str(user)):
                raise HttpError(409, "现在不在这一条的授权时间里")
            return self._send_json(200, {"authorizations": book.list(with_active=True)})
        if entry_id is not None:
            self._audit_target = f"authorization/{entry_id}"
            if not book.remove(int(entry_id)):
                raise HttpError(404, "没有这一条")
        else:
            self._audit_target = str(d.get("name", ""))[:64]
            self._audit_detail = {k: d.get(k) for k in ("zones", "days", "start", "end")}
            try:
                book.add(d, by=str(user))
            except AuthzError as exc:
                raise HttpError(400, str(exc)) from exc
        return self._send_json(200, {"authorizations": book.list(with_active=True)})

    def _pairing(self) -> None:
        """手机扫码添加站点的配对码(App V2):站点名 + 手机要连的地址 + 站点服务证书指纹,没有秘密。
        地址由网页带过来(站点不知道自己在外面叫什么)。"""
        from d1max_site.main import cmd_fingerprint
        from d1max_site.pairing import PairingError, encode
        home = self.site.home
        if home is None:
            raise HttpError(404, "此站点未接入站点目录")
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        url = (q.get("url") or [""])[0]
        name = self.site.site_name or "Nexus Warden"
        try:
            fp = cmd_fingerprint(home)
            code = encode(name, url, fp)
        except PairingError as exc:
            raise HttpError(400, str(exc)) from exc
        from d1max_site.pairing import check_url
        self._send_json(200, {"code": code, "name": name[:64], "url": check_url(url),
                              "fingerprint": fp})

    def _admin_robot(self, method: str, robot_id: str | None, action: str | None,
                     user=None) -> None:
        """登记新狗并出开通码、给已登记的狗重出开通码、吊销(商业化 B1b,``manage``)。跟命令行
        ``enroll --code`` / ``enroll-code`` / ``revoke`` 是同一套函数(证书、注册表、CRL 都一样)。
        W44 第二期:起名(``…/name``);吊销(界面上叫解绑)要再输一次密码。"""
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        d = self._body()
        if action == "name":
            from d1max_site.registry import RegistryError
            rid = unquote(robot_id or "")
            self._audit_target, self._audit_detail = rid, {"name": str(d.get("name"))[:64]}
            try:
                self.site.dispatcher.registry.set_name(rid, d.get("name"))
            except RegistryError as exc:
                raise HttpError(404 if "没有登记" in str(exc) else 400, str(exc)) from exc
            return self._send_json(200, {"robots": self.site.dispatcher.robots_view()})
        if action == "revoke":
            self._audit_target = unquote(robot_id or "")
            self._confirm(user, d)
        home = self.site.home
        if home is None:
            raise HttpError(404, "此站点未接入站点目录")
        from d1max_site import main as site_main
        try:
            if action is None:                                     # 登记新狗 + 出开通码
                rid = str(d.get("robot_id") or "")
                if not SAFE_ID.match(rid):
                    raise HttpError(400, "编号只收字母、数字、点、下划线、减号,最多 64 个")
                days = d.get("days", 365)
                if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650:
                    raise HttpError(400, "证书有效天数要是 1–3650 的整数")
                self._audit_target, self._audit_detail = rid, {"days": days}
                site_main.cmd_enroll(home, rid, days)
                code = site_main.cmd_enroll_code(home, rid, d.get("host") or None, 24.0)
                return self._send_json(200, {"robot_id": rid, "code": code, "hours": 24,
                                             "manual_only": True})
            rid = unquote(robot_id or "")
            if not SAFE_ID.match(rid):
                raise HttpError(404, "机器人不存在")
            self._audit_target = rid
            if action == "code":
                hours = d.get("hours", 24)
                if not isinstance(hours, (int, float)) or isinstance(hours, bool) \
                        or not 0.25 <= hours <= 168:
                    raise HttpError(400, "开通码有效小时数要在 0.25–168 之间")
                code = site_main.cmd_enroll_code(home, rid, d.get("host") or None, float(hours))
                return self._send_json(200, {"robot_id": rid, "code": code, "hours": hours})
            wiped = self._unbind_robot(rid, user)
            summary = site_main.cmd_revoke(home, rid)
            out = {"robot_id": rid, "summary": summary, "robot_wipe": wiped}
            # 吊销进了 CRL,broker 重启才读:站点自己重启它(systemd 拉起来);做不到才叫人去重启
            restart = self.site.broker_restart
            if restart is None or not restart():
                out["broker_restart"] = "systemctl restart d1max-mosquitto"
            return self._send_json(200, out)
        except site_main.SiteError as exc:
            msg = str(exc)
            # 命令行那边(main.py)的原话没改,新旧两种说法都认
            conflict = any(k in msg for k in ("已登记", "未登记", "没登记", "没有"))
            raise HttpError(409 if conflict else 400, msg) from exc

    def _change_own_password(self, user: str) -> None:
        d = self._body()
        self._audit_target, self._audit_detail = str(user), {"self_password_change": True}
        try:
            self.site.accounts.change_password(str(user), d.get("old"), d.get("new"))
        except LockedOut as exc:
            raise HttpError(429, str(exc)) from exc
        except AuthError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "note": "密码改了;所有会话已吊销,请重新登录"})

    def _desk(self):
        if self.site.incidents is None:
            raise HttpError(404, "此站点未启用事件派遣")
        return self.site.incidents

    def _raw_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise HttpError(400, "Content-Length 不是数") from exc
        if n < 0:
            raise HttpError(400, "Content-Length 不能是负的")
        if n > MAX_BODY:
            raise HttpError(413, f"请求体超过 {MAX_BODY} 字节")
        return self.rfile.read(n) if n else b""

    def _incident_in(self) -> None:
        from d1max_site.incidents import IncidentAuthError, IncidentError
        desk = self._desk()
        raw = self._raw_body()
        try:
            desk.verify(self.headers.get("X-D1MAX-Source"), self.headers.get("X-D1MAX-Timestamp"),
                        self.headers.get("X-D1MAX-Signature"), raw)
        except IncidentAuthError as exc:
            # 对外只说「验签没过」:具体哪一步没过只进日志(不让人探出哪些源登记过)。
            log.warning("事件回调验签没过(%s):%s", self.client_address[0], exc)
            raise HttpError(401, "验签未通过") from exc
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(400, f"JSON 无法解析: {exc}") from exc
        source = self.headers.get("X-D1MAX-Source") or ""
        self._actor = f"source:{source}"
        if not desk.allow(source):                       # W16:每个事件源每分钟有上限
            raise HttpError(429, "此事件源一分钟内报得太多,超出的不收")
        try:
            desk.parse(body)
        except IncidentError as exc:
            raise HttpError(400, str(exc)) from exc
        row = self.site.dispatch(lambda: desk.handle(source, body))
        self._send_json(200, row)

    def _deterrence(self, method: str, path: str, user) -> None:
        """分级驱离(W22)。看:``view``;就地开一场(W33,``start``)、跳级、往回退:``dispatch``(保安、管理员);解除:``abort``
        (保安、管理员、经理,同「叫停」)。"""
        from d1max_site.deterrence import DeterrenceError
        desk = self.site.deterrence
        if desk is None:
            raise HttpError(404, "此站点未启用驱离")
        if path == "/api/deterrence":
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            self._need(user, VIEW)
            return self._send_json(200, {"sessions": self.site.loop.call(
                lambda: _sync(desk.view))})
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        m = _DETER.match(path)
        robot_id, action = unquote(m.group(1)), m.group(2)
        self._audit_target = robot_id
        d = self._body()
        try:
            if action == "start":
                # W33(决策 48):就地驱离,保安、管理员
                self._need(user, DISPATCH)
                v = self.site.loop.call(lambda: desk.start_here(robot_id, by=str(user)),
                                        timeout_s=30)
                return self._send_json(200, {"session": v})
            if action == "level":
                self._need(user, DISPATCH)
                self._audit_detail = {"level": d.get("level")}
                v = self.site.loop.call(lambda: desk.set_level(robot_id, d.get("level"),
                                                               by=str(user)), timeout_s=30)
                return self._send_json(200, {"session": v})
            self._need(user, ABORT)
            self.site.loop.call(lambda: desk.release(robot_id, by=str(user)), timeout_s=30)
            return self._send_json(200, {"ok": True})
        except DeterrenceError as exc:
            raise HttpError(404 if "未在驱离" in str(exc) else 503 if "落进库" in str(exc)
                            or "落库失败" in str(exc) else 400, str(exc)) from exc

    def _mode(self, method: str, path: str, user) -> None:
        """布防模式(W20)。看:``view``。切到布防:``arm``(保安也行);切到留守、临时撤防:``set_mode``
        (经理、管理员)。哪些防区留守时撤防:``manage``。"""
        from d1max_site.modes import ModeError
        desk = self.site.arming
        if desk is None:
            raise HttpError(404, "此站点未启用布防模式")
        if method == "GET":
            if path != "/api/mode":
                raise HttpError(405, "只支持 POST")
            self._need(user, VIEW)
            return self._send_json(200, desk.view())
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        d = self._body()
        try:
            if path == "/api/mode/zones":
                self._need(user, MANAGE)
                self._audit_target = str(d.get("zone", ""))[:128]
                self._audit_detail = {"home_armed": d.get("home_armed")}
                return self._send_json(200, desk.set_zone_home(d.get("zone"),
                                                               d.get("home_armed")))
            mode = d.get("mode")
            self._need(user, ARM if mode == "armed" else SET_MODE)
            self._audit_target = str(mode)[:16]
            self._audit_detail = {k: d[k] for k in ("zones", "minutes") if k in d}
            return self._send_json(200, desk.set_mode(mode, by=str(user), zones=d.get("zones"),
                                                      minutes=d.get("minutes")))
        except ModeError as exc:
            raise HttpError(400, str(exc)) from exc

    def _arming_schedule(self, method: str, path: str, user) -> None:
        """布防时间表(W44 第二期):``GET /api/arming/schedule`` 看(``view``);``POST`` 整份换、
        ``POST /api/arming/exceptions`` 加例外、``…/{id}/remove`` 删例外(``manage``)。改了以后防区的
        布防状态可能跟着变:推一帧模式给手机。"""
        from d1max_site.arming_schedule import ScheduleError
        desk = self.site.arming
        sch = getattr(desk, "schedule", None)
        if sch is None:
            raise HttpError(404, "此站点未启用布防时间表")
        if method == "GET" and path == "/api/arming/schedule":
            self._need(user, VIEW)
            return self._send_json(200, sch.view())
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST" if path == "/api/arming/schedule"
                            else "只支持 POST")
        self._need(user, SCHEDULE)          # W53:布防时间经理也能排
        d = self._body()
        try:
            if path == "/api/arming/schedule":
                slots = d.get("slots")
                self._audit_detail = {"enabled": d.get("enabled"),
                                      "slots": len(slots) if isinstance(slots, list) else None}
                sch.put(d, by=str(user))
            elif path == "/api/arming/exceptions":
                self._audit_detail = {k: d.get(k) for k in ("first_day", "last_day", "state")}
                sch.add_exception(d, by=str(user))
            else:
                eid = int(_ARM_EX.match(path).group(1))
                self._audit_target = f"exception/{eid}"
                if not sch.remove_exception(eid):
                    raise HttpError(404, "没有这一条例外")
        except ScheduleError as exc:
            raise HttpError(400, str(exc)) from exc
        desk._changed()
        return self._send_json(200, sch.view())

    def _chargers(self, method: str, path: str, user) -> None:
        """充电桩(W13)。看:``view``(带每台狗这一轮回充走到哪一步);登记、改桩前对准点
        (``{map_id, map_version, x, y, yaw}``,或者 ``{here: true}`` 用狗现在的位姿)、
        删(``{remove: true}``):``manage``。"""
        from d1max_site.charging import ChargeError
        desk = self.site.charge
        if desk is None:
            raise HttpError(404, "此站点未启用自动回充")
        if path == "/api/chargers":
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            self._need(user, VIEW)
            return self._send_json(200, desk.view())
        m = _CHARGER.match(path)
        assert m is not None
        rid = unquote(m.group(1))
        if not SAFE_ID.match(rid):
            raise HttpError(404, "机器人不存在")
        self._need(user, MANAGE)
        self._audit_target = rid
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        d = self._body()
        if d.get("remove") is True:                       # 删桩(这台狗以后不自动回充)
            self._audit_detail = {"remove": True}
            return self._send_json(200, {"removed": desk.remove_charger(rid)})
        if d.get("here") is True:
            # 狗现在站的地方(人把它领到桩前约 1.5 m、正对桩):按它最近一份新鲜的遥测位姿
            c = self.site.dispatcher.clients.get(rid)
            pose = c.telemetry.pose if c is not None and c.telemetry is not None else None
            if pose is None or not self.site.dispatcher._fresh(c):
                raise HttpError(409, f"{rid} 不在线、没有新鲜的位姿:领到桩前、等它报了位置再设")
            d = {"map_id": pose.map_id, "map_version": pose.map_version, "x": pose.x,
                 "y": pose.y, "yaw": pose.yaw}
        vals = [d.get(k) for k in ("x", "y", "yaw")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals) or \
                not isinstance(d.get("map_id"), str) or not isinstance(d.get("map_version"), str):
            raise HttpError(400, "要 map_id、map_version、x、y、yaw")
        self._audit_detail = {k: d[k] for k in ("map_id", "map_version", "x", "y", "yaw")}
        try:
            return self._send_json(200, desk.set_charger(
                rid, map_id=d["map_id"], map_version=d["map_version"], x=float(vals[0]),
                y=float(vals[1]), yaw=float(vals[2]), by=str(user)))
        except ChargeError as exc:
            raise HttpError(400, str(exc)) from exc

    def _enroll_claim(self) -> None:
        """狗拿开通码领自己的证书包(商业化 A4)。不用登录;对不上一律 403 同一句话。记审计。"""
        from d1max_site.provision import ProvisionError, claim
        d = self._body()
        rid = str(d.get("robot_id") or "")[:64]
        self._actor, self._audit_target = f"robot:{rid}", rid
        if self.site.home is None:
            raise HttpError(404, "此站点未启用开通码")
        try:
            # 吊销、证书代次在 claim 的事务里跟登记表核(A 阶段外审 I1)
            files = claim(self.site.dispatcher.db, self.site.home, rid,
                          str(d.get("token") or ""), now_ms=self.site._now())
        except ProvisionError as exc:
            raise HttpError(403, str(exc)) from exc
        return self._send_json(200, {"robot_id": rid, "files": files})

    def _bind_desk(self) -> Any:
        """W42 绑定申请台(站点主程序接了站点目录才有;第一次用时现建)。"""
        site = self.site
        if site.home is None or site.dispatcher is None:
            raise HttpError(404, "此站点未启用绑定")
        if site.bind is None:
            from d1max_site import main as site_main
            from d1max_site.binding import BindDesk
            home = site.home
            from d1max_site.alert_store import LoopAlerts
            alerts = LoopAlerts(site.alerts, site.loop) if site.alerts is not None else None
            site.bind = BindDesk(
                site.dispatcher.db, now_ms=site._now, alerts=alerts,
                enroll=lambda serial: site_main.cmd_enroll(home, serial, 365),
                issue=lambda serial, host, fp: site_main.cmd_enroll_code(
                    home, serial, host or None, 1.0, expect_fp=fp),
                set_name=site.dispatcher.registry.set_name)
        return site.bind

    def _bind_robot(self, method: str, path: str) -> None:
        """机器人这头(不用登录,W42 §5.1):挂申请、轮询、问证书。"""
        from d1max_site.binding import BindRefused
        desk = self._bind_desk()
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        try:
            if method == "POST" and path == "/api/bind/request":
                d = self._body()
                serial = str(d.get("serial") or "")[:64] if isinstance(d, dict) else ""
                self._actor, self._audit_target = f"robot:{serial}", serial
                host = urlparse("//" + (self.headers.get("Host") or "")).hostname or ""
                return self._send_json(200, desk.request(d, source=self.client_address[0],
                                                         host=host))
            m = _BIND_POLL.match(path)
            if method == "GET" and m is not None:
                return self._send_json(200, desk.poll(m.group(1), (q.get("secret") or [""])[0]))
            if method == "GET" and path == "/api/bind/state":
                return self._send_json(200, {"state": desk.cert_state(
                    (q.get("serial") or [""])[0], (q.get("cert") or [""])[0])})
        except BindRefused as exc:
            raise HttpError(exc.status, exc.message) from exc
        raise HttpError(404, f"没有 {method} {path}")

    def _acct_desk(self) -> Any:
        """W52 账号申请台(第一次用时现建;告警经事件循环)。"""
        site = self.site
        if site.account_requests is None:
            from d1max_site.account_requests import AccountRequests
            from d1max_site.alert_store import LoopAlerts
            alerts = LoopAlerts(site.alerts, site.loop) if site.alerts is not None else None
            site.account_requests = AccountRequests(site.accounts, now_ms=site._now, alerts=alerts)
        return site.account_requests

    def _account_apply(self) -> None:
        from d1max_site.account_requests import RequestRefused
        d = self._body()
        name = str(d.get("name") or "")[:64] if isinstance(d, dict) else ""
        self._actor, self._audit_target = f"apply:{name}", name
        try:
            out = self._acct_desk().submit(d, source=self.client_address[0])
        except RequestRefused as exc:
            raise HttpError(exc.status, str(exc)) from exc
        self._send_json(200, out)

    def _account_requests(self, method: str, rid: str | None, action: str | None, user) -> None:
        """管理员看、批、拒账号申请(W52;批准要再输一次密码,批的时候选权限,默认只看)。"""
        from d1max_site.account_requests import RequestRefused
        desk = self._acct_desk()
        if rid is None:
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            return self._send_json(200, {"requests": desk.pending()})
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        d = self._body()
        self._audit_target = rid
        try:
            if action == "approve":
                self._confirm(user, d)
                out = desk.approve(rid, role=d.get("role"), by=str(user))
                self._audit_detail = out
            else:
                desk.reject(rid, by=str(user))
                out = {"ok": True}
        except RequestRefused as exc:
            raise HttpError(exc.status, str(exc)) from exc
        self._send_json(200, out)

    def _admin_bind(self, method: str, rid: str | None, action: str | None, user) -> None:
        """站点上的人看、批、拒绑定申请(W42 §5.1;批准要再输一次密码)。"""
        from d1max_site import main as site_main
        from d1max_site.binding import BindRefused
        desk = self._bind_desk()
        if rid is None:
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            return self._send_json(200, {"requests": desk.pending()})
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        d = self._body()
        self._audit_target = rid
        try:
            if action == "approve":
                self._confirm(user, d)
                out = desk.approve(rid, by=str(user), name=d.get("name"))
            else:
                out = desk.reject(rid, by=str(user))
        except BindRefused as exc:
            raise HttpError(exc.status, exc.message) from exc
        except site_main.SiteError as exc:
            raise HttpError(409, str(exc)) from exc
        self._audit_detail = {"serial": out["serial"], "outcome": out["state"]}
        return self._send_json(200, out)

    def _robot_wifi(self, robot_id: str, user) -> None:
        """W42 §6:``{"id": 改动编号, "approve": true/false}`` → 发给机器人(它挂着那一条才认)。"""
        if not SAFE_ID.match(robot_id):
            raise HttpError(404, "机器人不存在")
        d = self._body()
        cid, ok = d.get("id"), d.get("approve")
        if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{8,32}", cid) \
                or not isinstance(ok, bool):
            raise HttpError(400, "要 {\"id\": 改动编号, \"approve\": true/false}")
        self._audit_target, self._audit_detail = robot_id, {"id": cid, "approve": ok}
        return self._send_json(200, self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, "wifi_confirm", {"id": cid, "approve": ok}, issued_by=str(user))))

    def _unbind_robot(self, robot_id: str, user) -> str:
        """W42 §5.3:解绑先叫机器人恢复出厂(它不在线、老版本不认就算了 —— 它会自己来问,§5.3),
        回 ``sent`` / ``offline`` / ``unsupported``。"""
        rec = self.site.dispatcher.registry.get(robot_id)
        if rec is None or rec.revoked:
            return "offline"
        try:
            self.site.dispatch(lambda: self.site.dispatcher.map_command(
                robot_id, "unbind", {"cert": rec.fingerprint}, issued_by=str(user)))
        except HttpError as exc:
            return "unsupported" if exc.extra.get("unsupported") else "offline"
        return "sent"

    def _healthz(self) -> None:
        """探活(商业化 A1):不用登录,只回 ``{"ok", "version"}``;不好回 503。"""
        desk = self.site.health
        if desk is None:
            return self._send_json(200, {"ok": True, "version": ""})
        ok, body = desk.healthz()
        return self._send_json(200 if ok else 503, body)

    def _health(self, method: str, path: str, user) -> None:
        """体检、指标历史、诊断包(商业化 A1)。体检和诊断包要管理员;指标历史能看的都能看。"""
        desk = self.site.health
        if desk is None:
            raise HttpError(404, "此站点未启用体检")
        if method != "GET":
            raise HttpError(405, "只支持 GET")
        if path == "/api/metrics":
            self._need(user, VIEW)
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            since = q.get("since", ["0"])[0]
            return self._send_json(200, {"metrics": desk.metrics(
                key=(q.get("key") or [None])[0], since_ms=int(since) if since.isdigit() else 0)})
        self._need(user, MANAGE)
        if path == "/api/health":
            return self._send_json(200, desk.view())
        import tempfile

        from d1max_site.health import support_bundle
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        h = q.get("hours", ["24"])[0]
        hours = min(max(int(h), 1), 168) if h.isdigit() else 24      # W55:时间段,最长 7 天
        extra = self._bundle_extra(hours)
        with tempfile.TemporaryDirectory(prefix="d1max-bundle-") as tmp:
            out = support_bundle(Path(tmp) / "support.tar.gz", home=desk.home, db=desk.db,
                                 health=desk, now_ms=self.site._now(), hours=hours, extra=extra)
            return self._send_bytes(out.read_bytes(), "application/gzip")

    def _push_devices(self, method: str, path: str, user) -> None:
        """这部手机收不收 P1 推送(商业化 A6):``POST /api/push/devices`` 登记推送号、
        ``POST /api/push/devices/remove`` 注销。登录了的(包括值守令牌)都能给**自己**登记;推送号记在
        这个账号名下。"""
        desk = self.site.push
        if desk is None:
            raise HttpError(404, "此站点未启用推送")
        self._need(user, VIEW)
        d = self._body()
        reg = str(d.get("registration_id") or "")
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        try:
            if path.endswith("/remove"):
                desk.unregister(str(user), reg)
            else:
                from d1max_site.accounts import _token_hash
                if not desk.register(str(user), reg, str(d.get("platform") or "android"),
                                     session=_token_hash(self._token() or ""),
                                     provider=str(d.get("provider") or "jpush")):
                    raise HttpError(401, "这次登录已退出了:重新登录再登记推送")
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        # providers:站点配了哪几个通道(手机拿来提示「登记了但站点没配这个通道,收不到」)
        return self._send_json(200, {"ok": True, "providers": sorted(desk.senders)})

    def _weather(self, method: str, user) -> None:
        """天气(W29)。看:``view``。手动切(正常 / 下雨 / 雷暴 / 回到自动):``arm``(值班的保安、经理、
        管理员都能;切成雷暴会停巡检,切回正常会恢复,都记审计)。"""
        from d1max_site.weather import WeatherError
        desk = self.site.weather
        if desk is None:
            raise HttpError(404, "此站点未启用天气")
        if method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, desk.view())
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        self._need(user, ARM)
        d = self._body()
        self._audit_target = str(d.get("condition"))[:16]
        self._audit_detail = {k: d[k] for k in ("hours",) if k in d}
        try:
            return self._send_json(200, desk.set_manual(d.get("condition"), by=str(user),
                                                        hours=d.get("hours")))
        except WeatherError as exc:
            raise HttpError(400, str(exc)) from exc

    def _incident_admin(self, method: str, path: str) -> None:
        from d1max_site.incidents import IncidentError
        desk = self._desk()
        if path == "/api/incidents":
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            return self._send_json(200, {"incidents": desk.list()})
        if method == "GET":                              # W16:拦截点与防区(手机用)
            return self._send_json(200, desk.intercepts())
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        d = self._body()
        if not isinstance(d, dict):
            raise HttpError(400, "要一个对象")
        try:
            if path == "/api/intercepts" and d.get("remove") is True:
                desk.remove_intercept(str(d.get("name")))
            elif path == "/api/intercepts":
                desk.set_intercept(d.get("name"), map_id=d.get("map_id"),
                                   map_version=d.get("map_version"), x=d.get("x"), y=d.get("y"),
                                   yaw=d.get("yaw", 0.0))
            elif d.get("remove") is True:
                desk.unmap_zone(str(d.get("zone")))
            else:
                desk.map_zone(d.get("zone"), d.get("intercept"))
        except IncidentError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True} | desk.intercepts())

    def _import_bundle(self, user: str) -> None:
        from d1max_site.catalog import CatalogError, import_bundle, import_summary
        d = self._body()
        path = d.get("path")
        if not isinstance(path, str) or not path:
            raise HttpError(400, "要 path(站点主机上任务包目录的路径)")
        try:
            got = import_bundle(self.site.dispatcher.db, Path(path), imported_by=user,
                                now_ms=self.site._now())
        except CatalogError as exc:
            raise HttpError(409, str(exc)) from exc
        self._audit_target = f"{got['bundle_id']} v{got['version']}"
        self._audit_detail = {"routes": got["routes"], "schedules": got["schedules"],
                              "kept_schedules": got["kept_schedules"]}
        self._send_json(200, got | {"summary": import_summary(got)})

    def _routes(self, method: str, path: str, user) -> None:
        """路线库(``routes.py``):看 ``view``,改 ``manage``(进审计)。出错:校验没过 400(逐点的在
        ``errors``)、``base_revision`` 对不上 409、没有 404。"""
        from d1max_site.routes import RouteConflict, RouteError, RouteNotFound
        store = self.site.routes
        mr, ms = _ROUTE.match(path), _ROUTE_SCHED.match(path)
        read = method == "GET"
        # W53:日程(``/api/route-schedules``)经理也能改;路线、时区、导出还是 manage
        self._need(user, VIEW if read else SCHEDULE if ms is not None else MANAGE)
        try:
            if read:
                if path == "/api/routes":
                    q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                    return self._send_json(200, store.view(
                        (q.get("map_id") or [None])[0], (q.get("map_version") or [None])[0]))
                if mr is not None and not mr.group(2):
                    return self._send_json(200, store.get(unquote(mr.group(1))))
                if ms is not None and ms.group(1) is None:
                    v = store.view()
                    return self._send_json(200, {"timezone": v["timezone"],
                                                 "revision": v["revision"],
                                                 "schedules": v["schedules"]})
                raise HttpError(405, "此接口不能 GET")
            if method != "POST":
                raise HttpError(405, "只支持 GET/POST")
            d = self._body()
            if path == "/api/route-timezone":
                self._audit_detail = {"timezone": str(d.get("timezone"))[:64]}
                return self._send_json(200, store.set_timezone(d.get("timezone"),
                                                               by=str(user)))
            if path == "/api/route-export":
                if self.site.home is None:
                    raise HttpError(404, "此站点未配置站点目录,无法导出任务包")
                got = store.export_bundle(self.site.home / "route-exports", by=str(user))
                self._audit_target = f"{got['bundle_id']} v{got['version']}"
                return self._send_json(200, got)
            if mr is not None:
                rid = unquote(mr.group(1))
                self._audit_target = rid
                if mr.group(2):
                    old = store.get(rid)
                    store.delete_route(rid, base_revision=d.get("base_revision"), by=str(user))
                    self._audit_detail = {"delete": True}
                    self._hist("route", old["map_id"], old["map_version"], rid, user, body=None)
                    return self._send_json(200, {"ok": True})
                out = store.put_route(rid, d, by=str(user))
                self._hist("route", out["map_id"], out["map_version"], rid, user)
                self._audit_detail = {"revision": out["revision"], "points": len(out["waypoints"]),
                                      "map": f"{out['map_id']}:{out['map_version']}"}
                return self._send_json(200, out)
            if ms is not None and ms.group(1):
                eid = unquote(ms.group(1))
                self._audit_target = eid
                if ms.group(2):
                    store.delete_schedule(eid, base_revision=d.get("base_revision"),
                                          by=str(user))
                    self._audit_detail = {"delete": True}
                    return self._send_json(200, {"ok": True})
                self._audit_detail = {"mission": str(d.get("mission"))[:64],
                                      "at": str(d.get("at"))[:8]}
                return self._send_json(200, store.put_schedule(eid, d, by=str(user)))
        except RouteConflict as exc:
            raise HttpError(409, str(exc), extra={"errors": exc.errors}) from exc
        except RouteNotFound as exc:
            raise HttpError(404, str(exc)) from exc
        except RouteError as exc:
            raise HttpError(400, str(exc), extra={"errors": exc.errors}) from exc
        raise HttpError(404, f"没有 {method} {path}")

    def _halt(self, robot_id: str, user) -> None:
        """停车(W00c5c):走 cmd,不走遥控连接。``abort`` 权限 —— 经理也能按。"""
        self._need(user, ABORT)
        self._body()
        self._audit_target = robot_id
        try:
            if self.site.teleop is not None:
                r = self.site.teleop.halt(robot_id, user)
            else:
                r = self.site.loop.call(lambda: self.site.dispatcher.halt(
                    robot_id, issued_by=str(user)),
                    timeout_s=self.site.dispatcher.ack_timeout_s + 5)
        except DispatchRefused as exc:
            raise HttpError(409, str(exc)) from exc
        except (DispatchTimeout, TimeoutError, FutureTimeout) as exc:
            raise HttpError(504, "机器人未返回停车的回执:无法确认是否已停止,按机身急停") from exc
        ack = r.get("ack") if isinstance(r, dict) else None
        if isinstance(ack, dict) and ack.get("result") != "accepted":
            # 狗说停车没成(W00c5e 内部评审):任务它中止了,腿停没停住不知道。
            raise HttpError(502, f"机器人说停车失败({ack.get('reason', '')}):按机身急停")
        return self._send_json(200, r)

    def _resume(self, robot_id: str, user) -> None:
        """解除叫停(W00c5e):之后站点才重新给这只狗派单。``dispatch`` 权限 —— 经理能停,不能恢复
        (能不能让它再动,是会派它的人的事)。"""
        self._need(user, DISPATCH)
        self._body()
        self._audit_target = robot_id
        if self.site.dispatcher.registry.get(robot_id) is None:
            raise HttpError(404, "机器人不存在")
        was = self.site.dispatcher.resume(robot_id, by=str(user))
        return self._send_json(200, {"robot_id": robot_id, "was_held": was})

    def _service(self, robot_id: str, user) -> None:
        """只许手动派 / 接自动派遣(W33):``{manual_only: bool, note?}``。设成只许手动要 ``dispatch``
        (往安全那边走,能派它的人都能关);接自动派遣要 ``manage``(新登记的狗由管理员确认)。"""
        d = self._body()
        on = d.get("manual_only")
        if not isinstance(on, bool):
            raise HttpError(400, "要 manual_only: true / false")
        self._need(user, DISPATCH if on else MANAGE)
        self._audit_target = robot_id
        note = d.get("note") if isinstance(d.get("note"), str) else ""
        self._audit_detail = {"manual_only": on, **({"note": note[:200]} if note else {})}
        reg = self.site.dispatcher.registry
        if reg.get(robot_id) is None:
            raise HttpError(404, "机器人不存在")
        was = reg.set_manual_only(robot_id, on, by=str(user), now_ms=self.site.dispatcher._now(),
                                  note=note)
        return self._send_json(200, {"robot_id": robot_id, "manual_only": reg.manual_only(robot_id),
                                     "was_manual_only": was})

    def _deter(self, robot_id: str, user) -> None:
        """上装(W21):``{"output", "on", "max_s"?, "clip"?, "side"?, "color"?}``
        (``side`` 只给聚光灯、``color`` 只给开警灯,W40)。``dispatch`` 权限(保安、管理员)。
        喇叭的优先级站点定成「人手动的」(``MANUAL``);请求体里的 ``priority`` 不认。"""
        from d1max_site.priorities import MANUAL
        self._need(user, DISPATCH)
        self._audit_target = robot_id
        d = self._body()
        if not isinstance(d, dict):
            raise HttpError(400, "要一个对象")
        d = {k: d[k] for k in ("output", "on", "max_s", "clip", "side", "color") if k in d}
        self._audit_detail = {k: str(v)[:80] for k, v in d.items()}
        from d1max_contract.deter import parse_deter
        from d1max_contract.errors import ContractError
        try:
            parse_deter(d)
        except ContractError as exc:
            raise HttpError(400, str(exc)) from exc
        return self._send_json(200, self.site.dispatch(lambda: self.site.dispatcher.deter(
            robot_id, d, issued_by=str(user), priority=MANUAL)))

    def _relocalize(self, robot_id: str, user) -> None:
        """设位置(W00c6e,里程锚定):``{"x", "y", "yaw"}``、``{"at_home": true}`` 或
        ``{"global": true}``(W41:狗拿看到的墙在整张图上自己找;狗报了 ``relocalize.global``
        才发),转成 ``relocalize`` 命令;坐标按狗**当前加载的那张图**(命令里带上图号,狗核对)。
        ``dispatch`` 权限 —— 保安、管理员。不受监护闸约束(不动)。狗在走、原点没标过这些由狗拒,
        回执原样回。"""
        import math
        self._need(user, DISPATCH)
        self._audit_target = robot_id
        d = self._body()
        if not isinstance(d, dict):
            raise HttpError(400, "要一个对象")
        if "global" in d:
            if d.get("global") is not True:
                raise HttpError(400, "global 只能是 true")
            c0 = self.site.dispatcher.clients.get(robot_id)
            rc = c0.capabilities.tasks.get("relocalize", {}) if c0 and c0.capabilities else {}
            if not (isinstance(rc, dict) and rc.get("global") is True):
                raise HttpError(409, f"{robot_id} 不能自动找位置(要新版代理、配了避障和定位器)")
            payload: dict[str, Any] = {"global": True}
        elif "at_home" in d:
            if d.get("at_home") is not True:
                raise HttpError(400, "at_home 只能是 true")
            payload = {"at_home": True}
        else:
            payload = {}
            for k in ("x", "y", "yaw"):
                v = d.get(k)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise HttpError(400, f"{k} 要是有限数")
                payload[k] = float(v)
        # 客户端在哪张图上点的(外审 W39-I2,可选、要成对):网页底图、手机看的图跟狗加载的可能不是
        # 同一张 —— 原来坐标直接按狗加载的那张补图号,另一张图上的坐标就当成这张的发出去了。说了就
        # 核对,不一样不发;不说的(老客户端)照旧
        want = None
        if "map_id" in d or "map_version" in d:
            mid, ver = d.get("map_id"), d.get("map_version")
            if not (isinstance(mid, str) and mid and isinstance(ver, str) and ver):
                raise HttpError(400, "map_id、map_version 要成对给,都是非空字符串")
            want = (mid, ver)
        c = self.site.dispatcher.clients.get(robot_id)
        caps = c.capabilities.tasks.get("patrol", {}) if c and c.capabilities else {}
        if caps.get("map_id") is not None:
            have = (caps["map_id"], caps.get("map_version"))
            if want is not None and want != have:
                raise HttpError(409, f"位置是在 {want[0]} v{want[1]} 上点的,{robot_id} 加载的是 "
                                     f"{have[0]} v{have[1]}:换到此机器人的图上重新点")
            payload |= {"map_id": have[0], "map_version": have[1]}
        elif want is not None:
            payload |= {"map_id": want[0], "map_version": want[1]}   # 狗没报图:让狗自己核
        self._audit_detail = {k: payload[k] for k in ("x", "y", "yaw", "at_home", "global")
                              if k in payload}
        # 有效期短一点(W00c6e 内审):晚到的旧命令会把「当时的位置」锚到「现在」。30 s 同视频、
        # 遥控续租
        # (Orin 的钟不准,再短就收不下)。
        from d1max_site.dispatcher import VIDEO_COMMAND_TTL_MS
        return self._send_json(200, self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, "relocalize", payload, issued_by=str(user), ttl_ms=VIDEO_COMMAND_TTL_MS)))

    def _mark_home(self, robot_id: str, user) -> None:
        """在当前位置标原点(W00c6f):发 ``mark_home``,狗用它此刻锚定后的位置、定位不好就拒;站点把回执
        里的位置登记成这只狗的**默认待命点**(站点是权威,之后下发地图、回待命点都用它)。管理员(``manage``,
        跟下发地图同一级)。

        - 同名的点登记在别的图或版本上:409(``name_taken``),说了 ``replace: true`` 才搬过来
          (内审应修 5)。
        - 狗拒了 409 带原因(重投的回执按原结果,内审应修 2)。
        - 狗收下了、站点登记失败:500,带上狗上的位置和命令号(审计里也有),好手工补登
          (内审应修 7)。
        - 等回执超时:504,狗可能已经标了 —— 标了的话狗发的 ``home_marked`` 到了站点会补登记
          (内审应修 1,见 ``StandbyManager``)。"""
        from d1max_site.dispatcher import VIDEO_COMMAND_TTL_MS
        from d1max_site.standby import StandbyError
        self._need(user, MANAGE)
        self._audit_target = robot_id
        stb = self.site.standby
        if stb is None:
            raise HttpError(404, "此站点未启用待命点")
        d = self._body()
        if not isinstance(d, dict):
            raise HttpError(400, "要一个对象")
        name = d.get("name", "home")
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise HttpError(400, f"名字只许 ASCII 字母、数字、. _ -:{name!r}")
        replace = d.get("replace", False)
        if not isinstance(replace, bool):
            raise HttpError(400, "replace 要是 true/false")
        self._audit_detail = {"name": name}
        c = self.site.dispatcher.clients.get(robot_id)
        caps = c.capabilities.tasks.get("patrol", {}) if c and c.capabilities else {}
        loaded = (caps.get("map_id"), caps.get("map_version"))
        old = next((p for p in stb.list(robot_id) if p["name"] == name), None)
        if old is not None and loaded[0] is not None and not replace \
                and (old["map_id"], old["map_version"]) != loaded:
            raise HttpError(409, f"待命点 {name} 登记在 {old['map_id']}:{old['map_version']} 上,"
                                 f"机器人现在用的是 {loaded[0]}:{loaded[1]}:换个名字,"
                                 "或者说明要替换",
                            extra={"name_taken": {"map_id": old["map_id"],
                                                  "map_version": old["map_version"]}})
        # 有效期同设位置(30 s,Orin 的钟不准,再短就收不下);超时之后狗照样标了的,按事件补登记。
        try:
            r = self.site.dispatch(lambda: self.site.dispatcher.map_command(
                robot_id, "mark_home", {"name": name}, issued_by=str(user),
                ttl_ms=VIDEO_COMMAND_TTL_MS))
        except HttpError as exc:
            if exc.status == 504:
                raise HttpError(504, "等机器人回话超时:机器人可能已标了 —— "
                                     "标了的话站点收到机器人的消息会自动"
                                     "登记待命点,过一会儿刷新看看") from exc
            raise
        ack = r["ack"]
        got = ack if ack.get("result") != "duplicate" else (ack.get("original") or {})
        self._audit_detail |= {"command_id": r.get("command_id"), "task_id": r.get("task_id")}
        if got.get("result") != "accepted":
            raise HttpError(409, f"机器人标记失败:{got.get('reason') or got.get('result')}")
        data = got.get("data")
        if isinstance(data, dict):
            self._audit_detail |= {"map": f"{data.get('map_id')}:{data.get('map_version')}",
                                   "x": data.get("x"), "y": data.get("y"), "yaw": data.get("yaw")}
        try:
            stb.mark(robot_id, name, data)
        except (StandbyError, KeyError, TypeError) as exc:
            raise HttpError(500, f"机器人上的原点已更换,站点登记原点失败:{exc}。"
                                 "按下面的位置手工登记",
                            extra={"command_id": r.get("command_id"), "task_id": r.get("task_id"),
                                   "data": data}) from exc
        home = stb.home(robot_id, data["map_id"], data["map_version"])
        point = next((p for p in stb.list(robot_id) if p["name"] == name), None)
        return self._send_json(200, {"ack": ack, "home": home, "standby": point})

    def _here_pose(self, robot_id: str, user, name: str) -> dict:
        """狗此刻锚定后的地图位姿(``mark_home {target: standby}``:检查同标原点,狗上什么都不改)。
        能力里没报 ``mark_home.standby`` 的老代理不发(它会当成标原点)。不成就抛 ``HttpError``。"""
        from d1max_site.dispatcher import VIDEO_COMMAND_TTL_MS
        c = self.site.dispatcher.clients.get(robot_id)
        caps = c.capabilities.tasks if c and c.capabilities else {}
        if not (caps.get("mark_home") or {}).get("standby"):
            raise HttpError(409, f"{robot_id} 的代理无法上报「只要位置」(老代理会把它当成标原点):"
                                 "先升级机器人上的版本")
        try:
            r = self.site.dispatch(lambda: self.site.dispatcher.map_command(
                robot_id, "mark_home", {"name": name, "target": "standby"},
                issued_by=str(user), ttl_ms=VIDEO_COMMAND_TTL_MS))
        except HttpError as exc:
            if exc.status == 504:
                raise HttpError(504, "等机器人回话超时:机器人上什么都未改,再按一次") from exc
            raise
        ack = r["ack"]
        got = ack if ack.get("result") != "duplicate" else (ack.get("original") or {})
        self._audit_detail |= {"command_id": r.get("command_id"), "task_id": r.get("task_id")}
        if got.get("result") != "accepted":
            raise HttpError(409, f"机器人标记失败:{got.get('reason') or got.get('result')}")
        data = got.get("data")
        if not isinstance(data, dict) or data.get("target") != "standby":
            raise HttpError(500, "机器人未按「只要位置」回(老代理会把它当成标原点),什么都未登记")
        return data

    def _intercept_here(self, robot_id: str, user) -> None:
        """在狗现在的位置设拦截点(W16):位置同「在这儿设待命点」(狗用此刻锚定后的位姿、定位不好就拒,
        狗上什么都不改);写进拦截点(地图、版本跟着狗现在用的那张)。管理员。"""
        from d1max_site.incidents import IncidentError
        self._need(user, MANAGE)
        self._audit_target = robot_id
        d = self._body()
        if not isinstance(d, dict):
            raise HttpError(400, "要一个对象")
        name = d.get("name")
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise HttpError(400, f"名字只许 ASCII 字母、数字、. _ -:{name!r}")
        self._audit_detail = {"intercept": name}
        desk = self._desk()
        data = self._here_pose(robot_id, user, name)
        try:
            desk.set_intercept(name, map_id=data["map_id"], map_version=data["map_version"],
                               x=data["x"], y=data["y"], yaw=data["yaw"])
        except IncidentError as exc:
            # W23:狗站的这个位置走不到、站不下、不在图上:不收,原因原样给人
            raise HttpError(409, str(exc), extra={"data": data}) from exc
        except KeyError as exc:
            raise HttpError(500, f"站点登记拦截点失败:{exc}", extra={"data": data}) from exc
        self._audit_detail |= {"map": f"{data['map_id']}:{data['map_version']}",
                               "x": data["x"], "y": data["y"]}
        return self._send_json(200, {"ack": {"result": "accepted", "data": data}}
                               | desk.intercepts())

    def _standby_here(self, robot_id: str, user) -> None:
        """在当前位置设待命点(W13a,决策 16):发 ``mark_home {target: standby}``,
        狗用此刻锚定后的位置、
        检查跟标原点一样(停着、不忙、定位好),但**不动狗上的原点**;站点登记成待命点。跟标原点同一级
        (``manage``)。老代理不认 ``target``、会当成标原点 —— 能力里没报 ``mark_home.standby``
        就不发。
        超时(504)不补登记:狗上什么都没改,再按一次就是。"""
        from d1max_site.standby import StandbyError
        self._need(user, MANAGE)
        self._audit_target = robot_id
        stb = self.site.standby
        if stb is None:
            raise HttpError(404, "此站点未启用待命点")
        d = self._body()
        if not isinstance(d, dict):
            raise HttpError(400, "要一个对象")
        name = d.get("name")
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise HttpError(400, f"名字只许 ASCII 字母、数字、. _ -:{name!r}")
        default = d.get("default")
        if default is not None and not isinstance(default, bool):
            raise HttpError(400, "default 要是 true/false")
        self._audit_detail = {"name": name}
        data = self._here_pose(robot_id, user, name)
        try:
            stb.mark_standby(robot_id, name, data, default)
        except (StandbyError, KeyError, TypeError) as exc:
            raise HttpError(500, f"站点登记待命点失败:{exc}",
                            extra={"data": data}) from exc
        if isinstance(data, dict):
            self._audit_detail |= {"map": f"{data.get('map_id')}:{data.get('map_version')}",
                                   "x": data.get("x"), "y": data.get("y")}
        point = next((p for p in stb.list(robot_id) if p["name"] == name), None)
        return self._send_json(200, {"ack": {"result": "accepted", "data": data},
                                     "standby": point})

    def _mapping_read(self, robot_id: str, what: str, user) -> None:
        """录包时的轨迹(W00c6h)、建图预览(W09f),管理员 —— 录包本身就是管理员的事:发
        ``mapping_trail {since}`` / ``mapping_preview {since}``,回执里的数据原样回(轨迹:
        ``{points, since, total, full, recording}``,录包起点为原点的里程系;预览:``{live, seq, png?,
        res, origin, pose, trail, …}``,``since`` 是上次拿到的 ``seq``)。"""
        self._need(user, MANAGE)
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        try:
            since = int(q["since"][0]) if "since" in q else 0
        except ValueError:
            raise HttpError(400, "since 要是不小于 0 的整数") from None
        if since < 0:
            raise HttpError(400, "since 要是不小于 0 的整数")
        kind = f"mapping_{what}"
        r = self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, kind, {"since": since}, issued_by=str(user)))
        ack = r["ack"]
        if ack.get("result") != "accepted":
            reason = ack.get("reason") or ack.get("result")
            raise HttpError(409, f"机器人未提供:{reason}",
                            extra={"unsupported": True} if reason == "unsupported" else None)
        data = ack.get("data")
        if not isinstance(data, dict):
            raise HttpError(502, "机器人的回执里没有" + ("轨迹" if what == "trail" else "预览"))
        return self._send_json(200, data)

    def _proc_logs(self, robot_id: str, name: str | None, user) -> None:
        """建图进程日志(W00c6g):狗上录包、重建子进程日志的列表(``name`` 为空)与某一个的尾巴。
        **管理员**(``manage``,跟录包、重建同一级:日志里有路径、参数)。站点不存,现取现给:发
        ``proc_log``,回执里的数据原样回(事件流里推的回执不带它,见 ``Dispatcher``)。狗说没有这个
        日志 404,别的拒收 409;老代理(没报这项能力)409。"""
        self._need(user, MANAGE)
        payload: dict[str, Any] = {}
        if name is not None:
            name = unquote(name)
            if not _LOG_NAME.fullmatch(name):
                raise HttpError(400, "日志名只许小写字母、数字、. _ -(1–48 字)")
            payload["name"] = name
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if "bytes" in q:
                try:
                    n = int(q["bytes"][0])
                except ValueError:
                    raise HttpError(400, "bytes 要是正整数") from None
                if n < 1:
                    raise HttpError(400, "bytes 要是正整数")
                payload["bytes"] = n
        r = self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, "proc_log", payload, issued_by=str(user)))
        ack = r["ack"]
        if ack.get("result") != "accepted":
            reason = ack.get("reason") or ack.get("result")
            raise HttpError(404 if reason == "no_such_log" else 409, f"机器人未提供:{reason}")
        data = ack.get("data")
        if not isinstance(data, dict):
            raise HttpError(502, "机器人的回执里没有日志")
        return self._send_json(200, data)

    # ------------------------------------------------------------ W55 远程运维

    #: 机器人运维动作 → (方法, 机器人命令)。读的走 GET,动手的走 POST。
    _OPS_ACTIONS = {"journal": ("GET", "ops_journal"), "diag": ("GET", "ops_diag"),
                    "cleanup": (None, "ops_cleanup"), "restart": ("POST", "ops_restart"),
                    "reboot": ("POST", "ops_reboot"), "debug": ("POST", "ops_debug"),
                    "timesync": ("POST", "ops_timesync")}

    def _robot_ops(self, method: str, robot_id: str, action: str, user) -> None:
        """W55:``/api/robots/<id>/ops/<动作>`` —— 日志、诊断、清理、重启服务、重启整机(再输密码)、
        详细日志、对时、立即续签证书。只给管理员、工程师(``maintain``)。"""
        self._need(user, MAINTAIN)
        self._audit_target = robot_id
        if action == "cert-renew":
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            if self.site.cert_renewer is None:
                raise HttpError(404, "此站点未启用证书自动续签")
            try:
                got = self.site.loop.call(lambda: self.site.cert_renewer.renew(
                    robot_id, by=str(user)), timeout_s=120)
            except HttpError:
                raise
            except Exception as exc:  # 哪一步不行都说清楚(RenewError、CA、超时)
                raise HttpError(409, f"续签失败:{exc}") from exc
            return self._send_json(200, got)
        spec = self._OPS_ACTIONS.get(action)
        if spec is None:
            raise HttpError(404, f"没有这个运维动作:{action}")
        want, kind = spec
        if want is not None and method != want:
            raise HttpError(405, f"只支持 {want}")
        if method == "GET":
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            payload: dict[str, Any] = {}
            if action == "journal" and "unit" in q:
                payload["unit"] = q["unit"][0]
                h = q.get("hours", ["6"])[0]
                payload["hours"] = int(h) if h.isdigit() else -1
        else:
            payload = self._body()
            if action == "reboot":
                self._confirm(user, payload)
                payload = {}
            payload.pop("confirm_password", None)
            self._audit_detail = {k: v for k, v in payload.items()
                                  if isinstance(v, (str, int, list))}
        r = self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, kind, payload, issued_by=str(user)))
        ack = r["ack"]
        if ack.get("result") != "accepted":
            reason = str(ack.get("reason") or ack.get("result"))
            raise HttpError(409, f"机器人未执行:{reason}", extra={"reason": reason})
        return self._send_json(200, ack.get("data") if isinstance(ack.get("data"), dict) else {})

    def _bundle_extra(self, hours: int) -> dict[str, bytes]:
        """诊断包里另外带的(W55):每台在线机器人的诊断快照、主要服务的日志;App、网页的错误记录。
        机器人那边一项取不到写进它的 errors.txt,别的照样打包。"""
        from d1max_site.remote_ops import dumps
        out: dict[str, bytes] = {}
        since = self.site._now() - hours * 3_600_000
        if self.site.client_errors is not None:
            out["client_errors.json"] = dumps(self.site.client_errors.recent(since))
        disp = self.site.dispatcher
        units = ("d1max-agent", "d1max-localizer", "d1max-obstacles", "d1max-persons",
                 "d1max-lidar-merge", "d1max-sidecar")

        async def one(rid: str, kind: str, payload: dict[str, Any]) -> tuple[str, Any]:
            try:
                r = await disp.map_command(rid, kind, payload, issued_by="site:support-bundle")
                ack = r["ack"]
                if ack.get("result") != "accepted":
                    return "err", f"{kind} {payload.get('unit', '')}:{ack.get('reason')}"
                return "ok", ack.get("data")
            except Exception as exc:  # noqa: BLE001
                return "err", f"{kind} {payload.get('unit', '')}:{type(exc).__name__}: {exc}"

        async def gather() -> dict[str, list[tuple[str, dict[str, Any], tuple[str, Any]]]]:
            import asyncio
            jobs: list[tuple[str, str, dict[str, Any]]] = []
            for rid, c in list(getattr(disp, "clients", {}).items()):
                tasks = getattr(getattr(c, "capabilities", None), "tasks", {}) or {}
                if not (c.status and c.status.online):
                    continue
                if "ops_diag" in tasks:
                    jobs.append((rid, "ops_diag", {}))
                for u in units:
                    if u in (tasks.get("ops_journal") or {}).get("units", []):
                        jobs.append((rid, "ops_journal", {"unit": u, "hours": min(hours, 168)}))
            got = await asyncio.gather(*(one(r, k, p) for r, k, p in jobs))
            res: dict[str, list[Any]] = {}
            for (rid, kind, p), g in zip(jobs, got, strict=True):
                res.setdefault(rid, []).append((kind, p, g))
            return res

        try:
            per = self.site.loop.call(gather, timeout_s=90)
        except Exception as exc:  # noqa: BLE001
            out["robots/errors.txt"] = f"取机器人数据失败:{exc}\n".encode()
            return out
        for rid, items in per.items():
            errs = []
            for kind, p, (st, data) in items:
                if st != "ok":
                    errs.append(str(data))
                elif kind == "ops_diag":
                    out[f"robots/{rid}/diag.json"] = dumps(data)
                else:
                    out[f"robots/{rid}/{p['unit']}.log"] = str(
                        (data or {}).get("text", "")).encode()
            if errs:
                out[f"robots/{rid}/errors.txt"] = ("\n".join(errs) + "\n").encode()
        return out

    def _client_errors(self, method: str, user) -> None:
        """W55:App、网页报自己的错误(登录着的谁都能报);站点留 14 天,进诊断包。"""
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        if self.site.client_errors is None:
            raise HttpError(404, "此站点未启用错误记录")
        d = self._body()
        client = str(d.get("client") or "")
        if client not in ("app", "web"):
            raise HttpError(400, "client 要是 app 或 web")
        self._skip_audit = True                 # 每 10 分钟一批,不进操作记录
        try:
            n = self.site.client_errors.add(str(user), client, d.get("items"))
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        return self._send_json(200, {"stored": n})

    def _system(self, method: str, path: str, user) -> None:
        """W55 站点级运维:重启站点、测试推送、备份状态与恢复、升级站点、VPN 状态。"""
        self._need(user, MAINTAIN)
        what = path.removeprefix("/api/system/")
        priv = self.site.priv
        if what == "push-test":
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            if self.site.push is None or not self.site.push.senders:
                raise HttpError(409, "此站点未配置推送")
            acc = str(self._body().get("account") or user)
            return self._send_json(200, self.site.push.send_test(acc))
        if what == "backups":
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            if self.site.backup is None:
                raise HttpError(404, "此站点未启用备份")
            return self._send_json(200, self.site.backup.status())
        if what == "vpn":
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            vpn = self.site.vpn
            out: dict[str, Any] = {"ready": bool(vpn is not None and vpn.ready),
                                   "peers": vpn.list() if vpn is not None and vpn.ready else []}
            if priv is not None:
                try:
                    got = priv.call("wg_status", timeout_s=20)
                    out["status"] = got if got.get("ok") else {"error": got.get("error")}
                except Exception as exc:  # noqa: BLE001
                    out["status"] = {"error": str(exc)[:200]}
            return self._send_json(200, out)
        if what in ("restart", "restore", "update"):
            return self._system_priv(method, what, user, priv)
        raise HttpError(404, f"没有 /api/system/{what}")

    def _system_priv(self, method: str, what: str, user, priv: Any) -> None:
        """重启站点、恢复备份、升级站点:都会让站点自己停掉,所以只交请求、马上回 202;结果之后
        ``GET /api/system/<同一个>`` 查(执行器写在结果目录里,站点重启后照样读得到)。"""
        import json as _json
        home = self.site.home
        if home is None:
            raise HttpError(404, "此站点未启用站点目录")
        last = home / "ops-last.json"
        if method == "GET":
            try:
                rec = _json.loads(last.read_text(encoding="utf-8")).get(what)
            except (OSError, ValueError):
                rec = None
            if rec and priv is not None:
                rec = {**rec, "result": priv.result(rec["id"])}
            return self._send_json(200, {"last": rec})
        if method != "POST":
            raise HttpError(405, "只支持 GET、POST")
        if priv is None:
            raise HttpError(409, "此站点未安装特权执行器(装机脚本会装):只能上主机操作")
        args: dict[str, Any] = {}
        if what == "update":
            name = self._receive_update(user)
            args = {"package": name}
        else:
            self._confirm(user, self._body())
        action = {"restart": "restart_site", "restore": "restore", "update": "update"}[what]
        rid = priv.submit(action, args)
        try:
            rec = _json.loads(last.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = {}
        rec[what] = {"id": rid, "by": str(user), "at_ms": self.site._now(), **args}
        last.write_text(_json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        self._audit_detail = {"request": rid, **args}
        return self._send_json(202, {"id": rid})

    def _engineer_vpn(self, account: str, until: int) -> dict[str, Any]:
        """W55:工程师的 VPN 配置(同账号一起到点)。VPN 没装、执行器没装都明说,账号照开。"""
        from d1max_site.vpn import VpnError
        vpn = self.site.vpn
        if vpn is None or not vpn.ready:
            return {"error": "此站点未安装 VPN(d1max-site vpn init),工程师只能在现场局域网里连"}
        try:
            got = vpn.add("engineer", f"eng-{account}", expires_ms=until)
            conf = vpn.peer_conf(got["name"])
        except VpnError as exc:
            return {"error": str(exc)}
        out: dict[str, Any] = {"conf": conf, "address": got["address"], "expires_ms": until}
        if self.site.priv is None:
            out["warning"] = "站点未安装特权执行器:要在站点主机上重新装一遍 VPN 配置才生效"
            return out
        why = self.site.vpn_sync()           # 没装成的站点每一轮接着装(外审 W55-1 第 10 条)
        if why:
            out["warning"] = f"VPN 配置暂未生效,站点会自动重试:{why}"[:300]
        return out

    def _receive_update(self, user) -> str:
        """站点升级包(公司签名的 ``.tar.gz``)收进 ``<站点目录>/updates/``;验签由执行器做(root、
        root 属主的公钥),这里先核得出是个包。再输一次密码在 ``X-D1Max-Confirm`` 头里。"""
        home = self.site.home
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise HttpError(400, "Content-Length 不是数") from exc
        if n <= 0:
            raise HttpError(400, "未收到文件")
        if n > self.MAX_UPLOAD:
            raise HttpError(413, "升级包过大(最多 500 MB)")
        d = home / "updates"
        d.mkdir(exist_ok=True)
        for old in d.glob("site-*.tar.gz"):          # 只留这一个
            old.unlink(missing_ok=True)
        name = f"site-{self.site._now()}.tar.gz"
        dest = d / name
        left = n
        with open(dest, "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    dest.unlink(missing_ok=True)
                    raise HttpError(400, "文件未传完")
                fh.write(chunk)
                left -= len(chunk)
        try:
            self._confirm(user, {"confirm_password": self.headers.get("X-D1Max-Confirm") or ""})
        except HttpError:
            dest.unlink(missing_ok=True)
            raise
        import tarfile
        try:
            with tarfile.open(dest, "r:gz") as tf:
                if not any(m.name.endswith("/release.json") for m in tf.getmembers()):
                    raise HttpError(400, "这不是站点升级包(里面没有 release.json)")
        except (tarfile.TarError, OSError, EOFError) as exc:
            dest.unlink(missing_ok=True)
            raise HttpError(400, "这不是升级包(打不开)") from exc
        return name

    def _app_update(self, method: str, path: str, user) -> None:
        """W55 App 安装包:``GET /api/app-update?platform=&build=`` 有没有新的(登录着的都能问);
        ``GET /api/app-update/file/<平台>`` 下载;``POST /api/app-update/upload?platform=``
        上传(管理员、工程师;签名清单放在 ``X-App-Manifest`` 头里,base64 的 JSON)。"""
        from d1max_site.app_dist import AppDistError
        apps = self.site.apps
        if apps is None:
            raise HttpError(404, "此站点未启用 App 分发")
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        try:
            if path == "/api/app-update" and method == "GET":
                b = q.get("build", ["0"])[0]
                return self._send_json(200, apps.check(q.get("platform", [""])[0],
                                                       int(b) if b.isdigit() else 0))
            if path.startswith("/api/app-update/file/") and method == "GET":
                f, m = apps.file(path.removeprefix("/api/app-update/file/"))
                self._audit_target = m["name"]
                apk = m["name"].endswith(".apk")
                return self._send_file(f, "application/vnd.android.package-archive" if apk
                                       else "application/octet-stream")
            if path == "/api/app-update/upload" and method == "POST":
                self._need(user, MAINTAIN)
                import base64
                import json as _json
                try:
                    manifest = _json.loads(base64.b64decode(
                        self.headers.get("X-App-Manifest") or "", validate=True))
                except ValueError as exc:
                    raise HttpError(400, "X-App-Manifest 要是 base64 的 JSON") from exc
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError as exc:
                    raise HttpError(400, "Content-Length 不是数") from exc
                if n <= 0 or n > self.MAX_UPLOAD:
                    raise HttpError(413 if n > 0 else 400, "安装包为空或过大")
                buf = bytearray()
                while len(buf) < n:
                    chunk = self.rfile.read(min(n - len(buf), 1 << 20))
                    if not chunk:
                        raise HttpError(400, "文件未传完")
                    buf += chunk
                meta = apps.add(q.get("platform", [""])[0], bytes(buf), manifest,
                                now_ms=self.site._now())
                self._audit_target = meta["name"]
                return self._send_json(200, meta)
        except AppDistError as exc:
            raise HttpError(400 if "没有" not in str(exc) else 404, str(exc)) from exc
        raise HttpError(404, f"没有 {method} {path}")

    def _supervise(self, robot_id: str, user) -> None:
        """监护心跳(W00c6i):``{"action": "renew"|"release"}``。``dispatch`` 权限 —— 保安、
        管理员;经理不行(在现场看着狗动,是会派它的人的事)。审计只记开始与结束,续着的心跳不记。"""
        self._need(user, DISPATCH)
        d = self._body()
        action = d.get("action") if isinstance(d, dict) else None
        if action not in ("renew", "release"):
            raise HttpError(400, "action 要是 renew 或 release")
        session, seq = d.get("session"), d.get("seq")
        if not isinstance(session, str) or not 1 <= len(session) <= 64:
            raise HttpError(400, "要 session(1–64 字)")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise HttpError(400, "要 seq(≥1 的整数)")
        self._audit_target = robot_id
        if self.site.dispatcher.registry.get(robot_id) is None:
            raise HttpError(404, "机器人不存在")
        desk = self.site.supervision
        try:
            if action == "renew":
                ack, started = desk.renew(robot_id, str(user), session, seq)
                self._skip_audit = not started
                self._audit_detail = {"supervise": "start", "session": session} if started else {}
            else:
                ack = desk.release(robot_id, str(user), session, seq)
                self._audit_detail = {"supervise": "stop", "session": session}
        except DispatchRefused as exc:
            raise HttpError(409, str(exc)) from exc
        except (DispatchTimeout, TimeoutError, FutureTimeout) as exc:
            raise HttpError(504, f"等机器人的回执超时: {exc}") from exc
        return self._send_json(200, {"robot_id": robot_id, "ack": ack})

    def _teleop_ws(self, robot_id: str, user) -> None:
        """遥控(W00c5c):先开租约(拒绝回 HTTP 状态码),再升级成 WebSocket;连接就是租约的载体。"""
        from d1max_site.teleop import TeleopRefused
        from d1max_site.ws import WsClosed, check_upgrade, upgrade
        self._need(user, TELEOP)
        desk = self.site.teleop
        if desk is None:
            raise HttpError(404, "此站点未启用遥控")
        if check_upgrade(self.headers) is None:
            # 先查升级头再开租约:一个普通 GET 不许授予租约、抢占正在跑的巡检(W00c5c 内部评审)。
            raise HttpError(400, "要 WebSocket 升级请求")
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        reason = (q.get("takeover") or [""])[0].strip()[:200]
        try:
            session = desk.open(robot_id, user, takeover_reason=reason)
        except TeleopRefused as exc:
            raise HttpError(exc.status, exc.message,
                            extra={"reason": exc.reason} if exc.reason else None) from exc
        ws = upgrade(self)
        if ws is None:
            desk.close(session, "bad_upgrade")
            return
        session.ws = ws
        token = self._token()
        checked = time.monotonic()
        try:
            granted = {"kind": "granted", "lease_epoch": session.epoch,
                       "operator": session.operator, "max_vx": session.max_vx,
                       "max_wz": session.max_wz, "max_vy": session.max_vy,
                       "frame_period_ms": 100}
            if session.zone_told is not None:
                granted["zone_guard"] = session.zone_told    # 决策 65:老狗不守硬禁区,开场就说
            ws.send_text(json.dumps(granted, ensure_ascii=False))
            while not session.ended.is_set() and not self.site.stopping:
                msg = ws.recv(timeout_s=0.5)
                if msg is not None:
                    # 连接上攒着的一并收下(4G 憋了一下):都过判帧,只转最新的那一帧。
                    batch, rx = [msg], time.monotonic()
                    while len(batch) < 64:
                        more = ws.recv(timeout_s=0)
                        if more is None:
                            break
                        batch.append(more)
                    desk.on_messages(session, batch, rx=rx)
                if time.monotonic() - checked >= self.site.sse_recheck_s:
                    checked = time.monotonic()
                    who = self.site.accounts.check(token)
                    if who is None or not allowed(who.role, TELEOP):
                        desk.close(session, "logged_out")
        except WsClosed:
            pass
        finally:
            desk.close(session, "disconnected")    # 幂等:已经结束的不再结束一次
            ws.close(1000, session.end_reason)     # 别的线程排给它的「结束了」由这条线程发出去

    def _runs(self, method: str, path: str, user) -> None:
        """运行记录(W00c5d,决策 8:证据都在站点)。看:``view``(经理也能看);判读、复核:``review``
        (管理员、保安);导出:``export``(管理员)。"""
        from d1max_site.runs import RunError
        desk = self.site.runs
        if desk is None:
            raise HttpError(404, "此站点未启用证据库")
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

        def num(k: str) -> int | None:
            v = (q.get(k) or [""])[0]
            if not v:
                return None
            if not v.isdigit():
                raise HttpError(400, f"{k} 要是非负整数")
            return int(v)
        try:
            if path == "/api/runs" and method == "GET":
                self._need(user, VIEW)
                robot = (q.get("robot") or [None])[0]
                return self._send_json(200, {"runs": desk.store.runs(
                    robot_id=robot, since_ms=num("since"), until_ms=num("until"),
                    limit=num("limit") or 200, mission=(q.get("mission") or [None])[0])})
            m = _RUN.match(path)
            if m is not None:
                rid, what, arg = int(m.group(1)), m.group(2), m.group(3)
                self._audit_target = f"run/{rid}"
                if method == "GET" and what is None:
                    self._need(user, VIEW)
                    got = desk.detail(rid)
                    self._audit_view(f"run/{rid}")
                    return self._send_json(200, got)
                if method == "GET" and what == "photos" and arg:
                    self._need(user, VIEW)
                    p = desk.photo(rid, unquote(arg))
                    self._audit_view(f"run/{rid}/photo/{unquote(arg)[:100]}")
                    return self._send_file(p, "image/jpeg")
                if method == "POST" and what == "keep" and arg is None:
                    # W30(PDPA):标「留着」—— 过了留存期、按时间段删都不删(要留作证据的)
                    self._need(user, REVIEW)
                    d = self._body()
                    if not isinstance(d.get("keep"), bool) or self.site.privacy is None:
                        raise HttpError(400, "要 keep: true/false")
                    self._audit_detail = {"keep": d["keep"]}
                    try:
                        return self._send_json(200, self.site.privacy.set_keep(rid, d["keep"]))
                    except KeyError as exc:
                        raise HttpError(404, "任务记录不存在") from exc
                if method == "POST" and what == "judge" and arg is None:
                    self._need(user, REVIEW)
                    self._body()
                    return self._send_json(202, desk.judge_async(rid))
                if method == "POST" and what == "review" and arg:
                    self._need(user, REVIEW)
                    d = self._body()
                    self._audit_detail = {"photo": unquote(arg)[:200],
                                          "verdict": str(d.get("verdict", ""))[:16]}
                    return self._send_json(200, desk.review(
                        rid, unquote(arg), verdict=str(d.get("verdict", "")),
                        note=str(d.get("note", ""))))
            if path == "/api/exports":
                self._need(user, EXPORT)
                if method == "GET":
                    return self._send_json(200, {"exports": desk.exports()})
                if method == "POST":
                    d = self._body()
                    since, until = d.get("since_ms"), d.get("until_ms")
                    if not all(isinstance(v, int) and not isinstance(v, bool)
                               for v in (since, until)):
                        raise HttpError(400, "since_ms、until_ms 要是整数")
                    robot = d.get("robot_id")
                    meta = desk.export(since_ms=since, until_ms=until,
                                       robot_id=robot if isinstance(robot, str) else None)
                    self._audit_detail = {"export": meta["name"], "runs": meta["runs"]}
                    # 在后台打:202,之后看 GET /api/exports 里它的 state(building → ready/failed)。
                    return self._send_json(202, meta)
            m = _EXPORT.match(path)
            if m is not None and method == "GET":
                self._need(user, EXPORT)
                p = desk.export_path(unquote(m.group(1)))
                self._audit_view(f"export/{unquote(m.group(1))[:100]}", action="DOWNLOAD")
                return self._send_file(p, "application/zip")
        except RunError as exc:
            msg = str(exc)
            missing = msg.startswith(("没有", "任务记录不存在", "本次任务里没有"))
            raise HttpError(404 if missing else 400, msg) from exc
        raise HttpError(404, f"没有 {method} {path}")

    def _maps(self, method: str, path: str, user) -> None:
        """地图(W00c5d 第二部分,决策 8:站点是地图的唯一权威)。看:``view``;下发、录包、重建:
        ``manage``(管理员)。命令狗收下就回,做完狗发事件,失败出 ``map_failed`` 告警。"""
        from d1max_contract.errors import ContractError
        from d1max_contract.maps import mapping_target, parse_map_build, parse_mapping
        from d1max_site.maps import MapError
        cat = self.site.maps
        if cat is None:
            raise HttpError(404, "此站点未启用地图目录")
        if path == "/api/maps" and method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, {"maps": cat.list(), "bags": cat.bags()})
        m = _MAPVIEW.match(path)
        if m is not None and method == "GET":
            self._need(user, VIEW)
            return self._map_preview(unquote(m.group(1)), unquote(m.group(2)), m.group(3))
        m = _ZONES.match(path)
        if m is not None:
            return self._zones(method, unquote(m.group(1)), unquote(m.group(2)),
                               (m.group(3) or "").lstrip("/"), user)
        m = _ROUTENET.match(path)
        if m is not None:
            return self._route_net(method, unquote(m.group(1)), unquote(m.group(2)), user)
        m = _MAPCMD.match(path)
        if m is None or method != "POST":
            raise HttpError(404, f"没有 {method} {path}")
        self._need(user, MANAGE)
        robot_id, what = unquote(m.group(1)), m.group(2)
        if not SAFE_ID.match(robot_id):
            raise HttpError(404, "机器人不存在")
        self._audit_target = robot_id
        d = self._body()
        try:
            if what == "map":
                ref = cat.get(str(d.get("map_id", "")), str(d.get("version", "")))
                kind, payload = "map_activate", self._map_activate_payload(
                    robot_id, ref, d.get("without_home", False))
            elif what == "mapping":
                action, name = parse_mapping(d)
                if len(name) > 40:
                    raise HttpError(400, "包名最多 40 个字符(机器人还要加录的时刻、"
                                         "文件名还要加后缀)")
                kind, payload = "mapping", {"action": action, **({"name": name} if name else {})}
                target = mapping_target(d)
                if target is not None:           # W09c2:录包的同时在线建这一版
                    c = self.site.dispatcher.clients.get(robot_id)
                    caps = c.capabilities if c is not None else None
                    if caps is None or not caps.tasks.get("mapping", {}).get("live"):
                        # 老版本的狗不认地图号、版本:只录包、回收下,这一版就一直挂在「正在建」
                        raise HttpError(409, f"{robot_id} 不能边走边建(先升级);只录包就别填"
                                             "地图号、版本")
                    self._check_new_version(cat, *target)
                    payload |= {"map_id": target[0], "version": target[1]}
            elif what == "outbox_retry":
                # 站点改了收件规矩之后,让狗把隔离的文件再传一次(W00c5d 内部评审)。
                kind, payload = "outbox_retry", {}
            else:
                bag, map_id, version = parse_map_build(d)
                self._check_new_version(cat, map_id, version)
                kind, payload = "map_build", {"bag": bag, "map_id": map_id, "version": version}
        except MapError as exc:
            raise HttpError(404, str(exc)) from exc
        except ContractError as exc:
            raise HttpError(400, str(exc)) from exc
        self._audit_detail |= {k: v for k, v in payload.items() if k != "files"}
        return self._send_json(200, self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, kind, payload, issued_by=str(user))))

    def _map_wizard(self, method: str, user) -> None:
        """W54 建图向导(:mod:`d1max_site.map_wizard`):``GET`` 谁都能看(保安看得到「有人在建图」、
        站点有没有图);``POST`` ``{action, …}`` 要 ``manage``(管理员、工程师)。"""
        from d1max_site.map_wizard import MapWizard, WizardError
        cat, settings = self.site.maps, self.site.settings
        if cat is None or settings is None:
            raise HttpError(404, "此站点未启用地图目录")
        reg = self.site.dispatcher.registry

        def known(rid: str) -> bool:
            rec = reg.get(rid) if SAFE_ID.match(rid) else None
            return rec is not None and not getattr(rec, "revoked", False)
        w = MapWizard(settings, cat, self.site.dispatcher.db, now_ms=self.site._now, known=known,
                      zones=self.site.zones)
        if method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, {"wizard": w.view(), "maps": len(cat.list())})
        if method != "POST":
            raise HttpError(404, f"没有 {method} /api/map-wizard")
        self._need(user, MANAGE)
        d = self._body()
        try:
            out = w.apply(d, by=str(user))
        except WizardError as exc:
            raise HttpError(exc.status, str(exc),
                            extra={"reason": exc.reason} if exc.reason else None) from exc
        self._audit_target = "map_wizard"
        self._audit_detail = {k: d.get(k) for k in ("action", "step", "robot_id", "site_type",
                                                     "drive") if k in d} | (
            {"version": out["version"]} if out.get("version") else {})
        return self._send_json(200, {"wizard": out or None})

    def _maps_admin(self, method: str, path: str, user) -> None:
        """地图管理页(管理员):见 ``maps_admin_api``。"""
        from d1max_site.maps_admin_api import handle
        handle(self, method, path, user)

    def _map_activate_payload(self, robot_id: str, ref: Any, without: Any) -> dict[str, Any]:
        """给这台狗下发 ``ref`` 这一版的 ``map_activate`` 载荷;发布条件不满足抛 :class:`HttpError`。
        ``POST /api/robots/<id>/map`` 和地图管理页(``maps_admin_api``)共用同一套条件。"""
        if self.site.zones is not None and not self.site.zones.confirmed_current(ref.map_id,
                                                                       ref.version):
            # W10(W08 决定 5):发布前人工确认水池、高楼楼梯、陡坡、落差都画成禁行区了
            raise HttpError(409, f"{ref.map_id}:{ref.version} 的禁行区尚未确认(或改过之后"
                                 "未重新确认):先在地图页画好、确认再下发",
                            extra={"reason": "zones_unconfirmed"})
        payload = ref.to_wire()
        # 这台狗在这张图上的原点:站点原点表里的是权威(W13a),有就下发、盖过图里的 home.json
        # (W00c6f 内审应修 4);没有才按图里带的。**都没有**(新建的图、新版本):默认不下发,
        # 管理员明说 ``without_home: true`` 才发 —— 狗上没有原点,起飞前检查拒 goto、巡检,
        # 遥控、设位置、「在这儿标原点」照常(标原点要狗先载上这张图,所以不能一律拒)。
        # 待命点不顶替原点(W13a 外审阻断)。
        home = self._home_on(robot_id, ref.map_id, ref.version)
        if not isinstance(without, bool):
            raise HttpError(400, "without_home 要是 true/false")
        if home is not None:
            payload["home"] = home
        elif not any(f.name == "home.json" for f in ref.files):
            if not without:
                raise HttpError(
                    409, f"{robot_id} 在 {ref.map_id}:{ref.version} 上尚无原点:下发之后机器人"
                         "不接 goto、巡检,要先把机器人开到原点(充电桩前)、设好位置、"
                         "「在这儿标原点」。确认要这样下发就带上 without_home",
                    extra={"reason": "no_home"})
            self._audit_detail = {"without_home": True}
        return payload

    def _zones(self, method: str, map_id: str, version: str, what: str, user) -> None:
        """禁行区、限速区(W10):``GET`` 看(``view``);``POST`` 整份改(``manage``,带 ``base_revision``,
        改完发给加载着这一版的狗;决策 66:只收紧的当场算确认了,回 ``auto_confirmed``);
        ``POST …/confirm`` 发布前人工确认(``manage``;一个区域都没有的要明说 ``empty_ok``;R2:带
        ``adopt`` 采纳从上一版带过来、核过的那几个);``POST …/proposal`` ``{"action": "discard"}``
        丢掉带过来的建议(``manage``)。"""
        from d1max_site.maps import MapError
        from d1max_site.nav_zones import ZonesConflict, ZonesError
        if self.site.zones is None:
            raise HttpError(404, "此站点未启用地图目录")
        try:
            self.site.maps.get(map_id, version)
        except MapError as exc:
            raise HttpError(404, str(exc)) from exc
        if method == "GET" and not what:
            self._need(user, VIEW)
            return self._send_json(200, self.site.zones.view(map_id, version))
        self._need(user, MANAGE)
        self._audit_target = f"{map_id}:{version}"
        d = self._body()
        zones = self.site.zones
        try:
            if method == "POST" and what == "confirm":
                return self._zones_confirm(map_id, version, d, user)
            if method == "POST" and what == "proposal":
                if d.get("action") != "discard":
                    raise HttpError(400, "要 action: discard")
                zones.discard_proposal(map_id, version, by=str(user))
                self._audit_detail = {"proposal": "discard"}
                return self._send_json(200, zones.view(map_id, version))
            if method == "POST" and not what:
                zs, tighten = zones.put(map_id, version, d.get("zones"),
                                        base_revision=d.get("base_revision"), by=str(user))
                # 改之前修订号核过(base_revision),改完还是「确认的就是当前修订」= 这一次当场算确认了
                auto = zones.confirmed_current(map_id, version)
                self._audit_detail = {"revision": zs.revision, "tighten": tighten,
                                      "auto_confirmed": auto, "zones": len(zs.zones),
                                      "hard": len(zs.hard())}
                pushed = self.site.dispatch(lambda: self.site.dispatcher.push_zones(
                    zs, tighten=tighten, issued_by=str(user)))
                self._hist("nogo", map_id, version, "", user)
                return self._send_json(200, zones.view(map_id, version)
                                       | {"tighten": tighten, "pushed": pushed,
                                          "auto_confirmed": auto})
        except ZonesConflict as exc:
            raise HttpError(409, str(exc)) from exc
        except ZonesError as exc:
            raise HttpError(400, str(exc)) from exc
        raise HttpError(404, f"没有 {method} 区域接口")

    def _route_net(self, method: str, map_id: str, version: str, user) -> None:
        """路网(W48):``GET`` 看(``view``);``POST`` 整份改(``manage``,带 ``base_revision``;
        点、连线不许
        碰这一版在生效的禁行区),改完发给加载着这一版的机器人。"""
        from d1max_site.maps import MapError
        from d1max_site.route_nets import RouteNetConflict, RouteNetError
        rn = getattr(self.site, "route_nets", None)
        if rn is None:
            raise HttpError(404, "此站点未启用地图目录")
        try:
            self.site.maps.get(map_id, version)
        except MapError as exc:
            raise HttpError(404, str(exc)) from exc
        if method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, rn.view(map_id, version))
        if method != "POST":
            raise HttpError(404, f"没有 {method} 路网接口")
        self._need(user, MANAGE)
        self._audit_target = f"{map_id}:{version}"
        d = self._body()
        zones = self.site.zones
        keep_out = tuple(z for z in zones.effective(map_id, version).zones if z.keep_out) \
            if zones is not None else ()
        def place(points: list[tuple[str, float, float]]) -> str:
            # W48-M2:点在图外、墙里、离建图走过的路太远都拒(跟巡检点一个口径;老图没有
            # coverage.json 不查)
            try:
                return self.site.maps.reach_problem(map_id, version, points)
            except MapError as exc:
                return str(exc)
        try:
            net = rn.put(map_id, version, d, by=str(user), keep_out=keep_out,
                         place_problem=place)
        except RouteNetConflict as exc:
            raise HttpError(409, str(exc)) from exc
        except RouteNetError as exc:
            raise HttpError(400, str(exc)) from exc
        self._audit_detail = {"revision": net.revision, "nodes": len(net.nodes),
                              "edges": len(net.edges)}
        pushed = self.site.dispatch(lambda: self.site.dispatcher.push_route_net(
            net, issued_by=str(user)))
        return self._send_json(200, rn.view(map_id, version) | {"pushed": pushed})

    def _zones_confirm(self, map_id: str, version: str, d: dict, user) -> None:
        """确认(W08 决定 5)。一个区域都没有的要明说(R2:``empty_ok: true``,手机多弹一道提示 —— 重建图
        之后泳池的禁行区可能悄悄没了);``adopt`` 采纳从上一版带过来的:区域并进来一起确认,待命点、
        拦截点按各自的规矩搬到这一版(拦截点照样查走不走得到,不过的不搬、说原因)。"""
        zones = self.site.zones
        if d.get("confirm") is not True:
            raise HttpError(400, "要 confirm: true(确认那一句)")
        if self._via_cookie:
            # W44 第三期(设计 §5.2 第 4 条):网页上确认的这一份比在生效的松(删了、缩了)
            # → 再输一次密码。
            # 手机这一期不改(只改用词),照旧
            cur = zones.current(map_id, version).zones
            if any(z not in cur for z in zones.effective(map_id, version).zones):
                if not d.get("confirm_password"):
                    raise HttpError(403, "放宽禁行区要再输一次密码")
                self._confirm(user, d)
        empty_ok = d.get("empty_ok", False)
        if not isinstance(empty_ok, bool):
            raise HttpError(400, "empty_ok 要是 true/false")
        adopt, prop = d.get("adopt"), None
        want_sb: list[tuple[str, str]] = []
        want_ic: list[str] = []
        if adopt is not None:
            if not isinstance(adopt, dict):
                raise HttpError(400, "adopt 要是对象")
            prop = zones.proposal(map_id, version)
            if prop is None:
                raise HttpError(409, f"{map_id}:{version} 没有待核的、从上一版带过来的东西")
            sb, ic = adopt.get("standby", []), adopt.get("intercepts", [])
            have_sb = {(p["robot_id"], p["name"]) for p in prop.get("standby", [])}
            have_ic = {p["name"] for p in prop.get("intercepts", [])}
            if not isinstance(sb, list) or not all(
                    isinstance(p, list) and len(p) == 2 and tuple(p) in have_sb for p in sb):
                raise HttpError(400, "adopt.standby 要是带过来的待命点 [机器人, 名字] 的列表")
            if not isinstance(ic, list) or not all(isinstance(n, str) and n in have_ic
                                                   for n in ic):
                raise HttpError(400, "adopt.intercepts 要是带过来的拦截点名字的列表")
            want_sb, want_ic = [tuple(p) for p in sb], list(ic)
        n_adopt = len(adopt.get("zones", []) or []) if isinstance(adopt, dict) else 0
        if not zones.current(map_id, version).zones and not n_adopt and empty_ok is not True:
            prev = zones.view(map_id, version).get("previous")
            raise HttpError(409, f"{map_id}:{version} 一个禁行区、限速区都没有:确认之前再看一眼 ——"
                                 "水池、高楼楼梯、陡坡、落差真的一处都没有吗?"
                                 + (f"(上一版第 {prev['version']} 版画了 {prev['zones']} 个)"
                                    if prev and prev.get("zones") else "")
                                 + "真的没有就带上 empty_ok",
                            extra={"reason": "zones_empty", "previous": prev})
        rep = zones.confirm(map_id, version, revision=d.get("revision"), by=str(user),
                            adopt=adopt)
        adopted: dict[str, Any] = {"zones": rep["zones"], "standby": {}, "intercepts": {}}
        if prop is not None:
            src = prop["from_version"]
            for rid, name in want_sb:
                p = next(x for x in prop["standby"] if (x["robot_id"], x["name"]) == (rid, name))
                adopted["standby"][f"{rid}/{name}"] = self._adopt_standby(
                    map_id, version, src, p)
            for name in want_ic:
                p = next(x for x in prop["intercepts"] if x["name"] == name)
                adopted["intercepts"][name] = self._adopt_intercept(map_id, version, src, p)
        self._audit_detail = {"confirmed_rev": d.get("revision"), "empty_ok": empty_ok,
                              "adopted": {k: len(v) for k, v in adopted.items()}}
        # 确认了:在生效的可能变了(放宽的这才生效、采纳的并进来了,修订号也跟着加了)—— 马上发给狗
        # (外审 W38-I1;发不出去的补发兜底)
        eff = zones.effective(map_id, version)
        pushed = self.site.dispatch(lambda: self.site.dispatcher.push_zones(
            eff, tighten=False, issued_by=str(user)))
        out = zones.view(map_id, version) | {"pushed": pushed}
        return self._send_json(200, out | ({"adopted": adopted} if prop is not None else {}))

    def _adopt_standby(self, map_id: str, version: str, src: str, p: dict) -> str:
        """把上一版的一个待命点搬到这一版(同样的坐标,人核过了):上一版那个改过、删了就不搬。"""
        from d1max_site.standby import StandbyError
        if getattr(self.site, "standby", None) is None:
            return "此站点未启用待命点"
        rows = self.site.dispatcher.db.query(
            "SELECT map_id, map_version, x, y, yaw FROM standby_points WHERE robot_id=? AND "
            "name=?", (p["robot_id"], p["name"]))
        if not rows or (rows[0]["map_id"], rows[0]["map_version"], rows[0]["x"], rows[0]["y"],
                        rows[0]["yaw"]) != (map_id, src, p["x"], p["y"], p["yaw"]):
            return "上一版的此待命点后来改过或删了:未迁移,在新图上重新设"
        try:
            self.site.standby.set(p["robot_id"], p["name"], map_id=map_id, map_version=version,
                                  x=p["x"], y=p["y"], yaw=p["yaw"])
        except StandbyError as exc:
            return str(exc)[:200]
        return ""

    def _adopt_intercept(self, map_id: str, version: str, src: str, p: dict) -> str:
        """把上一版的一个拦截点搬到这一版:照样查走不走得到(W23),不过的不搬、说原因。"""
        from d1max_site.incidents import IncidentError
        if getattr(self.site, "incidents", None) is None:
            return "此站点未启用拦截点"
        rows = self.site.dispatcher.db.query(
            "SELECT map_id, map_version, x, y, yaw FROM intercepts WHERE name=?", (p["name"],))
        if not rows or (rows[0]["map_id"], rows[0]["map_version"], rows[0]["x"], rows[0]["y"],
                        rows[0]["yaw"]) != (map_id, src, p["x"], p["y"], p["yaw"]):
            return "上一版的此拦截点后来改过或删了:未迁移,在新图上重新设"
        try:
            self.site.incidents.set_intercept(p["name"], map_id=map_id, map_version=version,
                                              x=p["x"], y=p["y"], yaw=p["yaw"])
        except IncidentError as exc:
            return str(exc)[:200]
        return ""

    @staticmethod
    def _check_new_version(cat: Any, map_id: str, version: str) -> None:
        """要狗建的这一版:名字不太长、站点上还没有、没有别的狗正在建。"""
        if len(map_id) > 40 or len(version) > 16:
            raise HttpError(400, "地图号最多 40 个字符、版本最多 16 个(文件名里还要加后缀)")
        if any(r["map_id"] == map_id and r["version"] == version for r in cat.list()) \
                or cat.build_in_flight(map_id, version):
            raise HttpError(409, f"{map_id}:{version} 已存在(或正在建),换个版本号")

    def _map_preview(self, map_id: str, version: str, what: str) -> None:
        """建好的图的预览(W00c6h,``view``):``preview`` 是坐标换算 + 这张图这一版上登记的待命点
        (每台狗的),``preview.png`` 是图。没有这张图、没有栅格 404;文件坏了 500(说是哪个文件)。

        地图显示改进(只管显示,导航栅格、坐标都不动):``preview`` 另给 ``rotation_deg``(建议的显示
        转角:预览在屏幕上顺时针转这么多度,墙横平竖直;[-45, 45),算不出来 0)、``areas``(这张图
        这一版上画的防区 ``{name, points: [{x, y}]}``,跟 ``…/areas`` 一样谁能看图谁能看)、``plan``
        (``{url, scale}``:``plan.png`` 是平面图样子的灰度 + 透明 PNG,范围跟 ``preview.png``
        一模一样,像素是它的 ``scale`` 倍;客户端拉伸到预览的 ``width × height`` 画,叠加的换算都
        不变)。

        2026-10-14(地图台账 M9、44、M7):这张图这一版上的**抹掉区**(区域种类 ``erase``)在预览、
        平面图上抹成没建图(站点画好,客户端不用管);有抹掉区时平面图地址带 ``?e=<抹掉区的指纹>``
        (改了抹掉区地址就变,客户端重新取图);``preview`` 另给 ``content_bbox``(有内容的范围,预览像素
        ``[x0, y0, x1, y1]``,客户端「全图」按它缩放)、``seen``(这一版带「看到过、狗没走过」的淡色层,
        客户端据此在图例里加一行;没有就不给)。"""
        from d1max_site.map_preview import NoRaster, PreviewError, plan_scale
        from d1max_site.maps import MapError
        zs = (self.site.zones.current(map_id, version) if self.site.zones is not None else None)
        erase = tuple(z.polygon for z in zs.erase()) if zs is not None else ()
        try:
            if what == "plan.png":
                png, info = self.site.maps.plan(map_id, version, erase)[0], {}
            else:
                png, info = self.site.maps.preview(map_id, version, erase)
        except (MapError, NoRaster) as exc:
            raise HttpError(404, str(exc)) from exc
        except (PreviewError, OSError) as exc:
            raise HttpError(500, f"此地图无法预览:{exc}") from exc
        if what != "preview":
            return self._send_bytes(png, "image/png")
        rows = self.site.dispatcher.db.query(
            "SELECT robot_id, name, x, y, yaw, is_default FROM standby_points WHERE map_id=? AND "
            "map_version=? ORDER BY robot_id, name", (map_id, version))
        standby = [{"robot_id": r["robot_id"], "name": r["name"], "x": r["x"], "y": r["y"],
                    "yaw": r["yaw"], "default": bool(r["is_default"])} for r in rows]
        rows = self.site.dispatcher.db.query(
            "SELECT robot_id, name, x, y, yaw FROM homes WHERE map_id=? AND map_version=? "
            "ORDER BY robot_id", (map_id, version))
        homes = [{"robot_id": r["robot_id"], "name": r["name"], "x": r["x"], "y": r["y"],
                  "yaw": r["yaw"]} for r in rows]                 # W13a:原点单独画
        zones = zs.to_wire()["zones"] if zs is not None else []
        book = self.site.areas
        areas = [{"name": a["zone"], "points": [{"x": x, "y": y} for x, y in a["points"]]}
                 for a in (book.list(map_id, version) if book is not None else [])]
        url = f"/api/maps/{quote(map_id, safe='')}/{quote(version, safe='')}/plan.png"
        if erase:
            tag = hashlib.sha256(json.dumps(erase).encode()).hexdigest()[:12]
            url += f"?e={tag}"
        plan = {"url": url, "scale": plan_scale(info["width"], info["height"])}
        # W44 第四期:显示设置(摆正只转显示;区域名、特殊地面画在图上;底图只给管理页)
        disp = (self.site.map_display.get(map_id, version)
                if self.site.map_display is not None else None)
        if disp is not None and disp.get("rotate_deg") is not None:
            info = info | {"rotation_deg": disp["rotate_deg"]}
        display = None if disp is None else {k: disp[k] for k in ("rotate_deg", "regions",
                                                                    "terrain")}
        return self._send_json(200, info | {"standby": standby, "homes": homes, "zones": zones,
                                            "areas": areas, "plan": plan, "display": display})

    def _home_on(self, robot_id: str, map_id: str, version: str) -> dict[str, float] | None:
        """这台狗在这张图这个版本上的原点(W13a):**只看原点表**。待命点是运营调度的点、随时会加、挪、删,
        不能顶替安全返航的原点(外审阻断);老库的兼容只靠 schema 16 那一次迁移。"""
        rows = self.site.dispatcher.db.query(
            "SELECT x, y, yaw FROM homes WHERE robot_id=? AND map_id=? AND map_version=?",
            (robot_id, map_id, version))
        return {"x": rows[0]["x"], "y": rows[0]["y"], "yaw": rows[0]["yaw"]} if rows else None

    def _releases(self, method: str, path: str, user) -> None:
        """发布(W00c5d 第三部分)。看:``view``(每一版、每台狗在跑哪一版);装、切、退、查:``manage``。
        狗收下就回,做完发事件,失败出 ``release_failed`` 告警;切、退要狗空闲(狗自己查)。

        W00c6d 升级前检查:``precheck`` 发 ``release_precheck``、拿回执里狗的清单,合上站点这边的两项
        (新版要的任务包 schema、站点备份)回给人;``activate`` 之前站点先核 schema,不过就 409。"""
        from d1max_contract.errors import ContractError
        from d1max_contract.releases import check_release_name
        from d1max_site.releases import ReleaseCatalogError
        cat = self.site.releases
        if cat is None:
            raise HttpError(404, "此站点未启用发布目录")
        if path == "/api/releases" and method == "GET":
            self._need(user, VIEW)
            robots = {rid: ((c.capabilities.tasks.get("release_install") or {}).get("current")
                            if c.capabilities is not None else None)
                      for rid, c in self.site.dispatcher.clients.items()}
            return self._send_json(200, {"releases": cat.list(), "robots": robots})
        m = _RELCMD.match(path)
        if m is None or method != "POST":
            raise HttpError(404, f"没有 {method} {path}")
        self._need(user, MANAGE)
        robot_id = unquote(m.group(1))
        if not SAFE_ID.match(robot_id):
            raise HttpError(404, "机器人不存在")
        self._audit_target = robot_id
        d = self._body()
        action = d.get("action")
        try:
            if action == "precheck":
                # 没在站点登记的版本(U 盘装上去的)也能查:狗按槽里的自述比 schema(W00c6d 内审)。
                name = check_release_name(d.get("name"))
                self._audit_detail = {"action": action, "name": name}
                return self._send_json(200, self._release_precheck(robot_id, name, user))
            if action == "install":
                ref = cat.get(check_release_name(d.get("name")))
                kind, payload = "release_install", ref.to_wire()
            elif action == "activate":
                kind, payload = "release_activate", {"name": check_release_name(d.get("name")),
                                                     **self._mission_schema()}
                bad = [c for c in self._release_site_checks(payload["name"])
                       if not c["ok"] and c["blocking"]]
                if bad:
                    raise HttpError(409, bad[0]["detail"])
                if d.get("when") == "charging":
                    # W44 第四期:等充完电、空闲时再换(release_queue.py)。报过能力、里面没有换版本
                    # 的机器人当场拒绝(演示站点实测:原来照收,之后每一拍都发不出去);没报过的
                    # (不在线)照收
                    c = self.site.dispatcher.clients.get(robot_id)
                    caps = getattr(c, "capabilities", None)
                    if caps is not None and "release_activate" not in caps.tasks:
                        raise HttpError(409, f"{robot_id} 不支持换版本(代理版本过旧或未开启发布槽)",
                                        extra={"unsupported": True})
                    self.site.release_queue.add(robot_id, payload["name"], by=str(user))
                    self._audit_detail = {"action": "activate", "name": payload["name"],
                                          "when": "charging"}
                    return self._send_json(200, {"queued": True})
            elif action == "rollback":
                kind, payload = "release_rollback", {}
            else:
                raise HttpError(400, "action 只能是 install / activate / rollback / precheck")
        except ReleaseCatalogError as exc:
            raise HttpError(404, str(exc)) from exc
        except ContractError as exc:
            raise HttpError(400, str(exc)) from exc
        self._audit_detail = {"action": action, "name": str(d.get("name", ""))[:32]}
        return self._send_json(200, self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, kind, payload, issued_by=str(user))))

    #: 网页上传的升级包最大多少(W44 第四期)。
    MAX_UPLOAD = 500 * 1024 * 1024

    def _release_upload(self, user) -> None:
        """W44 第四期:网页上传升级包(``.tar.gz``,请求体就是文件;再输一次密码放在
        ``X-D1Max-Confirm`` 头里)。解到临时目录,走跟命令行 ``release-add`` 同一个登记(照旧验签、
        核指纹),错了说人话。"""
        import shutil
        import tarfile
        import tempfile
        from pathlib import Path

        from d1max_site import main as site_main
        from d1max_site.releases import ReleaseCatalog, ReleaseCatalogError
        self._need(user, MANAGE)
        if self.site.home is None or self.site.releases is None:
            raise HttpError(404, "此站点未启用发布目录")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise HttpError(400, "Content-Length 不是数") from exc
        if n <= 0:
            raise HttpError(400, "未收到文件")
        if n > self.MAX_UPLOAD:
            raise HttpError(413, "升级包太大了(最多 500 MB)")
        tmp = Path(tempfile.mkdtemp(prefix="upload-", dir=self.site.releases.root))
        try:
            raw = tmp / "pkg.tar.gz"
            left = n
            with open(raw, "wb") as fh:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise HttpError(400, "文件未传完")
                    fh.write(chunk)
                    left -= len(chunk)
            # 先收完再验密码:没读完就回 403,浏览器那边多半只看到「连接断了」,看不到「密码不对」
            self._confirm(user, {"confirm_password": self.headers.get("X-D1Max-Confirm") or ""})
            out = tmp / "x"
            out.mkdir()
            try:
                with tarfile.open(raw, "r:gz") as tf:
                    for m in tf.getmembers():
                        p = Path(m.name)
                        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
                            raise HttpError(400, "升级包里有不该有的东西(往外跳的路径、链接)")
                    # 上面逐个核过;新的 Python 再加一道 data 过滤
                    tf.extractall(out, **({"filter": "data"} if hasattr(tarfile, "data_filter")
                                          else {}))
            except (tarfile.TarError, OSError, EOFError) as exc:
                raise HttpError(400, "这不是升级包(打不开)") from exc
            tops = [p for p in out.iterdir() if (p / "release.json").is_file()]
            if len(tops) != 1:
                raise HttpError(400, "这不是升级包(里面没有 release.json)")
            note = str(self.headers.get("X-Release-Note") or "")[:200]
            self._audit_target = tops[0].name
            try:
                row = ReleaseCatalog(self.site.home, self.site.dispatcher.db, now_ms=self.site._now,
                                     pubkey=site_main.release_pubkey(self.site.home)
                                     ).add(tops[0], note=note or "网页上传")
            except ReleaseCatalogError as exc:
                msg = str(exc)
                if "已登记" in msg:
                    raise HttpError(409, "此版本已存在") from exc
                if "签名" in msg or "公钥" in msg:
                    raise HttpError(400, f"升级包的签名不匹配,不是公司发的({msg})") from exc
                if "指纹" in msg:
                    raise HttpError(400, "升级包已损坏(文件与清单不匹配)") from exc
                raise HttpError(400, msg) from exc
            except site_main.SiteError as exc:
                raise HttpError(500, str(exc)) from exc
            return self._send_json(200, {"release": row})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _mission_schema(self) -> dict[str, int]:
        """站点当前任务包的 schema,放进 ``release_precheck``/``release_activate`` 给狗比(没有当前包
        不给)。"""
        from d1max_site.catalog import active_bundle_schema
        cur = active_bundle_schema(self.site.dispatcher.db)
        return {"mission_schema": cur[1]} if cur is not None else {}

    def _release_site_checks(self, name: str) -> list[dict[str, Any]]:
        """升级前检查里站点这边的两项(W00c6d):新版要的任务包 schema(拦)、站点备份(只提示)。
        没登记的版本不知道要几,schema 那项不列(狗那头照样查自己的)。"""
        from d1max_site.catalog import active_bundle_schema
        from d1max_site.releases import ReleaseCatalogError
        out: list[dict[str, Any]] = []
        try:
            need: int | None = self.site.releases.requires_mission_schema(name)
        except ReleaseCatalogError:
            need = None
        if need is not None:
            cur = active_bundle_schema(self.site.dispatcher.db)
            if cur is None:
                out.append({"name": "schema", "ok": True, "blocking": True,
                            "detail": f"新版要任务包 schema ≥ {need};站点没有任务包,"
                                      "这一项没有可比的"})
            else:
                ok = need <= cur[1]
                out.append({"name": "schema", "ok": ok, "blocking": True,
                            "detail": f"新版要任务包 schema ≥ {need},站点当前任务包 {cur[0]} 是 "
                                      f"{cur[1]}" + ("" if ok else " —— 先导入新格式的任务包")})
        b = self.site.backup
        st = b.status() if b is not None else {"configured": False}
        if not st.get("configured"):
            out.append({"name": "backup", "ok": False, "blocking": False,
                        "detail": "站点未配置备份盘 —— 建议先配上(提示,不拦)"})
        elif st.get("last_ok_ms") is None:
            out.append({"name": "backup", "ok": False, "blocking": False,
                        "detail": "站点尚未成功备份过 —— 建议先备份一次(提示,不拦)"})
        else:
            hours = max(0.0, (self.site._now() - st["last_ok_ms"]) / 3_600_000)
            out.append({"name": "backup", "ok": not st.get("stale"), "blocking": False,
                        "detail": f"站点上次备份是 {hours:.1f} 小时前"
                                  + (" —— 已过期,建议先备份一次(提示,不拦)"
                                     if st.get("stale") else "")})
        return out

    def _release_precheck(self, robot_id: str, name: str, user) -> dict[str, Any]:
        r = self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, "release_precheck", {"name": name, **self._mission_schema()},
            issued_by=str(user)))
        ack = r["ack"]
        if ack["result"] not in ("accepted", "duplicate"):
            raise HttpError(409, f"机器人未检查:{ack.get('reason') or ack['result']}")
        data = ack.get("data") if ack["result"] == "accepted" else \
            (ack.get("original") or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
            raise HttpError(502, "机器人的回执里没有清单")
        checks = [c for c in data["checks"] if isinstance(c, dict)]
        dog_has = {c.get("name") for c in checks}
        # 狗按槽里的自述比过 schema 就用狗的;老代理没比,用站点按登记目录比的那一份。
        checks += [c for c in self._release_site_checks(name) if c["name"] not in dog_has]
        blocking = [c.get("name") for c in checks if not c.get("ok") and c.get("blocking")]
        return {"robot_id": robot_id, "name": name, "ok": not blocking, "blocking": blocking,
                "checks": checks}

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self._status = 200
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _cameras(self, method: str, path: str, user) -> None:
        """固定摄像头(W19):列出、状态(``view``,不带口令);实时画面 MJPEG(``view``)。加、删只走
        命令行。"""
        if method != "GET":
            raise HttpError(405, "只收 GET(加、删摄像头在站点主机上用命令行)")
        self._need(user, VIEW)
        mgr = self.site.cctv
        if mgr is None:
            raise HttpError(404, "此站点未启用摄像头")
        if path == "/api/cameras":
            # W44 第三期:带上放在图上的位置(保安地图页的摄像头图层)
            from d1max_site.cctv import placement_of
            pl = {r["name"]: placement_of(r) for r in self.site.dispatcher.db.query(
                "SELECT name, map_id, map_version, x, y, yaw, fov_deg FROM cameras")}
            return self._send_json(200, {"cameras": [c | {"placement": pl.get(c["name"])}
                                                     for c in mgr.view()]})
        m = _CAM.match(path)
        assert m is not None
        if m.group(2) == "live":
            frames = self._camera_frames(m.group(1))
            first = next(frames)                         # 起不来:HttpError(504 / 502)
            if self._still():
                frames.close()
                return self._send_bytes(first, "image/jpeg")
            return self._mjpeg(frames, first)
        cam = mgr.camera(m.group(1))
        if cam is None:
            raise HttpError(404, "摄像头不存在")
        if m.group(2) == "health":
            # 外审 R6:画面健康(最后一帧多久了),网页按这个标「画面断了」。没人在看:没在拉
            with self.site._cctv_lock:
                v = self.site._cctv_views.get(cam.name)
            return self._send_json(200, v.health() if v is not None and v.cam == cam
                                   else {"online": False, "last_frame_age_ms": None})
        raise HttpError(404, "没有此摄像头接口")

    def _camera_frames(self, name: str):
        """一台固定摄像头的画面生成器(HTTP 长连、W51 的 WebSocket 共用)。第一帧等不到:``next``
        抛 ``TimeoutError``;拉不下来:``StopIteration``。"""
        from d1max_site.cctv import CctvView
        mgr = self.site.cctv
        if mgr is None:
            raise HttpError(404, "此站点未启用摄像头")
        cam = mgr.camera(name)
        if cam is None:
            raise HttpError(404, "摄像头不存在")
        if not cam.rtsp_url:
            raise HttpError(404, "此摄像头未登记画面地址(camera-add --rtsp)")
        with self.site._cctv_lock:
            v = self.site._cctv_views.get(cam.name)
            if v is not None and v.cam != cam:           # 改过地址、口令:旧的收干净再换
                v.close()
                v = None
            if v is None:
                make = self.site.cctv_view_factory or CctvView
                v = self.site._cctv_views[cam.name] = make(cam)
        self._audit_view(f"camera/{cam.name}")
        return _cam_frames(v)

    def _report(self, method: str, user, path: str = "/api/reports/daily") -> None:
        """值班日报(C-B3b)、按天汇总(W44 第二期):能看的都能看(保安交班、经理)。"""
        from d1max_site.reports import ReportError, daily, summary
        from d1max_site.sitetime import site_tz
        if method != "GET":
            raise HttpError(405, "只收 GET")
        self._need(user, VIEW)
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

        def num(k: str) -> int:
            v = (q.get(k) or [""])[0]
            if not v.isdigit() or len(v) > 15:
                raise HttpError(400, f"{k} 要是非负整数(毫秒)")
            return int(v)
        db = self.site.accounts.db
        try:
            if path == "/api/reports/summary":
                return self._send_json(200, summary(db, num("since"), num("until"),
                                                    tz=site_tz(db)))
            return self._send_json(200, daily(db, num("since"), num("until")))
        except ReportError as exc:
            raise HttpError(400, str(exc)) from exc

    def _rec_frame(self, method: str, store: Any, user) -> None:
        """W44:``GET /api/recordings/frame?robot=&camera=&t=`` → 那一刻的一帧小图(JPEG)。

        报警记录的抓拍。看证据,记审计(同看录像)。没有那一段、抓不出来:404。"""
        if method != "GET":
            raise HttpError(405, "只收 GET")
        self._need(user, VIEW)
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        robot, camera = (q.get("robot") or [""])[0], (q.get("camera") or ["front"])[0]
        t = (q.get("t") or [""])[0]
        if not robot or not SAFE_ID.match(robot) or not t.isdigit():
            raise HttpError(400, "要 robot、t(毫秒)")
        if self.site.frames is None:
            from d1max_site.recordings import FrameCache
            self.site.frames = FrameCache(store)
        img = self.site.frames.frame(robot, camera, int(t))
        if img is None:
            raise HttpError(404, "那一刻没有录像,或者抓不出画面")
        self._audit_view(f"recording-frame/{robot}/{camera}")
        return self._send_bytes(img, "image/jpeg")

    def _recordings(self, method: str, path: str, user) -> None:
        """连续录像(W18)。看、放:``view``;标「留着」(过了 30 天也不删):``review``(值班的人
        判断留证据)。"""
        from d1max_site.recordings import SEGMENT_S
        store = self.site.recordings
        if store is None:
            raise HttpError(404, "此站点未启用录像")
        if path == "/api/recordings":
            if method != "GET":
                raise HttpError(405, "只收 GET")
            self._need(user, VIEW)
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

            def num(k: str) -> int | None:
                v = (q.get(k) or [None])[0]
                if v is None:
                    return None
                if not v.isdigit():
                    raise HttpError(400, f"{k} 要是非负整数")
                return int(v)
            rows = store.list(robot_id=(q.get("robot") or [None])[0],
                              camera=(q.get("camera") or [None])[0], since_ms=num("since"),
                              until_ms=num("until"), limit=num("limit") or 500)
            return self._send_json(200, {"recordings": rows, "segment_s": SEGMENT_S})
        if path == "/api/recordings/frame":
            return self._rec_frame(method, store, user)
        m = _REC.match(path)
        assert m is not None
        rid, what = int(m.group(1)), m.group(2)
        self._audit_target = f"recording/{rid}"
        row = store.get(rid)
        if row is None:
            raise HttpError(404, "没有此段(过了留存期删了?)")
        if what == "video":
            if method != "GET":
                raise HttpError(405, "只收 GET")
            self._need(user, VIEW)
            p = store.path(row)
            if not p.is_file():
                raise HttpError(404, "此段的文件不在了")
            self._audit_view(f"recording/{rid}")
            return self._send_file(p, "video/mp4", ranged=True)
        if method != "POST":
            raise HttpError(405, "只收 POST")
        self._need(user, REVIEW)
        body = self._body()
        if not isinstance(body.get("keep"), bool):
            raise HttpError(400, "要 keep: true/false")
        self._audit_detail = {"keep": body["keep"]}
        return self._send_json(200, {"recording": store.set_keep(rid, body["keep"])})

    def _audit_view(self, target: str, *, action: str = "VIEW") -> None:
        """看了、下载了证据(W30,PDPA):记审计。同一个人看同一样东西 10 分钟内只记一次(放录像、
        看画面会连着请求好多次)。记不下来不挡看(审计写不进去另有日志)。"""
        site = self.site
        if site.audit is None or not self._actor:
            return
        now = site._now()
        key = (self._actor, f"{action} {target}")
        with site._viewed_lock:
            last = site._viewed.get(key)
            if last is not None and now - last < VIEW_AUDIT_EVERY_MS:
                return
            if len(site._viewed) > 5000:
                site._viewed.clear()
            site._viewed[key] = now
        try:
            site.audit.record(actor=self._actor, action=action, target=target, status=200,
                              remote=self.client_address[0])
        except Exception:
            log.exception("看证据的审计写不进去")

    def _static(self, path: str) -> None:
        """网页值班台的静态文件(商业化 B1,见 :mod:`d1max_site.webui`)。"""
        from d1max_site.webui import SECURITY_HEADERS, WEB_DIR, resolve
        hit = resolve(path, getattr(self.site, "web_dir", None) or WEB_DIR)
        if hit is None:
            raise HttpError(404, "页面不存在(网页值班台未安装?)")
        f, typ, immutable = hit
        raw = f.read_bytes()
        self._status = 200
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if immutable
                         else "no-store")
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path, content_type: str, *, ranged: bool = False) -> None:
        """整份发。``ranged``(录像):认 ``Range: bytes=…``,只发要的那一段(206)—— 浏览器放录像拖进度
        靠它;要的范围不对回 416。"""
        size = path.stat().st_size
        start, end = 0, size - 1
        part = False
        if ranged:
            got = byte_range(self.headers.get("Range"), size)
            if got is False:
                self._status = 416
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if got is not None:
                (start, end), part = got, True
        self._status = 206 if part else 200
        self.send_response(self._status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        if ranged:
            self.send_header("Accept-Ranges", "bytes")
        if part:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        left = end - start + 1
        with open(path, "rb") as fh:
            fh.seek(start)
            while left > 0 and (chunk := fh.read(min(1 << 16, left))):
                self.wfile.write(chunk)
                left -= len(chunk)

    def _video(self, robot_id: str, what: str, user) -> None:
        """视频经站点(W00c5b)。``health``:每路画面健康;``front``/``back``:MJPEG 长连(``view``)。
        第一帧之前出的错回成 HTTP 状态码(502/504 带原因);之后断了就结束这条连接。"""
        from d1max_site.video import VideoError
        self._need(user, VIEW)
        hub = self.site.video
        if hub is None:
            raise HttpError(404, "此站点未启用视频")
        if what == "health":
            try:
                cams = hub.health(robot_id)
            except VideoError as exc:
                raise HttpError(exc.status, exc.message) from exc
            return self._send_json(200, {"robot_id": robot_id, "cameras": cams})
        try:
            frames = hub.stream(robot_id, what)
            first = next(frames)
        except VideoError as exc:
            raise HttpError(exc.status, exc.message) from exc
        if self._still():
            frames.close()                     # 取一张就走;那一路还留 idle_s 秒,下一张接得上
            return self._send_bytes(first, "image/jpeg")
        self._mjpeg(frames, first, VideoError)

    def _video_ws(self, user) -> None:
        """W51:网页的多路画面走一条 WebSocket(:mod:`d1max_site.video_mux`)。权限跟 HTTP 那条一样
        (``view``);每一路照原来的规矩开(狗的画面、固定摄像头,含审计)。靠 cookie 认的,还要
        ``Origin`` 是这个站点自己(别的网站的页面开不了这条)。令牌每隔一会儿复查。"""
        from d1max_site.video_mux import VideoMux
        from d1max_site.ws import WsClosed, check_upgrade, upgrade
        self._need(user, VIEW)
        if check_upgrade(self.headers) is None:
            raise HttpError(400, "要 WebSocket 升级请求")
        if self._via_cookie:
            origin = urlparse(self.headers.get("Origin") or "")
            if not origin.netloc or origin.netloc != (self.headers.get("Host") or ""):
                raise HttpError(403, "画面连接要从站点自己的网页开")
        ws = upgrade(self, ping_every_s=5.0, dead_after_s=30.0, send_timeout_s=15.0)
        if ws is None:
            return
        mux = VideoMux(ws.send_binary, ws.send_text, self._mux_open)
        token = self._token()
        checked = time.monotonic()
        try:
            while not self.site.stopping:
                msg = ws.recv(timeout_s=0.03)
                if msg is not None:
                    mux.on_message(msg)
                mux.pump()
                if time.monotonic() - checked >= self.site.sse_recheck_s:
                    checked = time.monotonic()
                    who = self.site.accounts.check(token)
                    if who is None or not allowed(who.role, VIEW):
                        break                            # 注销、过期、停用:画面跟着断
        except WsClosed:
            pass
        finally:
            mux.close()
            ws.close(1000, "")

    def _mux_open(self, stream_id: str) -> Iterator[bytes]:
        """W51:一路画面的地址(跟 HTTP 那条一样)→ 画面生成器。"""
        m = _VIDEO.match(stream_id)
        if m is not None and m.group(2) != "health":
            robot_id = unquote(m.group(1))
            if not SAFE_ID.match(robot_id):
                raise HttpError(404, "机器人不存在")
            hub = self.site.video
            if hub is None:
                raise HttpError(404, "此站点未启用视频")
            return hub.stream(robot_id, m.group(2))
        m = _CAM.match(stream_id)
        if m is not None and m.group(2) == "live":
            return self._camera_frames(m.group(1))
        raise HttpError(404, f"视频源不存在:{stream_id[:80]}")

    def _still(self) -> bool:
        """``?still=1``:只要一张 JPEG(演示站点实测:四台机器人 8 路长连占满浏览器对一个站点的 6 条
        连接,页面自己的数据进不来;超出的画面位改成隔一会儿取一张)。"""
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        return "still=1" in q.split("&")

    def _mjpeg(self, frames, first: bytes, *errors: type[BaseException]) -> None:
        """一条 MJPEG 长连(狗的画面、固定摄像头共用)。令牌每隔一会儿复查:注销、过期、停用,
        画面跟着断。"""
        boundary = b"frame"
        token = self._token()
        checked = time.monotonic()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            frame = first
            while not self.site.stopping:
                self.wfile.write(b"--" + boundary + b"\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                                 + frame + b"\r\n")
                self.wfile.flush()
                if time.monotonic() - checked >= self.site.sse_recheck_s:
                    checked = time.monotonic()
                    who = self.site.accounts.check(token)
                    if who is None or not allowed(who.role, VIEW):
                        break                    # 注销、过期、停用:画面跟着断
                try:
                    frame = next(frames)
                except (*errors, StopIteration):
                    break
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass                                  # 观众走了
        finally:
            frames.close()                        # 退场(可能连带收流)在生成器的 finally 里

    def _alert_snapshot(self) -> dict[str, Any]:
        """告警流的首帧(W17 外审):未解决的告警,不带狗的视图。"""
        desk = self.site.alerts
        # 告警簿只在事件循环里读(同 GET /api/alerts)
        rows = [] if desk is None else self.site.loop.call(lambda: _sync(desk.open))
        return {"kind": "snapshot", "alerts": rows}

    def _sse(self, *, alerts_only: bool = False) -> None:
        """事件流。``alerts_only``:只有告警(首帧是未解决的告警,之后只转 ``kind == "alert"`` 的帧与
        心跳),给值守令牌用。"""
        feed = self.site.dispatcher.feed
        snapshot = self._alert_snapshot if alerts_only else self.site.snapshot
        sub = feed.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        token = self._token()
        try:
            self._frame(snapshot())
            idle = since_check = 0.0
            while not self.site.stopping:
                if since_check >= self.site.sse_recheck_s:
                    since_check = 0.0
                    who = self.site.accounts.check(token)
                    if who is None or not allowed(who.role, VIEW):
                        break                        # 注销、过期、停用或降到没权看:流跟着断
                tick = min(1.0, self.site.sse_recheck_s)
                item = sub.get(timeout=tick)          # 常醒:站点停、令牌失效时别挂着
                since_check += tick
                if sub.lagged:
                    sub.lagged = False
                    sub.drain()                      # 先清积压,快照之后不再推旧的
                    self._frame(snapshot())
                    continue
                if item is None:
                    idle += tick
                    if idle >= SSE_HEARTBEAT_S:
                        idle = 0.0
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    continue
                idle = 0.0
                if alerts_only and item.get("kind") != "alert":
                    continue
                self._frame(item)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass                                              # 客户端走了
        finally:
            feed.unsubscribe(sub)

    def _frame(self, item: dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(item, ensure_ascii=False).encode("utf-8")
                         + b"\n\n")
        self.wfile.flush()
