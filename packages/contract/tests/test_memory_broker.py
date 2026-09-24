"""进程内 MemoryBroker:验收流程跑在它上面,所以它得把 MQTT 里我们用到的那几件事
学像:按主题订阅(含 + #)、QoS 1 至少一次、retained、LWT、断线期间发布入队重连后补发,
以及给测试用的注入口(重复投递、断线)。"""

from __future__ import annotations

import asyncio

import pytest

from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.transport import Message


class _收:
    def __init__(self) -> None:
        self.got: list[Message] = []

    async def __call__(self, m: Message) -> None:
        self.got.append(m)


@pytest.fixture
def broker():
    return MemoryBroker()


async def _client(broker, cid):
    t = MemoryTransport(broker, client_id=cid)
    await t.connect()
    return t


async def test_精确主题pub_sub(broker):
    a, b = await _client(broker, "a"), await _client(broker, "b")
    收 = _收()
    await b.subscribe("site/s/robot/r/cmd", 收)
    await a.publish("site/s/robot/r/cmd", b"1")
    await a.publish("site/s/robot/r/status", b"2")
    await broker.drain()
    assert [m.payload for m in 收.got] == [b"1"]
    assert 收.got[0].topic == "site/s/robot/r/cmd" and 收.got[0].qos == 1


@pytest.mark.parametrize("flt, hits", [
    ("site/+/robot/+/cmd", ["site/s/robot/r/cmd", "site/t/robot/q/cmd"]),
    ("site/s/robot/r/#", ["site/s/robot/r/cmd", "site/s/robot/r/cmd/ack"]),
    ("site/s/robot/r/cmd", ["site/s/robot/r/cmd"]),
])
async def test_通配过滤(broker, flt, hits):
    a, b = await _client(broker, "a"), await _client(broker, "b")
    收 = _收()
    await b.subscribe(flt, 收)
    for t in ("site/s/robot/r/cmd", "site/t/robot/q/cmd", "site/s/robot/r/cmd/ack", "other/x"):
        await a.publish(t, b"x")
    await broker.drain()
    assert sorted(m.topic for m in 收.got) == sorted(hits)


async def test_retained对后订阅者投递_且被新值覆盖(broker):
    a = await _client(broker, "a")
    await a.publish("site/s/robot/r/status", b"v1", retain=True)
    await a.publish("site/s/robot/r/status", b"v2", retain=True)
    await a.publish("site/s/robot/r/event", b"e", retain=False)
    b = await _client(broker, "b")
    收 = _收()
    await b.subscribe("site/s/robot/r/#", 收)
    await broker.drain()
    assert [(m.topic, m.payload, m.retain) for m in 收.got] == [
        ("site/s/robot/r/status", b"v2", True)]


async def test_LWT在断线时投递并改写retained(broker):
    dog = await _client(broker, "dog")
    dog.set_will("site/s/robot/r/status", b"offline", qos=1, retain=True)
    await dog.connect()
    await dog.publish("site/s/robot/r/status", b"online", retain=True)
    site = await _client(broker, "site")
    收 = _收()
    await site.subscribe("site/s/robot/r/status", 收)
    await broker.drain()
    broker.disconnect("dog")
    await broker.drain()
    assert [m.payload for m in 收.got] == [b"online", b"offline"]
    assert dog.connected is False
    late = await _client(broker, "late")
    收2 = _收()
    await late.subscribe("site/s/robot/r/status", 收2)
    await broker.drain()
    assert [m.payload for m in 收2.got] == [b"offline"]


async def test_断线期间发布入队_重连后按序补发(broker):
    dog, site = await _client(broker, "dog"), await _client(broker, "site")
    收 = _收()
    await site.subscribe("site/s/robot/r/event", 收)
    broker.disconnect("dog")
    await dog.publish("site/s/robot/r/event", b"1")
    await dog.publish("site/s/robot/r/event", b"2")
    await broker.drain()
    assert 收.got == [], "断线期间什么都不该到"
    await dog.connect()
    await broker.drain()
    assert [m.payload for m in 收.got] == [b"1", b"2"]


async def test_断线的客户端收不到_重连后订阅仍在(broker):
    """paho 的 clean_session=False 语义:重连后原订阅保留,断线期间发给它的 QoS1 消息补投。"""
    dog, site = await _client(broker, "dog"), await _client(broker, "site")
    收 = _收()
    await dog.subscribe("site/s/robot/r/cmd", 收)
    broker.disconnect("dog")
    await site.publish("site/s/robot/r/cmd", b"during", qos=1)
    await site.publish("site/s/robot/r/telemetry", b"q0", qos=0)
    await broker.drain()
    assert 收.got == []
    await dog.connect()
    await broker.drain()
    assert [m.payload for m in 收.got] == [b"during"], "QoS1 补投,QoS0 不补"


async def test_duplicate_next让下一条QoS1投递两次(broker):
    a, b = await _client(broker, "a"), await _client(broker, "b")
    收 = _收()
    await b.subscribe("site/s/robot/r/#", 收)
    broker.duplicate_next(1)
    await a.publish("site/s/robot/r/cmd", b"dup", qos=1)
    await a.publish("site/s/robot/r/cmd", b"once", qos=1)
    await a.publish("site/s/robot/r/telemetry", b"q0", qos=0)
    await broker.drain()
    assert [m.payload for m in 收.got] == [b"dup", b"dup", b"once", b"q0"]


async def test_一个handler炸了不影响其他订阅者(broker):
    a, b, c = await _client(broker, "a"), await _client(broker, "b"), await _client(broker, "c")

    async def 炸(m):
        raise RuntimeError("boom")

    收 = _收()
    await b.subscribe("t", 炸)
    await c.subscribe("t", 收)
    await a.publish("t", b"x")
    await broker.drain()
    assert [m.payload for m in 收.got] == [b"x"]


async def test_连接回调(broker):
    dog = await _client(broker, "dog")
    seen: list[bool] = []
    dog.on_connection(seen.append)
    broker.disconnect("dog")
    await dog.connect()
    assert seen == [False, True]


async def test_drain之后没有挂起的投递(broker):
    a, b = await _client(broker, "a"), await _client(broker, "b")
    收 = _收()
    await b.subscribe("t", 收)
    for i in range(50):
        await a.publish("t", str(i).encode())
    await broker.drain()
    assert len(收.got) == 50
    await asyncio.sleep(0)
    assert len(收.got) == 50
