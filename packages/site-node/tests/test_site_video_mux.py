"""W51:网页的多路画面走一条 WebSocket(``video_mux``)。原来每路 MJPEG 占一条 HTTP/1.1
连接,浏览器对一个站点最多 6 条,网页只敢开 3 路长连、别的隔 2 秒一张。这里先测复用器本身
(假画面源),再端到端(真 WebSocket)。"""

from __future__ import annotations

import json
import threading
import time

import pytest

from d1max_site.video_mux import MAX_FPS, MAX_STREAMS, Refused, VideoMux, frame_message


class 源:
    """假画面:每一路一个生成器,测试往里塞帧;记着开了几次、关了几次。"""

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.frames: dict[str, list[bytes]] = {}
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.end: set[str] = set()

    def push(self, sid: str, f: bytes) -> None:
        with self.cv:
            self.frames.setdefault(sid, []).append(f)
            self.cv.notify_all()

    def finish(self, sid: str) -> None:
        with self.cv:
            self.end.add(sid)
            self.cv.notify_all()

    def __call__(self, sid: str):
        self.opened.append(sid)
        if sid in self.fail:
            raise self.fail[sid]
        return self._gen(sid)

    def _gen(self, sid: str):
        try:
            while True:
                with self.cv:
                    while not self.frames.get(sid) and sid not in self.end:
                        self.cv.wait(0.05)
                    if not self.frames.get(sid):
                        return
                    f = self.frames[sid].pop(0)
                yield f
        finally:
            self.closed.append(sid)


class 台:
    def __init__(self) -> None:
        self.src = 源()
        self.t = 100.0
        self.bins: list[bytes] = []
        self.texts: list[dict] = []
        self.mux = VideoMux(self.bins.append, lambda s: self.texts.append(json.loads(s)),
                            self.src, clock=lambda: self.t)

    def sub(self, *items) -> None:
        self.mux.on_message(json.dumps({"sub": [
            {"id": i} if isinstance(i, str) else {"id": i[0], "fps": i[1]} for i in items]}))

    def got(self) -> list[tuple[str, bytes]]:
        out = []
        for b in self.bins:
            n = b[0]
            out.append((b[1:1 + n].decode(), b[1 + n:]))
        return out


