"""工单 18(2026-10-28 E5:真狗上航点拍照一定失败,「没有配 'front' 这路相机的取流」):到点拍照
用实时画面同一套 RTSP 地址,ffmpeg 抽一帧 JPEG。假 ffmpeg,不连真相机。"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from d1max_agent.bridges.rtsp_media import FfmpegCamera, rtsp_media
from d1max_agent.video_push import rtsp_source
from d1max_patrol.backends.base import MediaError

JPEG = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"


def _假ffmpeg(tmp_path, body):
    p = tmp_path / "ffmpeg"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


async def test_抽一帧_给的是实时画面同一个地址_拿回JPEG(tmp_path):
    args = tmp_path / "args"
    ff = _假ffmpeg(tmp_path, f'echo "$@" > {args}; printf "\\377\\330\\377\\340fakejpeg\\377\\331"')
    cam = FfmpegCamera(["-rtsp_transport", "tcp", "-i", "rtsp://h:8554/front"], ffmpeg=ff,
                       now_ms=lambda: 123)
    f = await cam.grab()
    assert f.data == JPEG and f.mime == "image/jpeg" and f.captured_at_ms == 123
    a = args.read_text().split()
    assert a[a.index("-i") + 1] == "rtsp://h:8554/front" and "-frames:v" in a


async def test_ffmpeg报错或者给的不是JPEG_抛MediaError(tmp_path):
    cam = FfmpegCamera(["-i", "x"], ffmpeg=_假ffmpeg(tmp_path, "echo 连不上 >&2; exit 1"),
                       now_ms=lambda: 0)
    with pytest.raises(MediaError, match="连不上"):
        await cam.grab()
    cam = FfmpegCamera(["-i", "x"], ffmpeg=_假ffmpeg(tmp_path, "printf notjpeg"),
                       now_ms=lambda: 0)
    with pytest.raises(MediaError):
        await cam.grab()


async def test_相机不出画面_超时抛MediaError_进程收掉(tmp_path):
    pid = tmp_path / "pid"
    cam = FfmpegCamera(["-i", "x"], ffmpeg=_假ffmpeg(tmp_path, f"echo $$ > {pid}; exec sleep 30"),
                       now_ms=lambda: 0, timeout_s=0.5)
    t0 = time.monotonic()
    with pytest.raises(MediaError, match="超时"):
        await cam.grab()
    assert time.monotonic() - t0 < 5
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_真狗的取图_每路相机一个_跟实时画面的相机一样():
    src = rtsp_source("192.168.168.168")
    m = rtsp_media(src, now_ms=lambda: 0)
    assert set(m) == set(src.cameras) and "front" in m
    assert isinstance(m["front"], FfmpegCamera)
