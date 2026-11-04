"""goto 跑在 MissionEngine 上(W00b 决定 2):契约不变,任务实现换成引擎。
SimRobot → 两个桥 → MissionEngine;EngineGotoTask 把引擎的 RunState 映成契约的 TaskState。"""

from __future__ import annotations

import asyncio
import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.assembly import build_engine
from d1max_agent.engine.machine import RunState
from d1max_agent.events import EventBook
from d1max_agent.tasks.engine_goto import EngineGotoTask
from d1max_contract.messages import MapPose, TaskState
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
    # W34:2026-10-08 C40221 急停后只写了「到不了下一个点(导航回了 Failed)」,没点明急停
    assert "急停按下了" in t.detail["reason"], t.detail


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


async def test_停止一直确认不了_超时按失败收尾并再发一次停(台子, monkeypatch):
    """W00d 内审:``stopped()`` 的阈值没实测过,真狗站着的里程噪声若大于它,「停了」永远不成立,
    任务会卡在 RUNNING、资源一直不放。兜底:引擎进终态后 ``STOP_CONFIRM_TIMEOUT_S`` 还确认不了,
    再发一次停车,按 ``stop_unconfirmed`` 失败收尾。"""
    from d1max_agent.tasks import engine_goto

    c, r, parts, book = 台子

    async def 永远没停():
        return False
    stops = []
    real_stop = r.stop

    async def 记着停():
        stops.append(c.ms)
        await real_stop()
    monkeypatch.setattr(r, "stopped", 永远没停)
    t = EngineGotoTask(task_id="t9", target=_target(0.5, 0.0), max_speed_mps=0.8, parts=parts,
                       events=book, now_ms=c, priority=0)
    await t.start()
    for _ in range(300):
        await _跑(c, r, parts, t, 1)
        if parts.engine.snapshot.state in (RunState.DONE, RunState.ABORTED):
            break
    assert parts.engine.snapshot.state is RunState.DONE
    monkeypatch.setattr(r, "stop", 记着停)
    n = int(engine_goto.STOP_CONFIRM_TIMEOUT_S / 0.1)
    await _跑(c, r, parts, t, n - 3)
    assert not t.done and not stops, "引擎跑完了,但停止没确认:超时之前不许终态"
    await _跑(c, r, parts, t, 8)
    assert t.state is TaskState.FAILED and t.detail == {"reason": "stop_unconfirmed"}, t.detail
    assert stops, "收尾前再发一次停车"


async def test_工单20_导航一派就拒_goal_blocked_任务记失败不记完成(台子, monkeypatch):
    """2026-10-28 16:26 回待命点(原点在膨胀区里):同一秒两次 goal_blocked、狗没动,任务却记 DONE。"""
    from d1max_patrol.backends.base import NavRequestError
    c, r, parts, book = 台子

    async def 拒(pose):
        raise NavRequestError("goto", "规划失败(goal_blocked):终点在障碍、禁行区里或贴得太近")
    monkeypatch.setattr(parts.nav, "goto", 拒)
    t = EngineGotoTask(task_id="standby-1", target=_target(2.0, 0.0), max_speed_mps=0.8,
                       parts=parts, events=book, now_ms=c, priority=-10)
    await t.start()
    await _跑(c, r, parts, t, 300)
    assert t.state is TaskState.FAILED, (t.state, t.detail)
    assert "goal_blocked" in t.detail.get("reason", ""), t.detail


def test_W73外审_报警出动的goto_出发线回撤线按停止线做底_普通goto照旧():
    """站点按「停止线 + 去回 × 1.5 + 看 5 分钟」派报警;狗上普通 goto 的底是 25%,不改会把它拒掉。"""
    from types import SimpleNamespace

    from d1max_agent.engine.homing import HomePoint
    from d1max_agent.engine.preflight import departure_line_pct
    from d1max_contract.charging import ALARM_PRIORITY

    def 任务(priority, charge=False):
        return EngineGotoTask(task_id="t", target=_target(3.0, 0.0), max_speed_mps=None,
                              parts=SimpleNamespace(), events=SimpleNamespace(),
                              now_ms=lambda: 0, priority=priority, charge=charge)._mission()

    home = HomePoint(map_id="m", pose=Pose.from_xy_yaw(0.0, 0.0, 0.0), marked_at_ms=0)
    alarm, plain = 任务(ALARM_PRIORITY), 任务(60)
    assert (alarm.policy.battery_return_pct, alarm.policy.battery_abort_pct) == (15.0, 15.0)
    assert plain.policy.battery_return_pct == 25.0
    assert departure_line_pct(alarm, home) < 20.0 < departure_line_pct(plain, home), \
        "近处报警 20% 能去;普通任务照旧不放"
    assert 任务(ALARM_PRIORITY, charge=True).policy.charging_trip, "去桩那一趟不受影响"


def test_W73外审r3_4_回待命点这一趟_停止线15_出发按路程算_不是去充电的10():
    """回家的路:低电不掉头、出发线 = 停止线 + 去那儿的耗电 × 1.5(不算回原点那段);
    但不是去补能 —— 停止线留 15%,不像去充电桩那趟放到 10%、也不跳过路程预算。"""
    from types import SimpleNamespace

    from d1max_agent.engine.homing import HomePoint
    from d1max_agent.engine.preflight import back_mode, departure_line_pct
    from d1max_contract.charging import HOMING_FLOOR_PCT

    def 任务(x, **kw):
        return EngineGotoTask(task_id="t", target=_target(x, 0.0), max_speed_mps=None,
                              parts=SimpleNamespace(), events=SimpleNamespace(),
                              now_ms=lambda: 0, priority=-10, **kw)._mission()

    home = HomePoint(map_id="m", pose=Pose.from_xy_yaw(0.0, 0.0, 0.0), marked_at_ms=0)
    near, far = 任务(3.0, homing=True), 任务(400.0, homing=True)
    assert HOMING_FLOOR_PCT == 15.0
    assert (near.policy.battery_return_pct, near.policy.battery_abort_pct) == (15.0, 15.0)
    assert near.policy.on_battery_low == "continue" and not near.policy.charging_trip
    assert back_mode(SimpleNamespace(), near) == "none"
    assert departure_line_pct(near, home, back="none") == 15.0 + 3.0 * 1.5, "近:底 15 + 最少 3%"
    assert departure_line_pct(far, home, back="none") > 25.0, "远:按路程算,不是只看停止线"
    plain = 任务(3.0)
    assert plain.policy.battery_return_pct == 25.0, "普通 goto 照旧"
