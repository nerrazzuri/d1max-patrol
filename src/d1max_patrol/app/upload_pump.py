"""把 :class:`Uploader` 放到后台跑。

**这是狗侧唯一一个 import threading 的文件。** 全部并发风险集中在这儿,
别处一行都不许有 —— 并发的 bug 散在十个文件里就没人查得动了。

spec §4.3 那句"上传线程绝不阻塞巡检",在代码上的落实是:这个 pump 只读
``runs/`` 下的文件、只写 ``queue.jsonl``,**不碰 MissionEngine、不碰
LoopBridge、不持有任何巡检用的锁**。它碰的唯一一个共享对象是自己那个
:class:`~d1max_patrol.engine.upload_queue.UploadQueue`,而那个队列从头到尾
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

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from d1max_patrol.engine.upload_queue import backoff_ms
from d1max_patrol.engine.uploader import Step, Uploader

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
    ) -> None:
        self._up = uploader
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

    # ---- 一步 ----

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
            log.exception("上传走一步时抛了意外异常,这一项退避重排")
            return self._出意外了(exc, now)

    def _一步(self, now: int) -> Step:
        if now >= self._next_scan_ms:
            # **先把下一次扫盘的时间推出去,再真的去扫。** 扫盘自己炸了(盘掉了、
            # 权限没了)的话,不能变成每一拍都去敲一次那块不在的盘。
            self._next_scan_ms = now + SCAN_EVERY_MS
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
        return step

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
                # "连退避重排那一步都炸了"(盘满,``queue.jsonl`` 写不进去)。
                # 那种时候更不能让线程死 —— 盘满是会被清掉的,线程死了没人再起来。
                # 同样靠 log.exception 让 BLE 放行,同样不许默默吞。
                log.exception("上传线程兜底:这一拍整个失败了,歇一拍再来")
                with self._lock:
                    self._last_step = "deferred"
                    self._last_error = "上传线程这一拍整个失败了(详见日志)"
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
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout_s)
