"""RTSP -> MJPEG。

**不需要 ffmpeg,也不需要 RTSP。** 假的 "ffmpeg" 是一个往 stdout 吐 JPEG 的
Python 脚本,外面套一层同名的可执行壳(Windows 上是 ``.bat``,别处是 ``.sh``)
—— 因为 :class:`MjpegSource` 起的就是一个普通子进程,只认"能不能执行"。

这样切帧、收尾、上限、进程被杀这几件事全都测得到,而它们恰恰是这一层唯一会
出问题的地方。转码本身是 ffmpeg 的事,不归我们测。
"""

from __future__ import annotations

import itertools
import os
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from d1max_patrol.app.server import AppServer
from d1max_patrol.app.video import (
    CAMERAS,
    MAX_VIEWERS,
    MjpegSource,
    VideoError,
)
from tests.app.conftest import get_err, status, url

# --------------------------------------------------------------- 假的 ffmpeg

#: 一口气吐两个完整 JPEG,然后退出。
_TWO_FRAMES = r'''
import sys
sys.stdout.buffer.write(b"\xff\xd8AAA\xff\xd9\xff\xd8BBB\xff\xd9")
sys.stdout.buffer.flush()
'''

#: 同样两个 JPEG,但**按 3 个字节一块**吐,块边界跟帧边界完全错开。
#: 前面还先吐一段垃圾 —— 真的 ffmpeg 偶尔也会在流头上带点别的。
_CHUNKED = r'''
import sys, time
data = b"garbage\xff\xd8AAAAAAAA\xff\xd9\xff\xd8BBBBBBBB\xff\xd9"
for i in range(0, len(data), 3):
    sys.stdout.buffer.write(data[i:i + 3])
    sys.stdout.buffer.flush()
    time.sleep(0.002)
'''

#: 吐一帧,然后**非零退出**。现场就是这样:流断了 ffmpeg 自己就死。
_DIES = r'''
import sys
sys.stdout.buffer.write(b"\xff\xd8AAA\xff\xd9")
sys.stdout.buffer.flush()
sys.stderr.write("Connection timed out\n")
sys.exit(1)
'''

#: 一帧都不吐,只往 stderr 写一句。拉不到流就是这个样子。
_NO_FRAMES = r'''
import sys
sys.stderr.write("rtsp://x: Connection refused\n")
sys.exit(1)
'''

#: 一直吐,直到被杀。用来测"观众断了 ffmpeg 立刻被杀"。
_FOREVER = r'''
import sys, time
while True:
    sys.stdout.buffer.write(b"\xff\xd8AAA\xff\xd9")
    sys.stdout.buffer.flush()
    time.sleep(0.02)
'''


@pytest.fixture
def ffdir(tmp_path_factory) -> Path:
    """假 ffmpeg 的落脚处。

    **不能用 tmp_path**:它的名字里带测试函数名,而这里的测试名是中文。
    Windows 上 cmd 按当前代码页(GBK)读 ``.bat``,路径里的中文到子进程手上
    就成了乱码,脚本直接打不开。``mktemp`` 给的目录名是纯 ASCII。
    """
    return tmp_path_factory.mktemp("ff")


def _fake(where: Path, body: str, tag: str) -> str:
    """把一段 Python 包成一个能直接执行的"ffmpeg"。

    包一层壳而不是直接把 ``.py`` 交给 ``Popen``:``.py`` 能不能直接执行取决于
    系统上的文件关联,而 ``.bat`` / 带 shebang 的 ``.sh`` 到哪儿都能跑。
    """
    script = where / f"{tag}.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        shim = where / f"{tag}.bat"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                        encoding="utf-8")
    else:
        shim = where / f"{tag}.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                        encoding="utf-8")
        shim.chmod(0o755)
    return str(shim)


@pytest.fixture
def fake_ffmpeg(ffdir) -> str:
    return _fake(ffdir, _TWO_FRAMES, "two")


@pytest.fixture
def fake_ffmpeg_chunked(ffdir) -> str:
    return _fake(ffdir, _CHUNKED, "chunked")


@pytest.fixture
def fake_ffmpeg_dies(ffdir) -> str:
    return _fake(ffdir, _DIES, "dies")


