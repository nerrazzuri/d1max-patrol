"""命令校验链,固定顺序(设计 §2):schema → 认证 → 代次 → 过期 → 重复 → 能力 → 前置 → 资源。
用假任务替掉真的 goto,只看校验与调度。"""

from __future__ import annotations

import pytest

from d1max_agent.commands import CommandProcessor
from d1max_agent.events import EventBook
from d1max_agent.idempotency import IdempotencyStore
from d1max_agent.resources import ResourceLedger
from d1max_agent.tasks.base import Task
from d1max_contract.messages import (
    AckResult,
    Command,
    MapPose,
    Precondition,
    TaskState,
)
from d1max_contract.registration import Registration

REG = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                   expires_at=10**12)
NOW = 100_000
POSE = MapPose(map_id="m1", map_version="3", frame_id="map", x=1.0, y=0.0, yaw=0.0)


class 假任务(Task):
    """可控的任务:state 由测试摆;abort 记一笔,stopped 由测试翻。"""

    def __init__(self, cmd, **kw):
        super().__init__(task_id=cmd.task_id, kind=cmd.kind, priority=cmd.priority)
        self.aborted_with: str | None = None
        self.stop_confirmed = False
        self.started = False

    async def start(self):
        self.started = True
        self.state = TaskState.RUNNING

    async def step(self, dt_s):
        if self.aborted_with is not None and self.stop_confirmed:
            preempted = self.aborted_with == "preempted"
            self.state = TaskState.PREEMPTED if preempted else TaskState.ABORTED

    async def abort(self, reason):
        self.aborted_with = reason


def _cmd(kind="goto", *, cid="c1", tid="t1", epoch=1, issued=NOW, ttl=60_000, priority=0,
         payload=None, pre=None):
    if payload is None:
        payload = {"target": POSE.to_wire(), "max_speed_mps": 0.5} if kind == "goto" else {}
    return Command(command_id=cid, task_id=tid, kind=kind, issued_at=issued,
                   expires_at=issued + ttl, control_epoch=epoch, payload=payload,
                   priority=priority, precondition=pre)


@pytest.fixture
def cp(tmp_path):
    clock = {"ms": NOW}
    p = CommandProcessor(
        registration=REG, now_ms=lambda: clock["ms"],
        idem=IdempotencyStore(tmp_path / "idem.jsonl"),
        events=EventBook(tmp_path / "events.jsonl", boot_id="b1", now_ms=lambda: clock["ms"]),
        ledger=ResourceLedger(), supported_tasks={"goto"},
        loaded_map=("m1", "3"), state_path=tmp_path / "state.json",
        task_factory=lambda cmd: 假任务(cmd))
    p.clock = clock
    return p


TOPIC = "site/s/robot/r/cmd"


async def test_合法goto被接受并起任务(cp):
    ack = await cp.handle(_cmd().to_wire(), TOPIC)
    assert ack.result is AckResult.ACCEPTED and ack.task_id == "t1"
    await cp.step(0.1)
    assert cp.current.task_id == "t1" and cp.current.started
    assert cp.ledger.holder("motion") == "t1"


