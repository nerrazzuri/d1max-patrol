"""异常受力检测(W26,决策 52),狗这一头:判翻倒、被抱起来、被撞;代理翻倒停车软急停、抱起来停车、
撞了只报;翻倒、抱起来时不收会让狗动的任务;能力里报状态(站点按它对账)。"""

from __future__ import annotations

import json
import math

import pytest
from test_runtime import REG, T, 站点耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.force import ForceConfig, ForceWatch
from d1max_agent.runtime import AgentRuntime
from d1max_contract.hal import ImuSample
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities, Command
from d1max_contract.transport import Message
from d1max_patrol.protocol.nav_types import Pose


def _s(roll=0.0, pitch=0.0, shock=0.0, load=100.0, valid=True):
    return ImuSample(stamp_ms=0, roll=roll, pitch=pitch, shock_g=shock, load=load, valid=valid)


def _喂(w, s, t0, secs, *, standing=True, dt=100):
    out = []
    for t in range(t0, t0 + int(secs * 1000), dt):
        out += w.feed(s, t, standing=standing)
    return out, t0 + int(secs * 1000)


def test_歪过60度半秒才算翻倒_扶正稳3秒才回ok():
    w = ForceWatch()
    got, t = _喂(w, _s(roll=1.2), 0, 0.4)
    assert not got and w.state == "ok", "不到半秒:不算(走崎岖路晃一下)"
    got, t = _喂(w, _s(roll=0.0), t, 0.5)
    got, t = _喂(w, _s(pitch=-1.3), t, 0.6)
    assert [f.kind for f in got] == ["flipped"] and w.state == "flipped"
    assert got[0].data["pitch_deg"] == pytest.approx(-74.5, abs=0.1)
    got, t = _喂(w, _s(), t, 2.5)
    assert not got and w.state == "flipped", "扶正不到 3 秒:还算翻着"
    got, t = _喂(w, _s(), t, 1.0)
    assert [f.kind for f in got] == ["upright"] and w.state == "ok"


def test_被抱起来_要先有站着的承重基线_承重掉到四分之一以下半秒():
    w = ForceWatch()
    got, t = _喂(w, _s(load=10.0), 0, 1.0)
    assert not got and not w.checks["lift"], "刚起来、没有基线:不判"
    got, t = _喂(w, _s(load=100.0), t, 3.0)
    assert w.checks["lift"]
    got, t = _喂(w, _s(load=10.0), t, 0.4)
    assert not got
    got, t = _喂(w, _s(load=10.0), t, 0.2)
    assert [f.kind for f in got] == ["lifted"] and got[0].data["baseline"] == 100.0
    got, t = _喂(w, _s(load=60.0), t, 2.1)
    assert [f.kind for f in got] == ["landed"] and w.state == "ok"
    assert not w.checks["lift"], "放下了:基线重攒"


def test_趴着承重掉了不算抱起来_读不到承重不判():
    w = ForceWatch()
    _, t = _喂(w, _s(), 0, 3.0)
    got, t = _喂(w, _s(load=5.0), t, 2.0, standing=False)
    assert not got and w.state == "ok", "运控说没站着(趴下了):不判"
    w2 = ForceWatch()
    _, t = _喂(w2, _s(load=None), 0, 5.0)
    assert w2.checks == {"flip": True, "lift": False, "bump": True}


def test_撞了一下_过门槛报一次_30秒内不重报_翻着不报撞():
    w = ForceWatch()
    got, t = _喂(w, _s(shock=1.4), 0, 0.1)
    assert not got
    got, t = _喂(w, _s(shock=2.0), t, 0.1)
    assert [f.kind for f in got] == ["bump"] and got[0].data["shock_g"] == 2.0
    got, t = _喂(w, _s(shock=3.0), t, 29.0)
    assert not got, "30 秒内不重报"
    got, t = _喂(w, _s(shock=3.0), t, 1.1)
    assert [f.kind for f in got] == ["bump"]
    w2 = ForceWatch()
    _, t = _喂(w2, _s(roll=1.5), 0, 0.6)
    got, t = _喂(w2, _s(roll=1.5, shock=5.0), t, 40)
    assert not got, "翻倒了再磕碰:不另报撞"


def test_数据无效什么都不判_状态不变():
    w = ForceWatch()
    _, t = _喂(w, _s(roll=1.5), 0, 0.6)
    got, t = _喂(w, _s(valid=False), t, 10)
    assert not got and w.state == "flipped"
    assert w.feed(None, t, standing=True) == []


