"""对桩、充电、出桩(W13,决策 25、45):仿真狗的假充电桩。站准了上桩、按电池确认充上、充到线出桩、
出了桩才算完;没站准对不上桩(超时、不发出桩);被抢、被中止先出桩再交;充电断了、出不了桩失败;
没配桩的狗不报 dock。"""

from __future__ import annotations

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.tasks.dock import DockTask
from d1max_contract.charging import DockRequest
from d1max_contract.messages import TaskState


class 钟:
    def __init__(self):
        self.ms = 1_000_000

    def __call__(self):
        return self.ms


def _台(*, at=(0.0, 0.0, 0.0), pct=25.0, **req):
    c = 钟()
    dog = SimRobot(now_ms=c, charger=(0.0, 0.0, 0.0), charge_pct_per_h=600.0)
    dog.x, dog.y, dog.yaw = at
    dog.inject_battery(pct)
    t = DockTask(task_id="charge-dock-1", req=DockRequest(**req), hal=dog, now_ms=c)
    return c, dog, t


async def _跑(c, t, secs, step=1.0):
    for _ in range(int(secs / step)):
        c.ms += int(step * 1000)
        await t.step(step)
        if t.done:
            return


async def test_站准了_上桩_充到90_出桩_出了才算完():
    c, dog, t = _台()
    await t.start()
    await _跑(c, t, 4)
    assert t.phase == "docking" and not t.done
    await _跑(c, t, 3)
    assert t.phase == "charging" and dog.docked
    await _跑(c, t, 600)                                  # 600%/h:25% → 90% 约 6.5 分钟
    assert t.phase == "undocking" or t.done
    await _跑(c, t, 30)
    assert t.state is TaskState.DONE and not dog.docked and dog.undock_calls == 1
    assert t.detail["percent"] >= 90


async def test_没站准_对不上桩_超时失败_不发出桩():
    c, dog, t = _台(at=(2.0, 0.0, 0.0), dock_timeout_s=60)
    await t.start()
    await _跑(c, t, 70)
    assert t.state is TaskState.FAILED and "没对上桩" in t.detail["reason"]
    assert dog.undock_calls == 0, "没上过桩:不发出桩(厂家的出桩不在桩上也会往后退)"


async def test_充着被抢_先出桩再交():
    c, dog, t = _台()
    await t.start()
    await _跑(c, t, 10)
    assert dog.docked
    await t.abort("preempted")
    assert not t.done, "还在桩上:不许马上交"
    await _跑(c, t, 30)
    assert t.state is TaskState.PREEMPTED and not dog.docked and dog.undock_calls == 1


async def test_对桩中被中止_停对桩_不发出桩():
    c, dog, t = _台()
    await t.start()
    await _跑(c, t, 2)
    await t.abort("abort")
    assert t.state is TaskState.ABORTED and dog.undock_calls == 0
    await _跑(c, t, 10)
    assert not dog.docked, "停对桩了:不会再上桩"


async def test_充着充着断了_出桩_失败():
    c, dog, t = _台()
    await t.start()
    await _跑(c, t, 10)
    dog.docked = False                                    # 被碰掉了
    dog._rebase_battery()
    await _跑(c, t, 40)
    assert t.state is TaskState.FAILED and "充电断了" in t.detail["reason"]


async def test_出不了桩_超时失败():
    c, dog, t = _台(resume_pct=30.0)
    await t.start()
    await _跑(c, t, 10)

    async def 出不来():
        dog.undock_calls += 1
    dog.undock = 出不来
    await _跑(c, t, 200)
    assert t.state is TaskState.FAILED and "出不了桩" in t.detail["reason"]


async def test_回充起不来_直接失败():
    c = 钟()
    dog = SimRobot(now_ms=c)                              # 没配桩
    t = DockTask(task_id="x", req=DockRequest(), hal=dog, now_ms=c)
    await t.start()
    assert t.state is TaskState.FAILED and "回充起不来" in t.detail["reason"]


async def test_真代理_配了桩的狗报dock_收命令_没配的不报(tmp_path):
    from test_runtime_maps import REG, T, _cmd, 耳朵

    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_patrol.protocol.nav_types import Pose
    for charger, want in (((0.0, 0.0, 0.0), True), (None, False)):
        broker, ears = MemoryBroker(), 耳朵()
        st = MemoryTransport(broker, "site")
        await st.connect()
        await st.subscribe(f"{T.prefix}/#", ears)
        c = 钟()
        c.mono = 100.0
        dog = SimRobot(now_ms=c, charger=charger)
        rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                          store_dir=tmp_path / str(want), now_ms=c, loaded_map=("m", "1"),
                          boot_id="b", home=Pose.from_xy_yaw(0.0, 0.0),
                          monotonic=lambda c=c: c.mono, odom_identity=True)
        await rt.start()
        await broker.drain()
        try:
            tasks = ears.by["capabilities"][-1]["tasks"]
            assert ("dock" in tasks) is want
            await rt._on_cmd(_cmd("dock", {"resume_pct": 90}, "d1", c))
            await broker.drain()
            ack = ears.by["cmd/ack"][-1]
            assert (ack["result"] == "accepted") is want, ack
        finally:
            await rt.close()


async def test_出桩_不充了还要稳5秒才算出了桩():
    c, dog, t = _台(resume_pct=30.0)
    await t.start()
    for _ in range(600):                                  # 上桩、充到 30%:开始出桩
        if t.phase == "undocking":
            break
        c.ms += 1000
        await t.step(1.0)
    assert t.phase == "undocking"
    for _ in range(4):
        c.ms += 1000
        await t.step(1.0)
    assert not dog.docked and not t.done, "刚不充:还没稳"
    await _跑(c, t, 10)
    assert t.state is TaskState.DONE
