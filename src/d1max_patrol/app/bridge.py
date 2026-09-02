"""HTTP 线程与 asyncio 线程之间**唯一**的那道门。

引擎、后端、桥客户端全是 asyncio 的,而 HTTP 服务是 ``http.server`` ——
一个请求一条线程,同步到底。两边不能直接互相调:在 HTTP 线程里
``await`` 不了,在事件循环线程里 ``queue.get()`` 会把整个循环堵死。

所以事件循环独占一条后台线程,HTTP 线程要做任何事都从这里过。这条规矩
的价值在于它是**唯一**的一条:只要没有第二条路,就不会有人在 HTTP 线程
里直接摸引擎的内部状态,也就不会有跨线程的数据竞争要查。

``call`` 收的是**工厂**不是协程对象。这一点不是风格,是必须的:协程对象
一旦创建就绑死在创建它的线程上下文里,在 HTTP 线程里先 ``engine.start(m)``
造出来再丢过去,``asyncio`` 会拒绝,或者更糟 —— 静悄悄地跑在错的循环上。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import queue
import threading
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

from d1max_patrol.backends.base import EventEmitter

_T = TypeVar("_T")
_E = TypeVar("_E")

#: 停循环时等它自己收拾的时间。超了就不等 —— 收尾路径卡住比漏个任务更糟。
_STOP_TIMEOUT_S = 5.0


class LoopBridge:
    """在一条后台线程里跑 asyncio 事件循环,给 HTTP 线程一个同步的门。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None:
            return
        self._closed = False
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="d1max-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(_STOP_TIMEOUT_S):
            raise RuntimeError("事件循环线程起不来")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            # 循环停下来之后还挂着的任务要取消掉,否则 close() 会警告
            # "Task was destroyed but it is pending",而真正的问题(谁忘了
            # 收尾)会被淹在告警里。
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            with contextlib.suppress(Exception):
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    def stop(self, timeout_s: float = _STOP_TIMEOUT_S) -> None:
        """停循环并等线程真的退出。停过之后再 ``call`` 会被拒。"""
        self._closed = True
        loop, thread = self._loop, self._thread
        self._thread = None
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout_s)

    @property
    def running(self) -> bool:
        return (not self._closed and self._loop is not None
                and self._loop.is_running())

    def __enter__(self) -> LoopBridge:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ---------------------------------------------------------------- 调用

    def _submit(self, factory: Callable[[], Awaitable[Any]]
                ) -> concurrent.futures.Future[Any]:
        loop = self._loop
        if self._closed or loop is None or not loop.is_running():
            raise RuntimeError("事件循环没在跑")

        async def _wrap() -> Any:
            return await factory()

        return asyncio.run_coroutine_threadsafe(_wrap(), loop)

    def call(self, coro_factory: Callable[[], Awaitable[_T]],
             timeout_s: float = 30.0) -> _T:
        """在循环线程里跑一个协程,把结果(或异常)带回调用线程。

        超时抛 ``TimeoutError`` 并且**把那个协程取消掉** —— 不取消的话,
        一个卡住的请求会一直占着后端,后面每个请求都跟着卡。
        """
        future = self._submit(coro_factory)
        try:
            return future.result(timeout_s)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"等了 {timeout_s:g}s 还没结果") from exc

    def spawn(self, coro_factory: Callable[[], Awaitable[Any]]) -> None:
        """丢过去就不管了。给"点一下就走"的动作用,比如启动一趟巡检。"""
        self._submit(coro_factory)

    # ---------------------------------------------------------------- 订阅

    def subscribe(self, emitter: EventEmitter[_E]) -> Iterator[_E]:
        """给 SSE 用的同步迭代器,阻塞等下一个事件。

        中转一道 ``queue.Queue`` 是必须的:``asyncio.Queue`` 不是线程安全的,
        在 HTTP 线程里对它 ``get_nowait`` 是在玩火。订阅动作本身也得回到
        循环线程里做 —— ``EventEmitter`` 的订阅者列表同样不带锁。

        迭代结束(SSE 客户端断了)时自动退订。不退的话每断一个浏览器标签
        就留一条永远没人读的队列,事件在里面越堆越多。
        """
        outbox: queue.Queue[Any] = queue.Queue()
        stop = object()

        async def _pump() -> None:
            with emitter.subscription() as inbox:
                try:
                    while True:
                        outbox.put(await inbox.get())
                except asyncio.CancelledError:
                    raise
                finally:
                    outbox.put(stop)

        future = self._submit(_pump)

        def _iterate() -> Iterator[_E]:
            try:
                while True:
                    item = outbox.get()
                    if item is stop:
                        return
                    yield item
            finally:
                future.cancel()

        return _iterate()


__all__ = ["LoopBridge"]
