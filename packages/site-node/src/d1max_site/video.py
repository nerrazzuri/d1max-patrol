"""视频经站点(W00c5b):狗按需把相机用 SRT(加密)推到站点,站点每路(狗, 相机)一条 ffmpeg 收流、
转 MJPEG,**多个观众共用这一条**。

- **按需**:第一个观众来了才起(挑一个 SRT 端口、生成这一次的口令、起收流进程、经 MQTT 发 ``video``
  命令);有观众期间每 ttl/2 续一次命令;最后一个观众走了 ``idle_s`` 后收掉。狗那头续不上就在有效期内
  自己停 —— 站点没了、网断了,狗不会一直往外推。
- **口令只经 mTLS 的 MQTT 下发**,每次起流随机生成;SRT 口令不对的推流连不上。
- **慢的观众丢帧,不排队**;**冻着的画面不算在线**(最近 ``stale_s`` 秒内真来过帧才算)。遥控的
  「没画面不许动」(W00c5c)读的就是 :meth:`VideoHub.health`。
- 帧切分、一路一条进程、锁外 kill 这几条,照搬狗上老服务 ``app/video.py`` 久经考验的写法
  (那一份随 W00c5e 退役)。

线程:HTTP 一请求一线程;每路一条收流线程、一条续命令线程;发命令经站点的事件循环线程
(``LoopThread.call``)。共享状态都在每路自己那把条件变量底下。
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from typing import IO, Any

from d1max_contract.messages import AckResult, Event
from d1max_contract.video import CAMERAS, VideoRequest

log = logging.getLogger(__name__)

MAX_VIEWERS = 6
STALE_S = 2.0
_SOI, _EOI = b"\xff\xd8", b"\xff\xd9"
_CHUNK = 65536
_ERR_TAIL = 400


class VideoError(RuntimeError):
    """拉不到画面。``status`` 是给 HTTP 的状态码,消息给人看。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _frames(pipe: IO[bytes]) -> Iterator[bytes]:
    """把字节流切成一个个完整 JPEG(管道给的块跟 JPEG 边界没关系,自己攒、自己找标记)。"""
    buf = bytearray()
    while True:
        chunk = pipe.read1(_CHUNK)  # type: ignore[attr-defined]
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(_SOI)
            if start < 0:
                buf.clear()
                break
            end = buf.find(_EOI, start + len(_SOI))
            if end < 0:
                del buf[:start]
                break
            yield bytes(buf[start:end + len(_EOI)])
            del buf[:end + len(_EOI)]


def _kill(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.kill()
    if proc.stdout is not None:
        with contextlib.suppress(OSError):
            proc.stdout.close()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5.0)


def _tail(f: IO[bytes]) -> str:
    try:
        f.seek(0)
        return f.read()[-_ERR_TAIL:].decode("utf-8", "replace").strip()
    except OSError:
        return ""


