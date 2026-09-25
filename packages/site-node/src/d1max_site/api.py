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
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    HANDLE_ALERTS,
    MANAGE,
    MANAGE_ACCOUNTS,
    VIEW,
    VIEW_AUDIT,
    allowed,
)
from d1max_site.priorities import MANUAL

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
SSE_HEARTBEAT_S = 15.0
#: 一个请求(含读请求头)多久没动静就断:慢速攻击(slowloris)不能一直占着线程。
REQUEST_TIMEOUT_S = 30.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ACCOUNT = re.compile(r"^/api/accounts/([A-Za-z0-9._-]{1,64})$")
#: 告警键 ``robot/kind#seq`` 里有 ``/`` 与 ``#``:客户端整个键编码成一段(``%2F``、``%23``)。
_ALERT = re.compile(r"^/api/alerts/([^/]{1,256})/(ack|resolve)$")
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
                 alerts: Any = None, now_ms: Callable[[], int] | None = None,
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
        self._now = now_ms or (lambda: int(__import__("time").time() * 1000))
        self.audit = AuditLog(dispatcher.db, now_ms=self._now) if dispatcher is not None else None
        self._stopping = threading.Event()
        api = self

        class Handler(_Handler):
            site = api
            timeout = request_timeout_s          # StreamRequestHandler:套接字超时

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self._scheme = "http"
        if tls is not None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(tls[0]), str(tls[1]))
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
            self._scheme = "https"
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


class _Handler(BaseHTTPRequestHandler):
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
        path = self.path.split("?", 1)[0]
        try:
            self._route(method, path)
        finally:
            # 未登录的乱请求(401/413/400,不是登录也不是事件回调)不进审计:谁都能发,
            # 记下来只会把表撑满(内部评审)。登录失败、事件回调照记。
            if method != "GET" and self.site.audit is not None and self._actor:
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
            fn = self.site.alerts.recent if q.get("all") == ["1"] else self.site.alerts.open
            return self._send_json(200, {"alerts": self.site.loop.call(lambda: _sync(fn))})
        if method == "GET" and path == "/api/watch/summary":
            self._need(user, VIEW)
            from d1max_site.watch import watch_summary
            return self._send_json(200, self.site.loop.call(lambda: _sync(
                watch_summary, self.site.dispatcher, self.site.alerts,
                now_ms=self.site._now(), scheduler=self.site.scheduler)))
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