async def test_schema主版本不同(cp):
    ack = await cp.handle({**_cmd().to_wire(), "schema": "2.0"}, TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason.startswith("schema")


async def test_不成形的命令也有回执(cp):
    ack = await cp.handle({"schema": "1.0", "command_id": "x"}, TOPIC)
    assert ack.result is AckResult.REJECTED and "task_id" in ack.reason


async def test_认证_不是发给我的(cp):
    ack = await cp.handle(_cmd().to_wire(), "site/s/robot/OTHER/cmd")
    assert ack.result is AckResult.REJECTED and ack.reason == "auth"
    ack = await cp.handle(_cmd(cid="c2").to_wire(), "site/t/robot/r/cmd")
    assert ack.result is AckResult.REJECTED and ack.reason == "auth"


async def test_代次_小的拒_大的采纳(cp):
    assert (await cp.handle(_cmd(cid="c1", epoch=5).to_wire(), TOPIC)).result is AckResult.ACCEPTED
    assert cp.control_epoch == 5
    ack = await cp.handle(_cmd(cid="c2", tid="t2", epoch=4).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "stale_epoch"
    ack = await cp.handle(_cmd("abort", cid="c3", tid="t1", epoch=7).to_wire(), TOPIC)
    assert ack.result is AckResult.ACCEPTED and cp.control_epoch == 7
    ack = await cp.handle(_cmd("abort", cid="c4", tid="t1", epoch=5).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "stale_epoch"


async def test_代次落盘_重启后旧代次仍拒(cp, tmp_path):
    await cp.handle(_cmd(cid="c1", epoch=5).to_wire(), TOPIC)
    again = CommandProcessor(
        registration=REG, now_ms=lambda: NOW, idem=IdempotencyStore(tmp_path / "idem2.jsonl"),
        events=EventBook(tmp_path / "events2.jsonl", boot_id="b2", now_ms=lambda: NOW),
        ledger=ResourceLedger(), supported_tasks={"goto"}, loaded_map=("m1", "3"),
        state_path=tmp_path / "state.json", task_factory=lambda cmd: 假任务(cmd))
    assert again.control_epoch == 5
    ack = await again.handle(_cmd(cid="c9", tid="t9", epoch=4).to_wire(), TOPIC)
    assert ack.reason == "stale_epoch"


async def test_过期(cp):
    ack = await cp.handle(_cmd(issued=NOW - 120_000, ttl=60_000).to_wire(), TOPIC)
    assert ack.result is AckResult.EXPIRED
    cp.clock["ms"] = NOW + 60_000
    ack = await cp.handle(_cmd(cid="c2", tid="t2").to_wire(), TOPIC)
    assert ack.result is AckResult.EXPIRED, "expires_at == now 也算过期"


async def test_重复投递回原结果且不重起(cp):
    first = await cp.handle(_cmd().to_wire(), TOPIC)
    await cp.step(0.1)
    task = cp.current
    again = await cp.handle(_cmd().to_wire(), TOPIC)
    assert again.result is AckResult.DUPLICATE
    assert again.original == first.to_wire()
    await cp.step(0.1)
    assert cp.current is task, "同一个任务对象,没有重起"
    rej = await cp.handle(_cmd("dance", cid="c2", tid="t2", payload={}).to_wire(), TOPIC)
    assert rej.result is AckResult.REJECTED and rej.reason == "unsupported"
    dup = await cp.handle(_cmd("dance", cid="c2", tid="t2", payload={}).to_wire(), TOPIC)
    assert dup.result is AckResult.DUPLICATE and dup.original == rej.to_wire(), "拒绝过的也记"


async def test_代次拒绝在重复判定之前_所以旧代次重投仍是stale(cp):
    """顺序钉死:代次 → 过期 → 重复。旧代次的命令重投回的还是 stale_epoch,不是 duplicate ——
    站点该看到的是「你的会话已经过时」,不是「这条命令我见过」。"""
    await cp.handle(_cmd(cid="c1", epoch=3).to_wire(), TOPIC)
    a = await cp.handle(_cmd(cid="c2", tid="t2", epoch=1).to_wire(), TOPIC)
    b = await cp.handle(_cmd(cid="c2", tid="t2", epoch=1).to_wire(), TOPIC)
    assert a.reason == b.reason == "stale_epoch"


async def test_重复判定在过期判定之后(cp):
    """顺序钉死:一条已经回过 accepted 的命令,过期后再来仍回 duplicate?
    不 —— 过期检查在前:重投的是同一条命令,它现在过期了,回 expired。这正是
    「重连后不执行十分钟前的出动」那一条。"""
    await cp.handle(_cmd(ttl=10_000).to_wire(), TOPIC)
    cp.clock["ms"] = NOW + 20_000
    ack = await cp.handle(_cmd(ttl=10_000).to_wire(), TOPIC)
    assert ack.result is AckResult.EXPIRED


async def test_不支持的任务(cp):
    ack = await cp.handle(_cmd("dance", payload={}).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "unsupported"


async def test_地图版本不符(cp):
    bad = MapPose(map_id="m1", map_version="4", frame_id="map", x=1, y=0, yaw=0)
    ack = await cp.handle(_cmd(payload={"target": bad.to_wire(), "max_speed_mps": 0.5}).to_wire(),
                          TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "map_mismatch"
    ack = await cp.handle(_cmd(cid="c2", payload={"target": {"x": 1}}).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason.startswith("payload")


async def test_前置条件(cp):
    await cp.handle(_cmd().to_wire(), TOPIC)
    await cp.step(0.1)
    ack = await cp.handle(_cmd("abort", cid="c2", tid="t1",
                               pre=Precondition(expect_task_state=TaskState.DONE)).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "precondition"
    ack = await cp.handle(_cmd("abort", cid="c3", tid="t1",
                               pre=Precondition(expect_task_state=TaskState.RUNNING)).to_wire(),
                          TOPIC)
    assert ack.result is AckResult.ACCEPTED
    ack = await cp.handle(_cmd("abort", cid="c4", tid="nope").to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "no_such_task"


async def test_资源忙_低优先级拒(cp):
    await cp.handle(_cmd().to_wire(), TOPIC)
    await cp.step(0.1)
    ack = await cp.handle(_cmd(cid="c2", tid="t2", priority=0).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "busy"


async def test_抢占_先停旧等确认再给新(cp):
    await cp.handle(_cmd().to_wire(), TOPIC)
    await cp.step(0.1)
    old = cp.current
    ack = await cp.handle(_cmd(cid="c2", tid="t2", priority=5).to_wire(), TOPIC)
    assert ack.result is AckResult.ACCEPTED
    await cp.step(0.1)
    assert old.aborted_with == "preempted", "先叫旧任务停"
    assert cp.ledger.holder("motion") == "t1", "旧任务还没确认停,motion 不许给新任务"
    assert cp.current is old
    assert not cp.pending[0].started, "新任务在旧任务确认停之前不许 start"
    await cp.step(0.1)                       # 再等一拍,旧任务仍没确认
    assert cp.ledger.holder("motion") == "t1" and not cp.pending[0].started
    old.stop_confirmed = True
    await cp.step(0.1)                       # 旧任务此拍进入 PREEMPTED
    await cp.step(0.1)                       # 新任务此拍拿到 motion 起跑
    assert cp.current.task_id == "t2" and cp.current.started
    assert cp.ledger.holder("motion") == "t2"
    kinds = [e.kind for e in cp.events.pending()]
    assert "task_preempted" in kinds


async def test_abort让当前任务停并释放资源(cp):
    await cp.handle(_cmd().to_wire(), TOPIC)
    await cp.step(0.1)
    ack = await cp.handle(_cmd("abort", cid="c2", tid="t1", payload={"reason": "op"}).to_wire(),
                          TOPIC)
    assert ack.result is AckResult.ACCEPTED
    await cp.step(0.1)
    assert cp.current.aborted_with == "op"
    cp.current.stop_confirmed = True
    await cp.step(0.1)
    assert cp.current is None
    assert cp.ledger.holder("motion") is None
    assert [e.kind for e in cp.events.pending()][-1] == "task_aborted"


async def test_注册过期一律auth拒(tmp_path):
    过期 = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                      expires_at=NOW - 1)
    p = CommandProcessor(
        registration=过期, now_ms=lambda: NOW, idem=IdempotencyStore(tmp_path / "i.jsonl"),
        events=EventBook(tmp_path / "e.jsonl", boot_id="b", now_ms=lambda: NOW),
        ledger=ResourceLedger(), supported_tasks={"goto"}, loaded_map=("m1", "3"),
        state_path=tmp_path / "s.json", task_factory=lambda cmd: 假任务(cmd))
    ack = await p.handle(_cmd().to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "auth"


async def test_abort还没起跑的pending任务直接撤(cp):
    await cp.handle(_cmd().to_wire(), TOPIC)
    await cp.step(0.1)
    ack = await cp.handle(_cmd(cid="c2", tid="t2", priority=5).to_wire(), TOPIC)   # 抢占,进 pending
    assert ack.result is AckResult.ACCEPTED and cp.pending[0].task_id == "t2"
    ack = await cp.handle(_cmd("abort", cid="c3", tid="t2", payload={"reason": "op"}).to_wire(),
                          TOPIC)
    assert ack.result is AckResult.ACCEPTED
    assert cp.pending == [] and cp.finished[-1].task_id == "t2"
    assert cp.finished[-1].state is TaskState.ABORTED
    assert [e.kind for e in cp.events.pending()][-1] == "task_aborted"


async def test_pending按优先级排_起跑前查过期(cp):
    await cp.handle(_cmd().to_wire(), TOPIC)
    await cp.step(0.1)
    old = cp.current
    await cp.handle(_cmd(cid="c2", tid="t2", priority=5, ttl=1_000).to_wire(), TOPIC)
    await cp.handle(_cmd(cid="c3", tid="t3", priority=7).to_wire(), TOPIC)
    assert [t.task_id for t in cp.pending] == ["t3", "t2"], "优先级高的排前面"
    old.stop_confirmed = True
    cp.clock["ms"] = NOW + 5_000                  # t2 已经过期
    await cp.step(0.1)                            # old 进终态
    await cp.step(0.1)                            # t3 起跑
    assert cp.current.task_id == "t3"
    cp.current.stop_confirmed = True
    await cp.current.abort("op")
    await cp.step(0.1)
    await cp.step(0.1)                            # 轮到 t2:过期,不起跑
    assert cp.current is None
    assert cp.finished[-1].task_id == "t2" and cp.finished[-1].state is TaskState.FAILED
    assert [e.kind for e in cp.events.pending()][-1] == "task_failed"
    assert cp.events.pending()[-1].data["reason"] == "expired_before_start"


def test_代次落盘是原子写(tmp_path):
    import inspect

    from d1max_agent import commands
    src = inspect.getsource(commands.CommandProcessor._save_epoch)
    assert "os.replace" in src, "写一半掉电会让代次倒退成 0"


async def test_限速是NaN或无穷_拒收(cp):
    """json.loads 认 NaN/Infinity;NaN <= 0 为假,只查「正数」会放它过去一路到 set_speed。"""
    for i, bad in enumerate((float("nan"), float("inf"))):
        ack = await cp.handle(_cmd(cid=f"n{i}", payload={"target": POSE.to_wire(),
                                                         "max_speed_mps": bad}).to_wire(), TOPIC)
        assert ack.result is AckResult.REJECTED and ack.reason.startswith("payload"), bad


def _patrol(cid, mission, version="3"):
    return _cmd("patrol", cid=cid, payload={"mission": mission, "map_version": version})


_MISSION = {"mission": "loop", "map_id": "m1", "waypoints": [
    {"name": "a", "pose": {"position": {"x": 1, "y": 0}, "orientation": {"x": 0, "y": 0,
                                                                           "z": 0, "w": 1}}}]}


async def test_patrol的payload校验(cp):
    assert cp.loaded_map == ("m1", "3"), "夹具的地图;下面的断言按它写"
    cp.supported.add("patrol")
    ack = await cp.handle(_patrol("p1", {"mission": "x"}).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason.startswith("payload: mission")
    ack = await cp.handle(_patrol("p2", _MISSION, version="4").to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "map_mismatch"
    vendor = dict(_MISSION, route={"source": "vendor_path", "path_id": "x"})
    ack = await cp.handle(_patrol("p3", vendor).to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and "inline" in ack.reason
    ack = await cp.handle(_cmd("patrol", cid="p4", payload={"mission": _MISSION}).to_wire(),
                          TOPIC)
    assert ack.result is AckResult.REJECTED and "map_version" in ack.reason


# ------------------------------------------------------------ W00c5b:video 命令(不是任务)

class 假推流:
    def __init__(self):
        self.got = []
        self.refuse = ""

    def request(self, req):
        self.got.append(req)
        return self.refuse


def _video(**kw):
    p = {"camera": "front", "url": "srt://10.0.0.5:8890", "passphrase": "Q7kP2mX9vL4nR8tW",
         "ttl_ms": 10_000} | kw
    return _cmd("video", tid="video-front", payload=p)


async def test_video命令交给推流_不起任务不占资源(cp):
    cp.video = 假推流()
    ack = await cp.handle(_video().to_wire(), TOPIC)
    assert ack.result is AckResult.ACCEPTED, ack
    assert cp.video.got[0].camera == "front" and cp.video.got[0].ttl_ms == 10_000
    await cp.step(0.1)
    assert cp.current is None and not cp.pending, "video 不是任务"
    ack = await cp.handle(_cmd(cid="c2").to_wire(), TOPIC)  # 同时派 goto 照常收
    assert ack.result is AckResult.ACCEPTED


async def test_video载荷坏的拒_推流起不来如实拒_没有推流能力拒(cp):
    cp.video = 假推流()
    ack = await cp.handle(_video(url="srt://x:1?mode=listener").to_wire(), TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason.startswith("payload:")
    cp.video.refuse = "起不了 ffmpeg"
    ack = await cp.handle({**_video().to_wire(), "command_id": "c9"}, TOPIC)
    assert ack.result is AckResult.REJECTED and "ffmpeg" in ack.reason
    cp.video = None
    ack = await cp.handle({**_video().to_wire(), "command_id": "c10"}, TOPIC)
    assert ack.result is AckResult.REJECTED and ack.reason == "unsupported"
