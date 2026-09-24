"""站点派遣器(W00c1 Task 4):MemoryBroker 上挂真的 AgentRuntime + SimRobot,钟注入、不睡。
派单经派遣条件、先落库再发、回执回写、事件去重落库,每条命令记下派单人。"""

from __future__ import annotations

import asyncio

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import MapPose
from d1max_patrol.protocol.nav_types import Pose
from d1max_site.db import SiteDB
from d1max_site.dispatcher import Dispatcher, DispatchRefused
from d1max_site.registry import Registry

SITE = "estate-1"
MAP = ("estate-1", "7")


class 钟:
    def __init__(self) -> None:
        self.ms = 1_800_000_000_000
        self.mono = 1000.0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))
        self.mono += dt_s


def target(x: float, y: float = 0.0) -> dict:
    return MapPose(map_id=MAP[0], map_version=MAP[1], frame_id="map", x=x, y=y,
                   yaw=0.0).to_wire()


class 台子:
    def __init__(self, tmp_path) -> None:
        self.clock = 钟()
        self.broker = MemoryBroker()
        self.db = SiteDB(tmp_path / "site.db")
        self.reg = Registry(self.db, site_id=SITE)
        self.reg.enroll("A", fingerprint="sha256:a", issued_at=self.clock.ms - 1,
                        expires_at=self.clock.ms + 10**10)
        self.site = Dispatcher(MemoryTransport(self.broker, "site"), self.db, self.reg,
                               now_ms=self.clock, ack_timeout_s=5.0)
        self.dog = SimRobot(now_ms=self.clock, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
        self.tmp = tmp_path
        self.agent: AgentRuntime | None = None

    async def start(self) -> None:
        await self.site.start()
        from d1max_contract.registration import Registration
        reg = Registration(site_id=SITE, robot_id="A", credential_fingerprint="sha256:a",
                           issued_at=0, expires_at=10**14)
        self.agent = AgentRuntime(transport=MemoryTransport(self.broker, "dogA"),
                                  registration=reg, hal=self.dog, store_dir=self.tmp / "agent",
                                  now_ms=self.clock, loaded_map=MAP, boot_id="boot-1",
                                  home=Pose.from_xy_yaw(0.0, 0.0),
                                  monotonic=lambda: self.clock.mono)
        await self.agent.start()
        await self.broker.drain()

    async def run(self, n: int, dt: float = 0.1) -> None:
        for _ in range(n):
            await self.agent.step(dt)
            self.dog.tick(dt)
            self.clock.advance(dt)
            for _ in range(4):
                await asyncio.sleep(0)
            await self.broker.drain()

    async def send(self, coro):
        """派单要等回执;回执要 broker 投递 —— 边等边推。"""
        task = asyncio.ensure_future(coro)
        for _ in range(50):
            await self.broker.drain()
            await asyncio.sleep(0)
            if task.done():
                break
        return await task

    async def close(self) -> None:
        if self.agent is not None:
            await self.agent.close()
        await self.site.close()
        self.db.close()


@pytest.fixture
async def 台(tmp_path):
    t = 台子(tmp_path)
    await t.start()
    yield t
    await t.close()


def _kinds(t, robot="A"):
    return [e["kind"] for e in reversed(t.site.recent_events(robot, 500))]


async def test_goto到done_事件落库_命令记着派单人(台):
    t = 台
    r = await t.send(t.site.goto("A", target(1.0), 0.8, issued_by="alice"))
    assert r["ack"]["result"] == "accepted"
    await t.run(200)
    kinds = _kinds(t)
    assert "task_done" in kinds or "task_state" in kinds, kinds
    cmds = t.site.commands("A")
    assert cmds[0]["issued_by"] == "alice" and cmds[0]["ack_result"] == "accepted"
    assert cmds[0]["kind"] == "goto" and cmds[0]["task_id"] == r["task_id"]
    view = t.site.robot_view("A")
    assert view["status"]["online"] is True and view["fresh"] is True
    assert (await t.dog.odometry()).x == pytest.approx(1.0, abs=0.15)


async def test_abort中途停下(台):
    t = 台
    r = await t.send(t.site.goto("A", target(6.0), 0.8, issued_by="alice"))
    await t.run(20)
    a = await t.send(t.site.abort("A", r["task_id"], issued_by="bob"))
    assert a["ack"]["result"] == "accepted", a
    await t.run(40)
    st = t.site.robot_view("A")["status"]
    assert st["task"] is None or st["task"]["state"] in ("aborted",), st
    assert await t.dog.stopped() is True and (await t.dog.odometry()).x < 5.0
    assert [c["issued_by"] for c in t.site.commands("A")] == ["bob", "alice"]


async def test_未登记_吊销_离线_不新鲜_都拒派(台):
    t = 台
    with pytest.raises(DispatchRefused, match="没有登记"):
        await t.site.goto("ghost", target(1.0), None, issued_by="alice")
    t.clock.advance(100)                                   # 状态不新鲜
    with pytest.raises(DispatchRefused, match="不新鲜"):
        await t.site.goto("A", target(1.0), None, issued_by="alice")
    await t.agent.close()                                  # LWT → offline
    await t.broker.drain()
    with pytest.raises(DispatchRefused, match="不在线"):
        await t.site.goto("A", target(1.0), None, issued_by="alice")
    t.reg.revoke("A")
    with pytest.raises(DispatchRefused, match="吊销"):
        await t.site.goto("A", target(1.0), None, issued_by="alice")
    t.agent = None
    assert t.site.commands("A") == [], "被拒的派单不落命令表"


async def test_急停时没就绪_拒派并说出哪一项(台):
    t = 台
    await t.dog.emergency_stop(True)
    await t.run(12)                                        # 等一次 status 刷新
    t.agent._next_status_ms = 0
    await t.run(1)
    with pytest.raises(DispatchRefused, match="estop_clear"):
        await t.site.goto("A", target(1.0), None, issued_by="alice")


async def test_没有派单人拒绝(台):
    with pytest.raises(DispatchRefused, match="派单人"):
        await 台.site.goto("A", target(1.0), None, issued_by="")


async def test_target不成形拒绝(台):
    with pytest.raises(DispatchRefused, match="target"):
        await 台.site.goto("A", {"x": 1}, None, issued_by="alice")


async def test_事件去重落库_订阅者收得到(台):
    t = 台
    sub = t.site.feed.subscribe()
    await t.send(t.site.goto("A", target(0.5), 0.8, issued_by="alice"))
    await t.run(100)
    n = len(t.site.recent_events("A", 500))
    assert n > 0
    # 站点重启后内存里的去重集合没了,broker 又补投同一批(QoS 1 重复):表里不多
    import json as _json

    from d1max_contract.messages import Event
    row = t.db.query("SELECT * FROM events LIMIT 1")[0]
    dup = Event(event_id=row["event_id"], seq=row["seq"], boot_id=row["boot_id"],
                stamp=row["stamp"], kind=row["kind"], data=_json.loads(row["data"]))
    t.site._on_event("A", dup)
    assert t.db.query("SELECT count(*) AS n FROM events")[0]["n"] == n
    got = []
    while (item := sub.get(0)) is not None:
        got.append(item["kind"])
    assert "ack" in got and "event" in got and "status" in got


async def test_登记之后add_robot就能派(台, tmp_path):
    t = 台
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    await t.site.add_robot("B")
    assert "B" in t.site.clients
    with pytest.raises(DispatchRefused, match="不在线"):
        await t.site.goto("B", target(1.0), None, issued_by="alice")
