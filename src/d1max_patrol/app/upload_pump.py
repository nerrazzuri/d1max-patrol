"""把 :class:`Uploader` 放到后台跑。

**这是狗侧唯一一个 import threading 的文件。** 全部并发风险集中在这儿,
别处一行都不许有 —— 并发的 bug 散在十个文件里就没人查得动了。

spec §4.3 那句"上传线程绝不阻塞巡检",在代码上的落实是:这个 pump 只读
``runs/`` 下的文件、只写 ``queue.jsonl``,**不碰 MissionEngine、不碰
LoopBridge、不持有任何巡检用的锁**。它碰的唯一一个共享对象是自己那个
:class:`~d1max_agent.engine.upload_queue.UploadQueue`,而那个队列从头到尾
只有这一个线程在写。

**这一层还是最后一道门(见 tick 的文档串)。** 下面那一层
(``engine/http_sink.py``)修了五轮才把七条异常泄漏堵上,最后一条是
``MemoryError`` —— 一个把 Content-Length 算错成 100 GB 的服务器就能让
``resp.read()`` 抛它。结论不是"现在安全了",是**只要 run_once() 里任何一个
异常能弄死这个线程,我们就是在赌下面没有第八条**。所以这里的职责跟 sink
那一层完全相反:sink 要窄而全(每支都是具体类型,绝不大兜底,不然会把我们
自己的 bug 吞成"网络不好");pump 要的只有一件事 —— **线程绝对不死**。

**线程调用约定:** ``tick()`` 和 ``scan()`` 走的是队列的写路径,
``UploadQueue`` 不是线程安全的 —— 起了线程之后就别再从外面打 ``tick()``。
跨线程只许读 ``stats()``,它有锁,拿到的是一份不会再变的快照。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from d1max_agent.engine.upload_queue import backoff_ms
from d1max_agent.engine.uploader import Step, Uploader

if TYPE_CHECKING:
    from d1max_agent.engine.upload_queue import UploadQueue


log = logging.getLogger(__name__)

#: 多久重扫一次 runs 目录。**未验证:这个数没在真机上量过** —— 真机上 rglob
#: 一趟要多久没量过,攒了三个月的归档目录可能有上万个文件,15 秒扫一次也许太密。
SCAN_EVERY_MS = 15_000

#: 没活干的时候睡多久。**未验证:这个数没在真机上量过** —— 空转一秒在
#: Orin NX 上占多少 CPU 没量过。
IDLE_SLEEP_S = 1.0


@dataclass
class UploadStats:
    """报给心跳和 ``/api/upload`` 看的。**只读快照,不是活对象。**"""

    backlog: int = 0
    last_step: str = ""
    last_ok_ms: int = 0
    last_error: str = ""
    sent_files: int = 0


class UploadPump:
    def __init__(
        self,
        uploader: Uploader,
        *,
        clock: Callable[[], int],
        sleep: Callable[[float], None] | None = None,
        on_done: Callable[[str], None] | None = None,
        on_scan: Callable[[], None] | None = None,
    ) -> None:
        self._up = uploader
        #: **上传状态与清盘之间的协调锁(W03)。** 谁会让一趟"从可删变成不可删":
        #: ``scan()`` 重开条目、``on_scan``/``on_done`` 撤标或打标、判读/复核改写
        #: 证据文件。清盘那条路由在"拿方案 → 重验 → rmtree"之间要握着它,不然
        #: 验证过的结论在删之前就可能过期(检查后使用竞态)。``run_once`` 那段
        #: 网络等待**不在**锁里 —— 它只会把条目往 done 推,不会制造新的 pending。
        self.coord_lock = threading.RLock()
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._backlog = 0
        self._last_step = ""
        self._last_ok_ms = 0
        self._last_error = ""
        self._sent_files = 0
        self._next_scan_ms = 0
        # 注入的 sleep 只给测试用;真跑的时候走 Event.wait,那个打得断。
        self._sleep = sleep
        # 一个文件对上了(``done``)之后喊一声,参数是那个 key。
        #
        # **为什么是注入的而不是这儿自己做。** 收到这一声之后该干的事是
        # "一趟的文件全传完了就给那个 run 目录打「可删」",而「可删」标记是
        # ``engine/retention.py`` 的事 —— 这个模块住在 ``app/`` 下,但它伺候
        # 的 ``Uploader``/``UploadQueue`` 全在 ``engine/`` 里,把 retention 的
        # 判据也搬进来就等于让上传这条线自己决定什么能删。**传成功只打标,
        # 不删**(spec §4.4),真删由水位线驱动;两件事分在两处才不会有人
        # 哪天顺手把 ``mark_uploaded`` 改成 ``rmtree``。
        self._on_done = on_done
        # 每次重扫 runs 目录之后喊一声,不带参数。
        #
        # **为什么光有 on_done 不够。** on_done 是"一个文件对上了"那一下,
        # 可它到不了"这一趟收工了"那一下:最后一个文件常常在 summary 写下
        # 之前就传完了,那之后队列里再没有东西会 done,**再也不会有第二声**。
        # 只挂 on_done 的话,这种趟永远打不上「可删」,水位线永远扫不到东西
        # 删 —— 盘照样会满。所以要有这么一个补漏的拍子:重扫盘那一拍顺带
        # 回头看看"传完了、当时还没收工"的那几趟现在够不够格。
        self._on_scan = on_scan

    # ---- 一步 ----

    @property
    def queue(self) -> UploadQueue:
        """活队列。清盘那条路由要拿它核一遍「可删」(见 server._mask_uploaded_by_queue)。"""
        return self._up.queue

    def tick(self) -> Step:
        """走一步。**所有测试打的都是这个方法** —— 一个测试都不许 sleep 等线程。

        **这里是最后一道门。** ``run_once()`` 里冒出来的任何 ``Exception``,
        不管认不认识,都在这儿被接住,翻成"这一项退避重排",循环继续,下一拍还来。

        接的是 ``Exception`` 不是 ``BaseException``:``KeyboardInterrupt`` 和
        ``SystemExit`` 是**有人在关进程**,不是"这一项失败了",它们照原样往上走,
        让这个线程干净地结束 —— 把它们当成一次上传失败去退避重试,等于按了
        Ctrl-C 还赖着不走。

        ruff 的 ``BLE`` 不许盲接、也不许拿 ``# noqa`` 压,这里不是绕过它:
        ``BLE001`` 本来就放行"把异常连 traceback 一起记进日志"的处理器 ——
        那正是这条规则想要的东西(它禁的是**默默**吞掉)。所以这儿一定要有
        ``log.exception``,而且 ``last_error`` 也得写上:出了没预料到的事,
        **值守屏上必须看得见**,而不是安静地重试到天荒地老。

        代价有两条,写在这儿免得后人当它是白捡的:

        1. **我们自己的 bug 会被翻译成"这一项传不上去"。** 一个 ``TypeError``
           和一次断网在 ``stats()`` 上长得一样(只差 ``last_error`` 里那个类型名),
           要靠日志里的 traceback 才分得清。换来的是线程不死 —— 值得,但日志
           不能不看。
        2. **出事的那一项会一直排在队里。** 这是故意的:``backlog()`` 不归零,
           §4.3 要的就是让值守屏上看得见一个不降的积压。
        """
        now = self._clock()
        try:
            return self._一步(now)
        except Exception as exc:
            # **先把这一项退避重排, 再记日志。** 反过来的话, 日志自己抛(handler 坏了)
            # 会把退避这一步跳过去 —— 那才是真正要命的: 这一项没退避, 下一拍
            # 又立刻撞上同一个异常。
            step = self._出意外了(exc, now)
            log.exception("上传走一步时抛了意外异常,这一项退避重排")
            return step

    def _一步(self, now: int) -> Step:
        扫过了 = now >= self._next_scan_ms
        if 扫过了:
            # **先把下一次扫盘的时间推出去,再真的去扫。** 扫盘自己炸了(盘掉了、
            # 权限没了)的话,不能变成每一拍都去敲一次那块不在的盘。
            self._next_scan_ms = now + SCAN_EVERY_MS
            with self.coord_lock:
                self._up.scan()
        step = self._up.run_once(now)
        with self._lock:
            self._backlog = self._up.backlog()
            self._last_step = step.action
            if step.action in {"done", "sent"}:
                self._last_ok_ms = now
                self._last_error = ""
                if step.action == "done":
                    self._sent_files += 1
            elif step.action in {"deferred", "rewound"}:
                self._last_error = step.detail
        # **两声都在锁外面、而且都在上面那段状态之后。** 顺序不是随便排的:
        # 上面那段在 ``done``/``sent`` 那一支里会把 ``last_error`` 清空,搁在
        # 它前面喊的话,回调写进去的那句话会被同一拍里一次成功的上传抹掉 ——
        # 而打不上「可删」恰恰是那种"上传一路顺风、盘却在满"的失败。
        # 两声回调各自在协调锁里:它们会打/撤「可删」标记,清盘那边在同一把锁里
        # 重验再删,所以标记不会在它验完、删之前被这里翻掉(W03)。
        if step.action == "done":
            with self.coord_lock:
                self._喊一声传完了(step.key)
        if 扫过了:
            with self.coord_lock:
                self._喊一声扫过了()
        return step

    def _喊一声传完了(self, key: str) -> None:
        """通知注入的那个回调。**在锁外面调** —— 它会去碰盘。

        回调炸了不许把这一步翻成失败:这个文件**确实**传上去了,把它改判成
        "退避重排"会让同一份字节再传一遍。但也不许静悄悄 —— 打不上「可删」
        的后果是水位线永远扫不到东西删,**真机上盘会满**,而那是最安静的一
        种失败。所以两件事都做:记 traceback,并且把话写到值守屏看得见的
        ``last_error`` 上。
        """
        if self._on_done is None:
            return
        try:
            self._on_done(key)
        except Exception as exc:
            detail = f"传完之后那一下没办成: {type(exc).__name__}: {exc}"
            with self._lock:
                self._last_error = detail
            log.exception("传完之后那一下回调抛了,这个文件照旧算传完了")

    def _喊一声扫过了(self) -> None:
        """通知那个补漏的回调。跟 :meth:`_喊一声传完了` 一样在锁外面调。

        炸了也不许把这一拍带走:重扫盘这件事本身已经办成了,上传照旧要往
        下走。但同样不许静悄悄 —— 这条路断了的后果跟那条一样是**盘会满**。
        """
        if self._on_scan is None:
            return
        try:
            self._on_scan()
        except Exception as exc:
            detail = f"重扫之后那一下没办成: {type(exc).__name__}: {exc}"
            with self._lock:
                self._last_error = detail
            log.exception("重扫之后那一下回调抛了,这一拍照旧往下走")

    def _出意外了(self, exc: BaseException, now: int) -> Step:
        """把一个没预料到的异常翻成"这一项退避重排"。

        退避用的是队列本来那套 :func:`backoff_ms`,**不新造时间常量** ——
        一个我们没见过的异常,跟一次断网该等多久没有理由不一样。
        """
        detail = f"上传线程遇到意外: {type(exc).__name__}: {exc}"
        key = self._退避排头的那一项(now)
        with self._lock:
            self._backlog = self._up.backlog()
            self._last_step = "deferred"
            self._last_error = detail
        return Step(key=key, action="deferred", detail=detail)

    def _退避排头的那一项(self, now: int) -> str:
        """把这一拍本来要传的那一条推到退避里去。

        ``run_once()`` 抛出来的时候没告诉我们它在传哪一条,所以照它自己的选法
        再算一遍:队列的排序是确定的,``pending()[0]`` 就是刚才那一条。
        队列空(比如炸在 ``scan()`` 里)就没什么可退避的,返回空 key。
        """
        ready = self._up.queue.pending(now)
        if not ready:
            return ""
        item = ready[0]
        self._up.queue.defer(item.key, next_ms=now + backoff_ms(item.attempts + 1))
        return item.key

    def stats(self) -> UploadStats:
        with self._lock:
            return UploadStats(
                backlog=self._backlog,
                last_step=self._last_step,
                last_ok_ms=self._last_ok_ms,
                last_error=self._last_error,
                sent_files=self._sent_files,
            )

    # ---- 线程 ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                step = self.tick()
            except Exception:
                # **第二道门。** ``tick()`` 自己已经兜了一层,能漏到这儿的只剩
                # "连退避重排那一步都炸了"(盘满,``queue.jsonl`` 写不进去),
                # 或者"``tick()`` 里那句 log.exception 自己又抛了"(handler 的
                # 流坏了、盘满了)。那种时候更不能让线程死 —— 盘满是会被清掉的,
                # 线程死了没人再起来。
                #
                # **状态先落, 日志后记。** 这道门下面已经没有人了 —— 它自己抛出去
                # 就是线程死。所以这里的顺序是: 先把值守屏看得见的那几个字写进去,
                # 再去记日志; 日志这条路自己炸了(handler 的流坏了、盘满了)也不许
                # 把线程带走。吞掉的代价是丢一条 traceback, 不吞的代价是上传线程
                # 死光、last_error 还是空的 —— 后者严重得多。
                with self._lock:
                    self._last_step = "deferred"
                    self._last_error = "上传线程这一拍整个失败了(详见日志)"
                with contextlib.suppress(Exception):
                    log.exception("上传线程兜底:这一拍整个失败了,歇一拍再来")
                self._歇一下()
                continue
            if step.action in {"idle", "deferred", "rewound"}:
                self._歇一下()

    def _歇一下(self) -> None:
        # **用 Event.wait,不用 time.sleep** —— stop() 要能当场打断它,
        # 否则关服务得等满一秒(重启升级链上每一拍都要还回去)。
        if self._sleep is not None:
            self._sleep(IDLE_SLEEP_S)
        else:
            self._stop.wait(IDLE_SLEEP_S)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="d1max-upload", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """**真的等它退出。** daemon=True 只保证进程不会被它吊住,不保证
        它不在关服务的过程中还在往 queue.jsonl 里写。
        """
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            # **没收回来就不许清句柄。** 清了的话下一次 start() 会起出第二条线程,
            # 两条一起往非线程安全的 queue.jsonl 里写。留着句柄, start() 那句
            # `if self._thread is not None: return` 就会拦住它。
            log.warning(
                "上传线程 %.1f 秒还没收回来,句柄留着不清 —— "
                "它多半卡在一个在途的 HTTP 请求上",
                timeout_s,
            )
            return
        self._thread = None
