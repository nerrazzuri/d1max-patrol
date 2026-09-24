"""运行时:起来就发 retained capabilities 与 status;断线 LWT;重连第一条 reconcile;
telemetry 每秒一条;命令经 ACL 守卫的 transport 进来、回执出去。"""

from __future__ import annotations

import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities, Reconcile, Status, Telemetry
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics
from d1max_patrol.protocol.nav_types import Pose

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


class 站点耳朵:
    def __init__(self) -> None:
        self.by_topic: dict[str, list[dict]] = {}
        self.order: list[str] = []

    async def __call__(self, m):
        kind = T.parse(m.topic)[2]
        self.by_topic.setdefault(kind, []).append(json.loads(m.payload))
        self.order.append(kind)


@pytest.fixture
async def 台子(tmp_path):
    broker = MemoryBroker()
    c = 钟()
    r = SimRobot(now_ms=c)
    ears = 站点耳朵()
    site = MemoryTransport(broker, "site")
    await site.connect()
    await site.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="boot-1", home=Pose.from_xy_yaw(0.0, 0.0),
                      monotonic=lambda: c.mono)
    yield broker, c, r, ears, rt, site
    await rt.close()


async def test_起来就发能力与状态_均retained(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    caps = Capabilities.from_wire(ears.by_topic["capabilities"][0])
    assert caps.robot_id == "r" and list(caps.tasks) == ["goto", "patrol"]
    assert caps.tasks["patrol"] == {"map_id": "m", "map_version": "1"}
    assert caps.tasks["goto"]["max_speed_mps"] == r.max_vx
    assert caps.adapter.startswith("sim/")
    st = Status.from_wire(ears.by_topic["status"][-1])
    assert st.online is True and st.boot_id == "boot-1" and st.ready.ok and st.task is None
    late = MemoryTransport(broker, "late")
    await late.connect()
    got: list[str] = []

    async def 收(m):
        got.append(T.parse(m.topic)[2])
    await late.subscribe(f"{T.prefix}/#", 收)
    await broker.drain()
    assert sorted(got) == ["capabilities", "status"], "两份都 retained"


async def test_起来之后设备桥也是连上的_老HTTP面的灯才是绿的(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    assert rt.parts.device.connected is True and rt.parts.nav.connected is True


async def test_断线LWT改写为offline_重连第一条是reconcile(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    ears.order.clear()
    broker.disconnect("dog")
    await broker.drain()
    st = Status.from_wire(ears.by_topic["status"][-1])
    assert st.online is False and st.boot_id == "boot-1"
    assert rt.online is False
    ears.order.clear()
    await rt.transport.connect()
    await broker.drain()
    assert ears.order[0] == "reconcile"
    rc = Reconcile.from_wire(ears.by_topic["reconcile"][-1])
    assert rc.boot_id == "boot-1" and rc.task is None
    assert Status.from_wire(ears.by_topic["status"][-1]).online is True


async def test_telemetry每秒一条(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    for _ in range(25):
        await rt.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
    await broker.drain()
    tele = [Telemetry.from_wire(d) for d in ears.by_topic.get("telemetry", [])]
    assert 2 <= len(tele) <= 3
    assert tele[-1].pose is not None and tele[-1].pose.map_id == "m"
    assert tele[-1].battery_pct > 0


async def test_命令进来回执出去_越界主题不发(台子):
    broker, c, r, ears, rt, site = 台子
    await rt.start()
    await broker.drain()
    cmd = {"schema": "1.0", "command_id": "c1", "task_id": "t1", "kind": "dance",
           "issued_at": c(), "expires_at": c() + 60_000, "control_epoch": 1, "priority": 0,
           "offline_policy": "default", "precondition": None, "payload": {}}
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await broker.drain()
    ack = ears.by_topic["cmd/ack"][-1]
    assert ack["command_id"] == "c1" and ack["result"] == "rejected"
    with pytest.raises(PermissionError):
        await rt.transport.publish(Topics(site_id="s", robot_id="OTHER").status, b"x")
    with pytest.raises(PermissionError):
        await rt.transport.subscribe("site/+/robot/+/cmd", ears)


async def test_断线期间事件攒着_重连后先reconcile再补发(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    broker.disconnect("dog")
    rt.events.emit("task_progress", {"task_id": "t", "distance_m": 1.0})
    rt.events.emit("task_progress", {"task_id": "t", "distance_m": 0.5})
    await rt.step(0.1)
    await broker.drain()
    assert "event" not in ears.by_topic
    ears.order.clear()
    await rt.transport.connect()
    await broker.drain()
    rc = Reconcile.from_wire(ears.by_topic["reconcile"][-1])
    assert (rc.unacked_from_seq, rc.unacked_to_seq) == (1, 2)
    assert ears.order.index("reconcile") < ears.order.index("event")
    assert [d["seq"] for d in ears.by_topic["event"]] == [1, 2]
    assert rt.events.unacked_range() == (0, 0)


async def test_断线又重连发生在同一拍之间_任务不会卡在holding(台子):
    """paho 自动重连、网络抖动 <100 ms 是真机常态:断线与重连都发生在两拍之间。
    原实现 step 先处理重连(on_online)再处理断线(on_offline)——已在线的任务被置 holding
    且没人再解。"""
    broker, c, r, ears, rt, site = 台子
    await rt.start()
    await broker.drain()
    r.inject_loc_lost(True)                       # 让「不安全」成立
    cmd = {"schema": "1.0", "command_id": "c1", "task_id": "t1", "kind": "goto",
           "issued_at": c(), "expires_at": c() + 60_000, "control_epoch": 1, "priority": 0,
           "offline_policy": "default", "precondition": None,
           "payload": {"target": {"schema": "1.0", "map_id": "m", "map_version": "1",
                                  "frame_id": "map", "x": 3.0, "y": 0.0, "yaw": 0.0}}}
    r.inject_loc_lost(False)
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await broker.drain()
    await rt.step(0.1)
    task = rt.processor.current
    assert task is not None
    r.inject_loc_lost(True)
    broker.disconnect("dog")                      # 断
    await rt.transport.connect()                  # 同一拍之间又连上了
    await broker.drain()
    await rt.step(0.1)
    assert task._holding is False, "已经在线,不许按断线策略把任务停住"


async def test_重连后命令先于reconcile到达也要先发reconcile(台子):
    """MemoryBroker 上「reconcile 第一条」靠的是投递顺序;真 broker 下离线补投的 cmd 可能先到。
    所以 _on_cmd 自己也要先把 reconcile 发掉。"""
    broker, c, r, ears, rt, site = 台子
    await rt.start()
    await broker.drain()
    ears.order.clear()
    rt._reconnect_pending = True                  # 模拟「连上了但还没来得及 flush」
    cmd = {"schema": "1.0", "command_id": "c1", "task_id": "t1", "kind": "dance",
           "issued_at": c(), "expires_at": c() + 60_000, "control_epoch": 1, "priority": 0,
           "offline_policy": "default", "precondition": None, "payload": {}}
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await broker.drain()
    assert ears.order.index("reconcile") < ears.order.index("cmd/ack")


async def test_断线时不安全就停住_安全就继续(台子):
    broker, c, r, ears, rt, site = 台子
    await rt.start()
    await broker.drain()
    cmd = {"schema": "1.0", "command_id": "c1", "task_id": "t1", "kind": "goto",
           "issued_at": c(), "expires_at": c() + 60_000, "control_epoch": 1, "priority": 0,
           "offline_policy": "default", "precondition": None,
           "payload": {"target": {"schema": "1.0", "map_id": "m", "map_version": "1",
                                  "frame_id": "map", "x": 5.0, "y": 0.0, "yaw": 0.0}}}
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await broker.drain()
    for _ in range(5):
        await rt.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
    r.inject_battery(10.0)                        # 低于底线 → 不安全
    broker.disconnect("dog")
    for _ in range(5):
        await rt.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
    assert rt.processor.current._holding is True
    assert (await r.odometry()).vx == 0.0


async def test_空闲时status也要周期刷新last_seen(台子):
    """总设计 §3.1 派遣条件要 last_seen 新鲜;只在变化时发的话静止在线的狗会被判不新鲜。"""
    broker, c, r, ears, rt, site = 台子
    await rt.start()
    await broker.drain()
    n = len(ears.by_topic["status"])
    for _ in range(int(rt.status_period_ms / 100) + 2):
        await rt.step(0.1)
        r.tick(0.1)
        c.advance(0.1)
    await broker.drain()
    assert len(ears.by_topic["status"]) > n
    assert Status.from_wire(ears.by_topic["status"][-1]).last_seen > \
        Status.from_wire(ears.by_topic["status"][n - 1]).last_seen


async def test_LWT主题也过ACL(台子):
    from d1max_agent.transport import GuardedTransport
    from d1max_contract.topics import TopicAcl
    broker, c, r, ears, rt, site = 台子
    g = GuardedTransport(MemoryTransport(broker, "x"), TopicAcl(T))
    with pytest.raises(PermissionError):
        g.set_will(Topics(site_id="s", robot_id="OTHER").status, b"x")


async def test_同一命令并发投递只有一条accepted(台子, monkeypatch):
    """QoS 1 的重复可能几乎同时到;处理里一旦有 await(W00b 接引擎后一定有),两条都会先
    通过幂等查询。运行时的命令入口要串行化。这里给 handle 加一个 await 点来逼出竞态。"""
    broker, c, r, ears, rt, site = 台子
    await rt.start()
    await broker.drain()
    import asyncio

    async def 让出(cmd):                      # 幂等查询之后的 await 点(W00b 这里是真 I/O)
        await asyncio.sleep(0)
    monkeypatch.setattr(rt.processor, "_admit", 让出)
    cmd = {"schema": "1.0", "command_id": "c1", "task_id": "t1", "kind": "goto",
           "issued_at": c(), "expires_at": c() + 60_000, "control_epoch": 1, "priority": 0,
           "offline_policy": "default", "precondition": None,
           "payload": {"target": {"schema": "1.0", "map_id": "m", "map_version": "1",
                                  "frame_id": "map", "x": 3.0, "y": 0.0, "yaw": 0.0}}}
    # 站点重发同一条命令(QoS 1 重投的等价物),两条投递在任一 handler 跑之前都已排进循环。
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await broker.drain()
    results = sorted(a["result"] for a in ears.by_topic["cmd/ack"])
    assert results == ["accepted", "duplicate"]
    assert len(rt.processor.pending) + (rt.processor.current is not None) == 1


async def test_启动顺序_先登记cmd订阅再连接(台子):
    """结构性钉住:runtime.start() 里 subscribe(cmd) 必须在 transport.connect() 之前。"""
    import inspect

    from d1max_agent import runtime as mod
    src = inspect.getsource(mod.AgentRuntime._start)          # start() 只包了一层回滚
    assert src.index("subscribe(self.topics.cmd") < src.index("await self.transport.connect()")


# ------------------------------------------------------------ HAL 生命周期(W00b 外审阻断项)


async def _hal已放(r):
    h = await r.health()
    return h.link_ok is False and (await r.control_status()).held is False


async def test_close之后HAL停了_控制权放了_链路关了(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    assert (await r.control_status()).held is True
    await rt.close()
    assert await _hal已放(r)
    assert await r.stopped() is True
    assert rt.parts.device.connected is False and rt.parts.nav.connected is False


async def test_连不上站点_已拿到的控制权要放掉(台子, monkeypatch):
    broker, c, r, ears, rt, _ = 台子

    async def 连不上():
        raise TimeoutError("broker 不在")

    monkeypatch.setattr(rt.transport._inner, "connect", 连不上)
    with pytest.raises(TimeoutError):
        await rt.start()
    assert await _hal已放(r), "start 失败要回滚:控制权不能留在一个起不来的进程手里"


async def test_清理某一步炸了_后面的照做(台子, monkeypatch):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()

    async def 炸():
        raise RuntimeError("引擎关不掉")

    closed = {"transport": False}
    real_close = rt.transport._inner.close

    async def 记着关(*a, **k):
        closed["transport"] = True
        await real_close(*a, **k)

    monkeypatch.setattr(rt.parts.engine, "aclose", 炸)
    monkeypatch.setattr(rt.transport._inner, "close", 记着关)
    await rt.close()                                   # 不抛
    assert await _hal已放(r) and closed["transport"]


async def test_放控制权炸了_链路照样关(台子, monkeypatch):
    broker, c, r, ears, rt, _ = 台子
    await rt.start()

    async def 炸():
        raise RuntimeError("SDK 不理")

    monkeypatch.setattr(r, "release_control", 炸)
    await rt.close()
    assert (await r.health()).link_ok is False


async def test_没start就close_以及close两次都安全(台子):
    broker, c, r, ears, rt, _ = 台子
    await rt.close()
    await rt.start()
    await rt.close()
    await rt.close()
    assert await _hal已放(r)


async def test_收尾顺序_先停再放控制权再关链路(台子, monkeypatch):
    """sim 放控制权时顺手清零速度,看不出顺序;真 HAL 放了控制权之后就发不了停止。"""
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    order: list[str] = []
    for name in ("stop", "release_control", "close"):
        real = getattr(r, name)

        async def 记(*a, _n=name, _real=real, **k):
            order.append(_n)
            await _real(*a, **k)
        monkeypatch.setattr(r, name, 记)
    await rt.close()
    assert order.index("stop") < order.index("release_control") < order.index("close")


async def test_正常收尾先发retained的offline_status_站点不用等过期(台子):
    """LWT 只在异常断线时由 broker 代发;正常退出(SIGTERM、停服务)要自己发,
    不然站点一直当它在线,直到 last_seen 过期(90 s)。"""
    broker, c, r, ears, rt, _ = 台子
    await rt.start()
    await broker.drain()
    await rt.close()
    await broker.drain()
    last = Status.from_wire(ears.by_topic["status"][-1])
    assert last.online is False and last.boot_id == rt.boot_id
