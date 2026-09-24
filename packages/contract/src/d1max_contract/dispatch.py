"""站点侧最小派遣客户端(设计 §5)。验收测试当「站点」用;W00c 的派遣器在它上面长。

它只做契约层的事:发命令、等对应 ``command_id`` 的回执、事件按 ``(boot_id, seq)`` 去重
排序、保存最新的 status/capabilities/reconcile。**不做**派遣决策(能不能派看总设计 §3.1
的条件,那是派遣器的活)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.messages import (
    Ack,
    Capabilities,
    Command,
    Event,
    Reconcile,
    Status,
)
from d1max_contract.topics import Topics
from d1max_contract.transport import Message, Transport

log = logging.getLogger(__name__)


class DispatchTimeout(TimeoutError):
    """规定时间内没等到回执。命令可能已到、可能没到 —— 重发同一个 ``command_id`` 是安全的。"""


def _decode(payload: bytes, what: str) -> dict[str, Any] | None:
    try:
        d = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("%s 报文解析不了(JSON): %s", what, exc)
        return None
    if not isinstance(d, dict):
        log.warning("%s 报文顶层不是对象", what)
        return None
    return d


class DispatchClient:
    def __init__(self, transport: Transport, topics: Topics, *,
                 now_ms: Callable[[], int]) -> None:
        self._t = transport
        self.topics = topics
        self._now = now_ms
        self._waiters: dict[str, asyncio.Future[Ack]] = {}
        self._seen_events: set[tuple[str, int]] = set()
        self._event_cbs: list[Callable[[Event], None]] = []
        self._reconcile_cbs: list[Callable[[Reconcile], None]] = []
        self._status_cbs: list[Callable[[Status], None]] = []
        self.status: Status | None = None
        self.capabilities: Capabilities | None = None
        self.reconcile: Reconcile | None = None
        self.acks: list[Ack] = []

    async def start(self) -> None:
        await self._t.connect()
        await self._t.subscribe(self.topics.ack, self._on_ack)
        await self._t.subscribe(self.topics.event, self._on_event)
        await self._t.subscribe(self.topics.status, self._on_status)
        await self._t.subscribe(self.topics.capabilities, self._on_caps)
        await self._t.subscribe(self.topics.reconcile, self._on_reconcile)

    async def close(self) -> None:
        await self._t.close()

    # ------------------------------------------------------------ 发命令

    def new_command(self, kind: str, payload: dict[str, Any], *, ttl_ms: int,
                    control_epoch: int, task_id: str | None = None, priority: int = 0,
                    precondition=None) -> Command:
        now = self._now()
        return Command(command_id=f"cmd-{uuid.uuid4().hex[:12]}",
                       task_id=task_id or f"task-{uuid.uuid4().hex[:12]}", kind=kind,
                       issued_at=now, expires_at=now + ttl_ms, control_epoch=control_epoch,
                       payload=payload, priority=priority, precondition=precondition)

    async def send(self, cmd: Command, *, timeout_s: float) -> Ack:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Ack] = loop.create_future()
        self._waiters[cmd.command_id] = fut
        try:
            await self._t.publish(self.topics.cmd, json.dumps(cmd.to_wire()).encode(), qos=1)
            try:
                return await asyncio.wait_for(fut, timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise DispatchTimeout(
                    f"{cmd.kind} {cmd.command_id} 等回执超过 {timeout_s:g}s") from exc
        finally:
            self._waiters.pop(cmd.command_id, None)

    # ------------------------------------------------------------ 订阅

    def on_event(self, cb: Callable[[Event], None]) -> None:
        self._event_cbs.append(cb)

    def on_reconcile(self, cb: Callable[[Reconcile], None]) -> None:
        self._reconcile_cbs.append(cb)

    def on_status(self, cb: Callable[[Status], None]) -> None:
        self._status_cbs.append(cb)

    async def _on_ack(self, m: Message) -> None:
        d = _decode(m.payload, "ack")
        if d is None:
            return
        try:
            ack = Ack.from_wire(d)
        except ContractError as exc:
            log.warning("ack 不合契约,丢弃: %s", exc)
            return
        self.acks.append(ack)
        fut = self._waiters.get(ack.command_id)
        if fut is not None and not fut.done():
            fut.set_result(ack)

    async def _on_event(self, m: Message) -> None:
        d = _decode(m.payload, "event")
        if d is None:
            return
        try:
            ev = Event.from_wire(d)
        except ContractError as exc:
            log.warning("event 不合契约,丢弃: %s", exc)
            return
        key = (ev.boot_id, ev.seq)
        if key in self._seen_events:
            return                      # QoS 1 的重复投递,正常现象
        self._seen_events.add(key)
        for cb in list(self._event_cbs):
            cb(ev)

    async def _on_status(self, m: Message) -> None:
        d = _decode(m.payload, "status")
        if d is None:
            return
        try:
            self.status = Status.from_wire(d)
        except ContractError as exc:
            log.warning("status 不合契约,丢弃: %s", exc)
            return
        for cb in list(self._status_cbs):
            cb(self.status)

    async def _on_caps(self, m: Message) -> None:
        d = _decode(m.payload, "capabilities")
        if d is None:
            return
        try:
            self.capabilities = Capabilities.from_wire(d)
        except ContractError as exc:
            log.warning("capabilities 不合契约,丢弃: %s", exc)

    async def _on_reconcile(self, m: Message) -> None:
        d = _decode(m.payload, "reconcile")
        if d is None:
            return
        try:
            self.reconcile = Reconcile.from_wire(d)
        except ContractError as exc:
            log.warning("reconcile 不合契约,丢弃: %s", exc)
            return
        for cb in list(self._reconcile_cbs):
            cb(self.reconcile)
