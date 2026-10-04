"""连续录像(W18,决策 31:**站点优先录**,WiFi 断了狗上存着,回来补推、狗上清掉)。

做法:狗上每路相机一条 ffmpeg,一直录,一分钟切一段(``-c copy`` 不转码;仿真的测试图要编码)。切好的
那段挪进录像发件箱 ``<根>/video/<相机>/<UTC 时刻>/video.mp4``,
由它自己的上传线程马上传站点(跟运行记录
同一套分块续传、按站点存下的字节比哈希);站点确认了才删狗上那份。WiFi 好的时候站点晚一分钟就有;WiFi
断了片段攒在狗上,回来自动补传、补完清掉。没有「推流 ↔ 本地录」的切换,切换的那几秒不会丢。

- **分片 mp4**(``frag_keyframe+empty_moov``):ffmpeg 被杀、断电,正写着的那段照样放得到断的地方。
- **狗上有配额**:攒的(没传完的)超过 ``quota_bytes``,或者盘用到 ``WARN_RATIO``,删最旧的段,报一次
  ``recording_dropped``(站点出告警:断网太久,这段录像没了)。巡检归档优先于录像。
- **录不了要让人知道**:ffmpeg 自己退了(相机不通、盘写不进)报一次 ``recording_failed``,隔一会儿重起;
  录回来报 ``recording_ok``。
- 时刻是**狗的钟**(``TZ=UTC`` 给 ffmpeg 的 strftime):狗的钟不准站点会报 ``clock_skew``(W09d)。

线程:只在代理的事件循环里调(``start``、``step`` 每拍、``close``)。ffmpeg 是子进程,不阻塞。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from d1max_contract.intake import VIDEO_FILE, split_video
from d1max_contract.storage import WARN_RATIO

log = logging.getLogger(__name__)

Source = Callable[[str], list[str]]

SEGMENT_S = 60
#: ffmpeg 退了之后隔多久重起(秒)。
RESTART_AFTER_S = 10.0
_ERR_TAIL = 300
#: 发件箱里录像那一块的子目录(``Outbox(sub=…)``)。
SUB = "video"
#: 正在写的段放这里(点开头:上传扫描不看)。
STAGING = ".rec"


def video_classify(rel: str) -> int | None:
    """录像发件箱只传 ``video.mp4``(``Outbox(classify=…)``)。"""
    return 5 if rel == VIDEO_FILE else None


class _Cam:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.err: IO[bytes] | None = None
        self.retry_at = 0.0
        self.failed = False


class Recorder:
    def __init__(self, root: Path, cameras: list[str], *, source: Source, encode: bool = False,
                 segment_s: int = SEGMENT_S, quota_bytes: int,
                 emit: Callable[[str, dict[str, Any]], None],
                 monotonic: Callable[[], float],
                 disk_usage: Callable[[Path], tuple[int, int, int]] = shutil.disk_usage,
                 popen: Callable[..., Any] = subprocess.Popen, ffmpeg: str = "ffmpeg") -> None:
        self.root = Path(root)
        self.out = self.root / SUB
        self.cameras = list(cameras)
        self._source = source
        self._encode = encode or bool(getattr(source, "raw", False))
        self.segment_s = segment_s
        self.quota_bytes = quota_bytes
        self._emit = emit
        self._mono = monotonic
        self._disk_usage = disk_usage
        self._popen = popen
        self._ffmpeg = ffmpeg
        self._cams = {c: _Cam() for c in self.cameras}
        self._dropping = False
        #: 删掉了几段没传完的(看得见才查得到)。
        self.dropped = 0

    # ------------------------------------------------------------ 起停

    def start(self) -> None:
        for c in self.cameras:
            (self.root / STAGING / c).mkdir(parents=True, exist_ok=True)
            self._collect(c, include_newest=True)   # 上次被杀掉留下的:都是写完了的(分片 mp4 放得了)
            self._spawn(c)

    def close(self) -> None:
        for c, s in self._cams.items():
            self._kill(s)
            self._collect(c, include_newest=True)

    def _argv(self, camera: str) -> list[str]:
        codec = (["-c:v", "libx264", "-preset", "ultrafast", "-g", "30"] if self._encode
                 else ["-c:v", "copy"])
        pattern = str(self.root / STAGING / camera / "%Y%m%dT%H%M%SZ.mp4")
        return [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                *self._source(camera), *codec, "-an",
                "-f", "segment", "-segment_time", str(self.segment_s), "-reset_timestamps", "1",
                "-strftime", "1", "-segment_format", "mp4",
                "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
                pattern]

    def _spawn(self, camera: str) -> None:
        s = self._cams[camera]
        s.err = tempfile.TemporaryFile()
        try:
            s.proc = self._popen(self._argv(camera), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=s.err,
                                 env=os.environ | {"TZ": "UTC"})
        except OSError as exc:
            s.proc = None
            self._failed(camera, s, f"起不来 ffmpeg: {exc}")

    def _kill(self, s: _Cam) -> None:
        p, s.proc = s.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        if s.err is not None:
            s.err.close()
            s.err = None

    def _tail(self, s: _Cam) -> str:
        if s.err is None:
            return ""
        with contextlib.suppress(OSError, ValueError):
            s.err.seek(0)
            return s.err.read()[-_ERR_TAIL:].decode("utf-8", "replace").strip()
        return ""

    def _failed(self, camera: str, s: _Cam, why: str) -> None:
        s.retry_at = self._mono() + RESTART_AFTER_S
        if not s.failed:
            s.failed = True
            log.warning("%s 录像断了:%s", camera, why)
            self._emit("recording_failed", {"camera": camera, "reason": why[:300]})

    # ------------------------------------------------------------ 每拍

    def step(self) -> None:
        for c, s in self._cams.items():
            if s.proc is not None and s.proc.poll() is not None:
                why = f"ffmpeg 退了({s.proc.returncode}):{self._tail(s)}"
                self._kill(s)
                self._collect(c, include_newest=True)
                self._failed(c, s, why)
            if s.proc is None and self._mono() >= s.retry_at:
                self._spawn(c)
            elif s.proc is not None:
                if self._collect(c, include_newest=False) and s.failed:
                    s.failed = False                     # 又切出新的一段:录回来了
                    log.info("%s 录像恢复", c)
                    self._emit("recording_ok", {"camera": c})
        self._enforce_quota()

    def _collect(self, camera: str, *, include_newest: bool) -> int:
        """把写完了的段挪进发件箱。正在写的是最新那一段(ffmpeg 还活着时不动它)。回挪了几段。"""
        d = self.root / STAGING / camera
        try:
            segs = sorted(p for p in d.iterdir() if p.suffix == ".mp4" and p.is_file())
        except OSError:
            return 0
        good = []
        for p in segs:
            if split_video(f"{camera}/{p.stem}") is None:
                p.unlink(missing_ok=True)  # 名字不合规:不传,也不能算「最新那段」
            else:
                good.append(p)
        if not include_newest:
            good = good[:-1]                             # 正在写的(可能刚开、还是空的):不碰
        moved = 0
        for p in good:
            if p.stat().st_size == 0:                    # 写完了还是空的:不传
                p.unlink(missing_ok=True)
                continue
            dest = self.out / camera / p.stem / VIDEO_FILE
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, dest)
            moved += 1
        return moved

    # ------------------------------------------------------------ 配额

    def _segments(self) -> list[Path]:
        """发件箱里的段目录,最旧的在前(按时刻,不分相机)。"""
        out: list[Path] = []
        for c in self.cameras:
            with contextlib.suppress(OSError):
                out += [p for p in (self.out / c).iterdir() if p.is_dir()]
        return sorted(out, key=lambda p: p.name)

    def _enforce_quota(self) -> None:
        segs = self._segments()
        sizes = {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in segs}
        total = sum(sizes.values())
        try:
            t, used, _ = self._disk_usage(self.root)
            ratio = used / t if t else 1.0
        except OSError:
            ratio = 0.0
        dropped: list[str] = []
        while segs and (total > self.quota_bytes or ratio >= WARN_RATIO):
            p = segs.pop(0)
            total -= sizes[p]
            shutil.rmtree(p, ignore_errors=True)
            dropped.append(f"{p.parent.name}/{p.name}")
            if ratio >= WARN_RATIO:
                with contextlib.suppress(OSError):
                    t, used, _ = self._disk_usage(self.root)
                    ratio = used / t if t else 1.0
        if dropped:
            self.dropped += len(dropped)
            if not self._dropping:
                self._dropping = True
                log.warning("录像攒太多(没传到站点),删了最旧的 %d 段", len(dropped))
                self._emit("recording_dropped", {
                    "count": len(dropped), "oldest": dropped[0], "newest": dropped[-1],
                    "reason": "disk" if ratio >= WARN_RATIO else "quota"})
        elif self._dropping and total < self.quota_bytes * 0.8:
            self._dropping = False

    def caps(self) -> dict[str, Any]:
        return {"cameras": list(self.cameras), "segment_s": self.segment_s}
