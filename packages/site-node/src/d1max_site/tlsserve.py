"""TLS 的线程化 HTTP 服务(W00c5d)。

标准库的做法是把监听套接字整个 ``wrap_socket``:TLS 握手就发生在 ``accept()`` 里,也就是**唯一那条
接连接的线程**里,而且没有超时 —— 一个连上来却不发 ClientHello 的客户端(4G 上卡住的手机、扫端口的)
能让整个服务再也接不了新连接。

这里改成:接连接的线程只接 TCP,包成「还没握手」的 TLS 套接字;**握手放到这条连接自己的处理线程里**,
带超时。握手失败(证书不对、超时、对面不是 TLS)只结束这一条连接,记一行日志。
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

#: 握手最多等多久(秒)。
HANDSHAKE_TIMEOUT_S = 10.0


class TlsThreadingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, ctx: ssl.SSLContext | None,
                 max_connections: int = 128, max_per_ip: int = 16) -> None:
        self.ctx = ctx
        #: 同时开着的连接上限(W00c5d 内部评审):一条连接一个线程,不设上限的话一串慢连接就能把
        #: 站点主机的线程、内存吃光。**每个来源地址另有一个上限**:不然谁都能拿一串空连接把位子
        #: 占满,手机的停车、狗的上传全进不来。超了的连接直接关掉。
        self._slots = threading.BoundedSemaphore(max_connections)
        self._per_ip: dict[str, int] = {}
        self._per_ip_max = max_per_ip
        self._ip_lock = threading.Lock()
        super().__init__(addr, handler)

    def process_request(self, request, client_address) -> None:
        ip = str(client_address[0]) if client_address else ""
        with self._ip_lock:
            if self._per_ip.get(ip, 0) >= self._per_ip_max:
                log.warning("%s 同时开的连接太多了,关掉这一条", ip)
                self.shutdown_request(request)
                return
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        if not self._slots.acquire(blocking=False):
            log.warning("同时连接数到上限了,关掉 %s", client_address)
            self._release_ip(ip)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            self._release_ip(ip)
            raise

    def _release_ip(self, ip: str) -> None:
        with self._ip_lock:
            n = self._per_ip.get(ip, 0) - 1
            if n > 0:
                self._per_ip[ip] = n
            else:
                self._per_ip.pop(ip, None)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
            self._release_ip(str(client_address[0]) if client_address else "")

    def get_request(self) -> tuple[socket.socket, object]:
        sock, addr = super().get_request()
        if self.ctx is None:
            return sock, addr
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address) -> None:
        log.warning("%s 这条连接出错了", client_address, exc_info=True)


class TlsHandlerMixin(BaseHTTPRequestHandler):
    """处理线程里先握手(带超时),再按普通 HTTP 处理。"""

    def setup(self) -> None:
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(HANDSHAKE_TIMEOUT_S)
            try:
                self.request.do_handshake()
            except (OSError, ssl.SSLError) as exc:
                log.info("%s TLS 握手没成: %s", self.client_address, exc)
                raise
        super().setup()