@pytest.fixture
def fake_ffmpeg_silent(ffdir) -> str:
    return _fake(ffdir, _NO_FRAMES, "silent")


@pytest.fixture
def fake_ffmpeg_forever(ffdir) -> str:
    return _fake(ffdir, _FOREVER, "forever")


def _src(ffmpeg: str) -> MjpegSource:
    return MjpegSource("rtsp://x:8554/front", ffmpeg=ffmpeg)


# ------------------------------------------------------------------- 切帧


def test_切得出一帧一帧的jpeg(fake_ffmpeg):
    frames = list(itertools.islice(_src(fake_ffmpeg).stream(), 2))
    assert len(frames) == 2
    assert all(f.startswith(b"\xff\xd8") and f.endswith(b"\xff\xd9")
               for f in frames)
    assert frames[0] == b"\xff\xd8AAA\xff\xd9"


def test_跨读取边界的帧也拼得回来(fake_ffmpeg_chunked):
    """管道给的块跟 JPEG 边界没有任何关系。"""
    frames = list(_src(fake_ffmpeg_chunked).stream())
    assert frames == [b"\xff\xd8AAAAAAAA\xff\xd9", b"\xff\xd8BBBBBBBB\xff\xd9"]


def test_流头上的垃圾不会被当成帧的一部分(fake_ffmpeg_chunked):
    """帧头之前的字节要丢掉,不能接在第一帧前面 —— 那样解码器就废了。"""
    first = next(iter(_src(fake_ffmpeg_chunked).stream()))
    assert not first.startswith(b"garbage")


def test_ffmpeg中途死了流干净地结束(fake_ffmpeg_dies):
    """已经出过画面就不算失败:流断了本来就该停,不该抛。"""
    src = _src(fake_ffmpeg_dies)
    assert list(src.stream()) == [b"\xff\xd8AAA\xff\xd9"]
    assert src.viewers == 0


def test_一帧都没出时报错里带着ffmpeg自己的话(fake_ffmpeg_silent):
    """"拉不到画面"这句话没用,人要的是**为什么**。"""
    with pytest.raises(VideoError) as exc:
        list(_src(fake_ffmpeg_silent).stream())
    assert "Connection refused" in str(exc.value)


def test_没装ffmpeg抛的是VideoError不是FileNotFoundError():
    with pytest.raises(VideoError) as exc:
        next(iter(_src("这个程序压根不存在").stream()))
    assert "ffmpeg" in str(exc.value)


# --------------------------------------------------------------- 观众与收尾


def test_起ffmpeg是在第一次取帧的时候(fake_ffmpeg_forever):
    """拿到生成器 != 起了进程。上限和"起不起得来"都要落在第一帧上。"""
    src = _src(fake_ffmpeg_forever)
    it = src.stream()
    assert src.viewers == 0
    next(it)
    assert src.viewers == 1
    it.close()


def test_观众断了ffmpeg立刻被杀(fake_ffmpeg_forever):
    src = _src(fake_ffmpeg_forever)
    it = src.stream()
    next(it)
    it.close()
    assert src.viewers == 0


def test_看完了观众数也回得去(fake_ffmpeg):
    src = _src(fake_ffmpeg)
    list(src.stream())
    assert src.viewers == 0


def test_超过上限的观众被明确拒绝而不是排队(fake_ffmpeg_forever):
    """排队意味着人盯着一个转圈的图标,不知道是没连上还是在等。"""
    src = _src(fake_ffmpeg_forever)
    live = []
    try:
        for _ in range(MAX_VIEWERS):
            it = src.stream()
            next(it)
            live.append(it)
        assert src.viewers == MAX_VIEWERS
        with pytest.raises(VideoError) as exc:
            next(iter(src.stream()))
        assert str(MAX_VIEWERS) in str(exc.value)
    finally:
        for it in live:
            it.close()
    assert src.viewers == 0


def test_被拒的那个观众不会把计数搞乱(fake_ffmpeg_forever):
    """拒绝发生在加一之前 —— 加了再拒,几次之后谁都看不了。"""
    src = _src(fake_ffmpeg_forever)
    live = [src.stream() for _ in range(MAX_VIEWERS)]
    try:
        for it in live:
            next(it)
        for _ in range(3):
            with pytest.raises(VideoError):
                next(iter(src.stream()))
        assert src.viewers == MAX_VIEWERS
    finally:
        for it in live:
            it.close()


