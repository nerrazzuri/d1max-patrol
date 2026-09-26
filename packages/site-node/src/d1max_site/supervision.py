"""过渡期的监护租约,站点这一侧(W00c6i,W08 决定 11)。

手机上「我在现场监护」开着时每 1 s 心跳一次;这里把每一次心跳转成一条有效期很短的
``supervise`` 命令发给狗(不是任务、不进命令账)。狗按单调钟记「监护到什么时候」,
``supervised`` 级别下没人监护就不收 goto/巡检、跑着的时候监护过期就当场中止。

这里只记「谁在监护、到什么时候」,用来判断一次心跳是不是**开始**监护(审计只记开始与结束,
续着的心跳不记 —— 不然一小时三千多行)。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from d1max_contract.supervision import SUPERVISE_TTL_DEFAULT_MS, Supervise


class SupervisionDesk:
    def __init__(self, dispatcher: Any, loop: Any, *, now_ms: Callable[[], int],
                 ttl_ms: int = SUPERVISE_TTL_DEFAULT_MS, ack_timeout_s: float = 3.0) -> None:
        self.dispatcher = dispatcher
        self.loop = loop
        self._now = now_ms
        self.ttl_ms = ttl_ms
        self.ack_timeout_s = ack_timeout_s
        self._lock = threading.Lock()
        #: robot_id → (谁, 站点这边算的到期时刻 ms)。
        self._active: dict[str, tuple[str, int]] = {}

    def renew(self, robot_id: str, operator: str) -> tuple[dict[str, Any], bool]:
        """续一次;→ (狗的回执, 这一次是不是**开始**监护)。发不出去抛 ``DispatchRefused`` 等。"""
        ack = self._send(robot_id, Supervise("renew", self.ttl_ms, operator))
        now = self._now()
        with self._lock:
            prev = self._active.get(robot_id)
            started = prev is None or prev[1] <= now or prev[0] != operator
            if ack.get("result") == "accepted":
                self._active[robot_id] = (operator, now + self.ttl_ms)
            else:
                self._active.pop(robot_id, None)
        return ack, started

    def release(self, robot_id: str, operator: str) -> dict[str, Any]:
        with self._lock:
            self._active.pop(robot_id, None)
        return self._send(robot_id, Supervise("release", self.ttl_ms, operator))

    def active(self, robot_id: str) -> str | None:
        """站点这边看到的「谁在监护」(到期了就是没有)。"""
        with self._lock:
            cur = self._active.get(robot_id)
        return cur[0] if cur is not None and cur[1] > self._now() else None

    def _send(self, robot_id: str, sup: Supervise) -> dict[str, Any]:
        ack = self.loop.call(lambda: self.dispatcher.supervise(robot_id, sup,
                                                               timeout_s=self.ack_timeout_s),
                             timeout_s=self.ack_timeout_s + 1.0)
        return ack.to_wire()
