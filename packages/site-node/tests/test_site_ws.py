"""W00c5c:站点的最小 WebSocket(RFC 6455 里用得到的那一部分)。握手、带掩码的文本帧、ping/pong、
close;不带掩码的客户端帧、分片、超长帧一律断开。"""

from __future__ import annotations

import base64
import os
import socket
import struct
import threading
import time

import pytest

from d1max_site.ws import WsClosed, WsConn, accept_key, check_upgrade


def _client_frame(payload: bytes, opcode: int = 1, *, mask: bool = True, fin: bool = True) -> bytes:
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    head = bytes([b0])
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _read_server_frame(s: socket.socket) -> tuple[int, bytes]:
    h = s.recv(2)
    op, n = h[0] & 0x0F, h[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", s.recv(2))[0]
    data = b""
    while len(data) < n:
        data += s.recv(n - len(data))
    return op, data


@pytest.fixture
def 一对():
    a, b = socket.socketpair()
    yield WsConn(a, ping_every_s=10, dead_after_s=30), b
    a.close()
    b.close()


def test_握手的应答键():
    # RFC 6455 §1.3 的例子
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    key = base64.b64encode(os.urandom(16)).decode()
    ok = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade",
          "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": key}
    assert check_upgrade(ok) == key
    for bad in ({**ok, "Upgrade": "h2c"}, {**ok, "Sec-WebSocket-Version": "8"},
                {**ok, "Sec-WebSocket-Key": "short"}, {**ok, "Connection": "close"}):
        assert check_upgrade(bad) is None


def test_收文本帧_回文本帧(一对):
    ws, peer = 一对
    peer.sendall(_client_frame(b'{"vx":0.2}'))
    assert ws.recv(timeout_s=2) == '{"vx":0.2}'
    ws.send_text("你好")
    op, data = _read_server_frame(peer)
    assert op == 1 and data.decode() == "你好"


def test_没东西来就返回None_不断开(一对):
    ws, _ = 一对
    assert ws.recv(timeout_s=0.2) is None
    assert not ws.closed


def test_ping回pong_close回close(一对):
    ws, peer = 一对
    peer.sendall(_client_frame(b"hi", opcode=9))
    assert ws.recv(timeout_s=1) is None
    op, data = _read_server_frame(peer)
    assert op == 10 and data == b"hi"
    peer.sendall(_client_frame(struct.pack(">H", 1000), opcode=8))
    with pytest.raises(WsClosed):
        ws.recv(timeout_s=1)
    op, _ = _read_server_frame(peer)
    assert op == 8


@pytest.mark.parametrize("frame", [
    _client_frame(b"x", mask=False),                       # 客户端帧必须带掩码
    _client_frame(b"x", fin=False),                        # 不收分片
    _client_frame(b"x" * 5000),                            # 超长
    _client_frame(b"x", opcode=2),                         # 不收二进制
    _client_frame(b"\xff\xfe", opcode=1),                  # 不是 UTF-8
])
def test_不合规矩的帧_断开(一对, frame):
    ws, peer = 一对
    peer.sendall(frame)
    t0 = time.monotonic()
    with pytest.raises(WsClosed):
        ws.recv(timeout_s=1)
    assert ws.closed
    assert time.monotonic() - t0 < 1.5, "要当场断,不是等到判死"


def test_对面没了_判死(一对):
    ws, peer = 一对
    ws.dead_after_s = 0.3
    ws.ping_every_s = 0.1
    with pytest.raises(WsClosed):
        for _ in range(20):
            ws.recv(timeout_s=0.1)                 # 对面从不回 pong


def test_别的线程关_收的那头立刻醒(一对):
    ws, peer = 一对
    got = []

    def 收():
        try:
            while True:
                ws.recv(timeout_s=5)
        except WsClosed as exc:
            got.append(str(exc))
    t = threading.Thread(target=收)
    t.start()
    ws.close(1001, "被接管")
    t.join(3)
    assert not t.is_alive() and got
    op, data = _read_server_frame(peer)
    assert op == 8 and data[2:].decode() == "被接管"
