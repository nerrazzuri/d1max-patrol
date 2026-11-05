"""站立、趴下、蹲下(POC 第 1 项):站点发 ``posture``,代理自己把关 —— 有任务在跑、急停、叫停栅栏、
翻倒抱起、桩上都不执行;能力里报这台能做哪几种;做完回执里带做成的那一种。真代理 + 仿真狗。"""

from __future__ import annotations

from test_deter import _台
from test_runtime_maps import _cmd

from nexwar_contract.hal import MotionStatus
from nexwar_contract.messages import MapPose


async def _姿(rt, broker, ears, mode, cid, c):
    await rt._on_cmd(_cmd("posture", {"mode": mode}, cid, c))
    await broker.drain()
    return ears.by["cmd/ack"][-1]


async def test_能力里报能做哪几种_趴下蹲下站起都做到(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    try:
        assert ears.by["capabilities"][-1]["tasks"]["posture"] == {
            "modes": ["stand", "lie", "crawl"]}
        ack = await _姿(rt, broker, ears, "lie", "p1", c)
        assert ack["result"] == "accepted" and ack["data"] == {"mode": "lie"}, ack
        assert await dog.motion_status() is MotionStatus.LYING
        ack = await _姿(rt, broker, ears, "crawl", "p2", c)
        assert ack["result"] == "accepted" and ack["data"] == {"mode": "crawl"}
        assert await dog.motion_status() is MotionStatus.STANDING, "蹲着:不是趴着,也不能走"
        ack = await _姿(rt, broker, ears, "stand", "p3", c)
        assert ack["result"] == "accepted"
        assert await dog.motion_status() is MotionStatus.READY
    finally:
        await rt.close()


async def test_有任务在跑_不让换姿势_说是哪个任务(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    try:
        target = MapPose(map_id="m", map_version="1", frame_id="map", x=30.0, y=0.0,
                         yaw=0.0).to_wire()
        await rt._on_cmd(_cmd("goto", {"target": target}, "g1", c))
        await broker.drain()
        ack = await _姿(rt, broker, ears, "lie", "p1", c)
        assert ack["result"] == "rejected" and ack["reason"].startswith("busy"), ack
        assert "goto-g1" in ack["reason"]
        assert await dog.motion_status() is MotionStatus.READY, "没趴"
    finally:
        await rt.close()


async def test_急停时不执行_载荷不对拒(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    try:
        await dog.emergency_stop(True)
        ack = await _姿(rt, broker, ears, "lie", "p1", c)
        assert ack["result"] == "rejected" and ack["reason"].startswith("estop"), ack
        await dog.emergency_stop(False)
        for i, bad in enumerate(("sit", "", 3)):
            ack = await _姿(rt, broker, ears, bad, f"b{i}", c)
            assert ack["result"] == "rejected" and ack["reason"].startswith("payload"), bad
    finally:
        await rt.close()


async def test_叫停栅栏立着_不执行(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    try:
        rt.processor._fence = object()
        ack = await _姿(rt, broker, ears, "lie", "p1", c)
        assert ack["result"] == "rejected" and ack["reason"] == "halting", ack
    finally:
        rt.processor._fence = None
        await rt.close()


async def test_重投同一条_回原来的回执_不再做一次(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    try:
        assert (await _姿(rt, broker, ears, "lie", "p1", c))["result"] == "accepted"
        await dog.set_motion_mode("stand")
        assert (await _姿(rt, broker, ears, "lie", "p1", c))["result"] == "duplicate"
        assert await dog.motion_status() is MotionStatus.READY, "重投不再趴一次"
    finally:
        await rt.close()


async def test_POC1105_遥控中停着_能换姿势_推着杆不换(tmp_path):
    """用户 11-05:站起、趴下、蹲下放进遥控页 —— 遥控这一个「任务」不挡换姿势(人在看着);
    推着杆的时候不换(先松杆);别的任务照旧不让。"""
    import json

    from test_runtime import T

    from nexwar_contract.messages import Command
    from nexwar_contract.teleop import TeleopFrame, teleop_grant_payload
    from nexwar_contract.transport import Message
    broker, c, ears, dog, rt = await _台(tmp_path)
    rt._video_live = lambda: True
    try:
        g = Command(command_id="g1", task_id="teleop-1", kind="teleop", issued_at=c.ms,
                    expires_at=c.ms + 60_000, control_epoch=1, priority=100,
                    payload=teleop_grant_payload(lease_epoch=1, operator="gina",
                                                 lease_ttl_ms=5000))
        await rt._on_cmd(Message(T.cmd, json.dumps(g.to_wire()).encode(), 1, False))
        for _ in range(3):
            await rt.step(0.1)
            dog.tick(0.1)
            c.ms += 100
            c.mono += 0.1
        assert rt.processor.current is not None and rt.processor.current.kind == "teleop"
        f = TeleopFrame(lease_epoch=1, seq=1, sent_at=5, ttl_ms=300, vx=0.3, wz=0.0)
        await rt._on_teleop(Message(T.teleop, json.dumps(f.to_wire()).encode(), 0, False))
        await rt.step(0.1)
        dog.tick(0.1)
        c.ms += 100
        c.mono += 0.1
        ack = await _姿(rt, broker, ears, "lie", "p1", c)
        assert ack["result"] == "rejected" and "松" in ack["reason"], ack
        for _ in range(10):                                # 不推了(帧过期)、停稳
            await rt.step(0.1)
            dog.tick(0.1)
            c.ms += 100
            c.mono += 0.1
        ack = await _姿(rt, broker, ears, "lie", "p2", c)
        assert ack["result"] == "accepted", ack
        assert await dog.motion_status() is MotionStatus.LYING
    finally:
        await rt.close()
