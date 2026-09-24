"""站点侧最小派遣客户端:发命令等回执、事件按 (boot_id, seq) 去重排序、状态/能力/对账最新值。
W00c 的派遣器在它上面长。"""

from __future__ import annotations

import json

import pytest

from d1max_contract.dispatch import DispatchClient, DispatchTimeout
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import (
    Ack,
    AckResult,
    Command,
    Event,
    Reconcile,
    Status,
)
from d1max_contract.topics import Topics

T = Topics(site_id="s", robot_id="r")


class _假代理:
    """收到 cmd 就回 accepted;可以叫它装死。"""

    def __init__(self, broker):
        self.t = MemoryTransport(broker, "dog")
        self.mute = False
        self.seen: list[Command] = []

    async def start(self):
        await self.t.connect()
        await self.t.subscribe(T.cmd, self._on_cmd)

    async def _on_cmd(self, m):
        cmd = Command.from_wire(json.loads(m.payload))
        self.seen.append(cmd)
        if self.mute:
            return
        ack = Ack(command_id=cmd.command_id, task_id=cmd.task_id, result=AckResult.ACCEPTED)
        await self.t.publish(T.ack, json.dumps(ack.to_wire()).encode())

    async def emit(self, seq, boot_id="b1", kind="task_progress"):
        e = Event(event_id=f"e{boot_id}-{seq}", seq=seq, boot_id=boot_id, stamp=seq, kind=kind,
                  data={"seq": seq})
        await self.t.publish(T.event, json.dumps(e.to_wire()).encode())


@pytest.fixture
async def 台子():
    broker = MemoryBroker()
    dog = _假代理(broker)
    await dog.start()
    site = DispatchClient(MemoryTransport(broker, "site"), T, now_ms=lambda: 10_000)
    await site.start()
    return broker, dog, site


async def test_new_command生成唯一id和过期时刻(台子):
    _, _, site = 台子
    a = site.new_command("goto", {"x": 1}, ttl_ms=5_000, control_epoch=2)
    b = site.new_command("goto", {"x": 1}, ttl_ms=5_000, control_epoch=2)
    assert a.command_id != b.command_id and a.task_id != b.task_id
    assert a.issued_at == 10_000 and a.expires_at == 15_000 and a.control_epoch == 2
    c = site.new_command("abort", {}, ttl_ms=1_000, control_epoch=2, task_id=a.task_id)
    assert c.task_id == a.task_id and c.command_id != a.command_id


async def test_send等到自己那条回执_别的不串(台子):
    broker, dog, site = 台子
    a = site.new_command("goto", {}, ttl_ms=5_000, control_epoch=1)
    b = site.new_command("goto", {}, ttl_ms=5_000, control_epoch=1)
    got_a = await site.send(a, timeout_s=2)
    got_b = await site.send(b, timeout_s=2)
    assert got_a.command_id == a.command_id and got_a.result is AckResult.ACCEPTED
    assert got_b.command_id == b.command_id
    assert [c.command_id for c in dog.seen] == [a.command_id, b.command_id]


async def test_没回执就超时(台子):
    _, dog, site = 台子
    dog.mute = True
    with pytest.raises(DispatchTimeout):
        await site.send(site.new_command("goto", {}, ttl_ms=5_000, control_epoch=1), timeout_s=0.05)


async def test_事件按boot_id_seq去重并排序(台子):
    broker, dog, site = 台子
    收: list[tuple[str, int]] = []
    site.on_event(lambda e: 收.append((e.boot_id, e.seq)))
    broker.duplicate_next(1)
    await dog.emit(1)
    await dog.emit(2)
    await dog.emit(1)                 # 站点侧再收一次重复
    await dog.emit(3)
    await dog.emit(1, boot_id="b2")   # 新 boot 的 seq 1 是新事件
    await broker.drain()
    assert 收 == [("b1", 1), ("b1", 2), ("b1", 3), ("b2", 1)]


async def test_status_capabilities_reconcile最新值可读(台子):
    broker, dog, site = 台子
    assert site.status is None and site.reconcile is None
    st = Status.from_wire({
        "schema": "1.0", "online": True, "boot_id": "b1",
        "ready": {"control": True, "motion": True, "estop_clear": True, "loc_ok": True},
        "control_epoch": 1, "last_seen": 5, "task": None})
    await dog.t.publish(T.status, json.dumps(st.to_wire()).encode(), retain=True)
    rc = Reconcile(boot_id="b1", control_epoch=1, task=None, unacked_from_seq=0, unacked_to_seq=0)
    await dog.t.publish(T.reconcile, json.dumps(rc.to_wire()).encode())
    await broker.drain()
    assert site.status == st
    assert site.reconcile == rc
    收: list[Reconcile] = []
    site.on_reconcile(收.append)
    await dog.t.publish(T.reconcile, json.dumps(rc.to_wire()).encode())
    await broker.drain()
    assert 收 == [rc]


async def test_坏报文只记日志不炸(台子, caplog):
    broker, dog, site = 台子
    await dog.t.publish(T.event, b"not json")
    await dog.t.publish(T.ack, json.dumps({"schema": "9.0"}).encode())
    await broker.drain()
    assert site.status is None
    assert any("解析" in r.getMessage() or "schema" in r.getMessage() for r in caplog.records)


async def test_attach只登记订阅不连接_站点一条连接挂多台狗():
    from d1max_contract.dispatch import DispatchClient
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_contract.topics import Topics

    broker = MemoryBroker()
    t = MemoryTransport(broker, "site")
    a = DispatchClient(t, Topics(site_id="s", robot_id="A"), now_ms=lambda: 1)
    b = DispatchClient(t, Topics(site_id="s", robot_id="B"), now_ms=lambda: 1)
    await a.attach()
    await b.attach()
    assert t.connected is False
    await t.connect()
    dog = MemoryTransport(broker, "dogB")
    await dog.connect()
    await dog.publish(Topics(site_id="s", robot_id="B").capabilities,
                      b'{"schema":"1.0","robot_id":"B","agent":"x","adapter":"y","tasks":{},'
                      b'"actuators":{},"sensing":{}}', qos=1, retain=True)
    await broker.drain()
    assert b.capabilities is not None and a.capabilities is None


async def test_遥测也订_存最近一条_回调(tmp_path):
    """W00c2c:站点按狗最近的位姿选最近的一台去拦截点。"""
    import json

    from d1max_contract.dispatch import DispatchClient
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_contract.messages import MapPose, Telemetry
    from d1max_contract.topics import Topics
    broker = MemoryBroker()
    t = Topics(site_id="s", robot_id="A")
    site = DispatchClient(MemoryTransport(broker, "site"), t, now_ms=lambda: 1)
    got = []
    site.on_telemetry(got.append)
    await site.start()
    dog = MemoryTransport(broker, "dog")
    await dog.connect()
    tel = Telemetry(stamp=5, pose=MapPose(map_id="m", map_version="1", frame_id="map", x=1.0,
                                          y=2.0, yaw=0.0), battery_pct=80.0, task_state=None,
                    loc_quality=1.0)
    await dog.publish(t.telemetry, json.dumps(tel.to_wire()).encode(), qos=0)
    await dog.publish(t.telemetry, b"not json", qos=0)
    await broker.drain()
    assert site.telemetry is not None and site.telemetry.pose.x == 1.0 and len(got) == 1
