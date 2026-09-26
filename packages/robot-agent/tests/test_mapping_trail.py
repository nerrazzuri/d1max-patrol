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


def test_左边是正y_不镜像():
    """内审应修 3:起点朝北,往西(起点的左手边)走 2 m → (0, +2)。以前 y 取反(左右镜像)测试
    也是绿的。"""
    t = MappingTrail()
    for i in range(5):                                      # 一拍 0.5 m(一拍挪 1 m 以上算跳)
        t.feed(10.0 - i * 0.5, 5.0, math.pi / 2)
    assert t.since(0)["points"][-1] == [0.0, 2.0]


def test_坏里程不记_里程跳了接上():
    """内审小问题 1、2:NaN/无穷的里程不记(以前每拍一点,很快塞满、回执里是 NaN,手机解不开);
    一拍挪 1 m 以上(运控重置、里程归零)当跳了:轨迹在跳的地方接上,不画一条长直线、后面整段错位。"""
    t = MappingTrail()
    t.feed(0.0, 0.0, 0.0)
    t.feed(float("nan"), 0.0, 0.0)
    t.feed(0.0, float("inf"), 0.0)
    t.feed(0.5, 0.0, 0.0)
    t.feed(100.0, 50.0, 1.0)                                # 里程重置到了别处
    t.feed(100.0 + 0.5 * math.cos(1.0), 50.0 + 0.5 * math.sin(1.0), 1.0)   # 接着往前 0.5 m
    d = t.since(0)
    assert d["points"] == [[0.0, 0.0], [0.5, 0.0], [1.0, 0.0]], d
    assert d["jumps"] == 1


def test_清空_上限():
    t = MappingTrail()
    for i in range(MAX_POINTS + 10):
        t.feed(i * 0.5, 0.0, 0.0)
    d = t.since(0)
    assert d["total"] == MAX_POINTS and d["full"] is True
    e = t.epoch
    t.reset()
    t.feed(3.0, 3.0, 0.0)
    assert t.since(0) == {"points": [[0.0, 0.0]], "since": 0, "total": 1, "full": False,
                          "epoch": e + 1, "jumps": 0}


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


class 慢录包:
    """开录要一阵(ros2 bag 起来十几秒):``start`` 等放行才算录上。"""
    def __init__(self):
        import asyncio
        self.recording = False
        self.last_bag = ""
        self.gate = asyncio.Event()
        self.stop_gate = asyncio.Event()

    async def start(self, name):
        await self.gate.wait()
        self.recording, self.last_bag = True, name

    async def stop(self):
        await self.stop_gate.wait()
        self.recording = False


async def test_开录还没起来_报starting_起来当场清空换一趟(tmp_path):
    """内审应修 1:收下开录就回执,录包起来要十几秒。这段时间手机来问,以前回「没在录」+ 上一趟的
    轨迹,手机就不问了;而且「哪一趟」只靠点数猜。现在回 starting,开录成功当场清空、换一个 epoch。"""
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    dog = SimRobot(now_ms=c)
    m = 慢录包()
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono, mapper=m)
    await rt.start()
    rt.trail.feed(9.0, 9.0, 0.0)                               # 上一趟留下的
    old = rt.trail.epoch
    await rt._on_cmd(_cmd("mapping", {"action": "start", "name": "yard"}, "s1", c))
    await _跑(rt, broker, n=1, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "t1", c))
    await broker.drain()
    d = _ack(ears, "t1")["data"]
    assert d["starting"] is True and d["recording"] is False and d["epoch"] == old
    m.gate.set()
    await _跑(rt, broker, n=2, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "t2", c))
    await broker.drain()
    d = _ack(ears, "t2")["data"]
    assert d["starting"] is False and d["recording"] is True
    assert d["epoch"] == old + 1 and d["points"][0] == [0.0, 0.0] and d["total"] == 1, \
        "开录成功当场清空(一趟一个 epoch)"
    await rt._on_cmd(_cmd("mapping", {"action": "stop"}, "s2", c))
    await _跑(rt, broker, n=1, r=dog, c=c)
    await rt._on_cmd(_cmd("mapping_trail", {}, "t3", c))
    await broker.drain()
    assert _ack(ears, "t3")["data"]["starting"] is False, "在停不是在起"
    m.stop_gate.set()
    await _跑(rt, broker, n=2, r=dog, c=c)
    await rt.close()
