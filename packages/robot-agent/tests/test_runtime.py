"""运行时:起来就发 retained capabilities 与 status;断线 LWT;重连第一条 reconcile;
telemetry 每秒一条;命令经 ACL 守卫的 transport 进来、回执出去。"""

from __future__ import annotations

import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities, Reconcile, Status, Telemetry
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics

REG = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                   expires_at=10**12)
T = Topics(site_id="s", robot_id="r")


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))


class 站点耳朵:
    def __init__(self) -> None:
        self.by_topic: dict[str, list[dict]] = {}
        self.order: list[str] = []

    async def __call__(self, m):
        kind = T.parse(m.topic)[2]
        self.by_topic.setdefault(kind, []).append(json.loads(m.payload))
        self.order.append(kind)


@pytest.fixture
async def 台子(tmp_path):
    broker = MemoryBroker()
    c = 钟()
    r = SimRobot(now_ms=c)
    ears = 站点耳朵()
    site = MemoryTransport(broker, "site")
    await site.connect()
    await site.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="boot-1")
    return broker, c, r, ears, rt, site


async def test_起来就发能力与状态_均retained(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    caps = Capabilities.from_wire(ears.by_topic["capabilities"][0])
    assert caps.robot_id == "r" and list(caps.tasks) == ["goto"]
    assert caps.tasks["goto"]["max_speed_mps"] == r.max_vx
    assert caps.adapter.startswith("sim/")
    st = Status.from_wire(ears.by_topic["status"][-1])
    assert st.online is True and st.boot_id == "boot-1" and st.ready.ok and st.task is None
    late = MemoryTransport(broker, "late")
    await late.connect()
    got: list[str] = []

    async def 收(m):
        got.append(T.parse(m.topic)[2])
    await late.subscribe(f"{T.prefix}/#", 收)
    await broker.drain()
    assert sorted(got) == ["capabilities", "status"], "两份都 retained"


async def test_断线LWT改写为offline_重连第一条是reconcile(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    ears.order.clear()
    broker.disconnect("dog")
    await broker.drain()
    st = Status.from_wire(ears.by_topic["status"][-1])
    assert st.online is False and st.boot_id == "boot-1"
    assert rt.online is False
    ears.order.clear()
    await rt.transport.connect()
    await broker.drain()
    assert ears.order[0] == "reconcile"
    rc = Reconcile.from_wire(ears.by_topic["reconcile"][-1])
    assert rc.boot_id == "boot-1" and rc.task is None
    assert Status.from_wire(ears.by_topic["status"][-1]).online is True


async def test_telemetry每秒一条(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    for _ in range(25):
        await rt.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
    await broker.drain()
    tele = [Telemetry.from_wire(d) for d in ears.by_topic.get("telemetry", [])]
    assert 2 <= len(tele) <= 3
    assert tele[-1].pose is not None and tele[-1].pose.map_id == "m"
    assert tele[-1].battery_pct > 0


async def test_命令进来回执出去_越界主题不发(台子):
    broker, c, r, ears, rt, site = 台子
    await rt.start()
    await broker.drain()
    cmd = {"schema": "1.0", "command_id": "c1", "task_id": "t1", "kind": "dance",
           "issued_at": c(), "expires_at": c() + 60_000, "control_epoch": 1, "priority": 0,
           "offline_policy": "default", "precondition": None, "payload": {}}
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await broker.drain()
    ack = ears.by_topic["cmd/ack"][-1]
    assert ack["command_id"] == "c1" and ack["result"] == "rejected"
    with pytest.raises(PermissionError):
        await rt.transport.publish(Topics(site_id="s", robot_id="OTHER").status, b"x")
    with pytest.raises(PermissionError):
        await rt.transport.subscribe("site/+/robot/+/cmd", ears)


async def test_断线期间事件攒着_重连后先reconcile再补发(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    broker.disconnect("dog")
    rt.events.emit("task_progress", {"task_id": "t", "distance_m": 1.0})
    rt.events.emit("task_progress", {"task_id": "t", "distance_m": 0.5})
    await rt.step(0.1)
    await broker.drain()
    assert "event" not in ears.by_topic
    ears.order.clear()
    await rt.transport.connect()
    await broker.drain()
    rc = Reconcile.from_wire(ears.by_topic["reconcile"][-1])
    assert (rc.unacked_from_seq, rc.unacked_to_seq) == (1, 2)
    assert ears.order.index("reconcile") < ears.order.index("event")
    assert [d["seq"] for d in ears.by_topic["event"]] == [1, 2]
    assert rt.events.unacked_range() == (0, 0)
