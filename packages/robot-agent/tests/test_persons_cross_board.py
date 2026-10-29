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


async def _连(srv, *, key=KEY, name="d1max-persons"):
    r, w = await asyncio.open_connection("127.0.0.1", srv.port, limit=pb.MAX_READ + 2)
    ch = pb.parse(await asyncio.wait_for(r.readline(), 2))
    assert isinstance(ch, pb.Challenge)
    mac = pb.hello_mac(key, ch.nonce, name) if key else ""
    w.write(pb.encode(pb.Hello(proto=pb.PROTO, name=name, mac=mac)))
    await w.drain()
    return r, w


async def _读(r):
    line = await asyncio.wait_for(r.readline(), 2)
    return pb.parse(line) if line else None


async def test_跨板_密钥对了才收_截图写进目录_persons照常_外参连上推一次换图再推(tmp_path):
    box = [("m:1", {"lidar": {"x": 0.4}})]
    srv, sink = await _起(tmp_path, frames=lambda: box[0])
    try:
        r, w = await _连(srv)
        assert isinstance(await _读(r), pb.Hello)
        f = await _读(r)
        assert isinstance(f, pb.FramesMsg) and f.ref == "m:1" and f.frames == {"lidar": {"x": 0.4}}
        w.write(pb.encode(pb.Snap.of("20261029T120000Z-front-3.jpg", JPEG)))
        w.write(pb.encode(pb.Persons(seq=3, stamp_ns=1, camera="front",
                                     snapshot="20261029T120000Z-front-3.jpg")))
        await w.drain()
        for _ in range(50):
            if sink.got:
                break
            await asyncio.sleep(0.02)
        assert sink.got and sink.got[0].snapshot == "20261029T120000Z-front-3.jpg"
        assert (tmp_path / "snaps" / "20261029T120000Z-front-3.jpg").read_bytes() == JPEG
        box[0] = ("m:2", {"lidar": {"x": 0.5}})
        srv.tick()
        f2 = await _读(r)
        assert isinstance(f2, pb.FramesMsg) and f2.ref == "m:2"
        srv.tick()                                   # 没换图不重发
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(r.readline(), 0.2)
        w.close()
    finally:
        await srv.close()


@pytest.mark.parametrize("key", [b"\x01" * 32, None])
async def test_跨板_密钥不对或者不带签名_断开(tmp_path, key):
    srv, sink = await _起(tmp_path)
    try:
        r, w = await _连(srv, key=key)
        m = await _读(r)
        assert isinstance(m, pb.Error) and "签名" in m.reason
        assert sink.connected == 0
        w.close()
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
        r, w = await _连(srv)
        await _读(r)
        n = MAX_REMOTE_SNAPS + 5
        for i in range(n):
            w.write(pb.encode(pb.Snap.of(f"s-{i:03d}.jpg", JPEG)))
        w.write(pb.encode(pb.Heartbeat(seq=1)))
        await w.drain()
        for _ in range(100):
            if (tmp_path / "snaps" / f"s-{n - 1:03d}.jpg").exists():
                break
            await asyncio.sleep(0.02)
        left = sorted(p.name for p in (tmp_path / "snaps").glob("*.jpg"))
        assert len(left) == MAX_REMOTE_SNAPS and left[-1] == f"s-{n - 1:03d}.jpg"
        w.close()
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
