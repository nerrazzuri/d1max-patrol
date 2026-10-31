"""连续录像(W18,决策 31:**站点优先录**,WiFi 断了狗上存着,回来补推、狗上清掉)。

做法:狗上每路相机一条 ffmpeg,一直录,一分钟切一段(``-c copy`` 不转码;仿真的测试图要编码)。切好的
那段挪进录像发件箱 ``<根>/video/<相机>/<UTC 时刻>/video.mp4``,
由它自己的上传线程马上传站点(跟运行记录
同一套分块续传、按站点存下的字节比哈希);站点确认了才删狗上那份。WiFi 好的时候站点晚一分钟就有;WiFi
断了片段攒在狗上,回来自动补传、补完清掉。没有「推流 ↔ 本地录」的切换,切换的那几秒不会丢。

- **分片 mp4**(``frag_keyframe+empty_moov``):ffmpeg 被杀、断电,正写着的那段照样放得到断的地方。
- **每 ``SYNC_S`` 秒 fsync 一次正在写的那段**(W46,上装 V2 随时可能断电):不然页缓存里可能压着半分钟,
  断电最多丢几秒。fsync 在后台线程里做,不卡事件循环;上一轮还没做完就跳过这一轮。
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
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from d1max_contract.intake import VIDEO_FILE, VIDEO_SEALED, split_video
from d1max_contract.storage import WARN_RATIO

log = logging.getLogger(__name__)

Source = Callable[[str], list[str]]

SEGMENT_S = 60
#: 正在写的段多久 fsync 一次(秒)。
SYNC_S = 2.0
#: ffmpeg 退了之后隔多久重起(秒)。
RESTART_AFTER_S = 10.0
_ERR_TAIL = 300
#: 发件箱里录像那一块的子目录(``Outbox(sub=…)``)。
SUB = "video"
#: 正在写的段放这里(点开头:上传扫描不看)。
STAGING = ".rec"


def video_classify(rel: str) -> int | None:
    """录像发件箱只传 ``video.mp4``(``Outbox(classify=…)``)。"""
    return 5 if rel in (VIDEO_FILE, VIDEO_SEALED) else None


def fsync_files(paths: list[Path]) -> None:
    """把这几个文件写进盘(页缓存里压着的)。文件没了、打不开就跳过。"""
    for p in paths:
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
        except OSError as exc:
            log.warning("录像段 fsync 不了:%s:%s", p.name, exc)
        finally:
            os.close(fd)


class _Cam:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.err: IO[bytes] | None = None
        self.retry_at = 0.0
        self.failed = False
        #: 最近一次断的原因(设备健康报给站点,W40)。
        self.why = ""


class Recorder:
    def __init__(self, root: Path, cameras: list[str], *, source: Source, encode: bool = False,
                 segment_s: int = SEGMENT_S, quota_bytes: int,
                 emit: Callable[[str, dict[str, Any]], None],
                 monotonic: Callable[[], float],
                 disk_usage: Callable[[Path], tuple[int, int, int]] = shutil.disk_usage,
                 popen: Callable[..., Any] = subprocess.Popen, ffmpeg: str = "ffmpeg",
                 sealer: Any = None) -> None:
        self.root = Path(root)
        #: 证据加密(W30b):配了就封着挪进发件箱(:class:`~d1max_contract.evseal.Sealer`)。
        self.sealer = sealer
        #: 封不上、扣在暂存里的段(``<相机>/<时刻>`` → 原因;W30b 外审 1):不进发件箱、不传明文,下一拍
        #: 再封;代理把件数报站点。也算进配额(openssl 一直坏着的话暂存不许把盘写满)。
        self.seal_failed: dict[str, str] = {}
        self.out = self.root / SUB
        self.cameras = list(cameras)
        self._source = source
        self._encode = encode or bool(getattr(source, "raw", False))
        self.segment_s = segment_s
        self.quota_bytes = quota_bytes
        #: 报事件:``(种类, 数据)``。运行时接到事件簿(``runtime`` 里 ``recorder.emit =
        #: events.emit``)。
        #: **只有这一个字段**(W18 外审:原先内部用的是带下划线的另一个名字,接线写到这个名字上,
        #: 事件全丢了)。
        self.emit = emit
        self._mono = monotonic
        self._disk_usage = disk_usage
        self._popen = popen
        self._ffmpeg = ffmpeg
        self._cams = {c: _Cam() for c in self.cameras}
        self._dropping = False
        #: 删掉了几段没传完的(看得见才查得到)。
        self.dropped = 0
        #: 正在写的段交给谁 fsync(测试换掉);下一次几点做。
        self._sync: Callable[[list[Path]], None] = self._sync_bg
        self._sync_at = 0.0
        self._sync_thread: threading.Thread | None = None
        #: 暂停录的几路(W46:上装过热降负载时停热像)。停着不重起、不算录像断了。
        self.held: set[str] = set()

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
            from d1max_contract.video import scrub_tail  # W40:热像的 RTSP 地址带口令;外审 R2
            return scrub_tail(s.err.read(), _ERR_TAIL)
        return ""

    def _failed(self, camera: str, s: _Cam, why: str) -> None:
        s.retry_at = self._mono() + RESTART_AFTER_S
        s.why = why[:200]
        if not s.failed:
            s.failed = True
            log.warning("%s 录像断了:%s", camera, why)
            self.emit("recording_failed", {"camera": camera, "reason": why[:300]})

    def camera_health(self) -> dict[str, str]:
        """每一路录着没有(W40 设备健康):相机名 → 空串(在录,或者故意停着,见 :attr:`held`)
        或断了的原因。"""
        return {c: (s.why or "录像中断") if s.failed and c not in self.held else ""
                for c, s in self._cams.items()}

    def hold(self, cameras: set[str]) -> None:
        """只停这几路(别的照录);空集合 = 都放开。停的那一路当场收掉 ffmpeg,写到一半的那段挪走。"""
        new = {c for c in cameras if c in self._cams}
        for c in new - self.held:
            s = self._cams[c]
            self._kill(s)
            self._collect(c, include_newest=True)
            s.failed, s.why, s.retry_at = False, "", 0.0
            log.warning("%s 暂停录像(上装过热降负载)", c)
        for c in self.held - new:
            log.info("%s 接着录", c)
        self.held = new

    # ------------------------------------------------------------ 每拍

    def step(self) -> None:
        for c, s in self._cams.items():
            if s.proc is not None and s.proc.poll() is not None:
                why = f"ffmpeg 退了({s.proc.returncode}):{self._tail(s)}"
                self._kill(s)
                self._collect(c, include_newest=True)
                self._failed(c, s, why)
            if c in self.held:
                continue
            if s.proc is None and self._mono() >= s.retry_at:
                self._spawn(c)
            elif s.proc is not None:
                if self._collect(c, include_newest=False) and s.failed:
                    s.failed = False                     # 又切出新的一段:录回来了
                    log.info("%s 录像恢复", c)
                    self.emit("recording_ok", {"camera": c})
        if self._mono() >= self._sync_at:
            self._sync_at = self._mono() + SYNC_S
            paths = self._writing()
            if paths:
                self._sync(paths)
        self._enforce_quota()

    def _writing(self) -> list[Path]:
        """每路 ffmpeg 活着的、正在写的那段(暂存里最新的)。"""
        out = []
        for c, s in self._cams.items():
            if s.proc is None or s.proc.poll() is not None:
                continue
            with contextlib.suppress(OSError):
                segs = sorted(p for p in (self.root / STAGING / c).iterdir() if p.suffix == ".mp4")
                if segs:
                    out.append(segs[-1])
        return out

    def _sync_bg(self, paths: list[Path]) -> None:
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return                                       # 上一轮还没做完(盘慢):跳过这一轮
        self._sync_thread = threading.Thread(target=fsync_files, args=(paths,),
                                             name="rec-fsync", daemon=True)
        self._sync_thread.start()

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
            if self.sealer is not None:
                # W30b:封着挪进发件箱(暂存里正在写的那段是明文,写完了才挪;封好了再删明文)
                key = f"{camera}/{p.stem}"
                try:
                    self.sealer.seal_file(p, dest.with_name(VIDEO_SEALED))
                except Exception as exc:
                    if key not in self.seal_failed:
                        log.exception("%s 这一段封不上(留在暂存,下一拍再封)", p.name)
                    self.seal_failed[key] = str(exc)[:200] or type(exc).__name__
                    continue
                self.seal_failed.pop(key, None)
                p.unlink(missing_ok=True)
            else:
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

    def _stuck(self) -> list[Path]:
        """封不上、扣在暂存里的段(W30b)。"""
        out = []
        for key in list(self.seal_failed):
            camera, stamp = key.split("/", 1)
            p = self.root / STAGING / camera / f"{stamp}.mp4"
            if p.is_file():
                out.append(p)
            else:
                self.seal_failed.pop(key, None)
        return out

    def _enforce_quota(self) -> None:
        # 发件箱里的段是目录(``<相机>/<时刻>/``),暂存里封不上的是文件(``<相机>/<时刻>.mp4``):
        # 一起按时刻排、最旧的先删
        segs = sorted(self._segments() + self._stuck(),
                      key=lambda p: p.stem if p.suffix == ".mp4" else p.name)
        sizes = {p: (p.stat().st_size if p.suffix == ".mp4" else
                     sum(f.stat().st_size for f in p.rglob("*") if f.is_file())) for p in segs}
        total = sum(sizes.values())
        try:
            t, used, _ = self._disk_usage(self.root)
            ratio = used / t if t else 1.0
        except OSError:
            ratio = 0.0
        dropped: list[str] = []
        while segs and (total > self.quota_bytes or ratio >= WARN_RATIO):
            p = segs.pop(0)
            if p.suffix == ".mp4":
                with contextlib.suppress(OSError):
                    p.unlink()
            else:
                shutil.rmtree(p, ignore_errors=True)
            if p.exists():                               # 删不掉(权限、只读):不算删了,换下一段试
                log.warning("录像段删不掉:%s", p)
                continue
            total -= sizes[p]
            dropped.append(f"{p.parent.name}/{p.stem if p.suffix == '.mp4' else p.name}")
            self.seal_failed.pop(dropped[-1], None)
            if ratio >= WARN_RATIO:
                with contextlib.suppress(OSError):
                    t, used, _ = self._disk_usage(self.root)
                    ratio = used / t if t else 1.0
        if dropped:
            self.dropped += len(dropped)
            if not self._dropping:
                self._dropping = True
                log.warning("录像攒太多(没传到站点),删了最旧的 %d 段", len(dropped))
                self.emit("recording_dropped", {
                    "count": len(dropped), "oldest": dropped[0], "newest": dropped[-1],
                    "reason": "disk" if ratio >= WARN_RATIO else "quota"})
        elif self._dropping and total < self.quota_bytes * 0.8:
            self._dropping = False

    def caps(self) -> dict[str, Any]:
        return {"cameras": list(self.cameras), "segment_s": self.segment_s}