def _等(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("等不到")


A, B = "/api/robots/A/video/front", "/api/robots/A/video/back"


def test_帧的格式_一字节长度_id_JPEG():
    assert frame_message("ab", b"\xff\xd8x") == b"\x02ab\xff\xd8x"


def test_两路都来_各发各的_带着是哪一路():
    s = 台()
    s.sub(A, B)
    s.src.push(A, b"a1")
    s.src.push(B, b"b1")
    _等(lambda: s.mux.pump() or len(s.bins) == 2)
    assert set(s.got()) == {(A, b"a1"), (B, b"b1")}


def test_按fps发_中间来的只发最新那一帧_不越积越晚():
    s = 台()
    s.sub((A, 2))                                        # 每秒最多 2 帧
    s.src.push(A, b"1")
    _等(lambda: s.mux.pump() == 1)
    for i in range(2, 6):
        s.src.push(A, str(i).encode())
    _等(lambda: not s.src.frames.get(A))
    time.sleep(0.05)
    assert s.mux.pump() == 0, "不到半秒不发"
    s.t += 0.5
    assert s.mux.pump() == 1
    assert s.got()[-1] == (A, b"5"), "中间的丢掉,发最新的"
    assert s.mux.pump() == 0, "没有新帧不重发"


def test_换订阅_不要的那一路停掉关生成器_留下的不重开():
    s = 台()
    s.sub(A, B)
    _等(lambda: len(s.src.opened) == 2)
    s.sub(A)
    s.src.push(B, b"x")                                  # 让拉 B 的线程醒过来看到停
    _等(lambda: B in s.src.closed)
    assert s.src.opened.count(A) == 1 and s.mux.active == [A]


def test_起不来_回原因_站点不自己重试_网页再订阅才重开():
    s = 台()
    s.src.fail[A] = Refused(504, "20 s 没等到 A 的 front 第一帧")
    s.sub(A)
    _等(lambda: s.mux.pump() is not None and s.texts)
    assert s.texts[0] == {"kind": "error", "id": A, "status": 504,
                          "message": "20 s 没等到 A 的 front 第一帧"}
    assert s.mux.active == []
    del s.src.fail[A]
    s.sub(A)                                             # 网页过一会儿再发一次
    s.src.push(A, b"ok")
    _等(lambda: s.mux.pump() == 1)
    assert s.src.opened == [A, A]


def test_画面中途没了_也回原因():
    s = 台()
    s.sub(A)
    s.src.push(A, b"1")
    _等(lambda: s.mux.pump() == 1)
    s.src.finish(A)
    _等(lambda: s.mux.pump() == 0 and s.texts)
    assert s.texts[0]["id"] == A and s.texts[0]["status"] == 502


def test_写错的订阅_说清楚_不起画面():
    s = 台()
    s.mux.on_message("not json")
    s.mux.on_message(json.dumps({"sub": [{"id": ""}]}))
    s.mux.on_message(json.dumps({"sub": [{"id": f"/x/{i}"} for i in range(MAX_STREAMS + 1)]}))
    s.mux.pump()
    assert [t["status"] for t in s.texts] == [400, 400, 400]
    assert s.src.opened == []


def test_fps_夹在范围里_写错当默认():
    s = 台()
    s.sub((A, 1000), (B, "fast"))
    _等(lambda: len(s.src.opened) == 2)
    st = s.mux._streams
    assert st[A].fps == MAX_FPS and st[B].fps == 10.0


def test_关连接_所有路都停():
    s = 台()
    s.sub(A, B)
    _等(lambda: len(s.src.opened) == 2)
    s.mux.close()
    s.src.push(A, b"x")
    s.src.push(B, b"x")
    _等(lambda: sorted(s.src.closed) == sorted([A, B]))
    assert s.mux.active == []


@pytest.mark.parametrize("n", [12])
def test_多路_不受浏览器六条连接的限制(n):
    """一条连接上 12 路一起出画面(原来网页只给 3 路长连)。"""
    s = 台()
    ids = [f"/api/robots/R{i}/video/front" for i in range(n)]
    s.sub(*ids)
    for i in ids:
        s.src.push(i, b"f")
    _等(lambda: s.mux.pump() >= 0 and len(s.bins) == n)
    assert {sid for sid, _ in s.got()} == set(ids)


# ---------------------------------------------------------------- 端到端:真站点、真 WebSocket

class 网页:
    """一条画面 WebSocket(测试用的最小客户端)。"""

    def __init__(self, s, tok, *, cookie: bool = False, origin: str | None = None) -> None:
        import base64
        import os
        import socket
        host, port = s.api.httpd.server_address[:2]
        self.sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        auth = f"Cookie: d1max_session={tok}\r\n" if cookie else f"Authorization: Bearer {tok}\r\n"
        if origin is not None:
            auth += f"Origin: {origin}\r\n"
        self.sock.sendall((f"GET /api/video/ws HTTP/1.1\r\nHost: site.local:8443\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{auth}\r\n"
                           ).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.sock.recv(1)
            if not c:
                break
            head += c
        self.status = int(head.split(b" ")[1]) if head else 0
        self.buf = b""

    def sub(self, *ids, fps=10) -> None:
        from d1max_site.ws import client_mask_frame
        self.sock.sendall(client_mask_frame(json.dumps(
            {"sub": [{"id": i, "fps": fps} for i in ids]}).encode()))

    def next(self, timeout=20.0):
        """下一条消息:二进制 → (id, jpeg),文本 → dict;ping 回 pong。"""
        import struct

        from d1max_site.ws import client_mask_frame
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            while len(self.buf) >= 2:
                op, n, h = self.buf[0] & 0x0F, self.buf[1] & 0x7F, 2
                if n == 126:
                    if len(self.buf) < 4:
                        break
                    n, h = struct.unpack(">H", self.buf[2:4])[0], 4
                elif n == 127:
                    if len(self.buf) < 10:
                        break
                    n, h = struct.unpack(">Q", self.buf[2:10])[0], 10
                if len(self.buf) < h + n:
                    break
                data, self.buf = self.buf[h:h + n], self.buf[h + n:]
                if op == 9:
                    self.sock.sendall(client_mask_frame(data, opcode=10))
                elif op == 2:
                    k = data[0]
                    return data[1:1 + k].decode(), data[1 + k:]
                elif op == 1:
                    return json.loads(data)
                elif op == 8:
                    return None
            self.sock.settimeout(max(0.05, end - time.monotonic()))
            try:
                c = self.sock.recv(65536)
            except TimeoutError:
                continue
            if not c:
                return None
            self.buf += c
        raise AssertionError("等不到消息")

    def close(self) -> None:
        self.sock.close()


def _有ffmpeg():
    import shutil
    return shutil.which("ffmpeg") is not None


needs_ffmpeg = pytest.mark.skipif(not _有ffmpeg(), reason="没有 ffmpeg")


@pytest.fixture
def 视频站(tmp_path):
    from test_site_video import _站
    s = _站(tmp_path)
    yield s
    s.close()


def _tok(s):
    from test_site_video import _登, _新鲜
    tok = _登(s)
    _新鲜(s, tok)
    return tok


@needs_ffmpeg
def test_端到端_一条连接看前后两路_都出画面_退订就停(视频站):
    s = 视频站
    tok = _tok(s)
    w = 网页(s, tok)
    try:
        assert w.status == 101
        w.sub(A, "/api/robots/A/video/back")
        seen: dict[str, int] = {}
        while not (seen.get(A, 0) >= 2 and seen.get("/api/robots/A/video/back", 0) >= 2):
            m = w.next()
            assert isinstance(m, tuple), m
            sid, jpeg = m
            assert jpeg[:2] == b"\xff\xd8"
            seen[sid] = seen.get(sid, 0) + 1
        w.sub(A)                                       # 退订后面那一路
        _等(lambda: s.hub.feed("A", "back").health()["viewers"] == 0, timeout=10)
    finally:
        w.close()
    _等(lambda: s.hub.feed("A", "front").health()["viewers"] == 0, timeout=10)


@needs_ffmpeg
def test_端到端_起不来的那一路回原因_别的照常(视频站):
    s = 视频站
    tok = _tok(s)
    w = 网页(s, tok)
    try:
        w.sub(A, "/api/robots/A/video/nope", "/api/robots/Z/video/front", "/etc/passwd")
        errs, frames = {}, 0
        while len(errs) < 3 or frames < 1:
            m = w.next()
            if isinstance(m, dict):
                errs[m["id"]] = m["status"]
            else:
                frames += 1
        assert errs["/etc/passwd"] == 404
        assert errs["/api/robots/A/video/nope"] in (404, 502)
        assert errs["/api/robots/Z/video/front"] >= 400
    finally:
        w.close()


def test_没登录401_业主能看(tmp_path):
    from test_site_api import PW, 站
    s = 站(tmp_path)
    try:
        assert 网页(s, "bad").status == 401
        s.accounts.add("olga", PW, role="owner")
        tok = s.req("POST", "/api/login", {"name": "olga", "password": PW})[1]["token"]
        w = 网页(s, tok)
        assert w.status == 101, "业主能看画面(view)"
        w.close()
    finally:
        s.close()


def test_网页cookie_要是站点自己的页面开的(tmp_path):
    """靠 cookie 认的:Origin 不是这个站点就拒(别的网站的页面开不了这条)。"""
    from test_site_api import PW, 站
    s = 站(tmp_path)
    try:
        tok = s.req("POST", "/api/login", {"name": "alice", "password": PW})[1]["token"]
        assert 网页(s, tok, cookie=True, origin="https://evil.example").status == 403
        assert 网页(s, tok, cookie=True).status == 403, "不带 Origin 也拒"
        w = 网页(s, tok, cookie=True, origin="https://site.local:8443")
        assert w.status == 101
        w.close()
    finally:
        s.close()

