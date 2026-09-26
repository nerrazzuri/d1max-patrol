"""录包时的轨迹(W00c6h):录包期间狗按里程记一条轨迹(以录包起点为原点),站点经 ``mapping_trail``
取新增的点给手机画 —— 录包时看得见哪儿走过了。"""

from __future__ import annotations

import math

import pytest
from test_runtime_maps import REG, T, _cmd, _跑, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.mapping_trail import MAX_POINTS, MappingTrail
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_patrol.protocol.nav_types import Pose


def test_按距离和转角抽稀_以起点为原点():
    t = MappingTrail()
    t.feed(10.0, 5.0, math.pi / 2)                  # 起点:朝北
    t.feed(10.0, 5.1, math.pi / 2)                  # 只挪了 0.1 m:不记
    t.feed(10.0, 5.4, math.pi / 2)                  # 0.4 m:记
    t.feed(10.0, 5.4, math.pi / 2 + 0.1)            # 转了 6°:不记
    t.feed(10.0, 5.4, math.pi / 2 + 0.3)            # 17°:记
    d = t.since(0)
    assert d["total"] == 3 and d["since"] == 0 and d["full"] is False
    (x0, y0), (x1, y1), (x2, y2) = d["points"]
    assert (x0, y0) == (0.0, 0.0)
    assert (x1, y1) == (0.4, 0.0), "起点朝北:往北走是起点坐标系的 +x"
    assert (x2, y2) == (0.4, 0.0)
    assert t.since(2)["points"] == [[0.4, 0.0]] and t.since(3)["points"] == []
    assert t.since(99)["points"] == [] and t.since(99)["total"] == 3


def test_清空_上限():
    t = MappingTrail()
    for i in range(MAX_POINTS + 10):
        t.feed(i * 0.5, 0.0, 0.0)
    d = t.since(0)
    assert d["total"] == MAX_POINTS and d["full"] is True
    t.reset()
    t.feed(3.0, 3.0, 0.0)
    assert t.since(0) == {"points": [[0.0, 0.0]], "since": 0, "total": 1, "full": False}


class 假录包:
    def __init__(self):
        self.recording = False
        self.last_bag = ""


async def _台(tmp_path, *, mapper=True):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    dog = SimRobot(now_ms=c)
    m = 假录包() if mapper else None
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono, mapper=m)
    await rt.start()
    await broker.drain()
    return broker, c, ears, dog, rt, m


def _ack(ears, cid):
    return [a for a in ears.by["cmd/ack"] if a["command_id"] == cid][-1]


async def test_录包时记轨迹_since只回新增_停了不再记_再录清空(tmp_path):
    broker, c, ears, dog, rt, m = await _台(tmp_path)
    assert "mapping_trail" in ears.by["capabilities"][-1]["tasks"]
    dog.teleport(5.0, 5.0, 0.0)
    await _跑(rt, broker, n=2, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "q0", c))
    await broker.drain()
    d = _ack(ears, "q0")["data"]
    assert d["recording"] is False and d["total"] == 0, "没在录:空的"
    m.recording = True
    for i in range(1, 6):                                       # 每拍往东挪 0.5 m
        dog.teleport(5.0 + i * 0.5, 5.0, 0.0)
        await _跑(rt, broker, n=1, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "q1", c))
    await broker.drain()
    d = _ack(ears, "q1")["data"]
    assert d["recording"] is True and d["points"][0] == [0.0, 0.0], "录包起点是原点"
    assert d["total"] == 5 and d["points"][-1] == [2.0, 0.0]
    n = d["total"]
    dog.teleport(8.5, 5.0, 0.0)
    await _跑(rt, broker, n=1, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {"since": n}, "q2", c))
    await broker.drain()
    d2 = _ack(ears, "q2")["data"]
    assert d2["points"] == [[3.0, 0.0]] and d2["since"] == n
    m.recording = False
    dog.teleport(20.0, 5.0, 0.0)
    await _跑(rt, broker, n=2, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "q3", c))
    await broker.drain()
    d3 = _ack(ears, "q3")["data"]
    assert d3["recording"] is False and d3["total"] == n + 1, \
        "停了:轨迹留着(看最后录的那一趟),不再记"
    m.recording = True
    await _跑(rt, broker, n=1, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "q4", c))
    await broker.drain()
    assert _ack(ears, "q4")["data"]["points"] == [[0.0, 0.0]], "再录:从头记"
    await rt.close()


@pytest.mark.parametrize("bad", [-1, "3", 1.5, True])
async def test_since不像话_拒(tmp_path, bad):
    broker, c, ears, dog, rt, m = await _台(tmp_path)
    await rt._on_cmd(_cmd("mapping_trail", {"since": bad}, "b1", c))
    await broker.drain()
    assert _ack(ears, "b1")["reason"].startswith("payload"), _ack(ears, "b1")
    await rt.close()


async def test_里程不新鲜不记_没有录包能力不报(tmp_path):
    broker, c, ears, dog, rt, m = await _台(tmp_path)
    m.recording = True
    dog.inject_loc_lost(True)                                  # 里程读不到(valid=False)
    dog.teleport(3.0, 0.0, 0.0)
    await _跑(rt, broker, n=2, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "v1", c))
    await broker.drain()
    assert _ack(ears, "v1")["data"]["total"] == 0
    await rt.close()
    broker, c, ears, dog, rt, m = await _台(tmp_path / "x", mapper=False)
    assert "mapping_trail" not in ears.by["capabilities"][-1]["tasks"]
    await rt.close()


async def test_不进幂等记录(tmp_path):
    broker, c, ears, dog, rt, m = await _台(tmp_path)
    m.recording = True
    await _跑(rt, broker, n=1, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "i1", c))
    await rt._on_cmd(_cmd("mapping_trail", {}, "i1", c))
    await broker.drain()
    assert [a["result"] for a in ears.by["cmd/ack"] if a["command_id"] == "i1"] == \
        ["accepted", "accepted"], "每 2 s 一条:记进幂等记录一天几万行"
    await rt.close()
