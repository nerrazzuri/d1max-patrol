"""goto 跑在 MissionEngine 上(W00b 决定 2):契约不变,任务实现换成引擎。
SimRobot → 两个桥 → MissionEngine;EngineGotoTask 把引擎的 RunState 映成契约的 TaskState。"""

from __future__ import annotations

import asyncio
import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.assembly import build_engine
from d1max_agent.events import EventBook
from d1max_agent.tasks.engine_goto import EngineGotoTask
from d1max_contract.messages import MapPose, TaskState
from d1max_patrol.engine.machine import RunState
from d1max_patrol.protocol.nav_types import Pose


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


def _target(x, y):
    return MapPose(map_id="m", map_version="1", frame_id="map", x=x, y=y, yaw=0.0)


async def _跑(c, r, parts, task, n, dt=0.1):
    for _ in range(n):
        await task.step(dt)
        await parts.nav.step(dt)
        await parts.device.step(dt)
        r.tick(dt)
        c.advance(dt)
        for _ in range(4):                   # 引擎自己的协程要几个循环轮次才消化完一条命令
            await asyncio.sleep(0)
        if task.done:
            return


async def test_goto经引擎走到点_状态序列与归档(台子, tmp_path):
    c, r, parts, book = 台子
    seen: list[RunState] = []
    q = parts.engine.subscribe()
    t = EngineGotoTask(task_id="t1", target=_target(2.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, parts, t, 300)
    while not q.empty():
        seen.append(q.get_nowait().state)
    assert t.state is TaskState.DONE, (t.state, t.detail)
    assert RunState.PREFLIGHT in seen and RunState.RUNNING in seen and seen[-1] is RunState.DONE
    assert seen.index(RunState.PREFLIGHT) < seen.index(RunState.RUNNING)
    o = await r.odometry()
    assert abs(o.x - 2.0) <= 0.2
    kinds = [e.kind for e in book.pending()]
    assert "task_progress" in kinds
    runs = list((tmp_path / "runs").rglob("manifest.json"))
    assert runs, "引擎归档目录里要有这趟的 manifest"


async def test_速度上限经set_speed承接(台子):
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(6.0, 0.0), max_speed_mps=0.4, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    vmax = 0.0
    for _ in range(40):
        await _跑(c, r, parts, t, 1)
        vmax = max(vmax, (await r.odometry()).vx)
    assert 0 < vmax <= 0.4 + 1e-9


async def test_abort经引擎_等停止确认才终态(台子):
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(6.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, parts, t, 15)
    assert (await r.odometry()).vx > 0 and t.state is TaskState.RUNNING
    await t.abort("operator")
    for _ in range(30):
        await _跑(c, r, parts, t, 1)
        if t.done:
            break
        assert t.state is not TaskState.ABORTED, "stopped() 为真之前不许进终态"
    assert t.state is TaskState.ABORTED and t.detail["reason"] == "operator"
    assert await r.stopped() is True
    assert parts.engine.state is RunState.ABORTED


async def test_preempted终态(台子):
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(6.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, parts, t, 10)
    await t.abort("preempted")
    await _跑(c, r, parts, t, 30)
    assert t.state is TaskState.PREEMPTED


async def test_抢占后新任务能走完_不撞导航的终态驻留(台子):
    """旧任务终态之后资源就放了、新任务马上 start;这时导航若还在 Cancelled 的 0.5 s 驻留里,
    新任务的预飞 nav_ready 会红,抢占就变成「旧的停了、新的也 failed」。"""
    c, r, parts, book = 台子
    t1 = EngineGotoTask(task_id="t1", target=_target(6.0, 0.0), max_speed_mps=0.8, parts=parts,
                        events=book, now_ms=c, priority=0)
    await t1.start()
    await _跑(c, r, parts, t1, 15)
    await t1.abort("preempted")
    await _跑(c, r, parts, t1, 60)
    assert t1.state is TaskState.PREEMPTED
    t2 = EngineGotoTask(task_id="t2", target=_target(0.0, 0.0), max_speed_mps=0.8, parts=parts,
                        events=book, now_ms=c, priority=5)
    await t2.start()                        # 紧接着起,中间不多给一拍
    await _跑(c, r, parts, t2, 400)
    assert t2.state is TaskState.DONE, (t2.state, t2.detail)


async def test_开始前就被abort_不动直接终态(台子):
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(6.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.abort("preempted")
    await t.start()
    assert t.state is TaskState.PREEMPTED and t.detail["reason"] == "preempted"
    await _跑(c, r, parts, t, 10)
    assert parts.engine.state is RunState.IDLE
    assert (await r.odometry()).x == 0.0


async def test_engine_start炸了别的异常_任务failed不挂着(台子, monkeypatch):
    c, r, parts, book = 台子

    async def 炸(*a, **k):
        raise RuntimeError("归档目录写不进去")

    monkeypatch.setattr(parts.engine, "start", 炸)
    t = EngineGotoTask(task_id="t1", target=_target(2.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    assert t.state is TaskState.FAILED and "归档目录写不进去" in t.detail["reason"]


async def test_上一趟的限速不带到下一趟(台子):
    c, r, parts, book = 台子
    t1 = EngineGotoTask(task_id="t1", target=_target(1.0, 0.0), max_speed_mps=0.3, parts=parts,
                        events=book, now_ms=c, priority=0)
    await t1.start()
    await _跑(c, r, parts, t1, 300)
    assert t1.state is TaskState.DONE, (t1.state, t1.detail)
    t2 = EngineGotoTask(task_id="t2", target=_target(6.0, 0.0), max_speed_mps=None, parts=parts,
                        events=book, now_ms=c, priority=0)
    await t2.start()
    vmax = 0.0
    for _ in range(60):
        await _跑(c, r, parts, t2, 1)
        vmax = max(vmax, (await r.odometry()).vx)
    assert vmax > 0.5, "没给限速就该按 HAL 上限走,不能沿用上一趟的 0.3"


async def test_限速低于死区_任务failed不假装在走(台子):
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(2.0, 0.0), max_speed_mps=0.01, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    assert t.state is TaskState.FAILED and "max_speed" in t.detail["reason"]
    assert parts.engine.state is RunState.IDLE


async def test_定位短暂丢失_停下等回来再接着走到点(台子):
    """HAL 没有「重定位」这个原语;桥的 reset_localization 若是拒绝,引擎会把任何一次
    LOC_LOST 都当成整趟中止。应当是:停下 → 等定位回来(引擎自己限时 30 s)→ 重发当前点。"""
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(4.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, parts, t, 15)
    r.inject_loc_lost(True)
    await _跑(c, r, parts, t, 20)
    assert not t.done, (t.state, t.detail)
    assert await r.stopped() is True, "定位丢了要停下来等"
    r.inject_loc_lost(False)
    await _跑(c, r, parts, t, 400)
    assert t.state is TaskState.DONE, (t.state, t.detail)


async def test_预飞没过就failed_理由带检查项(tmp_path):
    c = 钟()
    r = SimRobot(now_ms=c)
    await r.connect()
    await r.acquire_control()
    parts = build_engine(r, runs_root=tmp_path / "runs", now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=None)                   # 没原点 → home 那一项红
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    t = EngineGotoTask(task_id="t1", target=_target(2.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, parts, t, 30)
    assert t.state is TaskState.FAILED and "home" in t.detail["reason"]
    assert (await r.odometry()).x == 0.0
    await parts.engine.aclose()


async def test_急停期间引擎收尾为aborted_任务failed(台子):
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(6.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, parts, t, 15)
    await r.emergency_stop(True)
    await _跑(c, r, parts, t, 40)
    assert t.done and t.state is TaskState.FAILED, (t.state, t.detail)
    assert t.detail["reason"], "理由要说出来(引擎按 retry_then_skip 跳过的航点,note 里有)"


async def test_断线不安全时引擎暂停_重连继续(台子):
    c, r, parts, book = 台子
    t = EngineGotoTask(task_id="t1", target=_target(6.0, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, parts, t, 15)
    x0 = (await r.odometry()).x
    await t.on_offline(safe=False)
    await _跑(c, r, parts, t, 15)
    assert parts.engine.state is RunState.PAUSED
    assert (await r.odometry()).x - x0 < 0.5
    await t.on_online()
    await _跑(c, r, parts, t, 15)
    assert parts.engine.state is RunState.RUNNING and (await r.odometry()).vx > 0


def test_旧的GotoTask已经删了():
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("d1max_agent.tasks.goto")
    assert json
