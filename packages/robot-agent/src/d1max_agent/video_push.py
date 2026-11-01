"""按需把相机推到站点(W00c5b)。站点要看某路相机时发 ``video`` 命令(``d1max_contract.video``),
这里每路相机至多一条 ffmpeg:拉本机相机、推 SRT(加密)到站点。

- **有效期到了没续就停**:站点没了、网断了,狗不会一直往外推(4G 流量、决策 8)。
- 同一个地址同一个口令的命令只续期,不重起(画面不黑一下);地址或口令变了就重起。
- 推流进程**没到期就自己退了**(连不上站点、相机不通):报一条 ``video_failed``,只报一次。
- **默认不转码**(``-c:v copy``):不在 Orin 上白烧 CPU,上行也只推相机原样的那份。相机若不是
  H.264(真机没量过),用 ``transcode=True`` 转成 H.264。

线程:只在代理的事件循环里调(``request`` 由命令处理、``step`` 每拍)。ffmpeg 是子进程,不阻塞。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from d1max_contract.video import VideoRequest, scrub_tail, srt_push_url

log = logging.getLogger(__name__)

Source = Callable[[str], list[str]]

#: 报错时带多少 ffmpeg 的 stderr 尾巴。
_ERR_TAIL = 300
#: stderr 临时文件超过这么大就清空(libsrt 不管 -loglevel,丢包时会一直往 stderr 写)。
_ERR_MAX = 256 * 1024


class CameraConfigError(ValueError):
    """``cameras.json`` 写错了。"""


#: 相机地址:``rtsp://`` 或 ``rtsps://``,可以带 ``用户:口令@``。
_RTSP = re.compile(r"rtsps?://[^\s]{1,500}")


def load_cameras(path: Path) -> dict[str, dict[str, Any]]:
    """``/etc/d1max/cameras.json``(W40):狗自带两路以外的相机(上装 V1.2 的热像),或者改自带那两路的
    地址。``{"thermal": {"url": "rtsp://用户:口令@192.168.10.64:554/Streaming/Channels/101",
    "record": false}}``。文件里有口令:装机时 ``0640 root:robot``。没有这个文件 = 只有自带两路。

    ``record``:``--record auto`` 时录不录这一路(缺省不录:热像 24 小时录要占盘)。"""
    from d1max_contract.video import CAMERAS
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CameraConfigError(f"相机配置 {path} 无法读取: {exc}") from exc
    if not isinstance(d, dict):
        raise CameraConfigError("相机配置要是 JSON 对象")
    out: dict[str, dict[str, Any]] = {}
    for name, v in d.items():
        if name not in CAMERAS:
            raise CameraConfigError(f"相机名只认 {'/'.join(CAMERAS)}:{name!r}")
        if not isinstance(v, dict) or not isinstance(v.get("url"), str) \
                or not _RTSP.fullmatch(v["url"]):
            raise CameraConfigError(f"{name}.url 要是 rtsp:// 地址")
        if not isinstance(v.get("record", False), bool):
            raise CameraConfigError(f"{name}.record 要是 true/false")
        out[name] = {"url": v["url"], "record": v.get("record", False)}
    return out


def rtsp_source(host: str, extra: dict[str, dict[str, Any]] | None = None) -> Source:
    """真狗:RK3588 上的相机 RTSP(``rtsp://<host>:8554/<camera>``),走 TCP。``extra``
    (:func:`load_cameras`)里的相机按它自己的地址拉。返回的函数带 ``cameras``:这只狗有哪几路。"""
    from d1max_contract.video import STANDARD_CAMERAS
    urls = {c: f"rtsp://{host}:8554/{c}" for c in STANDARD_CAMERAS} | {
        c: v["url"] for c, v in (extra or {}).items()}

    def src(camera: str) -> list[str]:
        return ["-rtsp_transport", "tcp", "-i", urls[camera]]
    src.cameras = tuple(urls)  # type: ignore[attr-defined]
    return src


def lavfi_source(camera: str) -> list[str]:
    """仿真:ffmpeg 自己的测试图。原始帧,必须编码。"""
    return ["-re", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15"]


lavfi_source.raw = True  # type: ignore[attr-defined]


def _duration(path: Path, ffprobe: str = "ffprobe") -> float:
    import subprocess
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                             timeout=20).stdout.strip()
        return max(float(out), 1.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1.0


def file_snapshot_source(folder: Path, *, durations: dict[str, float] | None = None,
                         now_s: Callable[[], float] | None = None) -> Source:
    """W64:到点拍照从同一段录像截一帧(按当前时刻在片长里取一个位置,每次不一样)。"""
    import time
    clock = now_s or time.time
    lens = dict(durations or {})

    def src(camera: str) -> list[str]:
        path = folder / f"{camera}.mp4"
        if camera not in lens:
            lens[camera] = _duration(path)
        return ["-ss", f"{clock() % lens[camera]:.1f}", "-i", str(path)]
    from d1max_contract.video import STANDARD_CAMERAS
    src.cameras = tuple(STANDARD_CAMERAS)  # type: ignore[attr-defined]
    src.dynamic = True  # type: ignore[attr-defined]
    return src


def file_source(folder: Path) -> Source:
    """仿真、演示(W64):每路相机循环播 ``<folder>/<相机>.mp4``(现场录的真画面)。按实时速度读;
    要重新编码,循环接头处时间戳才连续。"""
    def src(camera: str) -> list[str]:
        return ["-re", "-stream_loop", "-1", "-i", str(folder / f"{camera}.mp4")]
    src.raw = True  # type: ignore[attr-defined]
    return src


def source_cameras(source: Source) -> tuple[str, ...]:
    """这个来源有哪几路相机(没说就是自带那两路)。"""
    from d1max_contract.video import STANDARD_CAMERAS
    return tuple(getattr(source, "cameras", STANDARD_CAMERAS))


@dataclass
class _Push:
    req: VideoRequest
    proc: subprocess.Popen[bytes]
    until: float
    errs: IO[bytes]


def _tail(f: IO[bytes]) -> str:
    """stderr 的尾巴,**口令抹掉**:ffmpeg 报错会带出完整的推流地址。"""
    try:
        f.seek(0)
        return scrub_tail(f.read(), _ERR_TAIL)          # 外审 R2:先打码再截
    except OSError:
        return ""


class VideoPusher:
    def __init__(self, *, source: Source, ffmpeg: str = "ffmpeg", transcode: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 emit: Callable[[str, dict[str, Any]], None] | None = None,
                 latency_ms: int = 200) -> None:
        self._source = source
        self._ffmpeg = ffmpeg
        self._transcode = transcode or bool(getattr(source, "raw", False))
        self._clock = clock
        #: ``video_failed`` 往哪儿报。代理运行时把它接到事件簿上。
        self.emit = emit
        self._latency_ms = latency_ms
        self._push: dict[str, _Push] = {}
        #: 一共起过几条推流进程。测试用的观察窗:「续期不重起」只有累计数看得见。
        self.starts = 0

    def argv(self, req: VideoRequest) -> list[str]:
        codec = (["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                  "-g", "30"] if self._transcode else ["-c:v", "copy"])
        return [self._ffmpeg, "-nostdin", "-loglevel", "error", *self._source(req.camera),
                "-an", *codec, "-f", "mpegts", srt_push_url(req, latency_ms=self._latency_ms)]

    def cameras(self) -> tuple[str, ...]:
        return source_cameras(self._source)

    def request(self, req: VideoRequest) -> str:
        """收下一条 ``video`` 命令。返回空串 = 收下;否则是拒绝原因。"""
        if req.camera not in self.cameras():
            return f"unsupported: 此机器人没有 {req.camera} 这路相机"
        if req.stop:
            self._stop(req.camera)                    # 站点收流之前说停:安静地停,不报失败
            return ""
        now = self._clock()
        cur = self._push.get(req.camera)
        if cur is not None and cur.proc.poll() is None and cur.req.url == req.url \
                and cur.req.passphrase == req.passphrase:
            cur.until = now + req.ttl_ms / 1000.0
            return ""
        if cur is not None:
            self._stop(req.camera)
        # 追加模式:清空之后子进程接着往新的结尾写,不会留一个越来越大的空洞文件。
        errs = tempfile.TemporaryFile(mode="a+b")
        try:
            proc = subprocess.Popen(self.argv(req), stdin=subprocess.DEVNULL,  # 自己拼的,没有 shell
                                    stdout=subprocess.DEVNULL, stderr=errs)
        except OSError as exc:
            errs.close()
            return f"无法启动 ffmpeg({self._ffmpeg}): {exc}"
        self.starts += 1
        self._push[req.camera] = _Push(req=req, proc=proc, until=now + req.ttl_ms / 1000.0,
                                       errs=errs)
        log.info("开始推 %s → %s(%d ms)", req.camera, req.url, req.ttl_ms)
        return ""

    def step(self) -> None:
        """每拍:到期没续的停;没到期就自己退了的报 ``video_failed``。"""
        now = self._clock()
        for cam, p in list(self._push.items()):
            with contextlib.suppress(OSError):
                if p.errs.seek(0, 2) > _ERR_MAX:
                    p.errs.truncate(0)
            if now >= p.until:
                log.info("%s 的推流到期没续,停", cam)
                self._stop(cam)
            elif p.proc.poll() is not None:
                why = _tail(p.errs) or "ffmpeg 未说原因"
                reason = f"推流进程退了(rc={p.proc.returncode}): {why}"
                self._stop(cam)
                log.warning("%s %s", cam, reason)
                if self.emit is not None:
                    # url 不带口令:站点据此认出是不是当前这一代(端口)。
                    self.emit("video_failed", {"camera": cam, "url": p.req.url,
                                               "reason": reason})

    def running(self) -> set[str]:
        return {c for c, p in self._push.items() if p.proc.poll() is None}

    def _stop(self, camera: str) -> None:
        p = self._push.pop(camera, None)
        if p is None:
            return
        if p.proc.poll() is None:
            with contextlib.suppress(OSError):
                p.proc.terminate()
            try:
                p.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    p.proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.proc.wait(timeout=2.0)
        with contextlib.suppress(OSError):
            p.errs.close()

    def close(self) -> None:
        for cam in list(self._push):
            self._stop(cam)
