"""代理侧的 Transport 包装:命名空间守卫(总设计 §3.6;W00 决定 4)。

一台狗只能发自己的主题、只能订自己的 ``cmd``。broker 侧的 ACL 归 W00c;这里先让代理
自己就拒绝越界 —— 发到别人的主题、订通配,都是 ``PermissionError``,不等 broker 来拒。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from d1max_contract.topics import TopicAcl
from d1max_contract.transport import ConnectionCallback, Handler, Transport


class GuardedTransport:
    def __init__(self, inner: Transport, acl: TopicAcl, *,
                 after_connect: Callable[[], Awaitable[None]] | None = None) -> None:
        self._inner = inner
        self._acl = acl
        self._after_connect = after_connect

    @property
    def connected(self) -> bool:
        return self._inner.connected

    async def connect(self) -> None:
        await self._inner.connect()
        if self._after_connect is not None:
            await self._after_connect()

    async def close(self) -> None:
        await self._inner.close()

    def set_will(self, topic: str, payload: bytes, qos: int = 1, retain: bool = True) -> None:
        if not self._acl.may_publish(topic):
            raise PermissionError(f"LWT 主题越界: {topic}")
        self._inner.set_will(topic, payload, qos, retain)

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1,
                      retain: bool = False) -> None:
        if not self._acl.may_publish(topic):
            raise PermissionError(f"这台狗不许发到 {topic}")
        await self._inner.publish(topic, payload, qos=qos, retain=retain)

    async def subscribe(self, topic_filter: str, handler: Handler, *, qos: int = 1) -> None:
        if not self._acl.may_subscribe(topic_filter):
            raise PermissionError(f"这台狗不许订 {topic_filter}")
        await self._inner.subscribe(topic_filter, handler, qos=qos)

    def on_connection(self, cb: ConnectionCallback) -> None:
        self._inner.on_connection(cb)
