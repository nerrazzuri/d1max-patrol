"""进程内的 broker 与客户端:验收流程跑在它上面(决定 2)。

**它是我们自己对 MQTT 语义的模型,不是 MQTT。** 学的只有用到的那几件:按主题订阅
(``+``/``#``)、QoS 1 至少一次、retained、LWT、``clean_session=False`` 式的离线补投、
客户端断线期间发布入队重连后按序补发(paho 的行为)。多出来的是测试注入口:
``disconnect(client_id)``、``duplicate_next(n)``、``drain()``。

投递是异步任务(``create_task``),一个 handler 炸了只记录、不影响别人;``drain()``
等到没有任何挂起投递为止(handler 里再发布出来的也算)。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from d1max_contract.transport import ConnectionCallback, Handler, Message, topic_matches

log = logging.getLogger(__name__)


@dataclass
class _Sub:
    topic_filter: str
    qos: int
    handler: Handler


@dataclass
class _ClientState:
    transport: MemoryTransport
    subs: list[_Sub] = field(default_factory=list)
    #: 断线期间发给它的 QoS 1 报文,重连后补投(clean_session=False)。
    inbox: list[Message] = field(default_factory=list)


class MemoryBroker:
    def __init__(self) -> None:
        self._retained: dict[str, Message] = {}
        self._clients: dict[str, _ClientState] = {}
        self._pending: set[asyncio.Task[None]] = set()
        self._dup_budget = 0

    # ------------------------------------------------------------ 注入口

    def duplicate_next(self, n: int) -> None:
        """接下来 n 条 QoS 1 投递每条投两次 —— 模拟至少一次语义下的重复。"""
        self._dup_budget += n

    def disconnect(self, client_id: str) -> None:
        """模拟链路非正常断开:客户端标为断线,LWT 代发,订阅与离线收件箱保留。"""
        state = self._clients.get(client_id)
        if state is None or not state.transport.connected:
            return
        state.transport._mark(False)
        will = state.transport._will
        if will is not None:
            self._publish(will, from_client=None)

    async def drain(self) -> None:
        """等到没有挂起的投递。handler 里再发布出来的新投递也一起等完。"""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        await asyncio.sleep(0)

    # ------------------------------------------------------------ 客户端调用

    def _attach(self, transport: MemoryTransport) -> None:
        state = self._clients.get(transport.client_id)
        if state is None:
            state = _ClientState(transport)
            self._clients[transport.client_id] = state
        else:
            state.transport = transport
        transport._mark(True)
        # 先补投断线期间攒下的(按序),再把它自己攒下的发出去。
        inbox, state.inbox = state.inbox, []
        for msg in inbox:
            self._deliver(state, msg)
        outbox, transport._outbox = list(transport._outbox), []
        for msg in outbox:
            self._publish(msg, from_client=transport.client_id)

    def _detach(self, transport: MemoryTransport) -> None:
        """正常关闭:不发 LWT,订阅也一并清掉。"""
        transport._mark(False)
        self._clients.pop(transport.client_id, None)

    def _subscribe(self, client_id: str, topic_filter: str, qos: int, handler: Handler) -> None:
        state = self._clients[client_id]
        state.subs.append(_Sub(topic_filter, qos, handler))
        for topic, msg in self._retained.items():
            if topic_matches(topic_filter, topic):
                self._deliver(state, Message(msg.topic, msg.payload, min(msg.qos, qos), True),
                              sub=state.subs[-1])

    def _publish(self, msg: Message, from_client: str | None) -> None:
        if msg.retain:
            if msg.payload:
                self._retained[msg.topic] = msg
            else:
                self._retained.pop(msg.topic, None)
        live = Message(msg.topic, msg.payload, msg.qos, False)
        for state in self._clients.values():
            for sub in state.subs:
                if not topic_matches(sub.topic_filter, msg.topic):
                    continue
                eff = Message(live.topic, live.payload, min(live.qos, sub.qos), False)
                if not state.transport.connected:
                    if eff.qos >= 1:
                        state.inbox.append(eff)
                    continue
                self._deliver(state, eff, sub=sub)
                if eff.qos >= 1 and self._dup_budget > 0:
                    self._dup_budget -= 1
                    self._deliver(state, eff, sub=sub)

    def _deliver(self, state: _ClientState, msg: Message, sub: _Sub | None = None) -> None:
        targets = [sub] if sub is not None else [
            s for s in state.subs if topic_matches(s.topic_filter, msg.topic)]
        for s in targets:
            task = asyncio.get_running_loop().create_task(self._run(s.handler, msg))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    @staticmethod
    async def _run(handler: Handler, msg: Message) -> None:
        try:
            await handler(msg)
        except Exception:
            log.exception("订阅 handler 处理 %s 时炸了,已忽略这一条", msg.topic)


class MemoryTransport:
    """MemoryBroker 的客户端。``connect()`` 幂等;断线期间 ``publish`` 的 QoS 1 报文入队。"""

    def __init__(self, broker: MemoryBroker, client_id: str) -> None:
        self._broker = broker
        self.client_id = client_id
        self._connected = False
        self._will: Message | None = None
        self._outbox: list[Message] = []
        self._cbs: list[ConnectionCallback] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def _mark(self, up: bool) -> None:
        if up == self._connected:
            return
        self._connected = up
        for cb in list(self._cbs):
            cb(up)

    async def connect(self) -> None:
        if self._connected:
            return
        self._broker._attach(self)

    async def close(self) -> None:
        if not self._connected:
            return
        self._broker._detach(self)

    def set_will(self, topic: str, payload: bytes, qos: int = 1, retain: bool = True) -> None:
        self._will = Message(topic, payload, qos, retain)

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1,
                      retain: bool = False) -> None:
        msg = Message(topic, payload, qos, retain)
        if not self._connected:
            if qos >= 1:
                self._outbox.append(msg)
            return
        self._broker._publish(msg, from_client=self.client_id)

    async def subscribe(self, topic_filter: str, handler: Handler, *, qos: int = 1) -> None:
        if not self._connected:
            raise RuntimeError("没连上就订阅:先 connect()")
        self._broker._subscribe(self.client_id, topic_filter, qos, handler)

    def on_connection(self, cb: ConnectionCallback) -> None:
        self._cbs.append(cb)
