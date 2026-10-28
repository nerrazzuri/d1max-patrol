"""真狗到点拍照的取图(工单 18,2026-10-28 E5:``--hal d1max`` 没接取图,拍照动作一定失败)。

跟实时画面(:func:`d1max_agent.video_push.rtsp_source`)用同一套 RTSP 地址,每次拍照起一个 ffmpeg
抽一帧 JPEG;超时、出错、给的不是 JPEG 都抛 :class:`MediaError`(引擎按这个点失败走任务包的策略)。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence

from d1max_contract.video import scrub
from d1max_patrol.backends.base import Frame, MediaError, MediaSource

#: 抽一帧最多等多久(秒):RTSP 建连 + 等一个关键帧。
GRAB_TIMEOUT_S = 8.0


class FfmpegCamera(MediaSource):
    def __init__(self, input_args: Sequence[str], *, now_ms: Callable[[], int],
                 ffmpeg: str = "ffmpeg", timeout_s: float = GRAB_TIMEOUT_S) -> None:
        self._input = list(input_args)
        self._now = now_ms
        self._ffmpeg = ffmpeg
        self._timeout = timeout_s
        #: 正在抽帧的 ffmpeg(外审 R6:关掉取图源时一并收掉)。
        self._procs: set[asyncio.subprocess.Process] = set()

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        for proc in list(self._procs):
            await _reap(proc)

    async def healthy(self) -> bool:
        return True

    async def grab(self) -> Frame:
        cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *self._input,
               "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"]
        at = self._now()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as exc:
            raise MediaError(f"起不了 ffmpeg:{exc}") from exc
        self._procs.add(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self._timeout)
        except asyncio.TimeoutError:
            await _reap(proc)
            raise MediaError(f"{self._timeout:g} 秒内没抽到画面(超时)") from None
        except BaseException:
            await _reap(proc)                        # 外审 R6:被取消也收掉,不留一个一直在拉流的
            raise
        finally:
            self._procs.discard(proc)
        if proc.returncode != 0:
            # 外审 R5:ffmpeg 报错带完整地址(含口令),先打码再截
            why = scrub(err.decode("utf-8", "replace")).strip().splitlines()[-1:] or ["?"]
            raise MediaError(f"ffmpeg 退出码 {proc.returncode}:{why[0][:200]}")
        if not out.startswith(b"\xff\xd8"):
            raise MediaError("ffmpeg 给的不是 JPEG")
        return Frame(data=out, mime="image/jpeg", captured_at_ms=at)


async def _reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.shield(proc.wait())


def rtsp_media(source: Callable[[str], list[str]], *, now_ms: Callable[[], int],
               ffmpeg: str = "ffmpeg") -> dict[str, MediaSource]:
    """实时画面的取流(``rtsp_source``)→ 每路相机一个取图源。"""
    return {cam: FfmpegCamera(source(cam), now_ms=now_ms, ffmpeg=ffmpeg)
            for cam in getattr(source, "cameras", ())}