class _Feed:
    """一路(狗, 相机)。"""

    def __init__(self, hub: VideoHub, robot_id: str, camera: str) -> None:
        self.hub, self.robot_id, self.camera = hub, robot_id, camera
        self._ready = threading.Condition()
        self._viewers = 0
        self._latest: bytes | None = None
        self._seq = 0
        self._last_at: float | None = None
        self._detail = ""
        self._gen = 0
        self._running = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._stop_timer: threading.Timer | None = None
        #: 一共起过几次(测试的观察窗:两个观众只该起一次)。
        self.starts = 0

    # ------------------------------------------------------------ 观众

    def join(self) -> None:
        with self._ready:
            if self._viewers >= self.hub.max_viewers:
                raise VideoError(429, f"{self.robot_id} 的 {self.camera} 已经有 {self._viewers} "
                                      f"个人在看了,最多 {self.hub.max_viewers} 个 —— 关掉一个再开")
            self._viewers += 1
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            if self._running:
                return
            port = self.hub._alloc_port()
            if port is None:
                self._viewers -= 1
                raise VideoError(503, "站点的视频端口都占满了")
            self._gen += 1
            self._running = True
            self.starts += 1
            self._port = port
            self._latest, self._last_at, self._detail = None, None, ""
            gen, pw = self._gen, secrets.token_hex(16)
        threading.Thread(target=self._pump, args=(gen, port, pw), daemon=True,
                         name=f"video-rx-{self.robot_id}-{self.camera}").start()
        threading.Thread(target=self._renew, args=(gen, port, pw), daemon=True,
                         name=f"video-cmd-{self.robot_id}-{self.camera}").start()

    def leave(self) -> None:
        with self._ready:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers or not self._running:
                return
            t = threading.Timer(self.hub.idle_s, self._stop_if_idle, args=(self._gen,))
            t.daemon = True
            self._stop_timer = t
        t.start()

    def _stop_if_idle(self, gen: int) -> None:
        with self._ready:
            if gen != self._gen or self._viewers:
                return
            doomed = self._detach_locked()
        _kill(doomed)

    def _detach_locked(self) -> subprocess.Popen[bytes] | None:
        """收掉这一代:推代次(续命令线程、收流线程下一圈自己退),把进程交出去在锁外 kill。"""
        self._gen += 1
        self._running = False
        proc, self._proc = self._proc, None
        if self._port is not None:
            self.hub._free_port(self._port)
            self._port = None
        self._last_at = None
        self._ready.notify_all()
        return proc

    def close(self) -> None:
        with self._ready:
            self._viewers = 0
            if self._stop_timer is not None:
                self._stop_timer.cancel()
            doomed = self._detach_locked()
        _kill(doomed)

    # ------------------------------------------------------------ 收流与续命令

    def _argv(self, port: int, pw: str) -> list[str]:
        return [self.hub.ffmpeg, "-nostdin", "-loglevel", "error", "-fflags", "nobuffer",
                "-probesize", "32768", "-analyzeduration", "500000",
                "-i", f"srt://{self.hub.bind_host}:{port}?mode=listener&passphrase={pw}"
                      f"&pbkeylen=16&latency={self.hub.latency_ms * 1000}",
                "-an", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "6",
                "-r", f"{self.hub.fps:g}", "-"]

    def _pump(self, gen: int, port: int, pw: str) -> None:
        errs = tempfile.TemporaryFile()
        proc = None
        try:
            try:
                proc = subprocess.Popen(self._argv(port, pw), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=errs)  # 没有 shell
            except OSError as exc:
                self.fail(gen, f"站点起不了 ffmpeg({self.hub.ffmpeg}): {exc}")
                return
            with self._ready:
                if gen != self._gen:
                    return
                self._proc = proc
            assert proc.stdout is not None
            try:
                for frame in _frames(proc.stdout):
                    with self._ready:
                        if gen != self._gen:
                            return
                        self._latest = frame
                        self._seq += 1
                        self._last_at = self.hub.clock()
                        self._detail = ""
                        self._ready.notify_all()
            except (OSError, ValueError):
                pass
            self.fail(gen, f"{self.robot_id} 的 {self.camera} 画面断了: "
                           f"{_tail(errs) or '狗那头停了推流'}")
        finally:
            _kill(proc)
            with contextlib.suppress(OSError):
                errs.close()

    def _renew(self, gen: int, port: int, pw: str) -> None:
        """发 ``video`` 命令,有观众期间每 ttl/2 续一次。代次一变就退。"""
        req = VideoRequest(camera=self.camera, url=f"srt://{self.hub.srt_host}:{port}",
                           passphrase=pw, ttl_ms=self.hub.ttl_ms)
        while True:
            with self._ready:
                if gen != self._gen:
                    return
            why = self.hub._send(self.robot_id, req)
            if why:
                self.fail(gen, f"狗没接推流命令: {why}")
            with self._ready:
                if gen != self._gen:
                    return
                self._ready.wait(self.hub.ttl_ms / 2000.0)

    def fail(self, gen: int | None, detail: str) -> None:
        """这一代出错了:观众拿到这句话,收流收掉。``gen`` 为 None = 当前这一代(狗报的事件)。"""
        with self._ready:
            if gen is not None and gen != self._gen:
                return
            if not self._running:
                return
            self._detail = detail
            doomed = self._detach_locked()
        _kill(doomed)
        log.warning("%s", detail)

    # ------------------------------------------------------------ 读

    def _online_locked(self) -> bool:
        return (self._running and self._last_at is not None
                and self.hub.clock() - self._last_at <= self.hub.stale_s)

    def health(self) -> dict[str, Any]:
        with self._ready:
            age = (None if self._last_at is None
                   else int((self.hub.clock() - self._last_at) * 1000))
            return {"online": self._online_locked(), "last_frame_age_ms": age,
                    "viewers": self._viewers, "detail": self._detail}

    def frames(self) -> Iterator[bytes]:
        """一次吐一个 JPEG。调用方**必须** ``close()`` 它(观众随时会走,退场在 ``finally`` 里)。
        第一帧等 ``first_frame_timeout_s``,等不到抛 504。"""
        self.join()
        try:
            with self._ready:
                seen = self._seq - 1 if self._latest is not None else self._seq
            deadline = self.hub.clock() + self.hub.first_frame_timeout_s
            first = True
            while True:
                with self._ready:
                    while self._seq <= seen or self._latest is None:
                        if self._detail:
                            raise VideoError(502, self._detail)
                        if not self._running:
                            raise VideoError(502, f"{self.robot_id} 的 {self.camera} 画面没了")
                        if first:
                            left = deadline - self.hub.clock()
                            if left <= 0:
                                raise VideoError(504, f"{self.hub.first_frame_timeout_s:g} s 没等到"
                                                      f" {self.robot_id} 的 {self.camera} 第一帧 ——"
                                                      f"狗不在线、相机不通,或者狗到站点的端口不通")
                            self._ready.wait(min(left, 0.5))
                        else:
                            self._ready.wait(1.0)
                            if not self._online_locked() and self._running and not self._detail:
                                raise VideoError(
                                    504, f"{self.robot_id} 的 {self.camera} 画面冻住了")
                    seen = self._seq
                    frame = self._latest
                first = False
                yield frame
        finally:
            self.leave()


