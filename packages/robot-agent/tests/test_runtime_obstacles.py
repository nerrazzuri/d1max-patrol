"""W11 运行时:本机障碍桥(真 Unix 套接字:握手、认账号、序号、坏行、没声断开)、能力里的避障状态、
``--obstacles bridge`` 要配 ``--nav planned``。"""

from __future__ import annotations

import asyncio
import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.obs_server import ObsBridgeServer
from d1max_agent.obstacles import ObstacleView
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities
from d1max_contract.obsbridge import Grid, Heartbeat, Hello, encode, pack_bits
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics
from d1max_patrol.protocol.nav_types import Pose

REG = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                   expires_at=10**13)
T = Topics(site_id="s", robot_id="r")


def _grid(seq, check="ok"):
    n = 10
    return Grid(seq=seq, stamp_ns=0, res=0.1, size=n, occ=pack_bits([False] * 100, n),
                known=pack_bits([True] * 100, n), check=check,
                reason="" if check == "ok" else "歪了")


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


async def _连(path):
    r, w = await asyncio.open_unix_connection(str(path))
    return r, w


async def test_障碍桥_握手_收栅格_序号_坏行_没声断开(tmp_path):
    c = 钟()
    view = ObstacleView(monotonic=c)
    view.note_odom(0, 0, 0)
    srv = ObsBridgeServer(tmp_path / "obs.sock", view, monotonic=c)
    await srv.start()
    try:
        assert (tmp_path / "obs.sock").stat().st_mode & 0o777 == 0o600
        r, w = await _连(tmp_path / "obs.sock")
        w.write(encode(Hello(proto=1, name="obstacles")))
        await w.drain()
        assert json.loads(await r.readline())["t"] == "hello"
        assert srv.connected and view.connected
        w.write(encode(_grid(2)))
        w.write(b"not json\n")
        w.write(encode(_grid(1)))                       # 旧的:丢
        w.write(encode(Heartbeat(seq=5)))
        await w.drain()
        await asyncio.sleep(0.05)
        assert len(view.frames) == 1 and view.state() == "ok"
        # 第二个连上来:旧的还有声,拒
        r2, w2 = await _连(tmp_path / "obs.sock")
        w2.write(encode(Hello(proto=1)))
        await w2.drain()
        assert json.loads(await r2.readline())["t"] == "error"
        c.t += 3.0
        srv.tick()
        assert not srv.connected and not view.connected
        w.close()
        # 版本不对:拒
        r3, w3 = await _连(tmp_path / "obs.sock")
        w3.write(encode(Hello(proto=9)))
        await w3.drain()
        assert json.loads(await r3.readline())["t"] == "error"
        w3.close()
    finally:
        await srv.close()
    assert not (tmp_path / "obs.sock").exists()


async def test_别的账号连不上(tmp_path):
    view = ObstacleView()
    srv = ObsBridgeServer(tmp_path / "obs.sock", view, allowed_uid=-5)
    await srv.start()
    try:
        r, w = await _连(tmp_path / "obs.sock")
        try:
            w.write(encode(Hello(proto=1)))
            await w.drain()
            assert await r.readline() == b""
        except ConnectionResetError:
            pass                                       # 对面当场断开:写的时候就被重置了
        assert not srv.connected
    finally:
        await srv.close()


class 耳朵:
    def __init__(self):
        self.caps = []

    async def __call__(self, m):
        if T.parse(m.topic)[2] == "capabilities":
            self.caps.append(json.loads(m.payload))


async def test_运行时_能力里报避障状态_外参没过带原因(tmp_path):
    broker = MemoryBroker()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    r = SimRobot(now_ms=lambda: 1_000_000)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=lambda: 1_000_000,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      nav="planned", obstacles="bridge", obs_socket=tmp_path / "o.sock")
    await rt.start()
    try:
        await broker.drain()
        caps = Capabilities.from_wire(ears.caps[-1])
        assert caps.tasks["obstacles"] == {"state": "lost", "rear": False}
        assert rt.parts.nav.obstacles is rt.obs_view and rt.parts.nav.guard is not None
        rd, w = await _连(tmp_path / "o.sock")
        w.write(encode(Hello(proto=1)))
        w.write(encode(_grid(1, check="extrinsic_bad")))
        await w.drain()
        await rd.readline()
        for _ in range(20):
            await rt.step(0.1)
            await asyncio.sleep(0.01)
            await broker.drain()
            if Capabilities.from_wire(ears.caps[-1]).tasks["obstacles"]["state"] != "lost":
                break
        o = Capabilities.from_wire(ears.caps[-1]).tasks["obstacles"]
        assert o["state"] == "extrinsic_bad" and o["reason"] == "歪了"
        w.close()
    finally:
        await rt.close()


def test_避障要配规划后端(tmp_path):
    r = SimRobot(now_ms=lambda: 0)
    with pytest.raises(ValueError, match="nav planned"):
        AgentRuntime(transport=MemoryTransport(MemoryBroker(), "dog"), registration=REG, hal=r,
                     store_dir=tmp_path, now_ms=lambda: 0, loaded_map=("m", "1"),
                     home=Pose.from_xy_yaw(0, 0, 0), obstacles="bridge")
    with pytest.raises(ValueError, match="obstacles 要是"):
        AgentRuntime(transport=MemoryTransport(MemoryBroker(), "dog"), registration=REG, hal=r,
                     store_dir=tmp_path, now_ms=lambda: 0, loaded_map=("m", "1"),
                     home=Pose.from_xy_yaw(0, 0, 0), nav="planned", obstacles="lidar")