# ------------------------------------------------------------------ 命令行


def test_ffmpeg命令行走的是tcp():
    """UDP 在现场 WiFi 上丢包丢到没法看 —— patrol_snap.sh 已经踩过。"""
    argv = _src("ffmpeg").argv
    assert "-rtsp_transport" in argv
    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"


def test_ffmpeg不抢标准输入():
    """不加 -nostdin,子进程会把父进程的 Ctrl+C 一起吃掉。"""
    assert "-nostdin" in _src("ffmpeg").argv


def test_命令行里有流地址而且输出到标准输出():
    argv = _src("ffmpeg").argv
    assert "rtsp://x:8554/front" in argv
    assert argv[-1] == "-"


# ------------------------------------------------------------------ HTTP


@pytest.fixture
def server_video(ctx, fake_ffmpeg_forever):
    """一台前相机能用、后相机没配地址的服务。"""
    ctx.video = {"front": MjpegSource("rtsp://x:8554/front",
                                      ffmpeg=fake_ffmpeg_forever)}
    s = AppServer(ctx, port=0)
    s.start()
    yield s
    s.stop()


def _open(server, path: str):
    """开一条长连,把响应对象交出去 —— 调用方自己读、自己关。

    conftest 的 ``request`` 会把响应体一次读完,而 MJPEG 是永远读不完的。
    """
    return urllib.request.urlopen(url(server, path), timeout=5.0)


def _until(pred, timeout: float = 5.0) -> bool:
    """等一个条件成立。服务端那条线程要先撞上写失败才会走到收尾。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_不认识的相机名给404(server):
    assert status(server, "/api/video/side") == 404


def test_相机没配地址给的是一句人话(server):
    err = get_err(server, "/api/video/front", 503)
    assert "front" in err["error"]


def test_没装ffmpeg给的是一句人话(ctx):
    """页面上要显示"拉不到前相机:ffmpeg 没装",不是一个破图标。"""
    ctx.video = {"front": MjpegSource("rtsp://x", ffmpeg="这个程序压根不存在")}
    s = AppServer(ctx, port=0)
    s.start()
    try:
        err = get_err(s, "/api/video/front", 503)
        assert "ffmpeg" in err["error"]
    finally:
        s.stop()


def test_拉不到流的时候先给503而不是一个空的200(ctx, fake_ffmpeg_silent):
    """响应头一旦发出去就只能是 200,那时再出错页面上只剩一个破图标。"""
    ctx.video = {"front": MjpegSource("rtsp://x", ffmpeg=fake_ffmpeg_silent)}
    s = AppServer(ctx, port=0)
    s.start()
    try:
        err = get_err(s, "/api/video/front", 503)
        assert "Connection refused" in err["error"]
    finally:
        s.stop()


def test_画面是multipart而且切得出jpeg(server_video):
    resp = _open(server_video, "/api/video/front")
    try:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith(
            "multipart/x-mixed-replace")
        body = resp.read(300)
    finally:
        resp.close()
    assert b"Content-Type: image/jpeg" in body
    assert b"\xff\xd8AAA\xff\xd9" in body


def test_观众超上限给的是明确拒绝不是排队(server_video):
    live = []
    try:
        for _ in range(MAX_VIEWERS):
            resp = _open(server_video, "/api/video/front")
            resp.read(64)       # 逼服务端真的把第一帧写出来
            live.append(resp)
        assert status(server_video, "/api/video/front") == 503
    finally:
        for resp in live:
            resp.close()


def test_观众关了页面ffmpeg就没了(server_video, ctx):
    src = ctx.video["front"]
    resp = _open(server_video, "/api/video/front")
    resp.read(64)
    assert src.viewers == 1
    resp.close()
    assert _until(lambda: src.viewers == 0),         "观众断了还不杀 ffmpeg,一晚上能攒出几十个进程"


def test_相机名就是CAMERAS里那两个():
    assert CAMERAS == ("front", "back")
