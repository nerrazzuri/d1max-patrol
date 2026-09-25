"""最小的 WebSocket 服务端(W00c5c,手机 → 站点的遥控连接)。站点 API 是标准库 ``http.server``,没有
WebSocket;这里只实现遥控用得到的那一部分(RFC 6455):

- 握手(``Sec-WebSocket-Accept``);
- **客户端的帧必须带掩码**;只收**不分片的文本帧**(UTF-8)、ping、pong、close;
  单帧上限 ``MAX_PAYLOAD``;
  别的一律按协议错误断开(遥控连接上出现看不懂的东西,宁可断开让狗停,也不猜);
- 站点每 ``ping_every_s`` 发一次 ping,``dead_after_s`` 里对面什么都没发(连 pong 都没有)就判死 ——
  **半开的连接要看得出来**:连接就是租约的载体,判死 = 放租 = 狗停。

读写直接走套接字,不走 ``BaseHTTPRequestHandler.rfile``:缓冲文件对象超时一次就再也读不了。
写有锁:遥控台可能从别的线程关这条连接(被接管、狗掉线、画面没了)。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
from collections.abc import Mapping

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: 单帧载荷上限。遥控帧是几十字节的 JSON。
MAX_PAYLOAD = 4096


class WsClosed(ConnectionError):
    """连接没了(对面关、协议错、判死、我们自己关)。消息是原因。"""


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


def check_upgrade(headers: Mapping[str, str]) -> str | None:
    """看请求头是不是一个像样的 WebSocket 升级请求;是 → 返回 ``Sec-WebSocket-Key``,不是 → None。"""
    def h(name: str) -> str:
        return (headers.get(name) or "").strip()
    if h("Upgrade").lower() != "websocket":
        return None
    if "upgrade" not in [p.strip().lower() for p in h("Connection").split(",")]:
        return None
    if h("Sec-WebSocket-Version") != "13":
        return None
    key = h("Sec-WebSocket-Key")
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            return None
    except (ValueError, TypeError):
        return None
    return key


class WsConn:
    def __init__(self, sock: socket.socket, *, ping_every_s: float = 1.0,
                 dead_after_s: float = 3.0) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._wlock = threading.Lock()
        self.ping_every_s = ping_every_s
        self.dead_after_s = dead_after_s
        self._last_rx = time.monotonic()
        self._last_ping = time.monotonic()
        self.closed = False
        self.close_reason = ""

    # ------------------------------------------------------------ 写

    def _send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        with self._wlock:
            if self.closed and opcode != 8:
                raise WsClosed(self.close_reason or "连接已关")
            try:
                self._sock.sendall(head + payload)
            except (OSError, ssl.SSLError) as exc:
                self._mark_closed(f"写不出去: {exc}")
                raise WsClosed(self.close_reason) from exc

    def send_text(self, text: str) -> None:
        self._send(1, text.encode("utf-8"))

    def close(self, code: int = 1000, reason: str = "") -> None:
        """关连接(可以从别的线程调)。发 close 帧、关套接字:正在 ``recv`` 的那头立刻醒来
        抛 WsClosed。"""
        if self.closed:
            return
        with contextlib.suppress(WsClosed):
            self._send(8, struct.pack(">H", code) + reason.encode("utf-8")[:120])
        self._mark_closed(reason or f"关了({code})")
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)

    def _mark_closed(self, reason: str) -> None:
        if not self.closed:
            self.closed = True
            self.close_reason = reason

    # ------------------------------------------------------------ 读

    def _fill(self, n: int, deadline: float | None) -> bool:
        """缓冲里凑够 ``n`` 字节。``deadline`` 到了还没凑够返回 False(已有的留着,下次接着凑)。"""
        while len(self._buf) < n:
            if self.closed:
                raise WsClosed(self.close_reason)
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            try:
                self._sock.settimeout(0.2 if left is None else max(0.01, min(0.2, left)))
                chunk = self._sock.recv(8192)
            except TimeoutError:
                self._maybe_ping()
                continue
            except ssl.SSLWantReadError:
                continue
            except (OSError, ssl.SSLError) as exc:
                self._mark_closed(f"读不到了: {exc}")
                raise WsClosed(self.close_reason) from exc
            if not chunk:
                self._mark_closed("对面断开了")
                raise WsClosed(self.close_reason)
            self._buf += chunk
            self._last_rx = time.monotonic()
        return True

    def _maybe_ping(self) -> None:
        now = time.monotonic()
        if now - self._last_rx > self.dead_after_s:
            self._fail(1001, f"{self.dead_after_s:g} s 没有任何回应,判定连接已死")
        if now - self._last_ping >= self.ping_every_s:
            self._last_ping = now
            self._send(9, b"")

    def _fail(self, code: int, reason: str) -> None:
        self.close(code, reason)
        raise WsClosed(reason)

    def recv(self, *, timeout_s: float) -> str | None:
        """收一条文本消息;``timeout_s`` 里没收到完整的一条返回 None(连接还在)。
        连接没了抛 WsClosed。"""
        deadline = time.monotonic() + timeout_s
        while True:
            if not self._fill(2, deadline):
                self._maybe_ping()
                return None
            b0, b1 = self._buf[0], self._buf[1]
            fin, opcode, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            head = 2
            if n == 126:
                self._fill(4, None)
                n = struct.unpack(">H", self._buf[2:4])[0]
                head = 4
            elif n == 127:
                self._fill(10, None)
                n = struct.unpack(">Q", self._buf[2:10])[0]
                head = 10
            if not masked:
                self._fail(1002, "客户端的帧没有掩码")
            if n > MAX_PAYLOAD:
                self._fail(1009, "帧太长")
            if not fin or opcode == 0:
                self._fail(1003, "不收分片")
            self._fill(head + 4 + n, None)
            key = self._buf[head:head + 4]
            data = bytes(b ^ key[i % 4] for i, b in enumerate(self._buf[head + 4:head + 4 + n]))
            del self._buf[:head + 4 + n]
            if opcode == 1:
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    self._fail(1007, "文本帧不是 UTF-8")
            elif opcode == 9:
                self._send(10, data)                  # ping → pong
            elif opcode == 10:
                pass                                  # pong:已经算作收到过东西
            elif opcode == 8:
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                self.close(1000, "")
                raise WsClosed(f"对面关了({code})")
            else:
                self._fail(1003, f"不收这种帧(opcode {opcode})")


def upgrade(handler) -> WsConn | None:
    """在一个 ``BaseHTTPRequestHandler`` 里把请求升级成 WebSocket。不是像样的升级请求 →
    回 400、返回 None。"""
    key = check_upgrade(handler.headers)
    if key is None:
        handler.send_error(400, "要 WebSocket 升级请求")
        return None
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    return WsConn(handler.connection)


def client_mask_frame(payload: bytes, opcode: int = 1) -> bytes:
    """(测试与工具用)客户端方向的一帧:带掩码。"""
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    else:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))
