"""代理这一层的 W00c6a(核查 B):叫停不依赖引擎、引擎死了任务自己收尾、归档写不进去报人。

以前(仿真实测过):引擎任务死掉之后叫停只做一次 ``hal.stop()``,导航桥还在 ACTIVE,下一拍又发
速度 —— 叫停后 5 秒狗从 1.3 m 走到 5.7 m,回执还是「收下」。
"""

from __future__ import annotations

import asyncio
import errno
import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.engine import archive as arc
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics
from d1max_patrol.protocol.nav_types import NavStatus, Pose

REG = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                   expires_at=10**12)
T = Topics(site_id="s", robot_id="r")


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000
        self.mono = 50.0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))
        self.mono += dt_s


class 耳朵:
    def __init__(self) -> None:
        self.by: dict[str, list[dict]] = {}

    async def __call__(self, m):
        kind = T.parse(m.topic)[2]
        self.by.setdefault(kind, []).append(json.loads(m.payload))


@pytest.fixture
async def 台子(tmp_path):
    broker, c = MemoryBroker(), 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    ears = 耳朵()
    site = MemoryTransport(broker, "site")
    await site.connect()
    await site.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono)
    await rt.start()
    await broker.drain()
    yield broker, c, r, ears, rt, site
    await rt.close()


def _cmd(kind, payload, cid, c, *, task_id=None):
    return {"schema": "1.0", "command_id": cid, "task_id": task_id or f"{kind}-{cid}",
            "kind": kind, "issued_at": c(), "expires_at": c() + 60_000, "control_epoch": 1,
            "priority": 100 if kind == "halt" else 0, "offline_policy": "default",
            "precondition": None, "payload": payload}


def _goto(x, cid, c):
    return _cmd("goto", {"target": {"schema": "1.0", "map_id": "m", "map_version": "1",
                                    "frame_id": "map", "x": x, "y": 0.0, "yaw": 0.0},
                         "max_speed_mps": 0.8}, cid, c)


async def _跑(rt, r, c, broker, n, dt=0.1):
    for _ in range(n):
        await rt.step(dt)
        r.tick(dt)
        c.advance(dt)
        for _ in range(4):
            await asyncio.sleep(0)
        await broker.drain()


async def _杀掉引擎(engine):
    """模拟「引擎任务死了、快照还是 RUNNING」:收尾那一段也不让它走(正常路径下 W00c6a
    的收尾一定落成终态,这里是给看门那一道用的)。"""
    async def 不收尾():
        return None
    engine._finish = 不收尾
    engine._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await engine._task


async def test_引擎死了之后叫停_狗停得住(台子):
    broker, c, r, ears, rt, site = 台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    assert (await r.odometry()).vx > 0.3, "前提:狗在走"
    await _杀掉引擎(rt.parts.engine)
    await site.publish(T.cmd, json.dumps(_cmd("halt", {"reason": "operator"}, "h1", c)).encode())
    await _跑(rt, r, c, broker, 5)
    x0 = (await r.odometry()).x
    await _跑(rt, r, c, broker, 50)
    o = await r.odometry()
    assert o.x - x0 < 0.05 and abs(o.vx) < 0.01, f"叫停后 5 秒又走了 {o.x - x0:.2f} m"
    assert await rt.parts.nav.nav_status() is not NavStatus.ACTIVE


async def test_引擎死了_任务自己收尾_停车_报失败(台子):
    broker, c, r, ears, rt, site = 台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    await _杀掉引擎(rt.parts.engine)
    await _跑(rt, r, c, broker, 1)
    assert rt.processor.current is not None and not rt.processor.current.done, \
        "先停车、等停稳和导航回待命,再收尾(不然下一趟在驻留期起跑)"
    await _跑(rt, r, c, broker, 60)
    failed = [e for e in ears.by.get("event", []) if e["kind"] == "task_failed"]
    assert failed and "engine_died" in failed[-1]["data"]["reason"]
    o = await r.odometry()
    assert abs(o.vx) < 0.01, "看门那一道也要把车停下"
    assert rt.processor.current is None or rt.processor.current.done


