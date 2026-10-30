"""配网服务的两个口(W42 §4)和主循环。

- **热点那一侧**:HTTPS(配网证书,指纹印在贴纸上,手机钉它),只绑路由器 LAN1 那个地址。每个请求带
  ``X-Bind-Code`` 头(贴纸上的绑定码)。

  ========================  ==============================================
  ``GET  /status``           序列号、状态、核对码、WiFi、挂着的改动
  ``POST /bind``             ``{"wifi": {"ssid","psk"}, "site": {"url","fp","name"}}``
  ``POST /wifi``             ``{"ssid","psk"}``(绑好以后换 WiFi,站点确认)
  ``POST /unbind``           ``{"token"}``(工程师的解绑令)
  ``POST /cancel``           放弃这次绑定
  ========================  ==============================================

- **代理那一侧**:本机 unix 套接字,一行一个 JSON。``{"op": "net"}`` → 遥测里的 ``net`` 那一块;
  ``{"op": "unbind", "cert"}``、``{"op": "wifi_confirm", "id", "approve"}`` → ``{"ok": true}`` 或
  ``{"error": 原因}``。
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from d1max_patrol.bind.core import BindCore, BindRefused

log = logging.getLogger(__name__)

PORT = 8450
SOCK = Path("/run/d1max/bind.sock")
MAX_BODY = 16 * 1024


class _Https(ThreadingHTTPServer):
    daemon_threads = True
    core: BindCore


class _Handler(BaseHTTPRequestHandler):
    server: _Https
    timeout = 15

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("热点 %s %s", self.client_address[0], fmt % args)

    def _send(self, status: int, d: dict[str, Any]) -> None:
        raw = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise BindRefused(413, "太大了")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError as exc:
            raise BindRefused(400, "要 JSON") from exc

    def _handle(self, method: str) -> None:
        core, code = self.server.core, self.headers.get("X-Bind-Code") or ""
        path = self.path.split("?", 1)[0]
        try:
            if method == "GET" and path == "/status":
                return self._send(200, core.status(code))
            if method == "POST" and path == "/bind":
                return self._send(200, core.bind(code, self._body()))
            if method == "POST" and path == "/wifi":
                return self._send(200, core.wifi(code, self._body()))
            if method == "POST" and path == "/unbind":
                d = self._body()
                return self._send(200, core.unbind_token(code, (d or {}).get("token")
                                                         if isinstance(d, dict) else None))
            if method == "POST" and path == "/cancel":
                return self._send(200, core.cancel(code))
            return self._send(404, {"error": f"没有 {method} {path}"})
        except BindRefused as exc:
            return self._send(exc.status, {"error": exc.message})
        except Exception:  # 一个请求炸了不能把服务带走
            log.exception("热点请求 %s %s 炸了", method, path)
            return self._send(500, {"error": "机器人内部错误"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def make_https(core: BindCore, host: str, port: int, cert: Path, key: Path) -> _Https:
    srv = _Https((host, port), _Handler)
    srv.core = core
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    return srv


class _Unix(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    core: BindCore


class _SockHandler(socketserver.StreamRequestHandler):
    server: _Unix
    timeout = 10

    def handle(self) -> None:
        for raw in self.rfile:
            try:
                d = json.loads(raw)
                out = self._one(d if isinstance(d, dict) else {})
            except ValueError:
                out = {"error": "要 JSON"}
            except Exception as exc:
                log.exception("代理请求炸了")
                out = {"error": f"{type(exc).__name__}"}
            self.wfile.write(json.dumps(out, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()

    def _one(self, d: dict[str, Any]) -> dict[str, Any]:
        core, op = self.server.core, d.get("op")
        if op == "net":
            return {"ok": True, "net": core.net_view()}
        if op == "unbind":
            why = core.site_unbind(d.get("cert"))
        elif op == "wifi_confirm":
            why = core.wifi_confirm(d.get("id"), d.get("approve") is True)
        else:
            why = f"不认识 {op!r}"
        return {"ok": True} if not why else {"error": why}


def make_unix(core: BindCore, path: Path, *, group: str | None = None) -> _Unix:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    srv = _Unix(str(path), _SockHandler)
    srv.core = core
    os.chmod(path, 0o660)
    if group:
        import grp
        try:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        except (KeyError, PermissionError) as exc:
            log.warning("套接字给不了组 %s:%s(代理可能连不上)", group, exc)
    return srv


def serve(core: BindCore, *, host: str, port: int, sock: Path, group: str | None,
          stop: threading.Event | None = None, period_s: float = 1.0) -> None:
    """起两个口,主线程每秒 ``tick``,``stop`` 置位就收。"""
    stop = stop or threading.Event()
    https = make_https(core, host, port, core.paths.tls_cert, core.paths.tls_key)
    unix = make_unix(core, sock, group=group)
    threads = [threading.Thread(target=s.serve_forever, daemon=True, name=n)
               for s, n in ((https, "bind-https"), (unix, "bind-sock"))]
    for t in threads:
        t.start()
    log.info("配网服务起来了:热点 https://%s:%d,代理套接字 %s", host, port, sock)
    try:
        while not stop.is_set():
            try:
                core.tick()
            except Exception:  # 一拍炸了下一拍再来
                log.exception("配网服务这一拍炸了")
            stop.wait(period_s)
    finally:
        for s in (https, unix):
            s.shutdown()
            s.server_close()
        sock.unlink(missing_ok=True)


def now_ms() -> int:
    return int(time.time() * 1000)
