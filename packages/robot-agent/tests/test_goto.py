"""最小 goto:按里程计向目标点走,进度事件,abort → stop → 等 stopped → 终态。SimRobot + 假钟。"""

from __future__ import annotations

import math

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.events import EventBook
from d1max_agent.tasks.goto import GotoTask
from d1max_contract.messages import MapPose, TaskState


class 钟:
    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))


@pytest.fixture
async def 台子(tmp_path):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, deadband_vx=0.05, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    return c, r, book


def _target(x, y, yaw=0.0):
    return MapPose(map_id="m", map_version="1", frame_id="map", x=x, y=y, yaw=yaw)


async def _跑(c, r, task, n, dt=0.1):
    for _ in range(n):
        await task.step(dt)
        r.tick(dt)
        c.advance(dt)
        if task.done:
            return


async def test_走到目标点_done_有进度事件(台子):
    c, r, book = 台子
    t = GotoTask(task_id="t1", target=_target(2.0, 0.0), max_speed_mps=0.8, hal=r, events=book,
                 now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, t, 200)
    assert t.state is TaskState.DONE
    o = await r.odometry()
    assert math.hypot(o.x - 2.0, o.y) <= 0.15
    kinds = [e.kind for e in book.pending()]
    assert "task_progress" in kinds and kinds.count("task_progress") >= 2
    prog = [e for e in book.pending() if e.kind == "task_progress"]
    assert prog[0].data["distance_m"] > prog[-1].data["distance_m"]
    assert prog[0].data["task_id"] == "t1"


async def test_速度不超过命令上限也不超过HAL上限(台子):
    c, r, book = 台子
    t = GotoTask(task_id="t1", target=_target(5.0, 0.0), max_speed_mps=0.4, hal=r, events=book,
                 now_ms=c, priority=0)
    await t.start()
    vmax = 0.0
    for _ in range(30):
        await t.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
        vmax = max(vmax, (await r.odometry()).vx)
    assert 0 < vmax <= 0.4 + 1e-9
    t2 = GotoTask(task_id="t2", target=_target(50.0, 0.0), max_speed_mps=9.0, hal=r, events=book,
                  now_ms=c, priority=0)
    await t2.start()
    vmax = 0.0
    for _ in range(30):
        await t2.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
        vmax = max(vmax, (await r.odometry()).vx)
    assert vmax <= 1.0 + 1e-9, "HAL 上限 1.0"


async def test_先转向再前进(台子):
    c, r, book = 台子
    t = GotoTask(task_id="t1", target=_target(0.0, 3.0), max_speed_mps=0.8, hal=r, events=book,
                 now_ms=c, priority=0)
    await t.start()
    await t.step(0.1)
    r.tick(0.1)
    c.advance(0.1)
    o = await r.odometry()
    assert o.vx == 0.0 and o.wz > 0, "朝向差大时只转不走"


async def test_abort先停再等确认再终态(台子):
    c, r, book = 台子
    t = GotoTask(task_id="t1", target=_target(5.0, 0.0), max_speed_mps=0.8, hal=r, events=book,
                 now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, t, 10)
    assert (await r.odometry()).vx > 0
    await t.abort("operator")
    await t.step(0.1)                       # 这一拍发 stop
    assert t.state is TaskState.RUNNING, "还没确认停,不许进终态"
    assert await r.stopped() is False
    r.tick(0.1)
    c.advance(0.1)
    await t.step(0.1)                       # 制动还剩 0.1 s:仍未确认
    assert await r.stopped() is False
    assert t.state is TaskState.RUNNING, "stopped() 还是 False,不许进终态"
    r.tick(0.1)
    c.advance(0.1)                          # 累计 0.2 s = stop_latency,停了
    assert await r.stopped() is True
    await t.step(0.1)
    assert t.state is TaskState.ABORTED and t.detail["reason"] == "operator"
    assert (await r.odometry()).vx == 0.0


async def test_抢占用preempted终态(台子):
    c, r, book = 台子
    t = GotoTask(task_id="t1", target=_target(5.0, 0.0), max_speed_mps=0.8, hal=r, events=book,
                 now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, t, 5)
    await t.abort("preempted")
    for _ in range(5):
        await t.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
    assert t.state is TaskState.PREEMPTED


async def test_急停期间速度被拒则任务failed(台子):
    c, r, book = 台子
    t = GotoTask(task_id="t1", target=_target(5.0, 0.0), max_speed_mps=0.8, hal=r, events=book,
                 now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, t, 3)
    await r.emergency_stop(True)
    await _跑(c, r, t, 3)
    assert t.state is TaskState.FAILED and t.detail["reason"] == "estop"


async def test_断线不安全时停住等待_重连后接着走(台子):
    c, r, book = 台子
    t = GotoTask(task_id="t1", target=_target(5.0, 0.0), max_speed_mps=0.8, hal=r, events=book,
                 now_ms=c, priority=0)
    await t.start()
    await _跑(c, r, t, 5)
    x0 = (await r.odometry()).x
    await t.on_offline(safe=False)
    await _跑(c, r, t, 10)
    assert (await r.odometry()).vx == 0.0 and t.state is TaskState.RUNNING
    assert (await r.odometry()).x - x0 < 0.3, "停住等待,不许继续走"
    await t.on_online()
    await _跑(c, r, t, 5)
    assert (await r.odometry()).vx > 0
    t2 = GotoTask(task_id="t2", target=_target(9.0, 0.0), max_speed_mps=0.8, hal=r, events=book,
                  now_ms=c, priority=0)
    await t2.start()
    await t2.on_offline(safe=True)
    await _跑(c, r, t2, 5)
    assert (await r.odometry()).vx > 0, "安全就按已获批的离线策略继续"
