"""W00c5c:遥控任务。按帧的速度走(限速 = HAL 能力的一半)、帧有效期到了自己停、没画面不动、租约到期/
halt/断线都先停稳再终态。仿真狗,注入的钟。"""

from __future__ import annotations

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.events import EventBook
from d1max_agent.tasks.teleop import TeleopTask
from d1max_contract.messages import TaskState
from d1max_contract.teleop import TeleopFrame


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
async def 台(tmp_path):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, deadband_vx=0.05, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    video = {"live": True}
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b", now_ms=c)
    t = TeleopTask(task_id="teleop-2", lease_epoch=2, operator="gina", lease_ttl_ms=5000, hal=r,
                   now_ms=c, video_live=lambda: video["live"], events=book)
    await t.start()
    return c, r, t, video


_seq = {"n": 0}


def _帧(c, vx=0.0, wz=0.0, epoch=2, ttl=300):
    _seq["n"] += 1
    return TeleopFrame(lease_epoch=epoch, seq=_seq["n"], sent_at=c.ms - 30, ttl_ms=ttl,
                       vx=vx, wz=wz)


async def _走(c, r, t, n=1, dt=0.1, frame=None):
    for _ in range(n):
        if frame is not None:
            t.on_frame(frame(), rx_ms=c.ms)
        await t.step(dt)
        r.tick(dt)
        c.ms += int(dt * 1000)


async def test_按帧走_限速是能力的一半_松手就停(台):
    c, r, t, _ = 台
    await _走(c, r, t, 10, frame=lambda: _帧(c, vx=3.0, wz=-9.0))
    o = await r.odometry()
    assert 0.45 <= o.vx <= 0.5 + 1e-9, o.vx          # max_vx 1.0 的一半
    assert abs(o.wz) <= 0.75 + 1e-9
    assert o.x > 0.2
    await _走(c, r, t, 5, frame=lambda: _帧(c))       # 松手:零速帧
    assert await r.stopped()
    assert t.state is TaskState.RUNNING, "松手不是结束,租约还在"


async def test_帧不来了_有效期到了自己停(台):
    c, r, t, _ = 台
    await _走(c, r, t, 3, frame=lambda: _帧(c, vx=0.4))
    assert (await r.odometry()).vx > 0
    await _走(c, r, t, 6)                            # 0.6 s 一帧都没来(帧有效期 300 ms)
    assert await r.stopped()


async def test_没画面不动_画面没了立刻停(台):
    c, r, t, video = 台
    video["live"] = False
    await _走(c, r, t, 5, frame=lambda: _帧(c, vx=0.4))
    assert (await r.odometry()).x == 0.0, "没画面不许动"
    video["live"] = True
    await _走(c, r, t, 3, frame=lambda: _帧(c, vx=0.4))
    assert (await r.odometry()).vx > 0
    video["live"] = False
    await _走(c, r, t, 4, frame=lambda: _帧(c, vx=0.4))
    assert await r.stopped()


async def test_别的代次的帧不执行(台):
    c, r, t, _ = 台
    await _走(c, r, t, 5, frame=lambda: _帧(c, vx=0.4, epoch=1))
    assert (await r.odometry()).x == 0.0
    assert t.gate.dropped["epoch"] == 5
    t.release()
    await _走(c, r, t, 2)
    assert t.detail == {"reason": "released", "dropped": {"epoch": 5, "seq": 0, "late": 0}}, \
        "丢帧计数随终态上站点(真机项 3c.9)"


async def test_租约到期_先停稳再终态_续了就不到期(台):
    c, r, t, _ = 台
    await _走(c, r, t, 30, frame=lambda: _帧(c, vx=0.4))       # 3 s
    t.renew(5000)
    await _走(c, r, t, 30, frame=lambda: _帧(c, vx=0.4))       # 又 3 s:续过了,没到期
    assert t.state is TaskState.RUNNING
    await _走(c, r, t, 25, frame=lambda: _帧(c, vx=0.4))       # 再 2.5 s:到期
    assert t.done and t.state is TaskState.FAILED and t.detail["reason"] == "lease_expired"
    assert await r.stopped()


async def test_halt_放租_断线_都先停再终态(台, tmp_path):
    c, r, t, _ = 台
    await _走(c, r, t, 5, frame=lambda: _帧(c, vx=0.4))
    await t.abort("halt")
    await _走(c, r, t, 1)
    assert not t.done, "还在制动:先停稳再终态"
    await _走(c, r, t, 5)
    assert t.state is TaskState.ABORTED and t.detail["reason"] == "halt"
    assert await r.stopped()
    # 放租 → DONE;断线 → FAILED(不续,决策 7)
    for how, want in (("release", TaskState.DONE), ("offline", TaskState.FAILED)):
        t2 = TeleopTask(task_id="teleop-3", lease_epoch=3, operator="gina", lease_ttl_ms=5000,
                        hal=r, now_ms=c, video_live=lambda: True,
                        events=EventBook(tmp_path / f"{how}.jsonl", boot_id="b", now_ms=c))
        await t2.start()
        if how == "release":
            t2.release()
        else:
            await t2.on_offline(False)
        await _走(c, r, t2, 6)
        assert t2.state is want, (how, t2.state, t2.detail)
    got = []
    t3 = TeleopTask(task_id="teleop-4", lease_epoch=4, operator="gina", lease_ttl_ms=5000,
                    hal=r, now_ms=c, video_live=lambda: True,
                    events=EventBook(tmp_path / "x.jsonl", boot_id="b", now_ms=c))
    await t3.start()
    await t3.abort("halt")
    t3.on_frame(TeleopFrame(lease_epoch=4, seq=1, sent_at=c.ms, ttl_ms=300, vx=0.4, wz=0),
                rx_ms=c.ms)
    await _走(c, r, t3, 3)
    got.append((await r.odometry()).vx)
    assert got == [0.0], "结束中的遥控不许再执行帧"


async def test_速度命令的有效期不超过这一帧剩下的有效期(台, monkeypatch):
    """帧不来了,HAL 那一层也要在帧有效期内自己停(不只靠这边一拍一看)。"""
    c, r, t, _ = 台
    ttls = []
    real = r.set_velocity

    async def 记(cmd):
        ttls.append(cmd.ttl_ms)
        return await real(cmd)
    monkeypatch.setattr(r, "set_velocity", 记)
    t.on_frame(_帧(c, vx=0.4, ttl=200), rx_ms=c.ms)
    await _走(c, r, t, 1)                           # 帧到 0 ms,这一拍 ttl ≤ 200
    await _走(c, r, t, 1)                           # 100 ms 之后:剩 100
    assert ttls and max(ttls) <= 200 and ttls[-1] <= 100, ttls
