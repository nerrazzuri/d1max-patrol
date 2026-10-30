"""人员检测跨板(2026-10-27 用户定:我们的服务在 RK3588 上,人员检测留在厂家 Orin):人员桥走 TCP。
代理这一头:挑战 + HMAC 认密钥、对端 IP、截图经桥传过来写进截图目录、外参连上推一次换图再推。
本机 Unix 套接字照旧是缺省(不配 --persons-listen 就不变)。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from d1max_agent.pers_server import MAX_REMOTE_SNAPS, PersonBridgeServer
from d1max_contract import persbridge as pb

KEY = bytes(range(32))
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 200 + b"\xff\xd9"


class 收:
    def __init__(self, d: Path):
        self.snapshot_dir = d
        self.got: list = []
        self.connected = 0

    def on_connect(self):
        self.connected += 1

    def on_disconnect(self, *a):
        pass

    def on_persons(self, m):
        self.got.append(m)


async def _起(tmp_path, *, peer=None, frames=None):
    sink = 收(tmp_path / "snaps")
    srv = PersonBridgeServer(tmp_path / "persons.sock", sink, listen=("127.0.0.1", 0), key=KEY,
                             peer=peer, frames=frames)
    await srv.start()
    return srv, sink


ND = "cd" * 16


class 连接:
    """假的检测节点这一头:握手(双向认证)以后收发都封好(复查 P212-I1)。"""

    def __init__(self, r, w, key, na):
        self.r, self.w, self.key, self.na = r, w, key, na
        self.ks: bytes | None = None
        self.tx = self.rx = 0

    def send(self, msg) -> None:
        line = pb.encode(msg)
        if self.ks is not None:
            self.tx += 1
            line = pb.seal(self.ks, pb.TO_AGENT, self.tx, line)
        self.w.write(line)

    async def read(self):
        line = await asyncio.wait_for(self.r.readline(), 2)
        if not line:
            return None
        if self.ks is None:
            m = pb.parse(line)
            if isinstance(m, pb.Hello):              # 代理回的 hello:核它的签名,核上了才开会话
                assert m.mac == pb.agent_mac(self.key, ND, self.na), "代理要证明自己有密钥"
                self.ks = pb.session_key(self.key, self.na, ND)
            return m
        self.rx, m = pb.unseal(self.ks, pb.TO_DETECTOR, line, self.rx)
        return m


async def _连(srv, *, key=KEY, name="d1max-persons"):
    r, w = await asyncio.open_connection("127.0.0.1", srv.port, limit=pb.MAX_READ + 2)
    ch = pb.parse(await asyncio.wait_for(r.readline(), 2))
    assert isinstance(ch, pb.Challenge)
    mac = pb.hello_mac(key, ch.nonce, name) if key else ""
    w.write(pb.encode(pb.Hello(proto=pb.PROTO, name=name, mac=mac, nonce=ND)))
    await w.drain()
    return 连接(r, w, key or KEY, ch.nonce)


async def test_跨板_密钥对了才收_截图写进目录_persons照常_外参连上推一次换图再推(tmp_path):
    box = [("m:1", {"lidar": {"x": 0.4}})]
    srv, sink = await _起(tmp_path, frames=lambda: box[0])
    try:
        c = await _连(srv)
        assert isinstance(await c.read(), pb.Hello)
        f = await c.read()
        assert isinstance(f, pb.FramesMsg) and f.ref == "m:1" and f.frames == {"lidar": {"x": 0.4}}
        c.send(pb.Snap.of("20261029T120000Z-front-3.jpg", JPEG))
        c.send(pb.Persons(seq=3, stamp_ns=1, camera="front",
                          snapshot="20261029T120000Z-front-3.jpg"))
        await c.w.drain()
        for _ in range(50):
            if sink.got:
                break
            await asyncio.sleep(0.02)
        assert sink.got and sink.got[0].snapshot == "20261029T120000Z-front-3.jpg"
        assert (tmp_path / "snaps" / "20261029T120000Z-front-3.jpg").read_bytes() == JPEG
        box[0] = ("m:2", {"lidar": {"x": 0.5}})
        srv.tick()
        f2 = await c.read()
        assert isinstance(f2, pb.FramesMsg) and f2.ref == "m:2"
        srv.tick()                                   # 没换图不重发
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c.r.readline(), 0.2)
        c.w.close()
    finally:
        await srv.close()


@pytest.mark.parametrize("key", [b"\x01" * 32, None])
async def test_跨板_密钥不对或者不带签名_断开(tmp_path, key):
    srv, sink = await _起(tmp_path)
    try:
        c = await _连(srv, key=key)
        m = pb.parse(await asyncio.wait_for(c.r.readline(), 2))
        assert isinstance(m, pb.Error) and "签名" in m.reason
        assert sink.connected == 0
        c.w.close()
    finally:
        await srv.close()


async def test_跨板_对端IP不对_连上就断(tmp_path):
    srv, sink = await _起(tmp_path, peer="10.9.9.9")
    try:
        r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        assert await asyncio.wait_for(r.readline(), 2) == b""
        w.close()
    finally:
        await srv.close()


async def test_跨板_截图目录最多留这么多张_最旧的删(tmp_path):
    srv, sink = await _起(tmp_path)
    try:
        c = await _连(srv)
        await c.read()
        n = MAX_REMOTE_SNAPS + 5
        for i in range(n):
            c.send(pb.Snap.of(f"s-{i:03d}.jpg", JPEG))
        c.send(pb.Heartbeat(seq=1))
        await c.w.drain()
        for _ in range(100):
            if (tmp_path / "snaps" / f"s-{n - 1:03d}.jpg").exists():
                break
            await asyncio.sleep(0.02)
        left = sorted(p.name for p in (tmp_path / "snaps").glob("*.jpg"))
        assert len(left) == MAX_REMOTE_SNAPS and left[-1] == f"s-{n - 1:03d}.jpg"
        c.w.close()
    finally:
        await srv.close()


def test_本机套接字照旧_不配监听就不变(tmp_path):
    srv = PersonBridgeServer(tmp_path / "persons.sock", 收(tmp_path))
    assert srv.listen is None and srv.key is None


def test_外参读不了_不推也不炸(tmp_path):
    def 坏():
        raise OSError("frames.json 读不了")
    srv = PersonBridgeServer(tmp_path / "p.sock", 收(tmp_path), listen=("127.0.0.1", 0), key=KEY,
                             frames=坏)
    srv._push_frames()                                # 没连着、读不了:都不炸
    assert json.dumps(srv._frames_ref) == "null"


def test_运行时推的外参_是正在用的那张图的frames(tmp_path):
    from types import SimpleNamespace as NS

    from d1max_agent.runtime import AgentRuntime
    d = tmp_path / "m" / "3"
    d.mkdir(parents=True)
    (d / "frames.json").write_text('{"lidar": {"x": 0.4}}', "utf-8")
    ref = NS(map_id="m", version="3")
    fake = NS(maps=NS(active=lambda: ref, dir_of=lambda r: d))
    assert AgentRuntime._active_frames(fake) == ("m:3", {"lidar": {"x": 0.4}})
    fake.maps.active = lambda: None
    assert AgentRuntime._active_frames(fake) is None
    assert AgentRuntime._active_frames(NS(maps=None)) is None


def test_代理参数_跨板要密钥_密钥文件写错就不起(tmp_path):
    import argparse

    from d1max_agent.main import _persons_key
    a = argparse.Namespace(persons_listen=("0.0.0.0", 47010), persons_key=None)
    with pytest.raises(SystemExit, match="persons-key"):
        _persons_key(a)
    k = tmp_path / "persons.key"
    k.write_text(KEY.hex() + "\n")
    k.chmod(0o644)
    a.persons_key = k
    with pytest.raises(SystemExit, match="chmod 600"):
        _persons_key(a)
    k.chmod(0o600)
    assert _persons_key(a) == KEY
    k.write_text("abcd\n")
    with pytest.raises(SystemExit, match="太短"):
        _persons_key(a)
    assert _persons_key(argparse.Namespace(persons_listen=None, persons_key=None)) is None


async def test_复查P212_握手以后_不封的和改过的都不收(tmp_path):
    """外审 P212-I1:握手以后每一行都要封好、签名对、序号往上走;能插进内网连接的设备塞进来的
    明文、改过的、重放的都不认。"""
    srv, sink = await _起(tmp_path)
    try:
        c = await _连(srv)
        assert isinstance(await c.read(), pb.Hello)
        c.w.write(pb.encode(pb.Persons(seq=1, stamp_ns=1, camera="front")))      # 明文
        p2 = pb.encode(pb.Persons(seq=2, stamp_ns=1, camera="front"))
        bad = pb.seal(c.ks, pb.TO_AGENT, 1, p2)
        c.w.write(bad.replace(b'\\"seq\\":2', b'\\"seq\\":3'))              # 改过
        c.tx = 1
        c.send(pb.Persons(seq=4, stamp_ns=1, camera="front"))                       # 封好的(序号 2)
        c.w.write(pb.seal(c.ks, pb.TO_AGENT, 2, pb.encode(pb.Persons(seq=5, stamp_ns=1,
                                                                     camera="front"))))  # 重放序号
        await c.w.drain()
        for _ in range(50):
            if sink.got:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        assert [m.seq for m in sink.got] == [4]
        c.w.close()
    finally:
        await srv.close()


async def test_复查P212_真的两头对接_双向认证过了_人员结果和外参都到(tmp_path):
    """代理的服务端对检测节点的 PersonLink(两头都是真的):握手、逐行签名对得上。"""
    import socket

    from d1max_localizer.persons import PersonLink
    box = [("m:1", {"lidar": {"x": 0.4}})]
    srv, sink = await _起(tmp_path, frames=lambda: box[0])
    frames: list = []
    try:
        link = PersonLink(lambda: socket.create_connection(("127.0.0.1", srv.port), timeout=1),
                          key=KEY, on_frames=lambda ref, d: frames.append(ref), timeout_s=2)
        ok = await asyncio.to_thread(link.send, json.loads(pb.encode(pb.Persons(
            seq=7, stamp_ns=1, camera="front")).decode()))
        assert ok and link.connected
        for _ in range(100):
            await asyncio.sleep(0.02)
            await asyncio.to_thread(link.poll)
            if sink.got and frames:
                break
        assert [m.seq for m in sink.got] == [7] and frames == ["m:1"]
        link.close()
    finally:
        await srv.close()


class 钟:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _等到(cond, n=100):
    for _ in range(n):
        if cond():
            return
        await asyncio.sleep(0.02)


async def test_外审Minor_接收端限速_发得太快的丢掉_过一会儿又收(tmp_path):
    """外审(PR212)Minor:接收端没有帧率、字节速率上限 ——
    失常的发送端能一直逼代理验签、解析、落盘。"""
    from d1max_agent.pers_server import LINE_BURST
    clock = 钟()
    sink = 收(tmp_path / "snaps")
    srv = PersonBridgeServer(tmp_path / "persons.sock", sink, listen=("127.0.0.1", 0), key=KEY,
                             monotonic=clock)
    await srv.start()
    try:
        c = await _连(srv)
        await c.read()
        for i in range(1, 301):                      # 时间不走:一口气 300 帧
            c.send(pb.Persons(seq=i, stamp_ns=i, camera="front"))
        await c.w.drain()
        await _等到(lambda: sink.got and sink.got[-1].seq >= LINE_BURST - 2)
        await asyncio.sleep(0.2)
        assert LINE_BURST - 5 <= len(sink.got) <= LINE_BURST, len(sink.got)
        clock.t += 2.0                               # 过两秒:又能收
        c.send(pb.Persons(seq=301, stamp_ns=301, camera="front"))
        await c.w.drain()
        await _等到(lambda: sink.got[-1].seq == 301)
        assert sink.got[-1].seq == 301
        c.w.close()
    finally:
        await srv.close()


async def test_外审Minor_截图按字节限速_大图一口气只收几张(tmp_path):
    from d1max_agent.pers_server import BYTE_BURST
    clock = 钟()
    sink = 收(tmp_path / "snaps")
    srv = PersonBridgeServer(tmp_path / "persons.sock", sink, listen=("127.0.0.1", 0), key=KEY,
                             monotonic=clock)
    await srv.start()
    big = b"\xff\xd8" + b"y" * (pb.MAX_SNAP_BYTES - 4) + b"\xff\xd9"
    try:
        c = await _连(srv)
        await c.read()
        for i in range(20):
            c.send(pb.Snap.of(f"b-{i:02d}.jpg", big))
            await c.w.drain()
        c.send(pb.Heartbeat(seq=1))
        await c.w.drain()
        await asyncio.sleep(0.5)
        saved = list((tmp_path / "snaps").glob("*.jpg"))
        assert 1 <= len(saved) <= BYTE_BURST // pb.MAX_SNAP_BYTES, len(saved)
        c.w.close()
    finally:
        await srv.close()
