"""W11a 头尾方向闸:前后雷达合并(W09i)之前,只有「狗头为前」才自己走。调过头尾、不知道:不收 goto /
巡检、跑着的当场中止、狗停下、发事件、能力里报;遥控照常;导航桥自己也停。"""

from __future__ import annotations

import asyncio
import json

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities, Command
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics
from d1max_contract.transport import Message
from d1max_patrol.protocol.nav_types import Pose

REG = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                   expires_at=10**13)
T = Topics(site_id="s", robot_id="r")


class 钟:
    def __init__(self):
        self.ms = 1_000_000
        self.mono = 50.0

    def __call__(self):
        return self.ms


class 耳朵:
    def __init__(self):
        self.by: dict[str, list] = {}

    async def __call__(self, m):
        self.by.setdefault(T.parse(m.topic)[2], []).append(json.loads(m.payload))


def _goto(cid, c, x=3.0):
    cmd = Command(command_id=cid, task_id=f"t-{cid}", kind="goto", issued_at=c.ms,
                  expires_at=c.ms + 60_000, control_epoch=1,
                  payload={"target": {"schema": "1.0", "map_id": "m", "map_version": "1",
                                      "frame_id": "map", "x": x, "y": 0.0, "yaw": 0.0}})
    return Message(T.cmd, json.dumps(cmd.to_wire()).encode(), 1, False)


async def _跑(rt, broker, r, c, n=10):
    for _ in range(n):
        await rt.step(0.1)
        r.tick(0.1)
        c.ms += 100
        c.mono += 0.1
        await asyncio.sleep(0.005)
    await broker.drain()


async def _台(tmp_path, head="head"):
    broker, c = MemoryBroker(), 钟()
    r = SimRobot(now_ms=c)
    r.inject_head(head)
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono)
    await rt.start()
    await broker.drain()
    return broker, c, r, ears, rt


async def test_起来就是狗尾为前_不收goto_能力里报(tmp_path):
    broker, c, r, ears, rt = await _台(tmp_path, head="tail")
    try:
        assert Capabilities.from_wire(ears.by["capabilities"][-1]).tasks["head"] == \
            {"direction": "tail"}
        await rt._on_cmd(_goto("g1", c))
        await broker.drain()
        ack = ears.by["cmd/ack"][-1]
        assert ack["result"] == "rejected" and ack["reason"] == "head_not_forward"
    finally:
        await rt.close()


async def test_跑着的时候调过头尾_当场中止_狗停_发事件_调回来又能派(tmp_path):
    broker, c, r, ears, rt = await _台(tmp_path)
    try:
        await rt._on_cmd(_goto("g1", c, x=8.0))
        await _跑(rt, broker, r, c, 15)
        assert abs(r.speed[0]) > 0.1, "前提:在走"
        r.inject_head("tail")
        await _跑(rt, broker, r, c, 15)
        assert abs(r.speed[0]) < 1e-9, "狗停下了"
        assert rt.processor.current is None or rt.processor.current.done
        ends = [e for e in ears.by["event"] if e["kind"].startswith("task_")
                and e["data"].get("task_id") == "t-g1" and e["kind"] != "task_progress"]
        assert ends and ends[-1]["kind"] == "task_aborted" \
            and ends[-1]["data"].get("reason") == "head_not_forward", ends[-1:]
        ev = [e for e in ears.by["event"] if e["kind"] == "head_changed"]
        assert ev and ev[-1]["data"] == {"head": "tail", "previous": "head"}
        assert Capabilities.from_wire(ears.by["capabilities"][-1]).tasks["head"] == \
            {"direction": "tail"}
        r.inject_head("head")
        await _跑(rt, broker, r, c, 3)
        await rt._on_cmd(_goto("g2", c))
        await broker.drain()
        assert ears.by["cmd/ack"][-1]["result"] == "accepted"
    finally:
        await rt.close()


async def test_导航桥自己也停_不等运行时(tmp_path):
    from d1max_agent.assembly import build_engine
    from d1max_patrol.protocol.nav_types import NavStatus
    c = 钟()
    r = SimRobot(now_ms=c)
    await r.connect()
    await r.acquire_control()
    parts = build_engine(r, runs_root=tmp_path, now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=Pose.from_xy_yaw(0, 0, 0))
    nav = parts.nav
    await nav.connect()
    await nav.goto(Pose.from_xy_yaw(5.0, 0.0))
    for _ in range(6):
        await nav.step(0.1)
        r.tick(0.1)
        c.ms += 100
    assert (await nav.nav_status()) is NavStatus.ACTIVE
    r.inject_head("unknown")
    await nav.step(0.1)
    assert (await nav.nav_status()) is NavStatus.FAILED
    await parts.engine.aclose()
