"""``scripts/fake-camera.py`` —— 彩排用的假相机。

它只在出发前的彩排里用得上,但**恰恰因此更要测**:彩排的全部意义是"现场之前
先把整条路走一遍",而如果假相机自己不对,彩排出来的绿是假的 —— 到现场才发现
拍照那一段从来没真跑通过,那时候没有第二次机会。

盯三件事:吐出来的是**真 JPEG**(报告要 base64 内嵌,浏览器解不开就白测了)、
前后两路给的图**不一样**(页面上要能分清)、以及它认得 ``app.video`` 真正发出
去的那两种命令行。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from d1max_patrol.app.video import CameraFeed, RtspStill

#: JPEG 起始标记。video.py 里是私有的,测试自己写一份不算重复。
SOI = b"\xff\xd8"

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fake-camera.py"


def _run(*args: str, timeout: float = 20.0) -> bytes:
    """直接用当前解释器跑它。绕开"``.py`` 能不能双击执行"这个系统差异。"""
    done = subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, timeout=timeout, check=True)
    return done.stdout


def _shim(where: Path) -> str:
    """包一个壳,让它能当 ``--ffmpeg`` 的值使 —— 那个位置只收可执行文件名。"""
    if os.name == "nt":
        shim = where / "cam.bat"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{SCRIPT}" %*\r\n',
                        encoding="utf-8")
    else:
        shim = where / "cam.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" "$@"\n',
                        encoding="utf-8")
        shim.chmod(0o755)
    return str(shim)


def test_脚本在仓库里而且带了shebang():
    """Ubuntu 上是 ``chmod +x`` 之后直接当 ffmpeg 使的。"""
    assert SCRIPT.exists()
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")


# --------------------------------------------------------------- 吐的是真图


def test_拿一帧就退给的是完整jpeg():
    out = _run("-i", "rtsp://x:8554/front", "-frames:v", "1", "-")
    assert out.startswith(b"\xff\xd8") and out.endswith(b"\xff\xd9")


def test_只吐一帧不多吐():
    """多吐一帧,拍照那边会把两张图当成一张存下来。"""
    out = _run("-i", "rtsp://x:8554/front", "-frames:v", "1", "-")
    assert out.count(SOI) == 1


def test_不是几个占位字节而是解得开的jpeg():
    """报告要把它 base64 内嵌进 HTML。占位字节在测试里能过,在浏览器里是叉。"""
    out = _run("-i", "rtsp://x:8554/front", "-frames:v", "1", "-")
    # JFIF/APP0 段在 SOI 之后。不引第三方解码器,只认这个结构性标记。
    assert b"JFIF" in out[:32]
    assert len(out) > 500, "真图不会只有几十个字节"


def test_前后两路给的图不一样():
    """页面上两块画面长得一模一样的话,接错线也看不出来。"""
    front = _run("-i", "rtsp://x:8554/front", "-frames:v", "1", "-")
    back = _run("-i", "rtsp://x:8554/back", "-frames:v", "1", "-")
    assert front != back


def test_认不出是哪一路就给前路():
    """认不出时得有个确定的回答,不能空手而归。"""
    out = _run("-i", "rtsp://x:8554/whatever", "-frames:v", "1", "-")
    assert out.startswith(b"\xff\xd8")


# ------------------------------------------------------------------ 连续那路


def test_不带frames就一直吐(tmp_path):
    """实时画面那路要的是一串首尾相接的 JPEG,不是一张。"""
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "-i", "rtsp://x:8554/front",
         "-r", "20", "-"], stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        got = proc.stdout.read(6000)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert got.count(SOI) >= 2, "只给一张的话画面是死的"


# ------------------------------------------------ 接到真的取图源上跑一遍


async def test_拍照那条路真能用它取到图(tmp_path):
    """这条才是彩排真正依赖的:``RtspStill`` 原样跑,只把 ffmpeg 换成它。"""
    still = RtspStill("rtsp://127.0.0.1:8554/front", ffmpeg=_shim(tmp_path))
    frame = await still.grab()
    assert frame.data.startswith(b"\xff\xd8") and frame.data.endswith(b"\xff\xd9")
    assert frame.mime == "image/jpeg"


async def test_实时画面那条路也真能用它(tmp_path):
    feed = CameraFeed("rtsp://127.0.0.1:8554/back", ffmpeg=_shim(tmp_path),
                      fps=20.0)
    stream = feed.stream()
    try:
        first = next(stream)
        second = next(stream)
    finally:
        stream.close()
        feed.close()
    assert first.startswith(b"\xff\xd8") and second.startswith(b"\xff\xd8")


@pytest.mark.skipif(os.name == "nt", reason="Windows 上没有可执行位这回事")
def test_可执行位在git里存着():
    """手册让人 ``--ffmpeg scripts/fake-camera.py`` 直接使,少了这一位就跑不了。"""
    assert os.access(SCRIPT, os.X_OK), "git 里的模式应该是 100755"
