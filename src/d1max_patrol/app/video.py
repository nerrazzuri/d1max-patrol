"""RTSP -> MJPEG:把机器狗的相机流变成浏览器放得出来的东西。

浏览器不认 RTSP。前后广角推的是 ``rtsp://<机器>:8554/{front,back}``(清单
#29,``scripts/patrol_snap.sh`` 已经在用),所以 app 起一条 ffmpeg 把它转成一
串 JPEG,再包成 ``multipart/x-mixed-replace`` 直接喂 ``<img>``。一个观众一条
ffmpeg 子进程。

**为什么是子进程而不是库。** 板载那台机器上 ffmpeg 是现成的,而 Python 侧的
RTSP 客户端要么带一整个 OpenCV,要么自己实现 RTP 重组。转码这件事本来就是
ffmpeg 干得最好的,进程边界还顺手把解码崩溃挡在 app 外面。

**为什么必须 TCP。** ``-rtsp_transport tcp``。现场是机器狗自己开的 WiFi AP,
UDP 在那上面丢包丢到画面没法看 —— ``patrol_snap.sh`` 已经踩过这个坑。

**为什么有并发上限。** 观众断开时如果不杀 ffmpeg,一晚上能攒出几十个进程,
每个都在解一路 1080p。上限是**每路流**的:同时看前后两个相机是两个观众各
一条,而反复刷新同一个画面页才是要挡的那件事。超了就明确拒绝,不排队 ——
排队意味着人盯着一个转圈的图标,不知道是没连上还是在等。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import IO

from d1max_patrol.backends.base import Frame, MediaError, MediaSource

#: app 认识的相机名。和 RTSP 路径最后一段一致。
CAMERAS = ("front", "back")

#: 同一路流最多几个人同时看。见模块开头。
MAX_VIEWERS = 2

#: JPEG 的起止标记。ffmpeg 的 image2pipe 输出就是一串首尾相接的 JPEG。
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

#: 一次从管道里捞多少。够大到不用捞很多次,又不至于攒出明显延迟。
_CHUNK = 65536

#: 拉流失败时,ffmpeg 的报错取最后这么多字节带给页面。
_ERR_TAIL = 400

#: 抓一张最多等多久。RTSP 握手加上等一个关键帧,现场那条 WiFi 上偶尔要好几秒。
STILL_TIMEOUT_S = 20.0


class VideoError(RuntimeError):
    """拉不到画面。**消息是给人看的**,会原样显示在页面上。"""


def _tail(handle: IO[bytes]) -> str:
    """把 ffmpeg 的 stderr 尾巴读出来。

    读不出来就当没有 —— 这个函数只在"已经出错了"的路径上被调用,它自己再
    抛一次只会把真正的原因盖掉。
    """
    try:
        handle.seek(0)
        return handle.read()[-_ERR_TAIL:].decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _kill(proc: subprocess.Popen[bytes] | None) -> None:
    """把子进程收干净。观众一断开就得走到这儿。"""
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


def _frames(pipe: IO[bytes]) -> Iterator[bytes]:
    """把字节流切成一个个完整 JPEG。

    **管道给的块和 JPEG 边界没有任何关系**:一次读可能拿到半帧,也可能拿到
    两帧半。所以要自己攒缓冲、自己找标记。

    用 ``read1`` 而不是 ``read``:后者会一直等到凑满 ``_CHUNK`` 或者管道关闭,
    在实时流上那就是凭空多出来的延迟。
    """
    buf = bytearray()
    while True:
        chunk = pipe.read1(_CHUNK)
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(_SOI)
            if start < 0:
                # 整段里连个帧头都没有,留着也没用。
                buf.clear()
                break
            end = buf.find(_EOI, start + len(_SOI))
            if end < 0:
                del buf[:start]     # 帧头之前的垃圾丢掉,帧头之后的留着接
                break
            yield bytes(buf[start:end + len(_EOI)])
            del buf[:end + len(_EOI)]


class MjpegSource:
    """一路 RTSP 流。每调一次 :meth:`stream` 就是一个观众。"""

    def __init__(self, rtsp_url: str, *, ffmpeg: str = "ffmpeg",
                 fps: float = 5.0) -> None:
        self._url = rtsp_url
        self._ffmpeg = ffmpeg
        self._fps = fps
        self._lock = threading.Lock()
        self._viewers = 0

    @property
    def url(self) -> str:
        return self._url

    @property
    def viewers(self) -> int:
        """现在有几个人在看。每个 HTTP 请求一条线程,所以这个数要加锁。"""
        with self._lock:
            return self._viewers

    @property
    def argv(self) -> tuple[str, ...]:
        """ffmpeg 的完整命令行。

        ``-nostdin`` 是必须的:不加的话 ffmpeg 会去抢终端的标准输入,把
        父进程的 Ctrl+C 一起吃掉。
        """
        return (self._ffmpeg, "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-i", self._url,
                "-f", "image2pipe", "-vcodec", "mjpeg",
                "-q:v", "6", "-r", f"{self._fps:g}", "-")

    def stream(self) -> Iterator[bytes]:
        """一次吐一个完整 JPEG。

        **第一次 ``next()`` 才真的起 ffmpeg** —— 生成器就是这样。上限和"起
        不起得来"两件事因此都落在第一帧上,调用方可以先取一帧,把失败变成一
        个说得清楚的 HTTP 状态码,再决定要不要开始往外写。

        调用方**必须** ``close()`` 它:人随时会关标签页,那时它正卡在读管道
        上,杀 ffmpeg 的逻辑只在 ``finally`` 里。
        """
        return self._stream()

    def _stream(self) -> Iterator[bytes]:
        with self._lock:
            if self._viewers >= MAX_VIEWERS:
                raise VideoError(
                    f"{self._url} 已经有 {self._viewers} 个人在看了,"
                    f"最多 {MAX_VIEWERS} 个 —— 关掉一个再开")
            self._viewers += 1
        proc: subprocess.Popen[bytes] | None = None
        errs = tempfile.TemporaryFile()
        try:
            try:
                proc = subprocess.Popen(          # 命令行是自己拼的,没有 shell
                    self.argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=errs)
            except OSError as exc:
                raise VideoError(
                    f"起不了 ffmpeg({self._ffmpeg}): {exc} —— "
                    f"装一个(apt install ffmpeg),或者用 --ffmpeg 指到它"
                ) from exc
            assert proc.stdout is not None      # Popen 的类型标注给不出这个
            got = 0
            for frame in _frames(proc.stdout):
                got += 1
                yield frame
            if got == 0:
                # ffmpeg 起来了但一帧都没出:多半是拉不到流(地址错、
                # 推流没开、网线没插)。把它自己的报错原样带给人。
                raise VideoError(f"拉不到 {self._url}: {_tail(errs) or 'ffmpeg 没说原因'}")
        finally:
            _kill(proc)
            with contextlib.suppress(OSError):
                errs.close()
            with self._lock:
                self._viewers -= 1


class RtspStill(MediaSource):
    """到点抓一张。巡检拍照走的就是它。

    跟 :class:`MjpegSource` 同一路流、同一串 ffmpeg 参数的前半截,区别只有一
    条:**抓完就把 ffmpeg 收掉**。巡检拍照是几分钟一次的事,让一条 ffmpeg 在
    两个点位之间空转几分钟,白解码是小事,真正的问题是它会在链路抖动时悄悄
    死掉 —— 而那一刻没有人在看,等到了点位要拍照才发现。

    这是 ``MediaSource`` 在这台机器上唯一的实现。备用路径(``take_photo``)
    不存在:SDK 没有拍照接口,``sidecar_device`` 会明确拒绝。
    """

    def __init__(self, rtsp_url: str, *, ffmpeg: str = "ffmpeg",
                 timeout_s: float = STILL_TIMEOUT_S) -> None:
        self._url = rtsp_url
        self._ffmpeg = ffmpeg
        self._timeout = timeout_s

    @property
    def url(self) -> str:
        return self._url

    @property
    def argv(self) -> tuple[str, ...]:
        """完整命令行。``-frames:v 1`` 是"拿到一帧就退",不是"截前一秒"。

        ``-rtsp_transport tcp`` 和实时画面那边是同一个理由,见模块开头。
        """
        return (self._ffmpeg, "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-i", self._url,
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg",
                "-q:v", "2", "-")

    async def open(self) -> None:
        """没有要开的东西 —— 每次抓图起自己那条 ffmpeg。"""

    async def close(self) -> None:
        """同上。抓图那条 ffmpeg 在 :meth:`grab` 返回时就已经退了。"""

    async def grab(self) -> Frame:
        """抓一帧。**在线程里跑** —— 等 ffmpeg 是阻塞的,不能占住事件循环。

        循环被占住的后果不是"慢一点":急停、遥控心跳、事件流全在这条循环上,
        而抓图最长能等 20 秒。
        """
        data = await asyncio.to_thread(self._grab)
        return Frame(data=data, mime="image/jpeg",
                     captured_at_ms=int(time.time() * 1000))

    async def healthy(self) -> bool:
        """真抓一张试试。

        比"看进程在不在"贵,但那个便宜的版本在这里什么也证明不了 —— 这个类
        平时根本没有进程。预检要回答的是"到了点位拍得出来吗"。
        """
        try:
            await self.grab()
        except MediaError:
            return False
        return True

    def _grab(self) -> bytes:
        try:
            done = subprocess.run(          # 命令行是自己拼的,没有 shell
                self.argv, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=self._timeout)
        except OSError as exc:
            raise MediaError(
                f"起不了 ffmpeg({self._ffmpeg}): {exc} —— "
                f"装一个(apt install ffmpeg),或者用 --ffmpeg 指到它"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaError(
                f"抓 {self._url} 超过 {self._timeout:g}s 还没出图 —— "
                f"多半是推流没开或者网断了") from exc
        # 走同一个切帧器:ffmpeg 偶尔会在图前面带点别的,而"第一个完整 JPEG"
        # 这件事两边必须是同一套判断,不然实时画面能看、拍照存下来是坏的。
        frame = next(_frames(io.BytesIO(done.stdout)), None)
        if frame is None:
            tail = done.stderr[-_ERR_TAIL:].decode("utf-8", "replace").strip()
            raise MediaError(f"抓不到 {self._url}: {tail or 'ffmpeg 没说原因'}")
        return frame


__all__ = ["CAMERAS", "MAX_VIEWERS", "STILL_TIMEOUT_S", "MjpegSource",
           "RtspStill", "VideoError"]
