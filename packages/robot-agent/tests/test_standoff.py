"""保持距离(W25,决策 40):人在 3 m 内往远离人的方向退、退到 4 m 外停;只退不进(从不往人那头走);
每个退法过避障、拴绳、禁行区、真拉开距离;一个都不行就原地站定、报无路可退;人不知道在哪不动;
到点、被抢先停稳再进终态。"""

from __future__ import annotations

import math
from types import SimpleNamespace

from d1max_agent.tasks.standoff import RETREAT_V, StandoffTask
from d1max_contract.hal import VelocityResult
from d1max_contract.messages import TaskState
from d1max_contract.standoff import StandoffRequest


class 假狗:
    def __init__(self):
        self.cmds, self.stops = [], 0
        self.is_stopped = True

    async def set_velocity(self, c):
        self.cmds.append((c.vx, c.wz))
        self.is_stopped = False
        return VelocityResult(applied_vx=c.vx, applied_wz=c.wz, clamped=False, rejected=False)

    async def stop(self):
        self.stops += 1
        self.is_stopped = True

    async def stopped(self):
        return self.is_stopped


class 台:
    def __init__(self, *, target=(0.0, 2.0), leash=6.0, blocked=lambda vx, wz: "",
                 nogo=lambda dx, dy: ""):
        self.t = 0
        self.target = target
        self.pose = (0.0, 0.0, 0.0)
        self.blocked = blocked
        self.dog = 假狗()
        self.task = StandoffTask(
            task_id="standoff-1", req=StandoffRequest(max_s=60, leash_m=leash), hal=self.dog,
            now_ms=lambda: self.t, target=lambda: self.target,
            check=lambda vx, wz: SimpleNamespace(ok=not self.blocked(vx, wz),
                                                 reason=self.blocked(vx, wz)),
            odom=lambda: self.pose, nogo=nogo)

    async def 拍(self, n=1):
        for _ in range(n):
            await self.task.step(0.1)
            self.t += 100


async def test_人在前面3米内_往后退_退到4米外停():
    s = 台(target=(0.0, 2.0))
    await s.task.start()
    await s.拍()
    assert s.task.mode == "retreat" and s.dog.cmds[-1] == (-RETREAT_V, 0.0)
    s.target = (0.0, 3.5)                                 # 3–4 m 之间:接着退(不在 3 m 线上来回翻)
    await s.拍()
    assert s.task.mode == "retreat"
    s.target = (0.0, 4.2)
    await s.拍()
    assert s.task.mode == "hold" and s.dog.stops == 1


async def test_人在后面_往前走_远离人():
    s = 台(target=(180.0, 2.0))
    await s.task.start()
    await s.拍()
    assert s.dog.cmds[-1][0] > 0


async def test_只退不进_人在远处不追_3到4米之间也不开始退():
    s = 台(target=(0.0, 8.0))
    await s.task.start()
    await s.拍(5)
    s.target = (0.0, 3.5)
    await s.拍(5)
    assert s.dog.cmds == [] and s.task.mode == "hold"


def _速度方向离人(cmd, bearing_deg):
    """这条速度的位移跟「狗 → 人」的方向夹角大于 90°:不往人那头走。"""
    b = math.radians(bearing_deg)
    return cmd[0] * math.cos(b) < 0


async def test_从不往人那头走_各个方向的人都一样():
    for bearing in range(-180, 181, 15):
        s = 台(target=(float(bearing), 2.0))
        await s.task.start()
        await s.拍()
        for c in s.dog.cmds:
            assert _速度方向离人(c, bearing) or abs(math.cos(math.radians(bearing))) < 1e-9, \
                (bearing, c)


async def test_后面有挡_带弧侧向脱困():
    s = 台(target=(0.0, 2.0), blocked=lambda vx, wz: "扫过区有挡" if wz == 0.0 else "")
    await s.task.start()
    await s.拍()
    assert s.task.mode == "retreat" and s.dog.cmds[-1][1] != 0.0


async def test_无路可退_原地站定_不转不进_说清原因():
    s = 台(target=(0.0, 2.0), blocked=lambda vx, wz: "扫过区有挡 3 处")
    await s.task.start()
    await s.拍(3)
    assert s.task.mode == "cornered" and s.dog.cmds == []
    assert "扫过区有挡" in s.task.why
    s.blocked = lambda vx, wz: ""                          # 后面让开了:接着退
    await s.拍()
    assert s.task.mode == "retreat"
    s.blocked = lambda vx, wz: "扫过区有挡"
    await s.拍()
    assert s.task.mode == "cornered" and s.dog.stops == 1, "退着退着被堵:停"


async def test_拴绳_离拦截点超过就不退了():
    s = 台(target=(0.0, 2.0), leash=2.0)
    await s.task.start()
    s.pose = (-1.8, 0.0, 0.0)                             # 已经退了 1.8 m
    await s.拍()
    assert s.task.mode == "cornered" and "超过 2 m" in s.task.why