async def test_归档写不进去_一趟报一条archive_write_failed(台子, monkeypatch):
    broker, c, r, ears, rt, site = 台子
    坏 = {"on": False}
    real = arc.RunArchive._line

    def line(self, *a, **k):
        if 坏["on"]:
            raise OSError(errno.EROFS, "Read-only file system")
        return real(self, *a, **k)
    monkeypatch.setattr(arc.RunArchive, "_line", line)
    await site.publish(T.cmd, json.dumps(_goto(3.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 10)
    坏["on"] = True
    await _跑(rt, r, c, broker, 80)
    got = [e for e in ears.by.get("event", []) if e["kind"] == "archive_write_failed"]
    assert len(got) == 1, [(e["kind"], e["data"]) for e in ears.by.get("event", [])
                           if e["kind"] != "task_progress"]
    assert got[0]["data"]["task_id"] == "goto-g1" and "Read-only" in got[0]["data"]["reason"]
    done = [e for e in ears.by.get("event", []) if e["kind"] in ("task_done", "task_failed")]
    assert done, "归档写不进去不拦这一趟跑完"


async def test_命令队列被堵住时_抢先的那一路叫停就停得住_引擎活着也不重走(台子):
    """halt 不排队(W00c5c):前面一条命令卡在等回执时(上行拥堵,可能好几秒),只有抢先的那一路在跑。
    W00c6a 内审前的突变检查发现:抢先那一路只停 HAL 的话,导航桥下一拍又发速度;只撤导航桥的话,
    活着的引擎把 Cancelled 当「这个点没到」、0.5 s 后重发这个点,狗又走起来 —— 直到排队的中止
    轮到为止。所以抢先那一路也要当场请求中止当前任务。"""
    broker, c, r, ears, rt, site = 台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    assert (await r.odometry()).vx > 0.3
    async with rt._cmd_lock:                        # 排队的那一路进不来
        t = asyncio.create_task(rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h1", c))))
        await asyncio.sleep(0.05)
        assert await rt.parts.nav.nav_status() is not NavStatus.ACTIVE, "抢先那一路要撤导航桥"
        await _跑(rt, r, c, broker, 5)
        x0 = (await r.odometry()).x
        await _跑(rt, r, c, broker, 30)             # 3 秒:引擎要是重发这个点,早就又走了
        o = await r.odometry()
        assert o.x - x0 < 0.05 and abs(o.vx) < 0.01, f"队列堵着时又走了 {o.x - x0:.2f} m"
    await t


async def test_引擎死了_队列又堵着_抢先的叫停当场撤导航桥(台子):
    """引擎死了就没人处理中止;看门那一道要等下一拍、而这一拍里导航桥先走(先发速度)。抢先那一路
    自己撤导航桥,这个窗口才没有。"""
    broker, c, r, ears, rt, site = 台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    await _杀掉引擎(rt.parts.engine)
    async with rt._cmd_lock:
        t = asyncio.create_task(rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h1", c))))
        await asyncio.sleep(0.05)
        assert await rt.parts.nav.nav_status() is not NavStatus.ACTIVE
    await t


async def test_排队那一路的停车挂钩也撤导航桥(台子):
    broker, c, r, ears, rt, site = 台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    assert await rt.parts.nav.nav_status() is NavStatus.ACTIVE
    await rt.processor.halt_hook()
    assert await rt.parts.nav.nav_status() is not NavStatus.ACTIVE


def _msg(cmd):
    from d1max_contract.transport import Message
    return Message(T.cmd, json.dumps(cmd).encode(), 1, False)


# ------------------------------------------------ 内部评审修复(W00c6a:叫停的顺序、栅栏、慢 HAL)


class 慢停车(SimRobot):
    """真狗的 ``hal.stop()`` 要等旁路进程回执(最长 5 s):**每一次** stop 都要等 ``latency_s``
    (按注入的钟,测试一拍一拍推进);``fail`` 为真时 stop 抛错(旁路进程回不了停车回执)。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.latency_s = 0.0
        self.fail = False

    async def stop(self):
        t0 = self._now()
        while self._now() - t0 < self.latency_s * 1000:
            await asyncio.sleep(0)
        if self.fail:
            raise TimeoutError("旁路进程回不了停车回执")
        return await super().stop()


@pytest.fixture
async def 慢台子(tmp_path):
    broker, c = MemoryBroker(), 钟()
    r = 慢停车(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    ears = 耳朵()
    site = MemoryTransport(broker, "site")
    await site.connect()
    await site.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono)
    await rt.start()
    await broker.drain()
    yield broker, c, r, ears, rt, site
    r.latency_s = 0.0
    r.fail = False
    await rt.close()


def _事件(ears, kind):
    return [e for e in ears.by.get("event", []) if e["kind"] == kind]


async def test_停车回执慢_叫停也不重走_终态是halt中止_运行时不炸(慢台子):
    """W00c6a 内审阻断 B2 + 应修 S1:以前先停 HAL 再中止,真 HAL 停车要等回执,这段时间引擎把导航的
    Cancelled 当「这个点没到」重发这个点;导航桥在等回执期间是「ACTIVE 却没有目标」,
    每拍 assert 炸。"""
    broker, c, r, ears, rt, site = 慢台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    assert (await r.odometry()).vx > 0.3
    r.latency_s = 1.5                                 # 每一次停车都要等 1.5 s 回执
    t = asyncio.create_task(rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h1", c))))
    await asyncio.sleep(0)
    for _ in range(50):                               # 等回执期间:运行时照常一拍一拍走
        await _跑(rt, r, c, broker, 1)
    await t
    await _跑(rt, r, c, broker, 5)
    x0 = (await r.odometry()).x
    await _跑(rt, r, c, broker, 40)
    o = await r.odometry()
    assert o.x - x0 < 0.05 and abs(o.vx) < 0.01, f"叫停后又走了 {o.x - x0:.2f} m"
    aborted = _事件(ears, "task_aborted")
    assert aborted and aborted[-1]["data"]["reason"] == "halt", "终态是叫停中止"
    assert not _事件(ears, "task_failed")


async def test_叫停时排队里已收下的任务也清掉_栅栏立着不起新任务(台子):
    """W00c6a 内审阻断 B1:低优先级 goto 在跑,高优先级 g2 已收下、在排队(等 g1 停稳);这时叫停,
    以前抢先那一路只中止当前的,g1 停稳之后下一拍 g2 起跑,狗又走了 1.27 m。"""
    broker, c, r, ears, rt, site = 台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    g2 = _goto(-5.0, "g2", c)
    g2["priority"] = 5
    async with rt._cmd_lock:
        pass
    await rt._on_cmd(_msg(g2))
    assert any(t.task_id == "goto-g2" for t in rt.processor.pending), "前提:g2 在排队"
    async with rt._cmd_lock:                          # 排队那一路的叫停被堵着
        t = asyncio.create_task(rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h1", c))))
        await asyncio.sleep(0.05)
        await _跑(rt, r, c, broker, 5)
        x0 = (await r.odometry()).x
        await _跑(rt, r, c, broker, 40)
        o = await r.odometry()
        assert abs(o.x - x0) < 0.05 and abs(o.vx) < 0.01, f"叫停后又走了 {o.x - x0:.2f} m"
    await t
    assert not [e for e in _事件(ears, "task_progress") if e["data"]["task_id"] == "goto-g2"]


async def test_叫停之前排在锁前面的运动命令_栅栏立着就拒(台子):
    """狗空闲时,一条比叫停先到、排在锁前面的 goto,以前会在抢先叫停之后被收下并起跑。"""
    broker, c, r, ears, rt, site = 台子
    async with rt._cmd_lock:
        g = asyncio.create_task(rt._on_cmd(_msg(_goto(5.0, "g9", c))))
        await asyncio.sleep(0.01)
        h = asyncio.create_task(rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h9", c))))
        await asyncio.sleep(0.01)
    await asyncio.gather(g, h)
    await broker.drain()
    acks = {a["command_id"]: a for a in ears.by.get("cmd/ack", [])}
    assert acks["g9"]["result"] != "accepted" and acks["g9"]["reason"] == "halting", acks["g9"]
    assert acks["h9"]["result"] == "accepted"
    await site.publish(T.cmd, json.dumps(_goto(5.0, "g10", c)).encode())
    await broker.drain()
    assert {a["command_id"]: a for a in ears.by["cmd/ack"]}["g10"]["result"] == "accepted", \
        "叫停处理完之后栅栏撤了"


async def test_重投的叫停_旧代次的叫停_抢先那一路只停车不中止任务(台子):
    """W00c6a 内审 S2:抢先那一路不查幂等和代次就中止任务 —— 同一条叫停重投(回执会是 duplicate)、
    旧代次的叫停(回执会是 stale_epoch),都把正在跑的 goto 中止了。"""
    broker, c, r, ears, rt, site = 台子
    h = _cmd("halt", {"reason": "operator"}, "h1", c)
    await rt._on_cmd(_msg(h))
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 10)
    await rt._on_cmd(_msg(h))                        # 重投
    stale = _cmd("halt", {"reason": "operator"}, "h2", c)
    stale["control_epoch"] = 0
    await rt._on_cmd(_msg(stale))
    await _跑(rt, r, c, broker, 30)
    assert not [e for e in _事件(ears, "task_aborted") if e["data"]["task_id"] == "goto-g1"]


async def test_HAL停车抛错_导航桥不卡_运行时不炸_任务收尾(慢台子):
    """W00c6a 内审 S1:HAL 停车抛错时,导航桥先清了目标、没进终态,之后每拍 assert 炸;看门那一道
    永远跑不到。"""
    broker, c, r, ears, rt, site = 慢台子
    await site.publish(T.cmd, json.dumps(_goto(20.0, "g1", c)).encode())
    await _跑(rt, r, c, broker, 20)
    r.fail = True
    await rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h1", c)))
    await broker.drain()
    ack = [a for a in ears.by["cmd/ack"] if a["command_id"] == "h1"][-1]
    assert ack["result"] != "accepted" and "stop_failed" in ack["reason"], "停车没成要如实说"
    engine = rt.parts.engine
    for _ in range(5):
        await _跑(rt, r, c, broker, 1)              # 停车还在失败:引擎处理中止也撞上它,不许抛
    r.fail = False
    for _ in range(80):
        await _跑(rt, r, c, broker, 1)              # 不许抛
    assert engine.crash == "" and engine.snapshot.reason == "halt", \
        "停导航失败也要把中止走完(原因是 halt),不是炸穿之后被收尾兜成「收尾时出错」"
    from d1max_patrol.protocol.nav_types import NavStatus
    assert await rt.parts.nav.nav_status() is not NavStatus.ACTIVE
    assert rt.processor.current is None or rt.processor.current.done


async def test_代理自己的事件簿和幂等记录写不进去_每拍不炸_叫停回执照发(台子, tmp_path):
    """W00c6a 内审 S3:默认部署下代理的 store-dir 跟归档在同一块盘上(``/var/lib/d1max``)。盘只读时
    以前事件簿、幂等记录的写一抛,那一拍的状态、遥测都不发;每条命令处理完记幂等时抛,回执发不出去
    —— 叫停也一样。"""
    import os
    import stat
    if os.geteuid() == 0:
        pytest.skip("root 写得进只读文件,这条测不出来")
    broker, c, r, ears, rt, site = 台子
    files = [rt.events._path, rt.idem._path]
    for p in files:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    try:
        for p in files:
            p.chmod(stat.S_IRUSR)
        await site.publish(T.cmd, json.dumps(_goto(5.0, "g1", c)).encode())
        await _跑(rt, r, c, broker, 10)                # 不许抛
        await rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h1", c)))
        await broker.drain()
        acks = {a["command_id"]: a["result"] for a in ears.by.get("cmd/ack", [])}
        assert acks.get("g1") == "accepted" and acks.get("h1") == "accepted", acks
        await _跑(rt, r, c, broker, 30)
        assert abs((await r.odometry()).vx) < 0.01
        assert any(e["kind"] == "task_aborted" for e in ears.by.get("event", [])), \
            "事件照发(内存里),只是落不了盘"
    finally:
        for p in files:
            p.chmod(stat.S_IRUSR | stat.S_IWUSR)


async def test_到了点_停稳之前来的叫停_报叫停中止(台子):
    """引擎已经 DONE、任务还在等停车确认时来了叫停:请求过中止就报中止(W00c6a 内审)。"""
    broker, c, r, ears, rt, site = 台子
    await site.publish(T.cmd, json.dumps(_goto(0.5, "g1", c)).encode())
    from d1max_agent.engine.machine import RunState
    for _ in range(100):
        await _跑(rt, r, c, broker, 1)
        if rt.parts.engine.snapshot.state is RunState.DONE:
            break
    cur = rt.processor.current
    assert cur is not None and not cur.done, "前提:引擎跑完了,任务还在等停稳"
    await rt._on_cmd(_msg(_cmd("halt", {"reason": "operator"}, "h1", c)))
    await _跑(rt, r, c, broker, 20)
    ends = [e for e in ears.by.get("event", []) if e["kind"] in ("task_done", "task_aborted")]
    assert ends and ends[-1]["kind"] == "task_aborted" and ends[-1]["data"]["reason"] == "halt"
