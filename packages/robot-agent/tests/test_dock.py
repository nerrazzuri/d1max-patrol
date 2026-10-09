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
    assert not t.done, "停对桩:确认停住了才交"
    await _跑(c, t, 10)
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


# ------------------------------------------------------------ W13 外审


def _挂(t):
    got = []
    t._on_hazard = got.append
    return got


async def test_外审1_停对桩失败_狗照样上了桩_改走出桩_确认离桩才交():
    c, dog, t = _台()
    await t.start()
    await _跑(c, t, 2)

    async def 停不了():
        raise OSError("串口没回")
    dog.recharge_stop = 停不了
    await t.abort("preempted")
    await _跑(c, t, 8)                                    # 停没停成,狗 5 秒时上了桩
    assert t.phase == "undocking" and not t.done
    await _跑(c, t, 30)
    assert t.state is TaskState.PREEMPTED and not dog.docked and dog.undock_calls == 1


async def test_外审1_刚上桩任务还没走下一拍_被抢_按实测出桩():
    c, dog, t = _台()
    await t.start()
    c.ms += 6000
    await dog.battery()                                   # 狗上了桩,任务还没走下一拍
    assert dog.docked and t.phase == "docking"
    await t.abort("preempted")
    assert t.phase == "undocking" and not t.done
    await _跑(c, t, 30)
    assert t.state is TaskState.PREEMPTED and not dog.docked


async def test_外审2_出不了桩_超时挂桩上危险_才结束():
    c, dog, t = _台(resume_pct=30.0)
    got = _挂(t)
    await t.start()

    async def 出不来():
        dog.undock_calls += 1
    dog.undock = 出不来
    await _跑(c, t, 300)
    assert t.state is TaskState.FAILED and "出不了桩" in t.detail["reason"]
    assert got and "出不了桩" in got[0] and dog.docked


async def test_外审3_电池一直读不到_期限照样到_停对桩确认不了就挂危险():
    c, dog, t = _台(dock_timeout_s=30)
    got = _挂(t)
    await t.start()

    async def 读不到():
        raise OSError("旁路进程没回")
    dog.battery = 读不到
    await _跑(c, t, 200)
    assert t.state is TaskState.FAILED and "停不住对桩" in t.detail["reason"], t.detail
    assert got, "说不清:挂桩上危险"


async def test_外审3_出桩命令一直发不出去_期限照样到():
    c, dog, t = _台(resume_pct=30.0)
    got = _挂(t)
    await t.start()

    async def 发不出去():
        raise OSError("超时")
    dog.undock = 发不出去
    await _跑(c, t, 400)
    assert t.state is TaskState.FAILED and got


async def test_外审2_真代理_出不了桩_排着的入侵任务不许起跑_离了桩才放(tmp_path):
    import json

    from test_runtime_maps import REG, T, 耳朵
    from test_runtime_maps import _跑 as 代理跑

    from d1max_contract.messages import Command
    from d1max_contract.transport import Message

    def _cmd(kind, payload, cid, c, priority=0):
        cmd = Command(command_id=cid, task_id=cid, kind=kind, issued_at=c.ms,
                      expires_at=c.ms + 3_600_000, control_epoch=1, payload=payload,
                      priority=priority)
        return Message(T.cmd, json.dumps(cmd.to_wire()).encode(), 1, False)

    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_contract.messages import MapPose
    from d1max_patrol.protocol.nav_types import Pose
    broker, ears = MemoryBroker(), 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    c = 钟()
    c.mono = 100.0
    dog = SimRobot(now_ms=c, charger=(0.0, 0.0, 0.0), charge_pct_per_h=60.0)
    dog.inject_battery(40.0)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono,
                      odom_identity=True)
    await rt.start()

    async def 跑(n):
        await 代理跑(rt, broker, n=n, r=dog, c=c, settle_s=0)

    def ack():
        return ears.by["cmd/ack"][-1]
    target = MapPose(map_id="m", map_version="1", frame_id="map", x=3.0, y=0.0,
                     yaw=0.0).to_wire()
    try:
        await 跑(5)
        assert ears.by["capabilities"][-1]["tasks"]["dock"]["on_dock"] is False
        await rt._on_cmd(_cmd("dock", {"resume_pct": 99}, "d1", c, priority=50))
        await broker.drain()
        await 跑(80)                                       # 上桩、充着
        assert dog.docked
        await rt._on_cmd(_cmd("goto", {"target": target}, "g0", c, priority=30))
        await broker.drain()
        assert ack()["result"] == "rejected", "不比 dock 优先:不收"

        async def 出不来():
            dog.undock_calls += 1
        dog.undock = 出不来
        await rt._on_cmd(_cmd("goto", {"target": target}, "g1", c, priority=80))
        await broker.drain()
        assert ack()["result"] == "accepted", "比 dock 优先:收下,dock 先出桩"
        await 跑(1300)                                     # 出桩 120 秒确认不了
        kinds = [(e["kind"], e["data"].get("task_id")) for e in ears.by["event"]]
        assert ("dock_hazard", None) in kinds
        failed = [e for e in ears.by["event"] if e["kind"] == "task_failed"
                  and e["data"]["task_id"] == "g1"]
        assert failed and "on_dock" in failed[0]["data"]["reason"], "排着的入侵任务不许起跑"
        assert abs(dog.x) < 0.01, "狗没动"
        assert "hazard" in ears.by["capabilities"][-1]["tasks"]["dock"]
        await rt._on_cmd(_cmd("goto", {"target": target}, "g1b", c, priority=80))
        await broker.drain()
        assert ack()["result"] == "rejected" and "on_dock" in ack()["reason"], \
            "挂着桩上危险:新的运动任务不收"
        dog.docked = False                                # 人把狗挪下了桩
        dog._rebase_battery()
        await 跑(20)
        assert rt._dock_hazard, "刚离桩:还没稳 5 秒,不摘"
        await 跑(50)
        assert "hazard" not in ears.by["capabilities"][-1]["tasks"]["dock"]
        await rt._on_cmd(_cmd("goto", {"target": target}, "g2", c, priority=80))
        await broker.drain()
        assert ack()["result"] == "accepted"
    finally:
        await rt.close()


