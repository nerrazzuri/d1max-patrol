"""RTSP -> MJPEG。

**不需要 ffmpeg,也不需要 RTSP。** 假的 "ffmpeg" 是一个往 stdout 吐 JPEG 的
Python 脚本,外面套一层同名的可执行壳(Windows 上是 ``.bat``,别处是 ``.sh``)
—— 因为 :class:`CameraFeed` 起的就是一个普通子进程,只认"能不能执行"。

这样切帧、收尾、上限、进程被杀这几件事全都测得到,而它们恰恰是这一层唯一会
出问题的地方。转码本身是 ffmpeg 的事,不归我们测。

**扇出之后这里多了一条规矩:等后台线程不许用固定的 sleep。** 泵是一条真的
操作系统线程,固定余量给小了偶发红、给大了每跑一次都白等。要等就用
``feed.等到第几帧()`` 或者 ``_until()`` —— 都是有截止期的轮询/条件变量。
"""

from __future__ import annotations

import threading
import time
import urllib.request

import pytest

from d1max_patrol.app import video as video_mod
from d1max_patrol.app.server import AppServer
from d1max_patrol.app.video import (
    CAMERAS,
    MAX_VIEWERS,
    CameraFeed,
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

#: 吐一帧,然后**活着不动**:进程在,画面冻住。
#:
#: 现有五个假件里没有这个形态 —— ``_FOREVER`` 一直吐帧,``_DIES`` 直接退。
#: 而"冻住"恰恰是 §5.9 要防的头号情况:RTSP 断了 ffmpeg 未必立刻死,它挂在
#: 那儿重连,进程还在,画面已经是一张不动的图。
#:
#: 那个 ``\x00`` 心跳不是装饰:Windows 上父进程杀的是 ``.bat`` 那层壳,真正
#: 的 python 是靠往管道里写、撞上 EPIPE 才退的。一个字节都不写的假件会变成
#: 孤儿进程,还会把 ``_kill()`` 里的 ``stdout.close()`` 堵在读上。
#: ``\x00`` 里没有 ``\xff\xd8``,切帧器会原样丢掉,所以不会多出一帧。
_FREEZES = r'''
import sys, time
sys.stdout.buffer.write(b"\xff\xd8AAA\xff\xd9")
sys.stdout.buffer.flush()
for _ in range(300):
    sys.stdout.buffer.write(b"\x00")
    sys.stdout.buffer.flush()
    time.sleep(0.05)
'''

#: 一帧都不吐,也**不退**。
#:
#: 旧实现里"一帧都没出"是靠 ffmpeg 自己退出发现的,所以这个形态以前测不着;
#: 共享泵是常驻的,没有 ``START_TIMEOUT_S`` 的话第一次 ``next()`` 会永远卡住,
#: 而路由正卡在它上面等着决定发 200 还是 503。心跳的理由同 ``_FREEZES``。
_HANGS = r'''
import sys, time
for _ in range(300):
    sys.stdout.buffer.write(b"\x00")
    sys.stdout.buffer.flush()
    time.sleep(0.05)
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


@pytest.fixture
def fake_ffmpeg_freezes(ffdir) -> str:
    return _fake(ffdir, _FREEZES, "freezes")


@pytest.fixture
def fake_ffmpeg_hangs(ffdir) -> str:
    return _fake(ffdir, _HANGS, "hangs")


def _feed(ffmpeg: str, **kw) -> CameraFeed:
    return CameraFeed("rtsp://x:8554/front", ffmpeg=ffmpeg, **kw)


# ------------------------------------------------------------------- 切帧


def test_切得出一帧一帧的jpeg(fake_ffmpeg):
    """六个字节里有两个 JPEG,切出来必须是两个,不是一坨。

    **断言的形状在扇出之后变了。** 旧实现是一个观众一条管道,每一帧都从这个
    观众手上过,所以数观众收到几帧就是数切出几帧;现在观众拿的是"当前最新
    那一张",跟不上就跳过(这正是 §"慢的观众丢帧"要的)。所以"切出几帧"
    去问泵的 ``帧号``,"切得干不干净"看观众手上那一张首尾全不全。
    """
    feed = _feed(fake_ffmpeg)
    s = feed.stream()
    try:
        first = next(s)
        assert feed.等到第几帧(2, timeout_s=5.0)
        assert feed.帧号 == 2, "两个 JPEG 被当成一坨了"
    finally:
        s.close()
        feed.close()
    assert first.startswith(b"\xff\xd8") and first.endswith(b"\xff\xd9")
    assert first in (b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BBB\xff\xd9")


def test_跨读取边界的帧也拼得回来(fake_ffmpeg_chunked):
    """管道给的块跟 JPEG 边界没有任何关系。

    块是 3 个字节一给的,跟帧边界完全错开。拼错了的话观众手上那一张就不是
    一个首尾完整的 JPEG,``帧号`` 也数不到 2。理由同上一条:观众看到的是
    最新那一张,不保证每一张都经他的手。
    """
    feed = _feed(fake_ffmpeg_chunked)
    got = []
    s = feed.stream()
    try:
        got.append(next(s))
        assert feed.等到第几帧(2, timeout_s=5.0)
        assert feed.帧号 == 2
    finally:
        s.close()
        feed.close()
    assert set(got) <= {b"\xff\xd8AAAAAAAA\xff\xd9", b"\xff\xd8BBBBBBBB\xff\xd9"}


def test_流头上的垃圾不会被当成帧的一部分(fake_ffmpeg_chunked):
    """帧头之前的字节要丢掉,不能接在第一帧前面 —— 那样解码器就废了。"""
    feed = _feed(fake_ffmpeg_chunked)
    try:
        first = next(iter(feed.stream()))
    finally:
        feed.close()        # 泵是一条真线程,靠 GC 收就是一个悬空的后台资源
    assert not first.startswith(b"garbage")


def test_ffmpeg中途死了流干净地结束(fake_ffmpeg_dies):
    """已经出过画面之后流断了,**收尾要干净**:观众数归零,泵不留。

    **共享泵之后"结束"的形状变了。** 旧实现是一个观众一条 ffmpeg,进程退了
    就是这一个观众的流到头了,悄悄结束正好;现在泵是整条流共用的,"画面断了"
    是这一路的状态 —— §5.9 那道闸(``online``)读的就是它 —— 所以断的时候要
    带着 ffmpeg 自己那句话抛出来,而不是假装看完了。
    """
    feed = _feed(fake_ffmpeg_dies)
    got = []
    with pytest.raises(VideoError, match="Connection timed out"):
        for frame in feed.stream():
            got.append(frame)
    assert got == [b"\xff\xd8AAA\xff\xd9"]
    assert feed.viewers == 0
    assert feed.处理器数 == 0
    feed.close()


def test_一帧都没出时报错里带着ffmpeg自己的话(fake_ffmpeg_silent):
    """"拉不到画面"这句话没用,人要的是**为什么**。"""
    feed = _feed(fake_ffmpeg_silent)
    try:
        with pytest.raises(VideoError) as exc:
            list(feed.stream())
    finally:
        feed.close()
    assert "Connection refused" in str(exc.value)


def test_没装ffmpeg抛的是VideoError不是FileNotFoundError():
    feed = _feed("这个程序压根不存在")
    try:
        with pytest.raises(VideoError) as exc:
            next(iter(feed.stream()))
    finally:
        feed.close()
    assert "ffmpeg" in str(exc.value)


# --------------------------------------------------------------- 观众与收尾


def test_起ffmpeg是在第一次取帧的时候(fake_ffmpeg_forever):
    """拿到生成器 != 起了进程。上限和"起不起得来"都要落在第一帧上。"""
    feed = _feed(fake_ffmpeg_forever)
    it = feed.stream()
    assert feed.viewers == 0
    assert feed.处理器数 == 0
    next(it)
    assert feed.viewers == 1
    it.close()
    feed.close()


def test_观众断了ffmpeg立刻被杀(fake_ffmpeg_forever):
    feed = _feed(fake_ffmpeg_forever)
    it = feed.stream()
    next(it)
    it.close()
    assert feed.viewers == 0
    assert feed.处理器数 == 0
    feed.close()


def test_看完了观众数也回得去(fake_ffmpeg):
    """**这一条只关心计数回不回得去。**

    假 ffmpeg 吐完两帧就退,泵跟着断,断了要抛(理由见
    ``test_ffmpeg中途死了流干净地结束``)—— 但不管是看完还是断了,
    ``viewers`` 都必须归零,不然几次之后谁都看不了。
    """
    feed = _feed(fake_ffmpeg)
    with pytest.raises(VideoError):
        list(feed.stream())
    assert feed.viewers == 0
    feed.close()


def test_超过上限的观众被明确拒绝而不是排队(fake_ffmpeg_forever):
    """排队意味着人盯着一个转圈的图标,不知道是没连上还是在等。"""
    feed = _feed(fake_ffmpeg_forever)
    live = []
    try:
        for _ in range(MAX_VIEWERS):
            it = feed.stream()
            next(it)
            live.append(it)
        assert feed.viewers == MAX_VIEWERS
        with pytest.raises(VideoError) as exc:
            next(iter(feed.stream()))
        assert str(MAX_VIEWERS) in str(exc.value)
    finally:
        for it in live:
            it.close()
        feed.close()
    assert feed.viewers == 0


def test_被拒的那个观众不会把计数搞乱(fake_ffmpeg_forever):
    """拒绝发生在加一之前 —— 加了再拒,几次之后谁都看不了。"""
    feed = _feed(fake_ffmpeg_forever)
    live = [feed.stream() for _ in range(MAX_VIEWERS)]
    try:
        for it in live:
            next(it)
        for _ in range(3):
            with pytest.raises(VideoError):
                next(iter(feed.stream()))
        assert feed.viewers == MAX_VIEWERS
    finally:
        for it in live:
            it.close()
        feed.close()


# --------------------------------------------------------- 扇出与"在不在线"


def test_两个观众只起一条ffmpeg(fake_ffmpeg_forever):
    """规格 §10 待办 1。**这是这个任务存在的理由。**

    两个人看同一路,今天是解两遍同一路 1080p。Orin 上那是实打实的 CPU,
    而它买不到任何东西 —— 两个人看的是同一帧。

    断言盯的是**进程数**,不是"看起来能同时看":后者在旧实现上也是绿的。

    **光看"此刻有几条"还不够。** 起泵前会先推一次 ``_gen``,上一条泵下一圈
    就自己退了 —— 所以"每个观众起一条"在任何一个瞬间数出来也还是 1,只不过
    ffmpeg 被反复重起(画面黑一下,CPU 白烧一遍)。累计数才看得见它。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    a = feed.stream()
    b = feed.stream()
    try:
        assert next(a).startswith(b"\xff\xd8")
        assert next(b).startswith(b"\xff\xd8")
        assert feed.处理器数 == 1
        assert feed.泵启动次数 == 1, "第二个观众又起了一条 ffmpeg"
    finally:
        a.close()
        b.close()
        feed.close()


def test_最后一个观众走了才收掉ffmpeg(fake_ffmpeg_forever):
    """**不是第一个走就收。** 收早了,还在看的那个人画面会黑。"""
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    a, b = feed.stream(), feed.stream()
    next(a)
    next(b)
    a.close()
    assert feed.处理器数 == 1
    b.close()
    assert feed.处理器数 == 0
    feed.close()


def test_没人看的时候不在线(fake_ffmpeg_forever):
    """**"在线"的意思是"这一刻真有画面流过来",不是"配了地址"。**

    这一条看着像多余,其实是 §5.9 那道闸的地基:没人看画面的时候不许开狗,
    正是那一条要的行为 —— 人不看着就不许动,这是规格的本意,不是副作用。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    assert feed.online is False
    s = feed.stream()
    try:
        next(s)
        assert feed.online is True
    finally:
        s.close()
        feed.close()
    assert feed.online is False


def test_画面冻住就算不在线(fake_ffmpeg_freezes):
    """**只看进程在不在是不够的。**

    RTSP 断了 ffmpeg 未必立刻死,它可能挂在那儿重连,而那段时间画面是冻的。
    冻着的画面是 §5.9 要防的头号情况:人看着一张不动的图,以为前面没障碍。

    时刻是注进来的 —— 不许 sleep(§8.5 第 2 条)。

    **用的是"吐一帧就不动"的假件,不是 ``fake_ffmpeg_forever``。** 后者每
    20ms 就往前推一次 ``_last_at``,把钟拨快之后随时可能被下一帧冲回在线 ——
    那是一条按运气红的测试;而且它模拟的根本不是"冻住"。
    """
    now = [1000.0]
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_freezes,
                      clock=lambda: now[0], stale_s=2.0, start_timeout_s=5.0)
    s = feed.stream()
    try:
        next(s)
        assert feed.online is True
        now[0] += 2.5           # 把钟往前拨,一帧都没再进来
        assert feed.online is False
    finally:
        s.close()
        feed.close()


def test_起不来的时候第一帧就报错_而不是干等(fake_ffmpeg_silent):
    """路由靠"先取一帧"把失败变成一个说得清楚的 503(见 ``server._video``)。
    头一旦发出去就只能是 200,那时再出错,页面上是一个不动的破图标。

    **共享泵之后这条必须显式做。** 旧实现里"一帧都没出"是靠 ffmpeg 自己
    退出发现的;共享泵是常驻的,ffmpeg 活着但一直不吐帧的话,``next()`` 会
    永远卡住 —— 所以第一帧有自己的截止时间。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_silent,
                      start_timeout_s=0.3)
    s = feed.stream()
    with pytest.raises(VideoError):
        next(s)
    s.close()
    feed.close()


def test_ffmpeg活着却一帧不吐时第一帧也会超时(fake_ffmpeg_hangs):
    """上一条里 ffmpeg 自己退了,超时那条路其实没走到。

    **这一条走的才是它。** 进程活着、管道通着、就是不出帧 —— 旧实现靠
    "ffmpeg 退出"发现失败,在这个形态上会永远卡住,而路由正卡在第一帧上
    等着决定发 200 还是 503。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_hangs,
                      start_timeout_s=0.3)
    s = feed.stream()
    try:
        with pytest.raises(VideoError, match="第一帧"):
            next(s)
    finally:
        s.close()
        feed.close()


def test_慢的观众丢帧不排队(fake_ffmpeg_forever):
    """跟不上的观众只看得到**当前最新那一帧**,中间的跳过。

    排队会让内存跟着最慢的那个人涨,而实时画面上一帧旧数据没有任何价值。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    s = feed.stream()
    try:
        next(s)
        feed.等到第几帧(feed.帧号 + 3, timeout_s=5.0)
        assert feed.积压 == 0
    finally:
        s.close()
        feed.close()


def test_超了上限明确拒绝_不排队(fake_ffmpeg_forever):
    """排队意味着人盯着一个转圈的图标,不知道是没连上还是在等。"""
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    open_ones = []
    try:
        for _ in range(MAX_VIEWERS):
            s = feed.stream()
            next(s)
            open_ones.append(s)
        extra = feed.stream()
        with pytest.raises(VideoError):
            next(extra)
        extra.close()
    finally:
        for s in open_ones:
            s.close()
        feed.close()


def test_上限至少够三个人各看两路():
    """§3.6 把非本机会话钉在 3 个。每人前后两路 = 6。

    上限的**理由**在扇出之后变了:一个观众的成本从"一条 ffmpeg"降成
    "一个套接字加一个指向最新帧的指针",剩下的成本是上行带宽。所以这个数
    是从会话上限推出来的,不是从 CPU 推出来的。
    """
    assert MAX_VIEWERS >= 6


def test_健康报告里有人看得懂的原因(fake_ffmpeg_silent):
    """``health()`` 是给人看的:值守的人要能从这一句话知道该去查什么。"""
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_silent,
                      start_timeout_s=0.3)
    s = feed.stream()
    with pytest.raises(VideoError):
        next(s)
    s.close()
    got = feed.health()
    assert got["online"] is False
    assert got["viewers"] == 0
    assert got["detail"]
    feed.close()


def test_泵收掉之后再来一个观众还能看(fake_ffmpeg_forever):
    """人关了标签页又打开一次 —— 这是最常见的一次操作。

    泵换代时上一张最新帧要作废(不然新观众看到的是几分钟前那一张),但帧号
    是不回头的,**"作废了"和"还没到新的"必须是同一件事**,否则第二次打开
    会拿到一个空帧。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    try:
        first = feed.stream()
        assert next(first).startswith(b"\xff\xd8")
        first.close()
        assert feed.处理器数 == 0
        again = feed.stream()
        try:
            assert next(again).startswith(b"\xff\xd8")
            assert feed.online is True
            assert feed.泵启动次数 == 2, "人都走光了,第二次当然要重新起"
        finally:
            again.close()
    finally:
        feed.close()


def test_关掉的时候还有人在看_观众数不会变负(fake_ffmpeg_forever):
    """``close()`` 把观众数清零,可那批生成器随后照样会走到自己的 finally。

    无下限地减,计数就掉到负的。掉到负的之后果是实打实的:再来的人一个个
    把它加回 0 的路上,**第一个走的人就会满足"最后一个走了"** —— 于是泵在
    还有人看的时候被收掉,那个人的画面当场黑掉。这正是
    ``test_最后一个观众走了才收掉ffmpeg`` 那句话要防的事,只是那一条没覆盖
    "``close()`` 的时候还有人在看"。

    ``health()["viewers"]`` 还要挂到 HTTP 上给人看,负数在那儿更没法解释。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    a, b = feed.stream(), feed.stream()
    next(a)
    next(b)
    assert feed.viewers == 2
    feed.close()
    a.close()               # 生成器的 finally 里是 _leave()
    b.close()
    assert feed.viewers == 0, "观众数减到负的了"
    assert feed.health()["viewers"] == 0


def test_关掉之后再来观众直接被拒_不会起新的ffmpeg(fake_ffmpeg_forever):
    """**``close()`` 是终态,不只是"收掉当前这条泵"。**

    只推代次的话 ``_join()`` 看到没泵就照起不误。``AppServer.stop()`` 不
    join 处理线程,所以一条还没跑完的请求线程完全可以在 ``feed.close()``
    之后走到 ``source.stream()`` 上,起一条**关服务之后才诞生的** ffmpeg。
    泵线程是 daemon,解释器退出时它没了,ffmpeg 子进程却成了孤儿 —— 没有
    任何人会去收它。
    """
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    s = feed.stream()
    next(s)
    s.close()
    feed.close()
    起过 = feed.泵启动次数
    late = feed.stream()
    try:
        with pytest.raises(VideoError):
            next(late)
    finally:
        late.close()
    assert feed.泵启动次数 == 起过, "关了之后还起了一条没人会收的 ffmpeg"
    assert feed.处理器数 == 0
    assert feed.viewers == 0


def test_收泵的时候锁不许攥着_online得立刻答得上来(fake_ffmpeg_forever, monkeypatch):
    """**收一条泵最长要 5 秒(``_kill`` 里的 ``proc.wait``),那 5 秒里锁不许被攥着。**

    下一卷要把 ``online`` 放进 ``/api/state`` 和 SSE 的同步路径上 —— SSE 是整个
    界面的心跳。ffmpeg 卡在不可中断状态时,收泵这一下要是在锁里做,界面**整体**
    停摆 5 秒;而现场看到的是"app 卡死",没有人会想到是某一路相机正在被 kill。

    **这一条是拿来钉住"kill 必须在锁外"的。** 光靠注释守不住:把 ``_kill`` 那几处
    挪回锁里,这个文件里别的用例一条都不红 —— 下一个人"顺手在锁里 kill 一下"就
    再没有东西拦得住他了。

    假的 ``_kill`` 就是"ffmpeg 卡住不肯死"。**没有固定 sleep**:门一开就往下走,
    那 3 秒只是"卡住"的上界,免得哪天写错了把整套测试挂死。
    """
    真的收 = video_mod._kill
    进了 = threading.Event()
    开门 = threading.Event()

    def 卡住的收(proc):
        进了.set()
        开门.wait(3.0)      # 修好了立刻放行;没修好,这就是界面停摆的时长
        真的收(proc)

    monkeypatch.setattr(video_mod, "_kill", 卡住的收)
    feed = CameraFeed("rtsp://x/front", ffmpeg=fake_ffmpeg_forever,
                      start_timeout_s=5.0)
    s = feed.stream()
    收 = threading.Thread(target=feed.close, name="收泵", daemon=True)
    try:
        next(s)
        收.start()
        assert 进了.wait(5.0), "close() 压根没走到收进程那一步"
        起 = time.monotonic()
        在线 = feed.online          # /api/state 和 SSE 读的就是这一句
        用了 = time.monotonic() - 起
    finally:
        开门.set()
        收.join(timeout=10.0)
        s.close()
    assert 在线 is False
    assert 用了 < 1.0, f"收泵时把锁攥了 {用了:.3f}s —— 界面这段时间整个停摆"


# ------------------------------------------------------------------ 命令行


def test_ffmpeg命令行走的是tcp():
    """UDP 在现场 WiFi 上丢包丢到没法看 —— patrol_snap.sh 已经踩过。"""
    argv = _feed("ffmpeg").argv
    assert "-rtsp_transport" in argv
    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"


def test_ffmpeg不抢标准输入():
    """不加 -nostdin,子进程会把父进程的 Ctrl+C 一起吃掉。"""
    assert "-nostdin" in _feed("ffmpeg").argv


def test_命令行里有流地址而且输出到标准输出():
    argv = _feed("ffmpeg").argv
    assert "rtsp://x:8554/front" in argv
    assert argv[-1] == "-"


# ------------------------------------------------------------------ HTTP


@pytest.fixture
def server_video(ctx, fake_ffmpeg_forever):
    """一台前相机能用、后相机没配地址的服务。"""
    feed = CameraFeed("rtsp://x:8554/front", ffmpeg=fake_ffmpeg_forever)
    ctx.video = {"front": feed}
    s = AppServer(ctx, port=0)
    s.start()
    try:
        yield s
    finally:
        s.stop()
        feed.close()        # 泵是一条真线程,不收就是一个悬空的后台资源


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
    feed = CameraFeed("rtsp://x", ffmpeg="这个程序压根不存在")
    ctx.video = {"front": feed}
    s = AppServer(ctx, port=0)
    s.start()
    try:
        err = get_err(s, "/api/video/front", 503)
        assert "ffmpeg" in err["error"]
    finally:
        s.stop()
        feed.close()


def test_拉不到流的时候先给503而不是一个空的200(ctx, fake_ffmpeg_silent):
    """响应头一旦发出去就只能是 200,那时再出错页面上只剩一个破图标。"""
    feed = CameraFeed("rtsp://x", ffmpeg=fake_ffmpeg_silent)
    ctx.video = {"front": feed}
    s = AppServer(ctx, port=0)
    s.start()
    try:
        err = get_err(s, "/api/video/front", 503)
        assert "Connection refused" in err["error"]
    finally:
        s.stop()
        feed.close()


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
    feed = ctx.video["front"]
    resp = _open(server_video, "/api/video/front")
    resp.read(64)
    assert feed.viewers == 1
    resp.close()
    assert _until(lambda: feed.viewers == 0),         "观众断了还不收泵,一晚上能攒出几十个进程"
    assert _until(lambda: feed.处理器数 == 0)


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
    assert _still("ffmpeg").url == CameraFeed("rtsp://x:8554/front").url
