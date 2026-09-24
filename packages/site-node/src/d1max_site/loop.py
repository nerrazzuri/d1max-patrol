"""后台事件循环线程:MQTT 与派遣器跑在这里,HTTP 线程经 ``call`` 同步地进去。
与根包 ``d1max_patrol.app.bridge.LoopBridge`` 同一个思路;站点不依赖根包,所以自带一份。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


class LoopThread:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="d1max-site-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("事件循环线程起不来")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        loop.call_soon(self._ready.set)            # 真跑起来了才算就绪
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def submit(self, factory: Callable[[], Awaitable[Any]]) -> concurrent.futures.Future:
        if self.loop is None or not self.loop.is_running():
            raise RuntimeError("事件循环没在跑")

        async def _wrap() -> Any:
            return await factory()

        return asyncio.run_coroutine_threadsafe(_wrap(), self.loop)

    def call(self, factory: Callable[[], Awaitable[_T]], timeout_s: float = 30.0) -> _T:
        fut = self.submit(factory)
        try:
            return fut.result(timeout_s)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError(f"事件循环里的调用 {timeout_s:g} s 没回来") from None

    def stop(self) -> None:
        loop, thread = self.loop, self._thread
        self._thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(10)
