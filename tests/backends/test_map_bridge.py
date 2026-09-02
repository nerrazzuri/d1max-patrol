"""地图桥客户端 —— 对着一个 asyncio 起的假桥跑。

真桥要 rclpy,CI 和开发机上都没有。假桥只要在同一条线协议上说话,
客户端就分不出真假 —— 这正是把线协议单独抽一层的用处。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from d1max_patrol.backends.base import NavConnectionError
from d1max_patrol.backends.map_bridge import MapBridgeClient, MapUpdate
from d1max_patrol.protocol.map_frames import encode_hello, encode_map


class FakeBridge:
    """一个只出不进的假地图桥。``proto`` 可以拧歪,用来测版本校验。"""

    def __init__(self, *, proto: int | None = None, hello: bool = True) -> None:
        self._proto = proto
        self._hello = hello
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.connected = asyncio.Event()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _on_client(self, reader, writer) -> None:
        self._writers.append(writer)
        if self._hello:
            line = encode_hello("/map")
            if self._proto is not None:
                line = line.replace(b'"proto":1', f'"proto":{self._proto}'.encode())
            writer.write(line)
            await writer.drain()
        self.connected.set()

    async def push(self, *, width: int = 2, height: int = 1,
                   cells=(0, 100), ts_ms: int = 1) -> None:
        await self._send(encode_map(width, height, 0.05, (0.0, 0.0, 0.0),
                                    list(cells), ts_ms))

    async def push_raw(self, line: bytes) -> None:
        await self._send(line)

    async def _send(self, payload: bytes) -> None:
        await asyncio.wait_for(self.connected.wait(), 2)
        for writer in self._writers:
            writer.write(payload)
            await writer.drain()

    async def drop(self) -> None:
        """桥那边主动关连接。"""
        writers, self._writers = self._writers, []
        for writer in writers:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        await self.drop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def fake_bridge():
    bridge = FakeBridge()
    await bridge.start()
    yield bridge
    await bridge.stop()


@pytest.fixture
async def fake_bridge_wrong_proto():
    bridge = FakeBridge(proto=99)
    await bridge.start()
    yield bridge
    await bridge.stop()


@pytest.fixture
async def silent_bridge():
    bridge = FakeBridge(hello=False)
    await bridge.start()
    yield bridge
    await bridge.stop()


async def _client(bridge) -> MapBridgeClient:
    client = MapBridgeClient("127.0.0.1", bridge.port)
    await client.connect()
    return client


async def _until(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


# ------------------------------------------------------------------ 连接


async def test_连上先收hello再收帧(fake_bridge):
    c = await _client(fake_bridge)
    q = c.subscribe()
    await fake_bridge.push(width=2, height=1, cells=[0, 100])
    update = await asyncio.wait_for(q.get(), 2)
    assert isinstance(update, MapUpdate)
    assert update.frame.data == (0, 100)
    await c.close()


async def test_hello里的话题记下来了(fake_bridge):
    c = await _client(fake_bridge)
    assert c.topic == "/map"
    await c.close()


async def test_协议版本对不上就断开(fake_bridge_wrong_proto):
    c = MapBridgeClient("127.0.0.1", fake_bridge_wrong_proto.port)
    with pytest.raises(NavConnectionError, match="协议"):
        await c.connect()
    assert not c.connected, "连不上就不该留着半开的连接"


async def test_桥不发hello不会永远挂着(silent_bridge, monkeypatch):
    """连错端口时最容易撞上:对面 accept 了但一句话不说。"""
    from d1max_patrol.backends import map_bridge

    monkeypatch.setattr(map_bridge, "HELLO_TIMEOUT_S", 0.2)
    c = MapBridgeClient("127.0.0.1", silent_bridge.port)
    with pytest.raises(NavConnectionError, match="hello"):
        await asyncio.wait_for(c.connect(), 3)


async def test_连不上的端口报得出是地图桥():
    c = MapBridgeClient("127.0.0.1", 1)
    with pytest.raises(NavConnectionError, match="地图桥"):
        await c.connect()


async def test_连两次不会起两条流(fake_bridge):
    c = await _client(fake_bridge)
    await c.connect()
    assert c.connected
    await c.close()


# ------------------------------------------------------------------ 收流


async def test_latest给的是最后一帧(fake_bridge):
    c = await _client(fake_bridge)
    assert c.latest is None, "还没收到图之前 latest 就该是 None"
    await fake_bridge.push(width=2, height=1, cells=[0, 100], ts_ms=1)
    await fake_bridge.push(width=2, height=1, cells=[100, 0], ts_ms=2)
    assert await _until(lambda: c.latest is not None and c.latest.ts_ms == 2)
    assert c.latest.data == (100, 0)
    await c.close()


async def test_两个订阅者都收得到同一帧(fake_bridge):
    c = await _client(fake_bridge)
    q1, q2 = c.subscribe(), c.subscribe()
    await fake_bridge.push()
    for q in (q1, q2):
        assert (await asyncio.wait_for(q.get(), 2)).frame.data == (0, 100)
    await c.close()


async def test_坏帧不会打死整条流(fake_bridge):
    """桥发了一行垃圾,客户端记一笔继续读下一行 —— 地图不是安全关键路径。"""
    c = await _client(fake_bridge)
    q = c.subscribe()
    await fake_bridge.push_raw("这不是 json\n".encode())
    await fake_bridge.push(width=2, height=1, cells=[0, 100])
    assert (await asyncio.wait_for(q.get(), 2)).frame.data == (0, 100)
    assert c.bad_frames == 1, "丢了帧不抛,但要留痕迹"
    assert c.connected
    await c.close()


async def test_桥断了connected变假(fake_bridge):
    c = await _client(fake_bridge)
    await fake_bridge.drop()
    assert await _until(lambda: not c.connected)
    await c.close()


async def test_桥断了latest还留着(fake_bridge):
    """链路没了不等于上一张图作废 —— UI 上留着最后一张比突然变白好。"""
    c = await _client(fake_bridge)
    await fake_bridge.push()
    assert await _until(lambda: c.latest is not None)
    await fake_bridge.drop()
    assert await _until(lambda: not c.connected)
    assert c.latest is not None
    await c.close()


# ------------------------------------------------------------------ 收尾


async def test_close之后不再收帧(fake_bridge):
    c = await _client(fake_bridge)
    q = c.subscribe()
    await c.close()
    await fake_bridge.push()
    await asyncio.sleep(0.1)
    assert q.empty()


async def test_没连上就close不会炸():
    await MapBridgeClient("127.0.0.1", 1).close()


async def test_close两次也不会炸(fake_bridge):
    c = await _client(fake_bridge)
    await c.close()
    await c.close()
    assert not c.connected


async def test_客户端不往桥里写东西(fake_bridge):
    """单向是这条协议的安全属性 —— 写一个字节都不行。"""
    import inspect

    from d1max_patrol.backends import map_bridge

    src = inspect.getsource(map_bridge)
    assert "writer.write(" not in src, "地图桥是只读的,客户端不该有写路径"
