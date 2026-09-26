"""站点 API 的最小版(W00c 设计决定四 A:标准库 ``ThreadingHTTPServer``)。

路由(除登录外都要 ``Authorization: Bearer <令牌>``):

- ``POST /api/login`` ``{"name","password"}`` → ``{"token","name","role"}``
- ``POST /api/logout``
- ``GET  /api/robots`` → ``{"robots":[…]}``
- ``GET  /api/robots/<id>`` → 视图 + 最近事件 + 最近命令
- ``POST /api/robots/<id>/goto`` ``{"target":MapPose,"max_speed_mps"?}``(优先级由站点定,W00c2b)
- ``POST /api/robots/<id>/abort`` ``{"task_id"}``
- ``POST /api/robots/<id>/patrol`` ``{"mission_id"}``:从当前任务包里起一趟(W00c2a)
- ``POST /api/bundles`` ``{"path"}``:导入站点主机上的一个任务包目录(W00c2a)
- ``GET/POST /api/robots/<id>/standby``、``POST /api/robots/<id>/standby/return``:待命点(W00c2b)
- ``POST /api/incidents``:外部事件(**不要登录,要签名**,见 ``incidents.py``;W00c2c)
- ``GET  /api/incidents``、``POST /api/intercepts``、``POST /api/zones``:
  事件账、拦截点、防区(W00c2c)
- ``GET/POST /api/accounts``、``POST /api/accounts/<name>``、``POST /api/me/password``、
  ``GET /api/audit``:账号、角色、审计(W00c3)
- ``GET  /api/schedule``:当前包的排程,每条下一轮何时、最近一次去向与结果(W00c2a)
- ``GET  /api/events`` SSE:第一帧全量快照,之后是派遣器的 status/event/ack/reconcile;
  订阅者跟不上时补发一帧全量快照(``lagged``),不悄悄丢

状态码:派遣条件不满足 409(带理由)、等回执超时 504、未登录 401、角色不够 403、锁定 429、
请求体超过 64 KB 413、JSON 坏 400。每条路由要什么权限见 ``permissions.py``;改动类请求(非 GET)
一律进审计(``audit.py``)。

**绑到非本机地址必须带 TLS**(站点服务证书):账号口令不能在局域网上明文走。手机信任
站点 CA 的事归 W00c4。
"""

from __future__ import annotations

