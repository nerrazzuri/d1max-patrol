"""W00c5b:代理按需把相机推到站点(SRT,加密)。真 ffmpeg:站点那头用一条 ffmpeg 的 SRT 监听
收流、转成 JPEG;狗这头的相机用 ffmpeg 的测试图(``lavfi testsrc``)顶。"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from d1max_agent.video_push import VideoPusher, lavfi_source, rtsp_source
from d1max_contract.video import VideoRequest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="没有 ffmpeg")

PW = "Q7kP2mX9vL4nR8tW"


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class 站点那头:
    """一条 SRT 监听的 ffmpeg,把收到的流转成 JPEG 数帧。"""

    def __init__(self, port: int, passphrase: str = PW) -> None:
        self.p = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer",
             "-probesize", "32768", "-analyzeduration", "500000", "-i",
             f"srt://127.0.0.1:{port}?mode=listener&passphrase={passphrase}&pbkeylen=16",
             "-an", "-f", "image2pipe", "-vcodec", "mjpeg", "-r", "5", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def jpegs(self, timeout_s: float) -> int:
        import os
        import select
        buf = b""
        deadline = time.monotonic() + timeout_s
        assert self.p.stdout is not None
        fd = self.p.stdout.fileno()
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
        return buf.count(b"\xff\xd8\xff")

    def close(self) -> None:
        self.p.kill()
        self.p.wait(5)


class 钟:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _req(port: int, *, ttl_ms: int = 10_000, camera: str = "front") -> VideoRequest:
    return VideoRequest(camera=camera, url=f"srt://127.0.0.1:{port}", passphrase=PW,
                        ttl_ms=ttl_ms)


@pytest.fixture
def 推(tmp_path):
    c = 钟()
    events: list[tuple[str, dict]] = []
    p = VideoPusher(source=lavfi_source, clock=c, emit=lambda k, d: events.append((k, d)))
    yield p, c, events
    p.close()


def test_收到命令就推_站点那头收得到画面(推):
    p, c, events = 推
    port = _port()
    site = 站点那头(port)
    try:
        assert p.request(_req(port)) == ""
        assert p.running() == {"front"}
        assert site.jpegs(8.0) >= 5, "站点那头要收到画面"
        assert events == []
    finally:
        site.close()


def test_有效期到了没续就停_续了就接着推(推):
    p, c, _ = 推
    port = _port()
    site = 站点那头(port)
    try:
        p.request(_req(port, ttl_ms=10_000))
        c.t += 6
        p.request(_req(port, ttl_ms=10_000))          # 同一个地址同一个口令:只续期,不重起
        assert p.starts == 1
        c.t += 6
        p.step()
        assert p.running() == {"front"}, "续过了,还没到期"
        c.t += 5
        p.step()
        assert p.running() == set(), "到期没续,自己停"
    finally:
        site.close()


def test_换了地址或口令就重起(推):
    p, c, _ = 推
    a, b = _port(), _port()
    p.request(_req(a))
    p.request(_req(b))
    assert p.starts == 2 and p.running() == {"front"}


def test_推流进程自己退了_报video_failed_只报一次(推):
    p, c, events = 推
    port = _port()                                     # 没人监听:SRT 连不上,ffmpeg 自己退
    p.request(_req(port))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not events:
        p.step()
        time.sleep(0.2)
    p.step()
    assert [k for k, _ in events] == ["video_failed"], events
    assert events[0][1]["camera"] == "front" and events[0][1]["reason"]
    assert p.running() == set()


def test_两路相机各推各的_关掉全收(推):
    p, c, _ = 推
    p.request(_req(_port(), camera="front"))
    p.request(_req(_port(), camera="back"))
    assert p.running() == {"front", "back"}
    p.close()
    assert p.running() == set()


def test_真狗的输入是相机RTSP_走TCP_默认不转码():
    argv = rtsp_source("192.168.234.1")("front")
    assert argv == ["-rtsp_transport", "tcp", "-i", "rtsp://192.168.234.1:8554/front"]
    p = VideoPusher(source=rtsp_source("192.168.234.1"))
    cmd = p.argv(_req(9000))
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[-1].startswith("srt://127.0.0.1:9000?passphrase=")
    t = VideoPusher(source=rtsp_source("192.168.234.1"), transcode=True).argv(_req(9000))
    assert t[t.index("-c:v") + 1] == "libx264"


def test_失败原因里没有口令(推):
    p, c, events = 推
    p.request(_req(_port()))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not events:
        p.step()
        time.sleep(0.2)
    assert events and PW not in events[0][1]["reason"] and PW not in str(events[0][1])
    assert events[0][1]["url"] .startswith("srt://127.0.0.1:"), events[0][1]


def test_站点说停_安静地停_不报失败(推):
    p, c, events = 推
    port = _port()
    site = 站点那头(port)
    try:
        p.request(_req(port))
        assert site.jpegs(8.0) >= 1
        p.request(VideoRequest(camera="front", url=f"srt://127.0.0.1:{port}", passphrase=PW,
                               ttl_ms=10_000, stop=True))
        assert p.running() == set()
        for _ in range(10):
            p.step()
        assert events == []
    finally:
        site.close()