def test_门槛文件_没有用缺省_写了的盖上_写错了报错(tmp_path):
    assert ForceConfig.load(tmp_path / "没有") == ForceConfig()
    p = tmp_path / "force.json"
    p.write_text(json.dumps({"bump_g": 2.5, "flip_hold_s": 1}))
    c = ForceConfig.load(p)
    assert c.bump_g == 2.5 and c.flip_hold_s == 1.0 and c.lift_load_ratio == 0.25
    for bad in ({"bump": 2}, {"bump_g": -1}, {"bump_g": "2"}, {"bump_g": True}, [1]):
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            ForceConfig.load(p)


# ------------------------------------------------------------ 代理


@pytest.fixture
async def 狗(tmp_path):
    broker, c = MemoryBroker(), 钟()
    r = SimRobot(now_ms=c, imu=True)
    ears = 站点耳朵()
    site = MemoryTransport(broker, "site")
    await site.connect()
    await site.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="boot-1", home=Pose.from_xy_yaw(0.0, 0.0),
                      monotonic=lambda: c.mono)
    rt._video_live = lambda: True
    emitted = []
    real_emit = rt.events.emit

    def emit(kind, data, **kw):
        emitted.append(kind)
        return real_emit(kind, data, **kw)
    rt.events.emit = emit
    await rt.start()

    async def 走(secs):
        for _ in range(int(secs * 10)):
            await rt.step(0.1)
            r.tick(0.1)
            c.advance(0.1)
        await broker.drain()

    async def 遥控(n):
        from d1max_contract.teleop import teleop_grant_payload
        g = Command(command_id=f"g{n}", task_id=f"teleop-{n}", kind="teleop", issued_at=c.ms,
                    expires_at=c.ms + 60_000, control_epoch=1, priority=100,
                    payload=teleop_grant_payload(lease_epoch=n, operator="gina",
                                                 lease_ttl_ms=5000))
        await rt._on_cmd(Message(T.cmd, json.dumps(g.to_wire()).encode(), 1, False))
        await 走(0.3)

    def 事件():
        return emitted

    def 能力():
        return Capabilities.from_wire(ears.by_topic["capabilities"][-1]).tasks.get("force")

    yield rt, r, 走, 遥控, 事件, 能力
    await rt.close()


async def test_代理_翻倒了_撤任务停车软急停_报状态_不收会动的任务(狗):
    rt, r, 走, 遥控, 事件, 能力 = 狗
    await 走(3)
    assert 能力()["state"] == "ok" and 能力()["checks"]["flip"]
    await 遥控(1)
    assert rt.processor.current is not None and rt.processor.current.kind == "teleop"
    r.inject_imu(roll=math.radians(80))
    await 走(1)
    assert rt.force.state == "flipped" and await r.estop_status(), "软急停了"
    assert "force_flipped" in 事件()
    assert rt.processor.current is None or rt.processor.current.kind != "teleop"
    assert 能力()["state"] == "flipped" and "since_ms" in 能力()
    await 遥控(2)
    assert rt.processor.current is None or rt.processor.current.task_id != "teleop-2", "不收"
    r.inject_imu(roll=0.0)
    await 走(3.5)
    assert rt.force.state == "ok" and 能力()["state"] == "ok"
    assert await r.estop_status(), "扶正了急停也不自己解(决策 52:人工解除)"


async def test_代理_被抱起来_撤任务停车_不急停_放下了能再动(狗):
    rt, r, 走, 遥控, 事件, 能力 = 狗
    await 走(3)
    await 遥控(1)
    r.inject_imu(load=5.0)
    await 走(1)
    assert rt.force.state == "lifted" and not await r.estop_status()
    assert "force_lifted" in 事件() and 能力()["state"] == "lifted"
    assert rt.processor.current is None or rt.processor.current.kind != "teleop"
    assert rt._force_gate().startswith("lifted")
    r.inject_imu(load=100.0)
    await 走(2.5)
    assert rt.force.state == "ok" and rt._force_gate() == ""
    await 遥控(3)
    assert rt.processor.current is not None and rt.processor.current.task_id == "teleop-3"


async def test_代理_撞了一下只报_任务照跑(狗):
    rt, r, 走, 遥控, 事件, 能力 = 狗
    await 走(3)
    await 遥控(1)
    r.inject_imu(shock_g=2.2)
    await 走(0.2)
    assert "force_bump" in 事件() and rt.force.state == "ok"
    assert rt.processor.current is not None and rt.processor.current.kind == "teleop"


async def test_代理_没有imu的狗不判_能力里不报(tmp_path):
    rt = AgentRuntime(transport=MemoryTransport(MemoryBroker(), "dog"), registration=REG,
                      hal=SimRobot(now_ms=钟()), store_dir=tmp_path / "a", now_ms=钟(),
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0))
    assert rt.force is None and "force" not in rt._extra_tasks() and rt._force_gate() == ""
