"""过渡期的监护租约,站点这一侧(W00c6i,W08 决定 11)。

手机上「我在现场监护」开着时每 1 s 心跳一次(带会话号与序号,手机每次打开开关新起一个会话、序号
单调递增);这里把每一次心跳转成一条 ``supervise`` 命令发给狗(不是任务、不进命令账;命令有效期
30 s 同遥控续租,租约本身 3 s 由狗按收到时刻计)。狗按会话记「监护到什么时候」,``supervised``
级别下没有一个会话在监护就不收 goto/巡检、跑着的时候监护过期就当场中止、停车。

站点这边也把一道关(W00c6i 内审:回滚到旧版代理时狗自己不查):``supervised`` 的狗没人监护时,
手动 goto、巡检、回待命点、巡检后自动回待命点都不派(``refusal``)。

这里按会话记「谁在监护、到什么时候」:判断一次心跳是不是**开始**监护(审计只记开始与结束,续着的
心跳不记 —— 不然一小时三千多行),以及站点这一道关。
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
        #: robot_id → {会话号: (谁, 站点这边算的到期时刻 ms)}。
        self._active: dict[str, dict[str, tuple[str, int]]] = {}

    def renew(self, robot_id: str, operator: str, session: str, seq: int
              ) -> tuple[dict[str, Any], bool]:
        """续一次;→ (狗的回执, 这一次是不是这个会话**开始**监护)。狗没收下的不算开始。"""
        ack = self._send(robot_id, Supervise("renew", self.ttl_ms, operator, session, seq))
        now = self._now()
        with self._lock:
            sessions = self._active.setdefault(robot_id, {})
            prev = sessions.get(session)
            started = prev is None or prev[1] <= now
            if ack.get("result") == "accepted":
                sessions[session] = (operator, now + self.ttl_ms)
            else:
                started = False
        return ack, started

    def release(self, robot_id: str, operator: str, session: str, seq: int) -> dict[str, Any]:
        with self._lock:
            self._active.get(robot_id, {}).pop(session, None)
        return self._send(robot_id, Supervise("release", self.ttl_ms, operator, session, seq))

    def active(self, robot_id: str) -> str | None:
        """站点这边看到的「谁在监护」(没有一个会话没到期就是 ``None``)。"""
        now = self._now()
        with self._lock:
            live = [op for op, until in self._active.get(robot_id, {}).values() if until > now]
        return live[0] if live else None

    def refusal(self, robot_id: str) -> str:
        """要让这条狗自主动之前问一句:``supervised`` 的狗没人监护 → 拒的理由;否则空串。
        没登记的狗不在这儿说(交给派遣器报真正的原因)。"""
        if self.dispatcher.registry.get(robot_id) is None:
            return ""
        if self.dispatcher.autonomy(robot_id) == "autonomous" or self.active(robot_id):
            return ""
        return f"{robot_id} 要人现场监护:先在手机上打开「我在现场监护」"

    def _send(self, robot_id: str, sup: Supervise) -> dict[str, Any]:
        ack = self.loop.call(lambda: self.dispatcher.supervise(robot_id, sup,
                                                               timeout_s=self.ack_timeout_s),
                             timeout_s=self.ack_timeout_s + 1.0)
        return ack.to_wire()
