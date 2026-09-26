"""任务的最小形状。**步进式**:运行时每拍调 ``step(dt)``,不自己起协程 —— 这样测试里
用注入的钟就能把整条流程走完,不睡一毫秒。

生命周期:``PENDING`` →(``start()``)→ ``RUNNING`` → 终态之一。``abort(reason)`` 只是
**请求**,任务在随后的 ``step`` 里等底层停止确认再进终态(``ABORTED``,或抢占时
``PREEMPTED``);停止请求与确认分开是总设计 §2.2 的规矩。
"""

from __future__ import annotations

from d1max_contract.messages import TaskState

TERMINAL = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.ABORTED, TaskState.PREEMPTED})


class Task:
    def __init__(self, *, task_id: str, kind: str, priority: int = 0) -> None:
        self.task_id = task_id
        self.kind = kind
        self.priority = priority
        self.state = TaskState.PENDING
        #: 终态时给事件用的说明。
        self.detail: dict = {}
        #: 命令的过期时刻;在 pending 里等太久、起跑前就过期的任务不起跑(``failed``)。
        self.expires_at: int | None = None

    @property
    def done(self) -> bool:
        return self.state in TERMINAL

    @property
    def aborting(self) -> bool:
        """已经请求过中止、还没进终态(子类据实回答;默认不知道就当没有)。"""
        return False

    async def start(self) -> None:
        self.state = TaskState.RUNNING

    async def step(self, dt_s: float) -> None:
        raise NotImplementedError

    async def abort(self, reason: str) -> None:
        raise NotImplementedError

    async def on_offline(self, safe: bool) -> None:
        """断线时运行时按策略表调一次;默认什么都不做。"""

    async def on_online(self) -> None:
        """重连时调一次;默认什么都不做。"""
