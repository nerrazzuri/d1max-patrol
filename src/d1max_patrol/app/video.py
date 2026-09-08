"""RTSP -> MJPEG:把机器狗的相机流变成浏览器放得出来的东西。

浏览器不认 RTSP。前后广角推的是 ``rtsp://<机器>:8554/{front,back}``(清单
#29,``scripts/patrol_snap.sh`` 已经在用),所以 app 起一条 ffmpeg 把它转成一
串 JPEG,再包成 ``multipart/x-mixed-replace`` 直接喂 ``<img>``。

**一路相机一条 ffmpeg,字节扇给 N 个观众**(§10 待办 1)。以前是一个观众一条
ffmpeg:两个人看同一路就在解两遍同一路 1080p —— Orin 上那是实打实的 CPU,而
它买不到任何东西,两个人看的是同一帧。现在一路只有一条常驻的泵,观众只是从
它手上取"当前最新那一张"。

**为什么是子进程而不是库。** 板载那台机器上 ffmpeg 是现成的,而 Python 侧的
RTSP 客户端要么带一整个 OpenCV,要么自己实现 RTP 重组。转码这件事本来就是
ffmpeg 干得最好的,进程边界还顺手把解码崩溃挡在 app 外面。

**为什么必须 TCP。** ``-rtsp_transport tcp``。现场是机器狗自己开的 WiFi AP,
UDP 在那上面丢包丢到画面没法看 —— ``patrol_snap.sh`` 已经踩过这个坑。

**为什么有并发上限。** 扇出之后上限拦的不再是 CPU 而是**上行带宽**:热点上
每个观众都在实打实地收 JPEG。所以这个数从会话上限推,见 :data:`MAX_VIEWERS`。
上限是**每路流**的:同时看前后两个相机是两路各一个观众。

**顺带产出的是"这一路现在在线吗"变成一个可以问的东西**(:attr:`CameraFeed
.online`)。常驻的泵手上"最后一帧是什么时候进来的"是现成的,而 §5.9 那道
硬闸非它不可。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from typing import IO, Any

from d1max_patrol.backends.base import Frame, MediaError, MediaSource

#: app 认识的相机名。和 RTSP 路径最后一段一致。
CAMERAS = ("front", "back")

#: 同一路流最多几个人同时看。
#:
#: **这个数的理由在扇出之后变了。** 旧的理由是"一个观众一条 ffmpeg,一晚上
#: 能攒出几十个进程"—— 现在一路只有一条泵,观众的成本降成一个套接字加一个
#: 指向最新帧的指针。剩下的成本是**上行带宽**:热点上每个观众都在实打实地
#: 收 JPEG。所以这个数从会话上限推:§3.6 把非本机会话钉在 3 个,每人前后
#: 两路,6。
#:
#: 超了就明确拒绝,不排队 —— 排队意味着人盯着一个转圈的图标,不知道是没连上
#: 还是在等。
MAX_VIEWERS = 6

#: 多久没进新帧就算这一路不在线。画面是 5fps,一帧 200ms —— 2 秒等于连着
#: 丢了十帧。**这个数是 §5.9 那道闸的分母**:定大了,人对着一张冻住的图
#: 还能开狗;定小了,WiFi 抖一下摇杆就灰。
STALE_S = 2.0

#: 第一帧最多等多久。RTSP 握手加上等一个关键帧,现场那条 WiFi 上偶尔要好几秒
#: —— 跟 ``STILL_TIMEOUT_S`` 同一个理由,同一个数。
#:
#: **共享泵之后这个超时必须显式做。** 旧实现里"一帧都没出"是靠 ffmpeg 自己
#: 退出发现的;常驻的泵在 ffmpeg 活着但一直不吐帧时不会退,``next()`` 会永远
#: 卡住,而路由正卡在它上面等着决定发 200 还是 503。
START_TIMEOUT_S = 20.0

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


class CameraFeed:
    """一路 RTSP 流。**一条 ffmpeg,字节扇给 N 个观众**(§10 待办 1)。

    第一个观众进来时起泵,最后一个走掉时收泵。泵是一条后台线程,把 ffmpeg
    吐出来的 JPEG 切好放在 ``_latest`` 上,谁来了就拿最新那一张。

    **慢的观众丢帧,不排队。** 每个观众只看得到当前最新那一帧,跟不上就跳过
    中间的。排队会让内存跟着最慢的那个人涨,而实时画面上一帧旧数据没有任何
    价值。

    **"在线"有两半:泵活着 + 最近 ``stale_s`` 秒内真收到过帧。** 只看进程
    在不在是不够的 —— RTSP 断了 ffmpeg 未必立刻死,它可能挂在那儿重连,而那
    段时间画面是冻的。冻着的画面是 §5.9 要防的头号情况:人看着一张不动的图,
    以为前面没障碍。

    线程安全:HTTP 是一请求一线程的(``ThreadingHTTPServer``),再加上泵自己
    那条线程,所有共享状态都在 ``_ready`` 这把条件变量底下。
    """

    def __init__(self, rtsp_url: str, *, ffmpeg: str = "ffmpeg",
                 fps: float = 5.0,
                 clock: Callable[[], float] = time.monotonic,
                 stale_s: float = STALE_S,
                 start_timeout_s: float = START_TIMEOUT_S) -> None:
        self._url = rtsp_url
        self._ffmpeg = ffmpeg
        self._fps = fps
        self._clock = clock
        self._stale = stale_s
        self._start_timeout = start_timeout_s
        #: 所有共享状态都在这把锁底下。观众等新帧也等在它上面。
        self._ready = threading.Condition()
        self._viewers = 0
        self._latest: bytes | None = None
        self._seq = 0
        self._last_at: float | None = None
        self._detail = ""
        self._proc: subprocess.Popen[bytes] | None = None
        #: 起过、还没退的 ffmpeg。``处理器数`` 数的就是它 —— 见那条属性。
        self._procs: list[subprocess.Popen[bytes]] = []
        self._pump: threading.Thread | None = None
        #: 这一路一共起过几条泵。见 ``泵启动次数``。
        self._starts = 0
        #: 泵换一代就作废上一代 —— 收泵和起泵之间可能有观众来去。
        self._gen = 0
        #: ``close()`` 之后就是终态,再没有人能起泵。见 ``close()``。
        self._closed = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def viewers(self) -> int:
        """现在有几个人在看。每个 HTTP 请求一条线程,所以这个数要加锁。"""
        with self._ready:
            return self._viewers

    @property
    def 处理器数(self) -> int:
        """现在有几条 ffmpeg 在跑。**测试用的观察窗** —— 扇出这件事只有从
        这个数上看得出来,"两个人都能看到画面"在旧实现上也是绿的。

        数的是**进程**,不是 ``_pump`` 那一个句柄。句柄只装得下一条泵,
        "每个观众起一条"的写法在它上面看起来照样是 1 —— 那样这个观察窗就
        看不见它唯一该看见的那件事了。

        **只数,不改名单。** 择掉退了的那件事是 ``_prune_locked()`` 的活,
        由泵自己在收尾时调 —— 一个读属性顺手改状态的写法,在并发代码里是
        下一个人踩坑的地方。
        """
        with self._ready:
            return sum(1 for p in self._procs if p.poll() is None)

    @property
    def 泵启动次数(self) -> int:
        """这一路**一共**起过几条泵。**测试用的观察窗。**

        "一路一条 ffmpeg"这件事只有累计数看得见。``处理器数`` 看不见它:
        起泵前先把 ``_gen`` 一推,上一条泵下一圈就自己退了 —— 于是"每个观众
        起一条"在任何一个瞬间数出来也还是 1,只不过 ffmpeg 被反复重起。
        重起本身就是这个任务要消灭的东西(画面会黑一下,CPU 白烧一遍),
        所以它必须有一个数得出来的形状。
        """
        with self._ready:
            return self._starts

    @property
    def 帧号(self) -> int:
        """进来的第几帧。测试用来等下一帧,不用 sleep。"""
        with self._ready:
            return self._seq

    @property
    def 积压(self) -> int:
        """攒了几帧没发出去。**永远是 0** —— 这里只存最新那一张,
        这个属性存在的意义就是让"不排队"这条规矩有一个测得到的形状。
        """
        return 0

    @property
    def online(self) -> bool:
        """这一刻真有画面流过来吗。**§5.9 那道闸读的就是它。**"""
        with self._ready:
            return self._online_locked()

    @property
    def argv(self) -> tuple[str, ...]:
        """ffmpeg 的完整命令行。

        ``-nostdin`` 是必须的:不加的话 ffmpeg 会去抢终端的标准输入,把
        父进程的 Ctrl+C 一起吃掉。

        **换 H.264 时只动这里和 content-type**(§10 待办 2,第 8 卷)——
        codec 的选择只出现在这一处,别处不许再判一次。
        """
        return (self._ffmpeg, "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-i", self._url,
                "-f", "image2pipe", "-vcodec", "mjpeg",
                "-q:v", "6", "-r", f"{self._fps:g}", "-")

    def _online_locked(self) -> bool:
        if self._pump is None or not self._pump.is_alive():
            return False
        if self._last_at is None:
            return False
        return (self._clock() - self._last_at) <= self._stale

    def health(self) -> dict[str, Any]:
        """给人看的一句话。值守的人要能从它知道该去查什么。"""
        with self._ready:
            since = (None if self._last_at is None
                     else round(self._clock() - self._last_at, 2))
            return {"online": self._online_locked(),
                    "viewers": self._viewers,
                    "since_frame_s": since,
                    "detail": self._detail}

    def 等到第几帧(self, seq: int, *, timeout_s: float) -> bool:
        """等到帧号追上 ``seq``。**带截止时间的等,不是 sleep。**

        真后台线程上不许用固定 ``sleep(余量)`` 等 —— 余量给小了偶发红,
        给大了每跑一次都白等。条件变量一有新帧就醒。
        """
        deadline = self._clock() + timeout_s
        with self._ready:
            while self._seq < seq:
                left = deadline - self._clock()
                if left <= 0:
                    return False
                self._ready.wait(left)
            return True

    def _prune_locked(self) -> None:
        """把已经退了的 ffmpeg 从名单里择掉。**必须在持锁时调。**"""
        self._procs = [p for p in self._procs if p.poll() is None]

    def _join(self) -> None:
        with self._ready:
            if self._closed:
                raise VideoError(f"{self._url} 的画面已经关了")
            if self._viewers >= MAX_VIEWERS:
                raise VideoError(
                    f"{self._url} 已经有 {self._viewers} 个人在看了,"
                    f"最多 {MAX_VIEWERS} 个 —— 关掉一个再开")
            self._viewers += 1
            if self._pump is None or not self._pump.is_alive():
                self._gen += 1
                self._starts += 1
                self._latest = None
                self._last_at = None
                self._detail = ""
                self._pump = threading.Thread(
                    target=self._run, args=(self._gen,),
                    name=f"video-{self._url}", daemon=True)
                self._pump.start()

    def _leave(self) -> None:
        doomed = None
        with self._ready:
            # **不许减到负数。** ``close()`` 把观众数直接清零,可那批还活着的
            # 生成器随后照样会走到自己的 ``finally`` —— 无下限地减,计数就掉到
            # 负的。掉到负的之后果是实打实的:再来的人把它一个个加回 0 的路上,
            # **第一个走的人就满足了"最后一个走了"**,泵在还有人看的时候被收,
            # 那个人的画面当场黑掉。``health()["viewers"]`` 还要挂到 HTTP 上。
            self._viewers = max(0, self._viewers - 1)
            # ``close()`` 之后已经没有泵可收了,再摘一次是空转。没有可观察的
            # 后果,但把话说全了,读的人才不用自己去推"关掉之后这儿会怎样"。
            if self._viewers == 0 and not self._closed:
                doomed = self._detach_locked()
        _kill(doomed)

    def _detach_locked(self) -> subprocess.Popen[bytes] | None:
        """把泵摘下来,**把要收的那个进程交出去**。必须在持锁时调。

        ``_gen`` 一推,泵那边下一圈就自己退了 —— 状态在这里就已经收干净。

        **真正的 kill 必须在锁外做。** ``_kill()`` 里有 ``proc.wait(timeout=
        5.0)``,在锁里等就是把整把 ``_ready`` 冻住最多 5 秒;而 ``online``
        读的就是这把锁,它挂在 ``/api/state`` 和 SSE 上 —— 那是整个界面的
        心跳。现场看到的会是"app 卡死",没人会想到是某一路相机正在被 kill。
        """
        self._gen += 1
        proc, self._proc = self._proc, None
        self._pump = None
        self._last_at = None
        self._ready.notify_all()
        return proc

    def close(self) -> None:
        """关服务时收干净。**不给狗发任何指令** —— 关服务不该让它动一下。

        **关了就是终态。** 只推代次是不够的:``_join()`` 看到没泵就会照起
        不误,于是一条还没跑完的请求线程(``AppServer.stop()`` 不 join 处理
        线程)能在关服务之后起一条**没有人会去收**的 ffmpeg —— 泵线程是
        daemon,解释器退出时它没了,ffmpeg 子进程却留成孤儿。
        """
        with self._ready:
            self._closed = True
            self._viewers = 0
            doomed = self._detach_locked()
        _kill(doomed)

    def _run(self, gen: int) -> None:
        """泵:一条 ffmpeg,把切好的帧放到 ``_latest`` 上。**在自己的线程里。**"""
        errs = tempfile.TemporaryFile()
        proc: subprocess.Popen[bytes] | None = None
        try:
            try:
                proc = subprocess.Popen(      # 命令行是自己拼的,没有 shell
                    self.argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=errs)
            except OSError as exc:
                self._fail(gen, f"起不了 ffmpeg({self._ffmpeg}): {exc} —— "
                                f"装一个(apt install ffmpeg),或者用 --ffmpeg 指到它")
                return
            with self._ready:
                retired = gen != self._gen
                if not retired:
                    self._proc = proc
                    self._procs.append(proc)
            if retired:
                # 刚起来就已经作废了。收它的是下面那个 ``finally`` ——
                # **锁里不许 kill**,理由见 ``_detach_locked``。
                return
            assert proc.stdout is not None   # Popen 的类型标注给不出这个
            got = 0
            try:
                for frame in _frames(proc.stdout):
                    with self._ready:
                        if gen != self._gen:
                            break
                        got += 1
                        self._latest = frame
                        self._seq += 1
                        self._last_at = self._clock()
                        self._detail = ""
                        self._ready.notify_all()
            except (OSError, ValueError) as exc:
                # 收泵是**别的线程**干的:``_kill()`` 把 stdout 关掉的时候,
                # 这条线程正卡在 ``read1`` 上 —— 关一个正在读的管道,Windows
                # 上抛的是 ValueError。换过代就是收工,不是故障;没换代才是
                # 管道真的坏了,那句话要留给人看。
                with self._ready:
                    retired = gen != self._gen
                if not retired:
                    self._fail(gen, f"{self._url} 的管道断了: {exc}")
                return
            if got == 0:
                # ffmpeg 起来了但一帧都没出:多半是拉不到流(地址错、推流
                # 没开、网线没插)。把它自己的报错原样带给人。
                self._fail(gen, f"拉不到 {self._url}: "
                                f"{_tail(errs) or 'ffmpeg 没说原因'}")
            else:
                self._fail(gen, f"{self._url} 的画面断了: "
                                f"{_tail(errs) or 'ffmpeg 没说原因'}")
        finally:
            _kill(proc)
            with contextlib.suppress(OSError):
                errs.close()
            with self._ready:
                self._prune_locked()
                self._ready.notify_all()

    def _fail(self, gen: int, detail: str) -> None:
        with self._ready:
            if gen != self._gen:
                return
            self._detail = detail
            self._last_at = None
            self._ready.notify_all()

    def stream(self) -> Iterator[bytes]:
        """一次吐一个完整 JPEG。

        **第一次 ``next()`` 才真的进场** —— 生成器就是这样。上限和"起不起得
        来"两件事因此都落在第一帧上,调用方可以先取一帧,把失败变成一个说得
        清楚的 HTTP 状态码,再决定要不要开始往外写。

        调用方**必须** ``close()`` 它:人随时会关标签页,那时它正等在条件变量
        上,退场(可能连带收泵)的逻辑只在 ``finally`` 里。
        """
        return self._stream()

    def _stream(self) -> Iterator[bytes]:
        self._join()
        try:
            with self._ready:
                # 手上那一张就是这个观众的第一帧;泵刚换代时 ``_latest`` 是
                # 空的,那就从下一帧开始等。**帧号是全流共用、不回头的**,
                # 从 0 起算会在"关了页面又打开一次"时越过一整代,拿到一个
                # 已经作废的空帧。
                seen = self._seq - 1 if self._latest is not None else self._seq
            deadline = self._clock() + self._start_timeout
            first = True
            while True:
                with self._ready:
                    while self._seq <= seen or self._latest is None:
                        if self._detail:
                            raise VideoError(self._detail)
                        if self._pump is None or not self._pump.is_alive():
                            raise VideoError(
                                f"{self._url} 的画面没了 —— "
                                f"{self._detail or '拉流的进程已经退了'}")
                        left = deadline - self._clock() if first else None
                        if first and left <= 0:      # first 为真时 left 是数
                            raise VideoError(
                                f"等 {self._url} 的第一帧超过 "
                                f"{self._start_timeout:g}s —— "
                                f"多半是推流没开或者网断了")
                        self._ready.wait(left)
                    seen = self._seq
                    frame = self._latest
                assert frame is not None    # 上面那圈保证了 _latest 不是空的
                first = False
                yield frame
        finally:
            self._leave()


class RtspStill(MediaSource):
    """到点抓一张。巡检拍照走的就是它。

    跟 :class:`CameraFeed` 同一路流、同一串 ffmpeg 参数的前半截,区别只有一
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


__all__ = ["CAMERAS", "MAX_VIEWERS", "STALE_S", "START_TIMEOUT_S",
           "STILL_TIMEOUT_S", "CameraFeed", "RtspStill", "VideoError"]