# ------------------------------------------------------------ W13 复查


def _状态读不到(dog):
    async def 读不到():
        raise OSError("回充状态读不到")
    dog.recharge_status = 读不到


async def test_复查_停对桩后状态一直读不到_不在充也不交_到期挂危险():
    c, dog, t = _台()
    got = _挂(t)
    await t.start()
    await _跑(c, t, 2)
    _状态读不到(dog)
    await t.abort("preempted")
    await _跑(c, t, 30)
    assert not t.done, "不在充 ≠ 离了桩:状态读不到就不交"
    await _跑(c, t, 200)
    assert t.state is TaskState.FAILED and got, "到期:失败、挂桩上危险"


async def test_复查_出桩后状态读不到_不交():
    c, dog, t = _台(resume_pct=30.0)
    got = _挂(t)
    await t.start()
    for _ in range(600):
        if t.phase == "undocking":
            break
        c.ms += 1000
        await t.step(1.0)
    _状态读不到(dog)
    await _跑(c, t, 30)
    assert not t.done and not dog.docked, "电池说不充了,可状态读不到:不算离桩"
    await _跑(c, t, 200)
    assert t.state is TaskState.FAILED and got


async def test_复查_真代理_挂着危险时状态读不到_不充也不摘锁(tmp_path):
    from test_runtime_maps import REG

    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_patrol.protocol.nav_types import Pose
    broker = MemoryBroker()
    c = 钟()
    c.mono = 100.0
    dog = SimRobot(now_ms=c, charger=(0.0, 0.0, 0.0))
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono,
                      odom_identity=True)
    await rt.start()
    try:
        rt._set_dock_hazard("出不了桩")
        _状态读不到(dog)
        for _ in range(100):                               # 10 秒,不在充
            await rt.step(0.1)
            c.ms += 100
            c.mono += 0.1
        assert rt._dock_hazard and rt._on_dock is None and rt._motion_gate(), "读不到:锁着"
        del dog.recharge_status                            # 状态读得到了、确实不在桩上
        for _ in range(70):
            await rt.step(0.1)
            c.ms += 100
            c.mono += 0.1
        assert not rt._dock_hazard and rt._motion_gate() == ""
    finally:
        await rt.close()


async def test_W28_去桩的goto_中止线是10_别的还是25():
    from d1max_agent.tasks.engine_goto import EngineGotoTask
    from d1max_contract.charging import TRIP_ABORT_PCT
    from d1max_contract.messages import MapPose
    t = EngineGotoTask.__new__(EngineGotoTask)
    t.task_id, t.photo = "charge-goto-1", None
    t.target = MapPose(map_id="m", map_version="1", frame_id="map", x=1.0, y=0.0, yaw=0.0)
    t.charge = True
    assert t._mission().policy.battery_abort_pct == TRIP_ABORT_PCT
    t.charge = False
    assert t._mission().policy.battery_abort_pct == 25.0


async def test_W26外审2_充着时翻倒抱起来_停对桩不出桩_已经在出桩的也改停(monkeypatch):
    c, dog, t = _台()
    await t.start()
    await _跑(c, t, 10)
    assert dog.docked
    await t.force_stop("lifted")
    await t.force_stop("lifted")                          # 重复调没事
    await _跑(c, t, 200)
    assert dog.undock_calls == 0, "受力故障:一次都不发出桩"
    assert t.state is TaskState.FAILED and "确认不了离了桩" in t.detail["reason"], \
        "还在桩上:挂桩上危险锁住,等人"
    c, dog, t = _台()
    await t.start()
    await _跑(c, t, 10)
    await t.abort("preempted")
    assert t.phase == "undocking"
    await t.force_stop("flipped")                         # 出桩中途翻了
    assert t.phase == "stopping"
    n = dog.undock_calls
    await _跑(c, t, 60)
    assert dog.undock_calls == n, "改成停:不再发出桩"
