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
        await self.broker.drain()
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


async def test_sync_robots把命令行新登记的狗挂上_吊销的不挂(台):
    t = 台
    t.reg.enroll("C", fingerprint="sha256:c", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    t.reg.enroll("D", fingerprint="sha256:d", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    t.reg.revoke("D")
    added = await t.site.sync_robots()
    assert added == ["C"] and "C" in t.site.clients and "D" not in t.site.clients
    assert await t.site.sync_robots() == []


async def test_关了之后到的上行报文不落库也不炸(台):
    """Paho 的回调是 call_soon_threadsafe 投回循环的:close() 之后还可能有排着队的上行
    回调跑起来,那时库已经关了。直接投一份,确认既不落库也不抛。"""
    import json as _json

    from d1max_contract.messages import Ack, AckResult, Event
    t = 台
    await t.run(5)
    st = t.site.clients["A"].status
    await t.site.close()
    t.db.close()
    t.site._on_status("A", st)
    t.site._on_event("A", Event(event_id="e", seq=999, boot_id="b", stamp=1, kind="x",
                                data={}))
    t.site._record_ack(Ack(command_id="c", task_id="t", result=AckResult.ACCEPTED))
    assert _json                                             # 只为别让 import 被当成没用
    await t.agent.close()
    await t.broker.drain()
    t.agent = None
    t.db = SiteDB(t.tmp / "site2.db")           # 夹具收尾要关一个库



async def test_新鲜度看站点收到的时刻_不看狗的钟(tmp_path):
    """Orin 的钟是错的(现场记录)。狗的钟慢 10 分钟,站点照样能派;
    last_seen 是狗填的,拿站点的钟减它会误判。"""
    t = 台子(tmp_path)
    await t.site.start()
    from d1max_contract.registration import Registration
    reg = Registration(site_id=SITE, robot_id="A", credential_fingerprint="sha256:a",
                       issued_at=0, expires_at=10**14)
    dog_clock = lambda: t.clock() - 600_000                      # noqa: E731
    t.dog = SimRobot(now_ms=dog_clock, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    t.agent = AgentRuntime(transport=MemoryTransport(t.broker, "dogA"), registration=reg,
                           hal=t.dog, store_dir=tmp_path / "agent", now_ms=dog_clock,
                           loaded_map=MAP, boot_id="boot-1", home=Pose.from_xy_yaw(0.0, 0.0),
                           monotonic=lambda: t.clock.mono)
    await t.agent.start()
    await t.broker.drain()
    r = await t.send(t.site.goto("A", target(0.5), 0.8, issued_by="alice"))
    assert r["ack"]["result"] in ("accepted", "expired"), r
    assert t.site.robot_view("A")["fresh"] is True
    await t.close()


async def test_回执超时记timeout_晚到的回执再补上(tmp_path):
    """狗收到了、但回执晚于超时:站点给人回 504,库里不能是空的;回执到了要补上。"""
    import json as _json

    from d1max_contract.dispatch import DispatchTimeout
    from d1max_contract.messages import Ack, AckResult, Ready, Status
    from d1max_contract.topics import Topics
    t = 台子(tmp_path)
    t.site.ack_timeout_s = 0.05
    await t.site.start()
    ta = Topics(site_id=SITE, robot_id="A")
    fake = MemoryTransport(t.broker, "fakeA")
    await fake.connect()
    st = Status(online=True, boot_id="b", ready=Ready(True, True, True, True), control_epoch=1,
                last_seen=t.clock(), task=None)
    await fake.publish(ta.status, _json.dumps(st.to_wire()).encode(), qos=1)
    await t.broker.drain()
    with pytest.raises(DispatchTimeout):
        await t.site.goto("A", target(0.5), 0.8, issued_by="alice")
    row = t.site.commands("A")[0]
    assert row["ack_result"] == "timeout"
    ack = Ack(command_id=row["command_id"], task_id=row["task_id"], result=AckResult.ACCEPTED)
    await fake.publish(ta.ack, _json.dumps(ack.to_wire()).encode(), qos=1)
    await t.broker.drain()
    assert t.site.commands("A")[0]["ack_result"] == "accepted"
    await t.site.close()
    t.db.close()


async def test_重复事件不重复推给订阅者(台):
    t = 台
    import json as _json

    from d1max_contract.messages import Event
    await t.send(t.site.goto("A", target(0.3), 0.8, issued_by="alice"))
    await t.run(40)
    row = t.db.query("SELECT * FROM events LIMIT 1")[0]
    sub = t.site.feed.subscribe()
    dup = Event(event_id=row["event_id"], seq=row["seq"], boot_id=row["boot_id"],
                stamp=row["stamp"], kind=row["kind"], data=_json.loads(row["data"]))
    t.site._on_event("A", dup)
    assert sub.get(0) is None


async def test_订阅者跟不上_清掉积压再给快照(台):
    t = 台
    sub = t.site.feed.subscribe()
    for i in range(1100):
        t.site.feed.publish({"kind": "x", "i": i})
    assert sub.lagged
    assert sub.drain() == 1000 and sub.get(0) is None


async def test_patrol太大_发之前就拒(台):
    t = 台
    pt = {"position": {"x": 0.1, "y": 0}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}
    big = {"mission": "big", "map_id": "estate-1", "policy": {},
           "waypoints": [{"name": f"p{i}", "pose": pt} for i in range(501)]}
    with pytest.raises(DispatchRefused, match="500"):
        await t.site.patrol("A", big, issued_by="alice")
    assert not [c for c in t.site.commands("A") if c["kind"] == "patrol"]


async def test_一直只收到过retained状态的狗_挂上超过时限就算过期(台):
    """W00c5a 内部评审:站点连着 broker 整机重启,没人发遗言,retained 里还写着在线 ——
    不能永远不算过期。"""
    from d1max_site.dispatcher import STALE_MS
    t = 台
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    await t.site.add_robot("B")
    assert not t.site.is_stale("B")
    t.clock.advance(STALE_MS / 1000 + 1)
    assert t.site.is_stale("B")
    assert not t.site.is_stale("ghost"), "没登记的不算"


async def test_video命令的有效期跟推流的有效期分开_至少30秒(台, monkeypatch):
    """W00c5b 内部评审:狗用自己的钟判命令过期,狗钟偏十几秒时,只给 10 s 的命令全会 expired。"""
    from d1max_contract.messages import Ack, AckResult
    from d1max_contract.video import VideoRequest
    from d1max_site.dispatcher import VIDEO_COMMAND_TTL_MS
    t = 台
    await t.run(3)
    got = {}

    async def 记下(cmd, timeout_s):
        got["cmd"], got["timeout"] = cmd, timeout_s
        return Ack(cmd.command_id, cmd.task_id, AckResult.ACCEPTED)
    monkeypatch.setattr(t.site.clients["A"], "send", 记下)
    req = VideoRequest(camera="front", url="srt://10.0.0.5:8890", passphrase="Q7kP2mX9vL4nR8tW",
                       ttl_ms=10_000)
    await t.site.video("A", req, timeout_s=2.5)
    c = got["cmd"]
    assert c.expires_at - c.issued_at == VIDEO_COMMAND_TTL_MS == 30_000
    assert got["timeout"] == 2.5


def test_推送流的同步监听_每条都调_监听炸了不挡订阅者():
    """W00c6b:站点自己的告警源要听推送流(``standby_failed``);监听自己炸了,SSE 订阅者照样收得到。"""
    from d1max_site.dispatcher import Feed
    feed = Feed()
    sub = feed.subscribe()
    got = []

    def 炸(item):
        raise RuntimeError("监听炸了")
    feed.listen(炸)
    feed.listen(got.append)
    feed.publish({"kind": "x"})
    assert got == [{"kind": "x"}] and sub.get(0) == {"kind": "x"}


async def test_发之前记账炸了_命令不留账(台):
    """外审(Qwen)第三节:「发之前记账」(排程执行器记这一轮起跑过)抛了异常,命令已经入了账却没发
    出去 —— 命令账里留一条永远等不到回执的。现在删掉它再往上抛。"""
    t = 台
    pt = {"position": {"x": 0.1, "y": 0}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}
    m = {"mission": "m", "map_id": "estate-1", "policy": {},
         "waypoints": [{"name": "p", "pose": pt}]}

    def 炸(cmd):
        raise RuntimeError("库写不进去")
    with pytest.raises(RuntimeError, match="库写不进去"):
        await t.site.patrol("A", m, issued_by="alice", before_send=炸)
    assert not [c for c in t.site.commands("A") if c["kind"] == "patrol"]
