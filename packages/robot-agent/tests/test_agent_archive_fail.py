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
