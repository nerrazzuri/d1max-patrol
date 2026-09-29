"""建图预览(W09f):站点经只读命令 ``mapping_preview {since}`` 取边走边建的最新快照给手机看。
服务层怎么读快照在 ``test_mapping_service.py``;这里是命令这一层:能力、参数、回执、不排队、
不记幂等。"""

from __future__ import annotations

import pytest
from test_runtime_maps import REG, T, _cmd, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.commands import READ_KINDS
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_patrol.protocol.nav_types import Pose


class 假录包:
    def __init__(self):
        self.recording = False
        self.last_bag = ""
        self.asked: list[int] = []
        self.alive = True

    def preview(self, since):
        self.asked.append(since)
        return {"live": True, "seq": 5, "recording": self.recording}

    def preview_alive(self):
        return self.alive


class 老录包:
    """没有预览的老服务(W09f 之前):不报这项能力。"""
    def __init__(self):
        self.recording = False
        self.last_bag = ""


async def _台(tmp_path, mapper):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path / "agent", now_ms=c,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      monotonic=lambda: c.mono, mapper=mapper)
    await rt.start()
    await broker.drain()
    return broker, c, ears, rt


def _acks(ears, cid):
    return [a for a in ears.by["cmd/ack"] if a["command_id"] == cid]


def test_只读命令():
    assert "mapping_preview" in READ_KINDS


async def test_能力_回执原样带服务给的_加上在不在起(tmp_path):
    m = 假录包()
    broker, c, ears, rt = await _台(tmp_path, m)
    assert "mapping_preview" in ears.by["capabilities"][-1]["tasks"]
    await rt._on_cmd(_cmd("mapping_preview", {"since": 4}, "p1", c))
    await rt._on_cmd(_cmd("mapping_preview", {}, "p2", c))
    await broker.drain()
    a = _acks(ears, "p1")[-1]
    assert a["result"] == "accepted"
    assert a["data"] == {"live": True, "seq": 5, "recording": False, "starting": False}
    assert m.asked == [4, 0], "since 不给按 0"
    await rt.close()


@pytest.mark.parametrize("bad", [-1, "3", 1.5, True])
async def test_since_不像话_拒(tmp_path, bad):
    m = 假录包()
    broker, c, ears, rt = await _台(tmp_path, m)
    await rt._on_cmd(_cmd("mapping_preview", {"since": bad}, "b1", c))
    await broker.drain()
    assert _acks(ears, "b1")[-1]["reason"].startswith("payload")
    assert m.asked == []
    await rt.close()


async def test_不进幂等记录(tmp_path):
    broker, c, ears, rt = await _台(tmp_path, 假录包())
    await rt._on_cmd(_cmd("mapping_preview", {}, "i1", c))
    await rt._on_cmd(_cmd("mapping_preview", {}, "i1", c))
    await broker.drain()
    assert [a["result"] for a in _acks(ears, "i1")] == ["accepted", "accepted"], \
        "每 3 s 一条:记进幂等记录一天几万行"
    await rt.close()


async def test_老服务不报能力_来了回不支持(tmp_path):
    broker, c, ears, rt = await _台(tmp_path, 老录包())
    assert "mapping_preview" not in ears.by["capabilities"][-1]["tasks"]
    assert "mapping_trail" in ears.by["capabilities"][-1]["tasks"]
    await rt._on_cmd(_cmd("mapping_preview", {}, "o1", c))
    await broker.drain()
    assert _acks(ears, "o1")[-1]["reason"] == "unsupported"
    await rt.close()


async def test_服务读快照炸了_回拒绝不炸代理(tmp_path):
    m = 假录包()

    def 炸(since):
        raise OSError("盘坏了")
    m.preview = 炸
    broker, c, ears, rt = await _台(tmp_path, m)
    await rt._on_cmd(_cmd("mapping_preview", {}, "e1", c))
    await broker.drain()
    assert _acks(ears, "e1")[-1]["reason"].startswith("read_failed")
    await rt.close()



async def test_在录时带上预览进程在不在(tmp_path):
    """内审应修 5:预览进程起来之后才退的,开录时记不下原因,手机一直说「等第一张」。"""
    m = 假录包()
    m.recording, m.alive = True, False
    broker, c, ears, rt = await _台(tmp_path, m)
    await rt._on_cmd(_cmd("mapping_preview", {}, "a1", c))
    await broker.drain()
    assert _acks(ears, "a1")[-1]["data"]["preview_running"] is False
    await rt.close()


async def test_服务读快照抛别的错_也回拒绝(tmp_path):
    """内审应修 6:原来只接 OSError,别的错这条命令没有回执、站点等到超时。"""
    m = 假录包()

    def 炸(since):
        raise TypeError("cannot unpack")
    m.preview = 炸
    broker, c, ears, rt = await _台(tmp_path, m)
    await rt._on_cmd(_cmd("mapping_preview", {}, "e2", c))
    await broker.drain()
    assert _acks(ears, "e2")[-1]["reason"].startswith("read_failed: TypeError")
    await rt.close()