async def test_禁行区_定位不可信_都不退():
    s = 台(target=(0.0, 2.0), nogo=lambda dx, dy: "再退就进禁行区「池塘」")
    await s.task.start()
    await s.拍()
    assert s.task.mode == "cornered" and "池塘" in s.task.why


async def test_人在哪不知道_不动():
    s = 台(target=None)
    await s.task.start()
    await s.拍(3)
    assert s.dog.cmds == [] and s.task.mode == "hold"
    s.target = (0.0, 2.0)
    await s.拍()
    s.target = None                                       # 退着退着检测断了:停
    await s.拍()
    assert s.task.mode == "hold" and s.dog.stops == 1


async def test_到点_被抢_先停稳再进终态():
    s = 台(target=(0.0, 2.0))
    await s.task.start()
    await s.拍()
    s.t = 61_000
    s.dog.is_stopped = False
    s.dog.stop = _慢停(s.dog)
    await s.拍()
    assert not s.task.done
    s.dog.is_stopped = True
    await s.拍()
    assert s.task.state is TaskState.DONE and s.task.detail["reason"] == "max_s"
    s2 = 台(target=(0.0, 2.0))
    await s2.task.start()
    await s2.task.abort("preempted")
    await s2.拍()
    assert s2.task.state is TaskState.PREEMPTED and s2.task.mode == "idle"


def _慢停(dog):
    async def stop():
        dog.stops += 1
    return stop


# ------------------------------------------------------------ 真代理 + 仿真狗


def _栅格(seq, behind_wall=False):
    from d1max_contract.obsbridge import Grid, pack_bits
    n, res = 60, 0.1
    occ, known = [], []
    for i in range(n):                                   # 行 → x(狗身系)
        x = (i - n / 2 + 0.5) * res
        for _ in range(n):
            occ.append(behind_wall and -1.2 < x < -0.9)
            known.append(True)
    return Grid(seq=seq, stamp_ns=0, res=res, size=n, occ=pack_bits(occ, n),
                known=pack_bits(known, n), rear=True, rear_cal=True)


async def _真台(tmp_path):
    from test_runtime_maps import REG, T, 耳朵, 钟

    from d1max_adapter_sim.robot import SimRobot
    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_patrol.protocol.nav_types import Pose
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    dog = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono,
                      odom_identity=True, telemetry_period_ms=100, nav="planned",
                      obstacles="bridge", obs_socket=tmp_path / "o.sock",
                      persons="bridge", persons_socket=tmp_path / "p.sock")
    await rt.start()
    await broker.drain()
    rt.obs_view.on_connect()
    rt.person_view.on_connect()
    return broker, c, ears, dog, rt


async def _拍(rt, broker, dog, c, n, *, wall=False, person=2.0):
    from test_persons import _帧
    for _ in range(n):
        _拍.seq += 1
        rt.obs_view.on_grid(_栅格(_拍.seq, wall))
        if person is not None:
            rt.person_view.on_persons(_帧(person))
        await rt.step(0.1)
        dog.tick(0.1)
        c.ms += 100
        c.mono += 0.1
        await broker.drain()


_拍.seq = 0


async def test_真代理(tmp_path):
    """能力里报保持距离、收命令、人近就往后退、背后堵了原地站定。"""
    from test_runtime_maps import _cmd
    broker, c, ears, dog, rt = await _真台(tmp_path)
    try:
        await _拍(rt, broker, dog, c, 3, person=None)
        assert ears.by["capabilities"][-1]["tasks"]["standoff"] == {"state": "idle"}
        await rt._on_cmd(_cmd("standoff", {"max_s": 60, "leash_m": 6.0}, "s1", c))
        await broker.drain()
        assert ears.by["cmd/ack"][-1]["result"] == "accepted", ears.by["cmd/ack"][-1]
        x0 = (await dog.odometry()).x
        await _拍(rt, broker, dog, c, 10)
        assert (await dog.odometry()).x < x0 - 0.1, "人在前面 2 m:往后退了"
        st = ears.by["capabilities"][-1]["tasks"]["standoff"]
        assert st["state"] == "retreat" and st["task_id"]
        await _拍(rt, broker, dog, c, 10, wall=True)
        st = ears.by["capabilities"][-1]["tasks"]["standoff"]
        assert st["state"] == "cornered" and "有挡" in st["reason"], st
        x1 = (await dog.odometry()).x
        await _拍(rt, broker, dog, c, 10, wall=True)
        assert abs((await dog.odometry()).x - x1) < 0.05, "无路可退:原地站定"
    finally:
        await rt.close()


async def test_人在正侧面_带弧被挡_直退拉不开_算无路可退不白走():
    s = 台(target=(90.0, 2.0), blocked=lambda vx, wz: "扫过区有挡" if wz != 0.0 else "")
    await s.task.start()
    await s.拍()
    assert s.task.mode == "cornered" and "拉不开距离" in s.task.why and s.dog.cmds == []
