"""``patrol``(W00c2a):整趟 Mission 在引擎上跑;每个航点一条 ``patrol_waypoint`` 事件;
终态映射同 goto。夹具与驱动同 ``test_engine_goto.py``。"""

from __future__ import annotations

import asyncio

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.assembly import build_engine
from d1max_agent.events import EventBook
from d1max_agent.tasks.patrol import PatrolTask
from d1max_contract.geometry import Pose
from d1max_contract.messages import TaskState
from d1max_contract.mission import Action, Mission, MissionWaypoint, Policy


class 钟:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000
        self.mono = 100.0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))
        self.mono += dt_s


@pytest.fixture
async def 台子(tmp_path):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    parts = build_engine(r, runs_root=tmp_path / "runs", now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=Pose.from_xy_yaw(0.0, 0.0))
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    yield c, r, parts, book
    await parts.engine.aclose()


async def _跑(c, r, parts, task, n, dt=0.1):
    for _ in range(n):
        await task.step(dt)
        await parts.nav.step(dt)
        await parts.device.step(dt)
        r.tick(dt)
        c.advance(dt)
        for _ in range(4):
            await asyncio.sleep(0)
        if task.done:
            return


def _任务(*pts, policy=None, actions=()):
    return Mission(mission="loop", map_id="m", policy=policy or Policy(),
                   waypoints=tuple(MissionWaypoint(name=f"p{i}", pose=Pose.from_xy_yaw(x, y),
                                                   actions=actions)
                                   for i, (x, y) in enumerate(pts)))


def _点事件(book):
    return [e.data for e in book.pending() if e.kind == "patrol_waypoint"]


async def test_三个点走完_每点一条事件_done(台子):
    c, r, parts, book = 台子
    t = PatrolTask(task_id="t1", mission=_任务((1.0, 0.0), (1.0, 1.0), (0.0, 1.0),
                                              actions=(Action(type="dwell", seconds=0.5),)),
                   parts=parts, events=book, now_ms=c)
    await t.start()
    await _跑(c, r, parts, t, 1500)
    assert t.state is TaskState.DONE, (t.state, t.detail)
    got = _点事件(book)
    assert [(g["index"], g["name"], g["ok"]) for g in got] == [
        (0, "p0", True), (1, "p1", True), (2, "p2", True)]
    o = await r.odometry()
    assert abs(o.x) < 0.2 and abs(o.y - 1.0) < 0.2


async def test_中途abort_aborted_停下(台子):
    c, r, parts, book = 台子
    t = PatrolTask(task_id="t1", mission=_任务((6.0, 0.0), (6.0, 6.0)), parts=parts,
                   events=book, now_ms=c)
    await t.start()
    await _跑(c, r, parts, t, 30)
    await t.abort("operator")
    await _跑(c, r, parts, t, 60)
    assert t.state is TaskState.ABORTED and await r.stopped()


async def test_到不了的点被跳过_failed并列出点名(台子, monkeypatch):
    """引擎的航点超时按真实时间算,测试的假钟推不动它;改让 HAL 对去 p1 的速度命令一律
    拒收(导航 Failed),引擎按 retry_then_skip 跳过 p1、接着走 p2。"""
    from d1max_contract.hal import VelocityResult
    c, r, parts, book = 台子
    real = r.set_velocity

    async def 拒去远点(cmd):
        tgt = parts.nav._target
        if tgt is not None and tgt.position.x > 10.0:
            return VelocityResult(0.0, 0.0, clamped=False, rejected=True, reason="blocked")
        return await real(cmd)

    monkeypatch.setattr(r, "set_velocity", 拒去远点)
    pol = Policy(waypoint_retry=0)
    t = PatrolTask(task_id="t1", mission=_任务((0.5, 0.0), (40.0, 0.0), (0.5, 0.5), policy=pol),
                   parts=parts, events=book, now_ms=c)
    await t.start()
    await _跑(c, r, parts, t, 2000)
    assert t.state is TaskState.FAILED, (t.state, t.detail)
    assert "p1" in t.detail["reason"] and "1 个航点没到" in t.detail["reason"]
    assert [g["ok"] for g in _点事件(book)] == [True, False, True]


async def test_sim相机_带拍照动作的任务走得完_照片进归档(tmp_path):
    """任务包里常有拍照动作;sim 没有相机的话每个拍照点都失败,整趟必 failed。"""
    from d1max_agent.bridges.sim_media import PLACEHOLDER_JPEG, sim_media
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    parts = build_engine(r, runs_root=tmp_path / "runs", now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=Pose.from_xy_yaw(0.0, 0.0), media=sim_media(c))
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    t = PatrolTask(task_id="t1", mission=_任务((1.0, 0.0), actions=(
        Action(type="photo", camera="front"),)), parts=parts, events=book, now_ms=c)
    await t.start()
    await _跑(c, r, parts, t, 500)
    assert t.state is TaskState.DONE, (t.state, t.detail)
    shots = list((tmp_path / "runs").rglob("*.jpg"))
    assert shots and shots[0].read_bytes() == PLACEHOLDER_JPEG
    await parts.engine.aclose()
