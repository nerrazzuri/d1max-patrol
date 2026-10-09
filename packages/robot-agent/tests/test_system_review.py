"""2026-10-09 系统审查(跨功能)狗这一头的回归:S01 受力锁住后任何运动出口都不许再发速度;S02 先落盘
再做要等的安全动作;S03 全狗限速管住遥控。审查报告的复现断言的是错的行为,这里断言对的。"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_runtime import REG, T, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.force import ForceWatch
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Command
from d1max_contract.transport import Message
from d1max_patrol.protocol.nav_types import Pose


@pytest.fixture
async def 台(tmp_path, monkeypatch):
    sent: list[tuple[float, float]] = []
    real = SimRobot.set_velocity

    async def 记(self, cmd):                              # 真正到了 HAL 的(闸后面)
        sent.append((cmd.vx, cmd.wz))
        return await real(self, cmd)
    monkeypatch.setattr(SimRobot, "set_velocity", 记)
    broker, c = MemoryBroker(), 钟()
    r = SimRobot(now_ms=c, imu=True)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono)
    rt._video_live = lambda: True
    await rt.start()

    async def 走(secs):
        for _ in range(int(secs * 10)):
            await rt.step(0.1)
            r.tick(0.1)
            c.advance(0.1)

    async def 命令(kind, payload, cid):
        cmd = Command(command_id=cid, task_id=f"{kind}-{cid}", kind=kind, issued_at=c.ms,
                      expires_at=c.ms + 60_000, control_epoch=1, priority=100, payload=payload)
        await rt._on_cmd(Message(T.cmd, json.dumps(cmd.to_wire()).encode(), 1, False))

    async def 遥控(vx, n=1):
        from d1max_contract.teleop import teleop_grant_payload
        await 命令("teleop", teleop_grant_payload(lease_epoch=n, operator="gina",
                                                  lease_ttl_ms=5000), f"t{n}")
        await 走(0.3)
        task = rt.processor.current
        assert task is not None and task.kind == "teleop"
        task._cmd = (vx, 0.0, rt._mono_ms() + 60_000, 300)
        return task

    yield rt, r, 走, 命令, 遥控, sent
    await rt.close()


async def test_S01_撤任务一直失败_旧遥控还在_受力锁住后HAL收不到非零速度(台, monkeypatch):
    rt, r, 走, 命令, 遥控, sent = 台
    await 走(3)
    task = await 遥控(0.3)

    async def 撤不掉(*a):
        raise OSError("abort unavailable")
    monkeypatch.setattr(rt.processor, "abort_kinds", 撤不掉)
    await 走(0.3)
    assert any(vx > 0 for vx, _ in sent), "锁之前遥控在发"
    r.inject_imu(load=5.0)
    await 走(0.6)
    assert rt.force.state == "lifted" and rt.processor.current is task, "撤不掉:旧任务还在"
    sent.clear()
    task._cmd = (0.3, 0.2, rt._mono_ms() + 60_000, 300)
    await 走(1.0)
    assert not any(vx or wz for vx, wz in sent), sent
    assert "abort" in rt.force.owed and (await r.odometry()).vx == 0


async def test_S02_识别出危险先落盘_等撤任务的时候进程没了_重启照旧锁着(台, monkeypatch):
    rt, r, 走, 命令, 遥控, sent = 台
    await 走(3)
    rt.force.save()
    entered, blocked = asyncio.Event(), asyncio.Event()

    async def 卡住(*a):
        entered.set()
        await blocked.wait()
    monkeypatch.setattr(rt.processor, "abort_kinds", 卡住)
    r.inject_imu(load=5.0)
    job = asyncio.create_task(走(0.8))
    await entered.wait()
    disk = ForceWatch(path=rt.force._path)                # 这时候进程没了:重启读到的
    assert disk.state == "lifted" and {"abort", "stop"} <= disk.owed
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job


async def test_S03_全狗限速_遥控也不超(台):
    rt, r, 走, 命令, 遥控, sent = 台
    await 走(0.5)
    await 命令("speed_cap", {"max_speed_mps": 0.3, "ttl_s": 60}, "cap")
    await 遥控(0.5)
    sent.clear()
    await 走(0.5)
    assert sent and max(abs(vx) for vx, _ in sent) <= 0.3 + 1e-9, sent
    await 走(61)                                          # 限速过期:恢复
    await 遥控(0.5, n=2)
    sent.clear()
    await 走(0.3)
    assert max(abs(vx) for vx, _ in sent) > 0.3, "过期了不再压"
