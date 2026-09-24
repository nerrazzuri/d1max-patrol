"""W00 验收(总设计 §6,一条流程必须端到端过):

站点派 goto → 代理校验 → sim 执行 → 回执与进度 → 中途断线 → 命令重复投递 →
重连对账 → abort → 停止确认。外加:代理重启(新 boot_id)后旧 command_id 仍 duplicate。

全部在 MemoryBroker 上,钟注入,不睡。"""

from __future__ import annotations

import asyncio
import math

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.dispatch import DispatchClient
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import AckResult, MapPose, Precondition, TaskState
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics

REG = Registration(site_id="penang-1", robot_id="D1MAX-C40011", credential_fingerprint="sha256:x",
                   issued_at=0, expires_at=10**13)
T = Topics(site_id="penang-1", robot_id="D1MAX-C40011")
MAP = ("estate-1", "7")


class 钟:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))


class 台子:
    def __init__(self, tmp_path):
        self.broker = MemoryBroker()
        self.clock = 钟()
        self.dog = SimRobot(now_ms=self.clock, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
        self.store = tmp_path / "agent"
        self.agent: AgentRuntime | None = None
        self.site = DispatchClient(MemoryTransport(self.broker, "site"), T, now_ms=self.clock)
        self.events = []
        self.site.on_event(self.events.append)
        self.reconciles = []
        self.site.on_reconcile(self.reconciles.append)

    async def start_agent(self, boot_id: str) -> AgentRuntime:
        self.agent = AgentRuntime(transport=MemoryTransport(self.broker, "dog"), registration=REG,
                                  hal=self.dog, store_dir=self.store, now_ms=self.clock,
                                  loaded_map=MAP, boot_id=boot_id)
        await self.agent.start()
        await self.broker.drain()
        return self.agent

    async def run(self, n: int, dt: float = 0.1) -> None:
        for _ in range(n):
            await self.agent.step(dt)
            self.dog.tick(dt)
            self.clock.advance(dt)
            await self.broker.drain()

    def kinds(self):
        return [e.kind for e in self.events]


def _goto(site, x, y, epoch, ttl_ms=60_000, speed=0.8):
    target = MapPose(map_id=MAP[0], map_version=MAP[1], frame_id="map", x=x, y=y, yaw=0.0)
    return site.new_command("goto", {"target": target.to_wire(), "max_speed_mps": speed},
                            ttl_ms=ttl_ms, control_epoch=epoch)


@pytest.fixture
async def 台(tmp_path):
    t = 台子(tmp_path)
    await t.site.start()
    await t.start_agent("boot-1")
    return t


async def test_W00_验收流程(台):
    t = 台
    site, dog, broker = t.site, t.dog, t.broker

    # 0. 亮相:能力里只有 goto;状态 online、ready、没有任务;首次连接的 reconcile 是干净的。
    assert list(site.capabilities.tasks) == ["goto"]
    assert site.status.online and site.status.ready.ok and site.status.task is None
    assert t.reconciles and t.reconciles[0].task is None
    assert (t.reconciles[0].unacked_from_seq, t.reconciles[0].unacked_to_seq) == (0, 0)

    # 1. 站点派 goto → 代理校验 → accepted。
    cmd = _goto(site, 6.0, 0.0, epoch=1)
    ack = await site.send(cmd, timeout_s=1.0)
    assert ack.result is AckResult.ACCEPTED

    # 2. sim 执行:位姿在动,进度事件在到,status 里挂着 running 的任务。
    await t.run(15)
    x1 = (await dog.odometry()).x
    assert x1 > 0.3
    assert "task_progress" in t.kinds()
    assert site.status.task is not None and site.status.task.state is TaskState.RUNNING
    assert site.status.task.task_id == cmd.task_id

    # 3. 中途断线:LWT 把 status 改写为 offline;狗在安全条件下按已获批策略继续走。
    broker.disconnect("dog")
    await broker.drain()
    assert site.status.online is False
    n_events_before = len(t.events)
    await t.run(10)
    x2 = (await dog.odometry()).x
    assert x2 > x1 + 0.3, "断线但安全:继续走"
    assert len(t.events) == n_events_before, "断线期间站点收不到事件"

    # 4. 断线期间:站点**真的发出**重复的 goto 和一条 abort(create_task,不是攥着协程),
    #    两条都在 broker 的离线收件箱里排队;此时代理收不到、也回不了。
    dup_task = asyncio.create_task(site.send(cmd, timeout_s=5.0))
    abort_cmd = site.new_command("abort", {"reason": "operator"}, ttl_ms=60_000, control_epoch=1,
                                 task_id=cmd.task_id,
                                 precondition=Precondition(expect_task_state=TaskState.RUNNING))
    abort_task = asyncio.create_task(site.send(abort_cmd, timeout_s=5.0))
    await asyncio.sleep(0)
    await broker.drain()
    n_acks = len(site.acks)
    await t.run(3)
    assert len(site.acks) == n_acks, "断线期间不该有任何回执回来"
    assert not dup_task.done() and not abort_task.done()
    x3 = (await dog.odometry()).x

    # 5. 重连:第一条是 reconcile(任务 running、未确认区间含断线期间的进度事件)。
    n_rc = len(t.reconciles)
    await t.agent.transport.connect()
    await broker.drain()
    rc = t.reconciles[n_rc]
    assert site.acks[n_acks:] , "重连后排队的两条命令才被处理"
    assert rc.boot_id == "boot-1" and rc.task.task_id == cmd.task_id
    assert rc.task.state is TaskState.RUNNING
    assert rc.unacked_from_seq >= 1 and rc.unacked_to_seq >= rc.unacked_from_seq
    assert t.events[-1].seq == rc.unacked_to_seq, "断线期间的事件补发到了站点"

    # 6. 重复投递回 duplicate + 原结果,任务没重起(位姿连续,没有回到起点)。
    dup = await dup_task
    assert dup.result is AckResult.DUPLICATE and dup.original["result"] == "accepted"
    assert (await dog.odometry()).x >= x3 - 1e-9

    # 7. abort accepted → 停止确认 → task_aborted → status 里没有任务。
    ab = await abort_task
    assert ab.result is AckResult.ACCEPTED
    await t.run(1)
    assert await dog.stopped() is False, "刚发 stop,还在制动"
    await t.run(1)                                   # 这一拍 step 时制动还剩 0.1 s,拍末 tick 才停下
    assert "task_aborted" not in t.kinds(), "停止没确认之前不许报 aborted"
    assert site.status.task is not None and site.status.task.state is TaskState.RUNNING
    await t.run(4)
    assert await dog.stopped() is True
    assert t.kinds()[-1] == "task_aborted"
    assert t.events[-1].data == {"task_id": cmd.task_id, "reason": "operator"}
    assert site.status.task is None and site.status.online is True
    x_end = (await dog.odometry()).x
    await t.run(5)
    assert (await dog.odometry()).x == x_end, "停了就是停了"

    # 8. 事件序号单调、无重复(站点侧按 (boot_id, seq) 去重)。
    seqs = [e.seq for e in t.events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


async def test_代理重启后旧命令仍是duplicate_新boot_id(台):
    t = 台
    cmd = _goto(t.site, 2.0, 0.0, epoch=2)
    first = await t.site.send(cmd, timeout_s=1.0)
    assert first.result is AckResult.ACCEPTED
    await t.run(3)
    # 重启:同一存储目录、新 boot_id;先把旧的连接干净关掉。
    await t.agent.close()
    t.broker.disconnect("dog")
    await t.broker.drain()
    t.dog = SimRobot(now_ms=t.clock)
    await t.start_agent("boot-2")
    assert t.site.status.boot_id == "boot-2"
    assert t.reconciles[-1].boot_id == "boot-2" and t.reconciles[-1].task is None
    again = await t.site.send(cmd, timeout_s=1.0)
    assert again.result is AckResult.DUPLICATE
    assert again.original == first.to_wire()
    assert t.agent.processor.current is None, "重复的命令没有重起任务"
    # 代次也记住了:旧代次的新命令被拒。
    stale = await t.site.send(_goto(t.site, 1.0, 0.0, epoch=1), timeout_s=1.0)
    assert stale.result is AckResult.REJECTED and stale.reason == "stale_epoch"


async def test_过期命令被拒_地图不符被拒(台):
    t = 台
    old = _goto(t.site, 1.0, 0.0, epoch=1, ttl_ms=1_000)
    t.clock.advance(5.0)
    assert (await t.site.send(old, timeout_s=1.0)).result is AckResult.EXPIRED
    bad = MapPose(map_id="estate-1", map_version="8", frame_id="map", x=1, y=0, yaw=0)
    cmd = t.site.new_command("goto", {"target": bad.to_wire()}, ttl_ms=60_000, control_epoch=1)
    ack = await t.site.send(cmd, timeout_s=1.0)
    assert ack.result is AckResult.REJECTED and ack.reason == "map_mismatch"
    assert math.isclose((await t.dog.odometry()).x, 0.0)