import json
import logging
import math
import re
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from d1max_contract.dispatch import DispatchTimeout
from d1max_site.accounts import Accounts, AuthError, LockedOut
from d1max_site.audit import AuditLog
from d1max_site.ca import SAFE_ID
from d1max_site.dispatcher import Dispatcher, DispatchRefused
from d1max_site.loop import LoopThread
from d1max_site.permissions import (
    ABORT,
    DISPATCH,
    EXPORT,
    HANDLE_ALERTS,
    MANAGE,
    MANAGE_ACCOUNTS,
    REVIEW,
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
#: ``GET /api/alerts?all=1&limit=`` 的上限。
ALERTS_LIMIT_MAX = 2000
#: 告警键 ``robot/kind#seq`` 里有 ``/`` 与 ``#``:客户端整个键编码成一段(``%2F``、``%23``)。
_ALERT = re.compile(r"^/api/alerts/([^/]{1,256})/(ack|resolve)$")
#: W00c5c:``/api/robots/<id>/teleop``(WebSocket)与 ``/api/robots/<id>/halt``。
_TELEOP = re.compile(r"^/api/robots/([^/]{1,64})/(teleop|halt|resume|supervise|relocalize)$")
#: W00c5b:``/api/robots/<id>/video/<front|back|health>``。
_VIDEO = re.compile(r"^/api/robots/([^/]{1,64})/video/([a-z]{1,16})$")
#: W00c5d:运行记录与导出。
_RUN = re.compile(r"^/api/runs/(\d{1,12})(?:/(judge|photos|review)(?:/([^/]{1,300}))?)?$")
_EXPORT = re.compile(r"^/api/exports/([^/]{1,128})$")
#: W00c5d 第二部分:给狗下发图、录包、重建。
_MAPCMD = re.compile(r"^/api/robots/([^/]{1,64})/(map|mapping|map_build|outbox_retry)$")
#: W00c5d 第三部分:给狗装、切、退版本。
_RELCMD = re.compile(r"^/api/robots/([^/]{1,64})/release$")
_ROBOT = re.compile(r"^/api/robots/([^/]+)(?:/(goto|abort|patrol|standby|standby/return))?$")


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def check_exposure(host: str, tls: tuple[Path, Path] | None) -> None:
    if host not in LOCAL_HOSTS and tls is None:
        raise SystemExit(f"拒绝启动:站点 API 绑到 {host} 会把账号口令明文暴露在网络上。"
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
                 releases: Any = None, supervision: Any = None,
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
        self._now = now_ms or (lambda: int(__import__("time").time() * 1000))
        #: 监护心跳(W00c6i):站点主程序会传同一个给待命点管理器;不传就自己建一个(测试)。
        from d1max_site.supervision import SupervisionDesk
        self.supervision = supervision if supervision is not None else \
            SupervisionDesk(dispatcher, loop, now_ms=self._now)
        self.audit = AuditLog(dispatcher.db, now_ms=self._now) if dispatcher is not None else None
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
        except DispatchRefused as exc:
            raise HttpError(409, str(exc)) from exc
        except (DispatchTimeout, TimeoutError) as exc:
            raise HttpError(504, f"等狗的回执超时: {exc}") from exc


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
            raise HttpError(400, f"JSON 解析不了: {exc}") from exc
        if not isinstance(d, dict):
            raise HttpError(400, "请求体要是 JSON 对象")
        return d

    def _token(self) -> str | None:
        h = self.headers.get("Authorization") or ""
        return h[7:].strip() if h.startswith("Bearer ") else None

    def _user(self) -> str:
        name = self.site.accounts.check(self._token())
        if name is None:
            raise HttpError(401, "没登录或登录已过期")
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
            if method == "POST" and path == "/api/login":
                return self._login()
            if method == "POST" and path == "/api/incidents":
                return self._incident_in()          # 摄像头不登录:验签
            user = self._user()
            if method == "POST" and path == "/api/logout":
                self.site.accounts.logout(self._token() or "")
                return self._send_json(200, {"ok": True})
            if method == "POST" and path == "/api/me/password":
                return self._change_own_password(user)
            if path == "/api/accounts" or _ACCOUNT.match(path):
                self._need(user, MANAGE_ACCOUNTS)
                return self._accounts(method, path)
            if method == "GET" and path == "/api/audit":
                self._need(user, VIEW_AUDIT)
                return self._send_json(200, {"audit": self.site.audit.list()})
            if method == "GET" and path == "/api/robots":
                self._need(user, VIEW)
                return self._send_json(200, {"robots": self.site.dispatcher.robots_view()})
            if method == "GET" and path == "/api/events":
                self._need(user, VIEW)
                return self._sse()
            if method == "GET" and path == "/api/schedule":
                self._need(user, VIEW)
                if self.site.scheduler is None:
                    raise HttpError(404, "这个站点没开排程")
                return self._send_json(200, self.site.scheduler.view())
            if path == "/api/alerts" or path == "/api/watch/summary" or _ALERT.match(path):
                return self._alerts(method, path, user)
            if method == "POST" and path == "/api/bundles":
                self._need(user, MANAGE)
                return self._import_bundle(user)
            if path in ("/api/incidents", "/api/intercepts", "/api/zones"):
                self._need(user, VIEW if (method == "GET" and path == "/api/incidents")
                           else MANAGE)
                return self._incident_admin(method, path)
            if path == "/api/runs" or path.startswith(("/api/runs/", "/api/exports")):
                return self._runs(method, path, user)
            if path == "/api/maps" or _MAPCMD.match(path):
                return self._maps(method, path, user)
            if path == "/api/releases" or _RELCMD.match(path):
                return self._releases(method, path, user)
            m = _TELEOP.match(path)
            if m is not None:
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "没有这台狗")
                if m.group(2) == "halt" and method == "POST":
                    return self._halt(robot_id, user)
                if m.group(2) == "resume" and method == "POST":
                    return self._resume(robot_id, user)
                if m.group(2) == "supervise" and method == "POST":
                    return self._supervise(robot_id, user)
                if m.group(2) == "relocalize" and method == "POST":
                    return self._relocalize(robot_id, user)
                if m.group(2) == "teleop" and method == "GET":
                    return self._teleop_ws(robot_id, user)
                raise HttpError(404, f"没有 {method} {path}")
            m = _VIDEO.match(path)
            if m is not None and method == "GET":
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "没有这台狗")
                return self._video(robot_id, m.group(2), user)
            m = _ROBOT.match(path)
            if m is not None:
                robot_id = unquote(m.group(1))
                if not SAFE_ID.match(robot_id):
                    raise HttpError(404, "没有这台狗")
                return self._robot(method, robot_id, m.group(2), user)
            raise HttpError(404, f"没有 {method} {path}")
        except HttpError as exc:
            self.close_connection = True          # 请求体可能没读完(413),连接不能复用
            self._send_json(exc.status, {"error": exc.message})
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
            raise HttpError(404, "这个站点没开告警")
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
                backup=self.site.backup)))
        m = _ALERT.match(path)
        if method != "POST" or m is None:
            raise HttpError(404, f"没有 {method} {path}")
        self._need(user, HANDLE_ALERTS)
        key = unquote(m.group(1))
        self._audit_target = key[:256]
        self._body()                                    # 读掉请求体;里面的 who 不信
        try:
            if m.group(2) == "ack":
                a = self.site.loop.call(lambda: _sync(self.site.alerts.ack, key, who=str(user)))
            else:
                a = self.site.loop.call(
                    lambda: _sync(self.site.alerts.resolve, key, who=str(user)))
        except AlertNotFound as exc:
            raise HttpError(404, "没有这条告警") from exc
        return self._send_json(200, {"alert": a.to_wire()})

    def _login(self) -> None:
        d = self._body()
        name, pw = d.get("name"), d.get("password")
        if not isinstance(name, str) or not isinstance(pw, str):
            raise HttpError(400, "要 name 与 password 两个字符串")
        self._actor = f"login:{name[:64]}"
        try:
            token = self.site.accounts.login(name, pw)
        except LockedOut as exc:
            raise HttpError(429, str(exc)) from exc
        except AuthError as exc:
            raise HttpError(401, str(exc)) from exc
        who = self.site.accounts.check(token)
        self._send_json(200, {"token": token, "name": name,
                              "role": getattr(who, "role", "")})

    def _robot(self, method: str, robot_id: str, action: str | None, user: str) -> None:
        disp = self.site.dispatcher
        if method == "GET":
            self._need(user, VIEW)
        elif action == "abort":
            self._need(user, ABORT)
        elif action == "standby":
            self._need(user, MANAGE)
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
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        d = self._body()
        if action in ("goto", "patrol"):
            # 站点这一道关(W00c6i 内审):要人监护的狗没人监护就不派 —— 回滚到旧版代理时狗自己不查。
            why = self.site.supervision.refusal(robot_id)
            if why:
                raise HttpError(409, why)
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
            result = self.site.dispatch(lambda: disp.goto(
                robot_id, target, speed, issued_by=str(user), priority=MANUAL))
        elif action == "patrol":
            mid = d.get("mission_id")
            if not isinstance(mid, str) or not mid:
                raise HttpError(400, "要 mission_id")
            from d1max_site.catalog import active_bundle
            act = active_bundle(disp.db)
            if act is None or mid not in act.missions:
                raise HttpError(404, f"当前任务包里没有任务 {mid!r}")
            wire = act.missions[mid].to_wire()
            result = self.site.dispatch(lambda: disp.patrol(robot_id, wire, issued_by=str(user),
                                                            priority=MANUAL))
        else:
            task_id = d.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise HttpError(400, "要 task_id")
            result = self.site.dispatch(lambda: disp.abort(robot_id, task_id, issued_by=str(user)))
        self._send_json(200, result)

    def _standby(self, method: str, robot_id: str, action: str, user: str) -> None:
        from d1max_site.standby import StandbyError
        stb = self.site.standby
        if stb is None:
            raise HttpError(404, "这个站点没开待命点")
        if action == "standby/return":
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            why = self.site.supervision.refusal(robot_id)
            if why:
                raise HttpError(409, why)
            result = self.site.dispatch(lambda: stb.return_to(robot_id, issued_by=str(user)))
            return self._send_json(200, result)
        if method == "GET":
            return self._send_json(200, {"points": stb.list(robot_id)})
        if method != "POST":
            raise HttpError(405, "只支持 GET/POST")
        d = self._body()
        try:
            if d.get("remove") is True:
                stb.remove(robot_id, d.get("name"))
            else:
                stb.set(robot_id, d.get("name"), map_id=d.get("map_id"),
                        map_version=d.get("map_version"), x=d.get("x"), y=d.get("y"),
                        yaw=d.get("yaw", 0.0), default=d.get("default"))
        except StandbyError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"points": stb.list(robot_id)})

    def _accounts(self, method: str, path: str) -> None:
        acc = self.site.accounts
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
                acc.add(d.get("name"), d.get("password"), role=d.get("role", "guard"))
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
            {"password_reset": True} if "password" in d else {})
        if "password" in d and name == str(self._actor):
            # 管理员的令牌被偷了,也不能靠这条路不验旧口令就把自己的账号改走。
            raise HttpError(400, "改自己的口令走 /api/me/password(要旧口令)")
        try:
            acc.update(name, role=d.get("role"), disabled=d.get("disabled"),
                       password=d.get("password"))
        except AuthError as exc:
            raise HttpError(400 if "没有账号" not in str(exc) else 404, str(exc)) from exc
        self._send_json(200, {"accounts": acc.list()})

    def _change_own_password(self, user: str) -> None:
        d = self._body()
        self._audit_target, self._audit_detail = str(user), {"self_password_change": True}
        try:
            self.site.accounts.change_password(str(user), d.get("old"), d.get("new"))
        except LockedOut as exc:
            raise HttpError(429, str(exc)) from exc
        except AuthError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "note": "口令改了;所有会话已吊销,请重新登录"})

    def _desk(self):
        if self.site.incidents is None:
            raise HttpError(404, "这个站点没开事件派遣")
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
            raise HttpError(401, "验签没过") from exc
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(400, f"JSON 解析不了: {exc}") from exc
        source = self.headers.get("X-D1MAX-Source") or ""
        self._actor = f"source:{source}"
        try:
            desk.parse(body)
        except IncidentError as exc:
            raise HttpError(400, str(exc)) from exc
        row = self.site.dispatch(lambda: desk.handle(source, body))
        self._send_json(200, row)

    def _incident_admin(self, method: str, path: str) -> None:
        from d1max_site.incidents import IncidentError
        desk = self._desk()
        if path == "/api/incidents":
            if method != "GET":
                raise HttpError(405, "只支持 GET")
            return self._send_json(200, {"incidents": desk.list()})
        if method != "POST":
            raise HttpError(405, "只支持 POST")
        d = self._body()
        try:
            if path == "/api/intercepts":
                desk.set_intercept(d.get("name"), map_id=d.get("map_id"),
                                   map_version=d.get("map_version"), x=d.get("x"), y=d.get("y"),
                                   yaw=d.get("yaw", 0.0))
            else:
                desk.map_zone(d.get("zone"), d.get("intercept"))
        except IncidentError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True})

    def _import_bundle(self, user: str) -> None:
        from d1max_site.catalog import CatalogError, import_bundle
        d = self._body()
        path = d.get("path")
        if not isinstance(path, str) or not path:
            raise HttpError(400, "要 path(站点主机上任务包目录的路径)")
        try:
            got = import_bundle(self.site.dispatcher.db, Path(path), imported_by=user,
                                now_ms=self.site._now())
        except CatalogError as exc:
            raise HttpError(409, str(exc)) from exc
        self._send_json(200, got)

    def _halt(self, robot_id: str, user) -> None:
        """停车(W00c5c):走 cmd,不走遥控连接。``abort`` 权限 —— 业主也能按。"""
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
            raise HttpError(504, "狗没回停车的回执:看不到它停了没有,按机身急停") from exc
        ack = r.get("ack") if isinstance(r, dict) else None
        if isinstance(ack, dict) and ack.get("result") != "accepted":
            # 狗说停车没成(W00c5e 内部评审):任务它中止了,腿停没停住不知道。
            raise HttpError(502, f"狗说停车没成({ack.get('reason', '')}):按机身急停")
        return self._send_json(200, r)

    def _resume(self, robot_id: str, user) -> None:
        """解除叫停(W00c5e):之后站点才重新给这只狗派单。``dispatch`` 权限 —— 业主能停,不能恢复
        (能不能让它再动,是会派它的人的事)。"""
        self._need(user, DISPATCH)
        self._body()
        self._audit_target = robot_id
        if self.site.dispatcher.registry.get(robot_id) is None:
            raise HttpError(404, "没有这台狗")
        was = self.site.dispatcher.resume(robot_id, by=str(user))
        return self._send_json(200, {"robot_id": robot_id, "was_held": was})

    def _relocalize(self, robot_id: str, user) -> None:
        """设位置(W00c6e,里程锚定):``{"x", "y", "yaw"}`` 或 ``{"at_home": true}``,转成
        ``relocalize`` 命令;坐标按狗**当前加载的那张图**(命令里带上图号,狗核对)。``dispatch`` 权限
        —— 保安、管理员。不受监护闸约束(不动)。狗在走、原点没标过这些由狗拒,回执原样回。"""
        import math
        self._need(user, DISPATCH)
        self._audit_target = robot_id
        d = self._body()
        if not isinstance(d, dict):
            raise HttpError(400, "要一个对象")
        if "at_home" in d:
            if d.get("at_home") is not True:
                raise HttpError(400, "at_home 只能是 true")
            payload: dict[str, Any] = {"at_home": True}
        else:
            payload = {}
            for k in ("x", "y", "yaw"):
                v = d.get(k)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise HttpError(400, f"{k} 要是有限数")
                payload[k] = float(v)
        c = self.site.dispatcher.clients.get(robot_id)
        caps = c.capabilities.tasks.get("patrol", {}) if c and c.capabilities else {}
        if caps.get("map_id") is not None:
            payload |= {"map_id": caps["map_id"], "map_version": caps.get("map_version")}
        self._audit_detail = {k: payload[k] for k in ("x", "y", "yaw", "at_home") if k in payload}
        return self._send_json(200, self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, "relocalize", payload, issued_by=str(user))))

    def _supervise(self, robot_id: str, user) -> None:
        """监护心跳(W00c6i):``{"action": "renew"|"release"}``。``dispatch`` 权限 —— 保安、
        管理员;业主不行(在现场看着狗动,是会派它的人的事)。审计只记开始与结束,续着的心跳不记。"""
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
            raise HttpError(404, "没有这台狗")
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
            raise HttpError(504, f"等狗的回执超时: {exc}") from exc
        return self._send_json(200, {"robot_id": robot_id, "ack": ack})

    def _teleop_ws(self, robot_id: str, user) -> None:
        """遥控(W00c5c):先开租约(拒绝回 HTTP 状态码),再升级成 WebSocket;连接就是租约的载体。"""
        from d1max_site.teleop import TeleopRefused
        from d1max_site.ws import WsClosed, check_upgrade, upgrade
        self._need(user, TELEOP)
        desk = self.site.teleop
        if desk is None:
            raise HttpError(404, "这个站点没开遥控")
        if check_upgrade(self.headers) is None:
            # 先查升级头再开租约:一个普通 GET 不许授予租约、抢占正在跑的巡检(W00c5c 内部评审)。
            raise HttpError(400, "要 WebSocket 升级请求")
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        reason = (q.get("takeover") or [""])[0].strip()[:200]
        try:
            session = desk.open(robot_id, user, takeover_reason=reason)
        except TeleopRefused as exc:
            raise HttpError(exc.status, exc.message) from exc
        ws = upgrade(self)
        if ws is None:
            desk.close(session, "bad_upgrade")
            return
        session.ws = ws
        token = self._token()
        checked = time.monotonic()
        try:
            ws.send_text(json.dumps({"kind": "granted", "lease_epoch": session.epoch,
                                     "operator": session.operator, "max_vx": session.max_vx,
                                     "max_wz": session.max_wz, "frame_period_ms": 100},
                                    ensure_ascii=False))
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
        """运行记录(W00c5d,决策 8:证据都在站点)。看:``view``(业主也能看);判读、复核:``review``
        (管理员、保安);导出:``export``(管理员)。"""
        from d1max_site.runs import RunError
        desk = self.site.runs
        if desk is None:
            raise HttpError(404, "这个站点没开证据库")
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
                    limit=num("limit") or 200)})
            m = _RUN.match(path)
            if m is not None:
                rid, what, arg = int(m.group(1)), m.group(2), m.group(3)
                self._audit_target = f"run/{rid}"
                if method == "GET" and what is None:
                    self._need(user, VIEW)
                    return self._send_json(200, desk.detail(rid))
                if method == "GET" and what == "photos" and arg:
                    self._need(user, VIEW)
                    return self._send_file(desk.photo(rid, unquote(arg)), "image/jpeg")
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
                return self._send_file(desk.export_path(unquote(m.group(1))), "application/zip")
        except RunError as exc:
            msg = str(exc)
            raise HttpError(404 if msg.startswith(("没有", "这一趟里没有")) else 400, msg) from exc
        raise HttpError(404, f"没有 {method} {path}")

    def _maps(self, method: str, path: str, user) -> None:
        """地图(W00c5d 第二部分,决策 8:站点是地图的唯一权威)。看:``view``;下发、录包、重建:
        ``manage``(管理员)。命令狗收下就回,做完狗发事件,失败出 ``map_failed`` 告警。"""
        from d1max_contract.errors import ContractError
        from d1max_contract.maps import parse_map_build, parse_mapping
        from d1max_site.maps import MapError
        cat = self.site.maps
        if cat is None:
            raise HttpError(404, "这个站点没开地图目录")
        if path == "/api/maps" and method == "GET":
            self._need(user, VIEW)
            return self._send_json(200, {"maps": cat.list(), "bags": cat.bags()})
        m = _MAPCMD.match(path)
        if m is None or method != "POST":
            raise HttpError(404, f"没有 {method} {path}")
        self._need(user, MANAGE)
        robot_id, what = unquote(m.group(1)), m.group(2)
        if not SAFE_ID.match(robot_id):
            raise HttpError(404, "没有这台狗")
        self._audit_target = robot_id
        d = self._body()
        try:
            if what == "map":
                ref = cat.get(str(d.get("map_id", "")), str(d.get("version", "")))
                kind, payload = "map_activate", ref.to_wire()
                if not any(f.name == "home.json" for f in ref.files):
                    # 这台狗在这张图上的原点(待命点):图里没带就用站点登记的。都没有就不下发 ——
                    # 狗换了坐标系没有原点,之后派什么都过不了起飞前检查(W00c5d 内部评审)。
                    home = self._home_on(robot_id, ref.map_id, ref.version)
                    if home is None:
                        raise HttpError(409, f"{robot_id} 在 {ref.map_id}:{ref.version} 上还没有"
                                             "待命点(原点):先登记一个待命点再下发这张图")
                    payload["home"] = home
            elif what == "mapping":
                action, name = parse_mapping(d)
                if len(name) > 40:
                    raise HttpError(400, "包名最多 40 个字符(狗还要加录的时刻、文件名还要加后缀)")
                kind, payload = "mapping", {"action": action, **({"name": name} if name else {})}
            elif what == "outbox_retry":
                # 站点改了收件规矩之后,让狗把隔离的文件再传一次(W00c5d 内部评审)。
                kind, payload = "outbox_retry", {}
            else:
                bag, map_id, version = parse_map_build(d)
                if len(map_id) > 40 or len(version) > 16:
                    raise HttpError(400, "地图号最多 40 个字符、版本最多 16 个(文件名里还要加后缀)")
                if any(r["map_id"] == map_id and r["version"] == version for r in cat.list()) \
                        or cat.build_in_flight(map_id, version):
                    raise HttpError(409, f"{map_id}:{version} 已经有了(或正在建),换个版本号")
                kind, payload = "map_build", {"bag": bag, "map_id": map_id, "version": version}
        except MapError as exc:
            raise HttpError(404, str(exc)) from exc
        except ContractError as exc:
            raise HttpError(400, str(exc)) from exc
        self._audit_detail = {k: v for k, v in payload.items() if k != "files"}
        return self._send_json(200, self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, kind, payload, issued_by=str(user))))

    def _home_on(self, robot_id: str, map_id: str, version: str) -> dict[str, float] | None:
        rows = self.site.dispatcher.db.query(
            "SELECT x, y, yaw FROM standby_points WHERE robot_id=? AND map_id=? AND map_version=? "
            "ORDER BY is_default DESC, name LIMIT 1", (robot_id, map_id, version))
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
            raise HttpError(404, "这个站点没开发布目录")
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
            raise HttpError(404, "没有这台狗")
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
                        "detail": "站点没配备份盘 —— 建议先配上(提示,不拦)"})
        elif st.get("last_ok_ms") is None:
            out.append({"name": "backup", "ok": False, "blocking": False,
                        "detail": "站点还没成功备份过 —— 建议先备份一次(提示,不拦)"})
        else:
            hours = max(0.0, (self.site._now() - st["last_ok_ms"]) / 3_600_000)
            out.append({"name": "backup", "ok": not st.get("stale"), "blocking": False,
                        "detail": f"站点上次备份是 {hours:.1f} 小时前"
                                  + (" —— 过期了,建议先备份一次(提示,不拦)"
                                     if st.get("stale") else "")})
        return out

    def _release_precheck(self, robot_id: str, name: str, user) -> dict[str, Any]:
        r = self.site.dispatch(lambda: self.site.dispatcher.map_command(
            robot_id, "release_precheck", {"name": name, **self._mission_schema()},
            issued_by=str(user)))
        ack = r["ack"]
        if ack["result"] not in ("accepted", "duplicate"):
            raise HttpError(409, f"狗没查:{ack.get('reason') or ack['result']}")
        data = ack.get("data") if ack["result"] == "accepted" else \
            (ack.get("original") or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
            raise HttpError(502, "狗的回执里没有清单")
        checks = [c for c in data["checks"] if isinstance(c, dict)]
        dog_has = {c.get("name") for c in checks}
        # 狗按槽里的自述比过 schema 就用狗的;老代理没比,用站点按登记目录比的那一份。
        checks += [c for c in self._release_site_checks(name) if c["name"] not in dog_has]
        blocking = [c.get("name") for c in checks if not c.get("ok") and c.get("blocking")]
        return {"robot_id": robot_id, "name": name, "ok": not blocking, "blocking": blocking,
                "checks": checks}

    def _send_file(self, path, content_type: str) -> None:
        size = path.stat().st_size
        self._status = 200
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 16):
                self.wfile.write(chunk)

    def _video(self, robot_id: str, what: str, user) -> None:
        """视频经站点(W00c5b)。``health``:每路画面健康;``front``/``back``:MJPEG 长连(``view``)。
        第一帧之前出的错回成 HTTP 状态码(502/504 带原因);之后断了就结束这条连接。"""
        from d1max_site.video import VideoError
        self._need(user, VIEW)
        hub = self.site.video
        if hub is None:
            raise HttpError(404, "这个站点没开视频")
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
                except (VideoError, StopIteration):
                    break
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass                                  # 观众走了
        finally:
            frames.close()                        # 退场(可能连带收流)在生成器的 finally 里

    def _sse(self) -> None:
        feed = self.site.dispatcher.feed
        sub = feed.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        token = self._token()
        try:
            self._frame(self.site.snapshot())
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
                    self._frame(self.site.snapshot())
                    continue
                if item is None:
                    idle += tick
                    if idle >= SSE_HEARTBEAT_S:
                        idle = 0.0
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    continue
                idle = 0.0
                self._frame(item)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass                                              # 客户端走了
        finally:
            feed.unsubscribe(sub)

    def _frame(self, item: dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(item, ensure_ascii=False).encode("utf-8")
                         + b"\n\n")
        self.wfile.flush()
