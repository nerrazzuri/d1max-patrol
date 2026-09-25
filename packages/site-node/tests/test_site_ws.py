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
    bytes([0xC1]) + _client_frame(b"x")[1:],               # RSV1 置了(没协商过扩展)
    _client_frame(b"p" * 126, opcode=9),                   # 控制帧超过 125 字节
    _client_frame(b"p", opcode=9, fin=False),              # 控制帧分片
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


def test_TLS上_别的线程发和关_都由收的那条线程做(tmp_path):
    """站点的 API 是 TLS:一个 SSLSocket 两条线程一读一写会读出假的「对面断了」(W00c5c 内部评审)。
    别的线程发的、关的都排队,由收的那条发出去;对面照样一条不少地收到,最后收到 close。"""
    import ssl
    from pathlib import Path as _P
    sup = _P(__file__).resolve().parents[3] / "mobile" / "test" / "support"
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(str(sup / "site_test.crt"), str(sup / "site_test.key"))
    cli_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cli_ctx.check_hostname = False
    cli_ctx.verify_mode = ssl.CERT_NONE
    a, b = socket.socketpair()
    out: dict = {}

    def _server():
        s = srv_ctx.wrap_socket(a, server_side=True)
        ws = WsConn(s, ping_every_s=10, dead_after_s=30)
        out["ws"] = ws
        got = []
        try:
            while True:
                m = ws.recv(timeout_s=0.3)
                if m is not None:
                    got.append(m)
        except WsClosed as exc:
            out["why"] = str(exc)
        out["got"] = got
    t = threading.Thread(target=_server)
    t.start()
    c = cli_ctx.wrap_socket(b, server_hostname="x")
    c.sendall(_client_frame(b"hello"))
    deadline = time.monotonic() + 5
    while "ws" not in out and time.monotonic() < deadline:
        time.sleep(0.01)
    ws = out["ws"]
    senders = [threading.Thread(target=ws.send_text, args=(f"m{i}",)) for i in range(20)]
    for x in senders:
        x.start()
    for x in senders:
        x.join()
    threading.Thread(target=ws.close, args=(1000, "结束了")).start()
    frames = []
    c.settimeout(5)
    while True:
        op, data = _read_server_frame(c)
        frames.append((op, data))
        if op == 8:
            break
    t.join(5)
    assert sorted(d.decode() for op, d in frames if op == 1) == sorted(f"m{i}" for i in range(20))
    assert frames[-1][0] == 8 and "结束了" in frames[-1][1][2:].decode()
    assert out["got"] == ["hello"]
    c.close()


def test_不等待地收_已经到了的照样收下(一对):
    """``recv(timeout_s=0)``:时间到了也非阻塞地读一次 —— 手机那头攒着的帧要一批收下(只转最新的)。"""
    ws, peer = 一对
    peer.sendall(_client_frame(b"a") + _client_frame(b"b"))
    time.sleep(0.05)
    assert ws.recv(timeout_s=0) == "a"
    assert ws.recv(timeout_s=0) == "b"
    assert ws.recv(timeout_s=0) is None


class _记线程的套接字:
    """包一层 socketpair 的一头:记下每次写、关是哪条线程做的。"""

    def __init__(self, sock) -> None:
        self._s = sock
        self.who: list[tuple[str, int]] = []

    def sendall(self, data):
        self.who.append(("sendall", threading.get_ident()))
        return self._s.sendall(data)

    def shutdown(self, how):
        self.who.append(("shutdown", threading.get_ident()))
        return self._s.shutdown(how)

    def __getattr__(self, name):
        return getattr(self._s, name)


def test_套接字只由收的那条线程碰_别的线程的发与关都排队():
    a, b = socket.socketpair()
    rec = _记线程的套接字(a)
    out: dict = {}
    ready = threading.Event()

    def _收():
        ws = WsConn(rec, ping_every_s=10, dead_after_s=30)
        out["ws"], out["owner"] = ws, threading.get_ident()
        ready.set()
        try:
            while True:
                ws.recv(timeout_s=0.3)
        except WsClosed:
            pass
    t = threading.Thread(target=_收)
    t.start()
    assert ready.wait(2)
    ws = out["ws"]
    for i in range(5):
        threading.Thread(target=ws.send_text, args=(f"m{i}",)).start()
    time.sleep(0.05)
    threading.Thread(target=ws.close, args=(1000, "bye")).start()
    t.join(3)
    assert rec.who, "该发的没发出去"
    assert {tid for _, tid in rec.who} == {out["owner"]}, rec.who
    b.close()
    a.close()
