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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

#: 握手最多等多久(秒)。
HANDSHAKE_TIMEOUT_S = 10.0


class TlsThreadingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, ctx: ssl.SSLContext | None) -> None:
        self.ctx = ctx
        super().__init__(addr, handler)

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
