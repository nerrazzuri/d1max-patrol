"""消息总线的最小接口。代理与站点只依赖它,不依赖任何 MQTT 库(决定 2)。

实现有两个:``memory_broker.MemoryTransport``(进程内,测试与验收用)与
``paho_transport.PahoTransport``(真 broker)。语义按 MQTT 3.1.1 我们用到的那一小块:
QoS 0 尽力、QoS 1 至少一次(**重复投递是正常现象**,消费方按 id 去重)、retained、
LWT(连接非正常断开时 broker 代发)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    qos: int
    #: 收方看到的 retain 标志:存储的 retained 报文投给新订阅者时为 True,现场转发为 False。
    retain: bool


Handler = Callable[[Message], Awaitable[None]]
ConnectionCallback = Callable[[bool], None]


class Transport(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    def set_will(self, topic: str, payload: bytes, qos: int = 1, retain: bool = True) -> None:
        """LWT:非正常断开时 broker 代发。要在 ``connect()`` 之前设。"""
        ...

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1,
                      retain: bool = False) -> None: ...

    async def subscribe(self, topic_filter: str, handler: Handler, *, qos: int = 1) -> None: ...

    def on_connection(self, cb: ConnectionCallback) -> None:
        """连上(True)/断了(False)时回调。断线检测是代理断线策略的入口。"""
        ...


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT 过滤规则:``+`` 匹配一级,``#`` 只能在末尾、匹配其后全部(含零级)。"""
    f = topic_filter.split("/")
    t = topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return i == len(f) - 1
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)