class VideoHub:
    def __init__(self, *, send: Callable[[str, VideoRequest], str], srt_host: str,
                 ports: tuple[int, int] = (8890, 8989), bind_host: str = "0.0.0.0",
                 ffmpeg: str = "ffmpeg", ttl_ms: int = 10_000, idle_s: float = 10.0,
                 first_frame_timeout_s: float = 8.0, max_viewers: int = MAX_VIEWERS,
                 stale_s: float = STALE_S, fps: float = 5.0, latency_ms: int = 200,
                 clock: Callable[[], float] = time.monotonic) -> None:
        #: 发 ``video`` 命令:``(robot_id, req) -> 拒绝理由(空串 = 收下)``。
        self._send = send
        self.srt_host, self.bind_host = srt_host, bind_host
        self._ports = range(ports[0], ports[1] + 1)
        self._used: set[int] = set()
        self._plock = threading.Lock()
        self.ffmpeg = ffmpeg
        self.ttl_ms, self.idle_s = ttl_ms, idle_s
        self.first_frame_timeout_s = first_frame_timeout_s
        self.max_viewers, self.stale_s, self.fps = max_viewers, stale_s, fps
        self.latency_ms = latency_ms
        self.clock = clock
        self._feeds: dict[tuple[str, str], _Feed] = {}
        self._flock = threading.Lock()

    def feed(self, robot_id: str, camera: str) -> _Feed:
        if camera not in CAMERAS:
            raise VideoError(404, f"没有相机 {camera!r}(只有 {'/'.join(CAMERAS)})")
        with self._flock:
            f = self._feeds.get((robot_id, camera))
            if f is None:
                f = self._feeds[(robot_id, camera)] = _Feed(self, robot_id, camera)
            return f

    def stream(self, robot_id: str, camera: str) -> Iterator[bytes]:
        return self.feed(robot_id, camera).frames()

    def health(self, robot_id: str) -> dict[str, Any]:
        return {cam: self.feed(robot_id, cam).health() for cam in CAMERAS}

    def on_event(self, robot_id: str, e: Event) -> None:
        """狗报推流失败:这一路的观众立刻拿到原因,不用干等超时。"""
        if e.kind != "video_failed" or not isinstance(e.data, dict):
            return
        cam = e.data.get("camera")
        with self._flock:
            f = self._feeds.get((robot_id, cam)) if isinstance(cam, str) else None
        if f is not None:
            f.fail(None, f"狗推流失败: {e.data.get('reason', '')}")

    def _alloc_port(self) -> int | None:
        with self._plock:
            for p in self._ports:
                if p in self._used:
                    continue
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    try:
                        s.bind((self.bind_host, p))
                    except OSError:
                        continue
                self._used.add(p)
                return p
        return None

    def _free_port(self, port: int) -> None:
        with self._plock:
            self._used.discard(port)

    def close(self) -> None:
        with self._flock:
            feeds = list(self._feeds.values())
        for f in feeds:
            f.close()


def dispatcher_sender(dispatcher, loop) -> Callable[[str, VideoRequest], str]:
    """把 ``Dispatcher.video`` 包成 :class:`VideoHub` 要的同步发送:在站点的事件循环里发、等回执。"""
    from d1max_contract.dispatch import DispatchTimeout
    from d1max_site.dispatcher import DispatchRefused

    def send(robot_id: str, req: VideoRequest) -> str:
        try:
            ack = loop.call(lambda: dispatcher.video(robot_id, req),
                            timeout_s=dispatcher.ack_timeout_s + 5)
        except DispatchRefused as exc:
            return str(exc)
        except (DispatchTimeout, TimeoutError):
            return "等狗的回执超时"
        if ack.result is AckResult.ACCEPTED:
            return ""
        return f"{ack.result.value}: {ack.reason}"
    return send
