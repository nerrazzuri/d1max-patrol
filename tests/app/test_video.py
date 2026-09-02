"""RTSP -> MJPEG。

**不需要 ffmpeg,也不需要 RTSP。** 假的 "ffmpeg" 是一个往 stdout 吐 JPEG 的
Python 脚本,外面套一层同名的可执行壳(Windows 上是 ``.bat``,别处是 ``.sh``)
—— 因为 :class:`MjpegSource` 起的就是一个普通子进程,只认"能不能执行"。

这样切帧、收尾、上限、进程被杀这几件事全都测得到,而它们恰恰是这一层唯一会
出问题的地方。转码本身是 ffmpeg 的事,不归我们测。
"""

from __future__ import annotations

import itertools
import time
import urllib.request

import pytest

from d1max_patrol.app.server import AppServer
from d1max_patrol.app.video import (
    CAMERAS,
    MAX_VIEWERS,
    MjpegSource,
    RtspStill,
    VideoError,
)
from d1max_patrol.backends.base import MediaError
from tests.app.conftest import _fake, get_err, status, url

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


# --------------------------------------------------------------- 拍照抓帧


def _still(ffmpeg: str) -> RtspStill:
    return RtspStill("rtsp://x:8554/front", ffmpeg=ffmpeg, timeout_s=20.0)


async def test_抓得出一张jpeg(fake_ffmpeg):
    frame = await _still(fake_ffmpeg).grab()
    assert frame.data.startswith(b"\xff\xd8") and frame.data.endswith(b"\xff\xd9")
    assert frame.mime == "image/jpeg"
    assert frame.captured_at_ms > 0


async def test_只要第一张不把整段管道当成一张(fake_ffmpeg):
    """假 ffmpeg 一口气吐了两张。拿两张拼成的字节串存下来就是一个坏文件。"""
    assert await _still(fake_ffmpeg).grab() and True
    assert (await _still(fake_ffmpeg).grab()).data == b"\xff\xd8AAA\xff\xd9"


async def test_流头上的垃圾不会混进照片里(fake_ffmpeg_chunked):
    data = (await _still(fake_ffmpeg_chunked).grab()).data
    assert data == b"\xff\xd8AAAAAAAA\xff\xd9"
    assert b"garbage" not in data


async def test_一帧都抓不到时把ffmpeg的原话带出来(fake_ffmpeg_silent):
    with pytest.raises(MediaError, match="Connection refused"):
        await _still(fake_ffmpeg_silent).grab()


async def test_拍照那条路上没装ffmpeg也给一句人话(ffdir):
    with pytest.raises(MediaError, match="ffmpeg"):
        await _still(str(ffdir / "根本没有这个")).grab()


async def test_抓不到时healthy是假的不是抛(fake_ffmpeg_silent):
    """预检要的是一个能写进检查表的布尔值,不是一个异常。"""
    assert await _still(fake_ffmpeg_silent).healthy() is False


async def test_抓得到时healthy是真的(fake_ffmpeg):
    assert await _still(fake_ffmpeg).healthy() is True


def test_拍照走的也是tcp():
    """UDP 在现场那条 WiFi 上丢包 —— 实时画面和拍照是同一条链路,别只改一边。"""
    argv = _still("ffmpeg").argv
    assert "-rtsp_transport" in argv
    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"


def test_拍照只要一帧就退():
    """不加 -frames:v 1 的话 ffmpeg 会一直拉流,而这里没有人来关它。"""
    argv = _still("ffmpeg").argv
    assert "-frames:v" in argv
    assert argv[argv.index("-frames:v") + 1] == "1"


def test_拍照和实时画面拉的是同一个地址():
    """两边地址各拼各的,迟早有一天页面上看着是前广角、存下来的是后广角。"""
    assert _still("ffmpeg").url == MjpegSource("rtsp://x:8554/front").url
