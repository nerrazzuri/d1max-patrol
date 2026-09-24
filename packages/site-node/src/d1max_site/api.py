"""站点 API 的最小版(W00c 设计决定四 A:标准库 ``ThreadingHTTPServer``)。

路由(除登录外都要 ``Authorization: Bearer <令牌>``):

- ``POST /api/login`` ``{"name","password"}`` → ``{"token","name"}``
- ``POST /api/logout``
- ``GET  /api/robots`` → ``{"robots":[…]}``
- ``GET  /api/robots/<id>`` → 视图 + 最近事件 + 最近命令
- ``POST /api/robots/<id>/goto`` ``{"target":MapPose,"max_speed_mps"?}``(优先级由站点定,W00c2b)
- ``POST /api/robots/<id>/abort`` ``{"task_id"}``
- ``POST /api/robots/<id>/patrol`` ``{"mission_id"}``:从当前任务包里起一趟(W00c2a)
- ``POST /api/bundles`` ``{"path"}``:导入站点主机上的一个任务包目录(W00c2a)
- ``GET/POST /api/robots/<id>/standby``、``POST /api/robots/<id>/standby/return``:待命点(W00c2b)
- ``GET  /api/schedule``:当前包的排程,每条下一轮何时、最近一次去向与结果(W00c2a)
- ``GET  /api/events`` SSE:第一帧全量快照,之后是派遣器的 status/event/ack/reconcile;
  订阅者跟不上时补发一帧全量快照(``lagged``),不悄悄丢

状态码:派遣条件不满足 409(带理由)、等回执超时 504、未登录 401、锁定 429、请求体超过
64 KB 413、JSON 坏 400。

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
from urllib.parse import unquote

from d1max_contract.dispatch import DispatchTimeout
from d1max_site.accounts import Accounts, AuthError, LockedOut
from d1max_site.ca import SAFE_ID
from d1max_site.dispatcher import Dispatcher, DispatchRefused
from d1max_site.loop import LoopThread
from d1max_site.priorities import MANUAL

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
SSE_HEARTBEAT_S = 15.0
#: 一个请求(含读请求头)多久没动静就断:慢速攻击(slowloris)不能一直占着线程。
REQUEST_TIMEOUT_S = 30.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
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


class SiteApi:
    def __init__(self, *, host: str, port: int, loop: LoopThread, dispatcher: Dispatcher,
                 accounts: Accounts, tls: tuple[Path, Path] | None = None,
                 scheduler: Any = None, standby: Any = None,
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
        self._now = now_ms or (lambda: int(__import__("time").time() * 1000))
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

    def _send_json(self, status: int, body: Any) -> None:
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
        return name

    def _handle(self, method: str) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if method == "POST" and path == "/api/login":
                return self._login()
            user = self._user()
            if method == "POST" and path == "/api/logout":
                self.site.accounts.logout(self._token() or "")
                return self._send_json(200, {"ok": True})
            if method == "GET" and path == "/api/robots":
                return self._send_json(200, {"robots": self.site.dispatcher.robots_view()})
            if method == "GET" and path == "/api/events":
                return self._sse()
            if method == "GET" and path == "/api/schedule":
                if self.site.scheduler is None:
                    raise HttpError(404, "这个站点没开排程")
                return self._send_json(200, self.site.scheduler.view())
            if method == "POST" and path == "/api/bundles":
                return self._import_bundle(user)
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

    def _login(self) -> None:
        d = self._body()
        name, pw = d.get("name"), d.get("password")
        if not isinstance(name, str) or not isinstance(pw, str):
            raise HttpError(400, "要 name 与 password 两个字符串")
        try:
            token = self.site.accounts.login(name, pw)
        except LockedOut as exc:
            raise HttpError(429, str(exc)) from exc
        except AuthError as exc:
            raise HttpError(401, str(exc)) from exc
        self._send_json(200, {"token": token, "name": name})

    def _robot(self, method: str, robot_id: str, action: str | None, user: str) -> None:
        disp = self.site.dispatcher
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
                robot_id, target, speed, issued_by=user, priority=MANUAL))
        elif action == "patrol":
            mid = d.get("mission_id")
            if not isinstance(mid, str) or not mid:
                raise HttpError(400, "要 mission_id")
            from d1max_site.catalog import active_bundle
            act = active_bundle(disp.db)
            if act is None or mid not in act.missions:
                raise HttpError(404, f"当前任务包里没有任务 {mid!r}")
            wire = act.missions[mid].to_wire()
            result = self.site.dispatch(lambda: disp.patrol(robot_id, wire, issued_by=user,
                                                            priority=MANUAL))
        else:
            task_id = d.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise HttpError(400, "要 task_id")
            result = self.site.dispatch(lambda: disp.abort(robot_id, task_id, issued_by=user))
        self._send_json(200, result)

    def _standby(self, method: str, robot_id: str, action: str, user: str) -> None:
        from d1max_site.standby import StandbyError
        stb = self.site.standby
        if stb is None:
            raise HttpError(404, "这个站点没开待命点")
        if action == "standby/return":
            if method != "POST":
                raise HttpError(405, "只支持 POST")
            result = self.site.dispatch(lambda: stb.return_to(robot_id, issued_by=user))
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
                    if self.site.accounts.check(token) is None:
                        break                        # 注销或过期了:流跟着断
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
