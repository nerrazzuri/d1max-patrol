"""过渡期自主移动技术闸(W00c6i,W08 决定 11)。

W11 避障真机验收之前,真狗的自主移动是直线、拿运控里程当地图、没有避障;口头规矩挡不住站点排程、
事件派遣在没人时把狗派出去。``supervised`` 级别下:没人监护不收 ``goto``/``patrol``,跑着的时候
监护过期当场中止。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.main import build_parser, resolve_autonomy
from d1max_agent.runtime import AgentRuntime
from d1max_agent.status import compose_capabilities
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
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


class 耳朵:
    def __init__(self) -> None:
        self.by: dict[str, list[dict]] = {}

    async def __call__(self, m):
        kind = T.parse(m.topic)[2]
        self.by.setdefault(kind, []).append(json.loads(m.payload))


def _台子(autonomy):
    @pytest.fixture
    async def f(tmp_path):
        broker, c = MemoryBroker(), 钟()
        r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
        ears = 耳朵()
        site = MemoryTransport(broker, "site")
        await site.connect()
        await site.subscribe(f"{T.prefix}/#", ears)
        rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                          store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                          boot_id="b", home=Pose.from_xy_yaw(0.0, 0.0),
                          monotonic=lambda: c.mono, autonomy=autonomy)
        await rt.start()
        await broker.drain()
        yield broker, c, r, ears, rt, site
        await rt.close()
    return f


监护台 = _台子("supervised")
自主台 = _台子("autonomous")


def _cmd(kind, payload, cid, c, *, ttl_ms=60_000, task_id=None):
    return {"schema": "1.0", "command_id": cid, "task_id": task_id or f"{kind}-{cid}",
            "kind": kind, "issued_at": c(), "expires_at": c() + ttl_ms, "control_epoch": 1,
            "priority": 100 if kind == "halt" else 0, "offline_policy": "default",
            "precondition": None, "payload": payload}


def _goto(x, cid, c):
    return _cmd("goto", {"target": {"schema": "1.0", "map_id": "m", "map_version": "1",
                                    "frame_id": "map", "x": x, "y": 0.0, "yaw": 0.0},
                         "max_speed_mps": 0.8}, cid, c)


_序号 = {"n": 0}


def _监护(action, cid, c, *, ttl_ms=3000, cmd_ttl_ms=30_000, session="s-gina", operator="gina",
        seq=None):
    """一条监护心跳。序号默认自增(同一会话里单调递增,跟手机一样)。"""
    if seq is None:
        _序号["n"] += 1
        seq = _序号["n"]
    return _cmd("supervise", {"action": action, "ttl_ms": ttl_ms, "operator": operator,
                              "session": session, "seq": seq}, cid, c,
                ttl_ms=cmd_ttl_ms, task_id="supervise-r")


async def _发(site, broker, cmd):
    await site.publish(T.cmd, json.dumps(cmd).encode())
    await broker.drain()


def _回执(ears, cid):
    return [a for a in ears.by.get("cmd/ack", []) if a["command_id"] == cid][-1]


async def _跑(rt, r, c, broker, n, dt=0.1):
    for _ in range(n):
        await rt.step(dt)
        r.tick(dt)
        c.advance(dt)
        for _ in range(4):
            await asyncio.sleep(0)
        await broker.drain()


# ------------------------------------------------------------ 级别从哪来


@pytest.mark.parametrize("argv, want", [
    (["--hal", "d1max"], "supervised"),
    (["--hal", "sim"], "autonomous"),
    (["--hal", "d1max", "--autonomy", "autonomous"], "autonomous"),
    (["--hal", "sim", "--autonomy", "supervised"], "supervised"),
])
def test_真狗默认要人监护_仿真默认可自主_显式配置覆盖(argv, want):
    args = build_parser().parse_args(["--transport", "memory://", "--registration", "r.json",
                                      "--store-dir", "s", "--map", "m:1", *argv])
    assert resolve_autonomy(args) == want


def test_能力里报自主级别():
    r = SimRobot(now_ms=lambda: 0)
    caps = compose_capabilities(robot_id="A", hal_caps=r.hal_capabilities(), adapter_id="sim",
                                loaded_map=("m", "1"), nav_path="straight",
                                autonomy="supervised")
    assert caps.tasks["goto"]["autonomy"] == "supervised"
    assert caps.tasks["patrol"]["autonomy"] == "supervised"


# ------------------------------------------------------------ 代理的闸


async def test_要人监护_没人监护就不收goto(监护台):
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _goto(5.0, "g1", c))
    a = _回执(ears, "g1")
    assert a["result"] != "accepted" and a["reason"] == "unsupervised"


async def test_要人监护_监护中收下_监护过期当场中止_狗停下(监护台):
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _监护("renew", "s1", c))
    assert _回执(ears, "s1")["result"] == "accepted"
    await _发(site, broker, _goto(20.0, "g1", c))
    assert _回执(ears, "g1")["result"] == "accepted"
    for i in range(20):                               # 监护一直续着:照跑
        if i % 10 == 0:
            await _发(site, broker, _监护("renew", f"s{i + 2}", c))
        await _跑(rt, r, c, broker, 1)
    assert (await r.odometry()).vx > 0.3
    await _跑(rt, r, c, broker, 60)                   # 不续了:3 s 后过期
    aborted = [e for e in ears.by.get("event", []) if e["kind"] == "task_aborted"]
    assert aborted and aborted[-1]["data"]["reason"] == "supervision_lost"
    o = await r.odometry()
    assert abs(o.vx) < 0.01, "监护过期,狗要停"


async def test_过期的监护命令续不上租约(监护台):
    """断线重连补投的旧心跳:命令本身过期了,代理拒收,租约不因它续上。"""
    broker, c, r, ears, rt, site = 监护台
    stale = _监护("renew", "s0", c, cmd_ttl_ms=3000)
    c.advance(5.0)
    await _发(site, broker, stale)
    assert _回执(ears, "s0")["result"] == "expired"
    await _发(site, broker, _goto(5.0, "g1", c))
    assert _回执(ears, "g1")["reason"] == "unsupervised"


async def test_放租立刻生效_跑着的当场中止(监护台):
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _监护("renew", "s1", c))
    await _发(site, broker, _goto(20.0, "g1", c))
    await _跑(rt, r, c, broker, 10)
    await _发(site, broker, _监护("release", "s2", c))
    await _跑(rt, r, c, broker, 20)
    aborted = [e for e in ears.by.get("event", []) if e["kind"] == "task_aborted"]
    assert aborted and aborted[-1]["data"]["reason"] == "supervision_lost"


async def test_监护载荷不对就拒(监护台):
    broker, c, r, ears, rt, site = 监护台
    for i, bad in enumerate([{"action": "forever"},
                             {"action": "renew", "ttl_ms": 3_600_000, "session": "x", "seq": 1},
                             {"action": "renew", "operator": "g" * 65, "session": "x", "seq": 1},
                             {"action": "renew", "session": "", "seq": 1},
                             {"action": "renew", "session": "x", "seq": 0}]):
        await _发(site, broker, _cmd("supervise", bad, f"b{i}", c, ttl_ms=3000))
        a = _回执(ears, f"b{i}")
        assert a["result"] != "accepted" and a["reason"].startswith("payload"), (bad, a)


async def test_要人监护_叫停照收(监护台):
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _cmd("halt", {"reason": "operator"}, "h1", c))
    assert _回执(ears, "h1")["result"] == "accepted"


async def test_可自主_不看监护(自主台):
    broker, c, r, ears, rt, site = 自主台
    await _发(site, broker, _goto(5.0, "g1", c))
    assert _回执(ears, "g1")["result"] == "accepted"
    await _跑(rt, r, c, broker, 60)
    assert not [e for e in ears.by.get("event", []) if e["kind"] == "task_aborted"]


async def test_监护过期_排队的goto也清掉_不起跑(监护台):
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _监护("renew", "s1", c))
    await _发(site, broker, _goto(20.0, "g1", c))
    await _跑(rt, r, c, broker, 5)
    g2 = _goto(-5.0, "g2", c)
    g2["priority"] = 5                                 # 抢占 g1:g1 停稳之前 g2 在排队
    await _发(site, broker, g2)
    assert _回执(ears, "g2")["result"] == "accepted"
    await _发(site, broker, _监护("release", "s2", c))
    await _跑(rt, r, c, broker, 60)
    aborted = {e["data"]["task_id"]: e["data"]["reason"]
               for e in ears.by.get("event", []) if e["kind"] == "task_aborted"}
    assert aborted.get("goto-g2") == "supervision_lost", aborted
    assert not [e for e in ears.by.get("event", [])
                if e["kind"] == "task_progress" and e["data"]["task_id"] == "goto-g2"], "g2 没起跑"


async def test_监护心跳不进幂等记录(监护台):
    """心跳每秒一条:记进幂等记录的话,一天几十万行、代理起来还要全量重放。"""
    broker, c, r, ears, rt, site = 监护台
    for i in range(5):
        await _发(site, broker, _监护("renew", f"s{i}", c))
    assert all(rt.idem.lookup(f"s{i}") is None for i in range(5))



async def test_多人监护_一个人放了_另一个人还在续就照跑(监护台):
    """W00c6i 内审:以前只记一个「监护到什么时候」,谁放都清空。"""
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _监护("renew", "a1", c, session="s-gina"))
    await _发(site, broker, _监护("renew", "b1", c, session="s-bob", operator="bob"))
    await _发(site, broker, _goto(20.0, "g1", c))
    assert _回执(ears, "g1")["result"] == "accepted"
    await _发(site, broker, _监护("release", "a2", c, session="s-gina"))
    await _跑(rt, r, c, broker, 2)                     # bob 这两拍不续:他上一次续的还在有效期里
    for i in range(30):
        if i % 10 == 0:
            await _发(site, broker, _监护("renew", f"b{i + 2}", c, session="s-bob", operator="bob"))
        await _跑(rt, r, c, broker, 1)
    assert not [e for e in ears.by.get("event", []) if e["kind"] == "task_aborted"]


async def test_同一会话序号不增的心跳不认_迟到的旧续约续不上(监护台):
    """放租之后才到的旧续约(手机到站点慢网,或断线补投):序号比放租那一条小,不认。"""
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _监护("renew", "s1", c, seq=5))
    await _发(site, broker, _监护("release", "s2", c, seq=6))
    await _发(site, broker, _监护("renew", "s3", c, seq=4))
    assert _回执(ears, "s3")["reason"] == "stale_seq"
    await _发(site, broker, _goto(5.0, "g1", c))
    assert _回执(ears, "g1")["reason"] == "unsupervised"


async def test_狗的墙钟不准_监护照样能用_租约按单调钟(监护台):
    """命令有效期按狗的墙钟判(站点给 30 s,同遥控续租);租约按收到时刻 + 3 s 的单调钟。"""
    broker, c, r, ears, rt, site = 监护台
    beat = _监护("renew", "s1", c)
    c.ms += 5_000                                     # 狗的墙钟比站点快 5 s(单调钟不动)
    await _发(site, broker, beat)
    assert _回执(ears, "s1")["result"] == "accepted"
    c.ms += 3_600_000                                 # 墙钟再往后跳一小时:租约不受影响
    await _发(site, broker, _goto(5.0, "g1", c))
    assert _回执(ears, "g1")["result"] == "accepted"


async def test_要人监护_遥控不受监护约束(监护台):
    from d1max_contract.teleop import teleop_grant_payload
    broker, c, r, ears, rt, site = 监护台
    grant = _cmd("teleop", teleop_grant_payload(lease_epoch=1, operator="gina",
                                                lease_ttl_ms=5000), "t1", c, task_id="teleop-1")
    grant["priority"] = 100
    await _发(site, broker, grant)
    assert _回执(ears, "t1")["result"] == "accepted", _回执(ears, "t1")


async def test_引擎卡住时监护过期_照样停得住(监护台):
    """W00c6i 内审阻断 2:以前监护过期只请求引擎中止,引擎卡住(活着但不读命令)时狗照样往前走。"""
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _监护("renew", "s1", c))
    await _发(site, broker, _goto(20.0, "g1", c))
    await _跑(rt, r, c, broker, 10)
    assert (await r.odometry()).vx > 0.3

    async def 卡住(_timeout):                         # 引擎活着,但再也不取下一个输入
        await asyncio.Event().wait()
    rt.parts.engine._next = 卡住
    rt.parts.engine._queue = asyncio.Queue()           # 手上那一次取数也收不到之后来的东西
    await _跑(rt, r, c, broker, 35)                   # 3 s 后过期
    x0 = (await r.odometry()).x
    await _跑(rt, r, c, broker, 20)
    o = await r.odometry()
    assert o.x - x0 < 0.05 and abs(o.vx) < 0.01, f"监护过期后又走了 {o.x - x0:.2f} m"


async def test_叫停之后再放监护_中止原因还是叫停(监护台):
    broker, c, r, ears, rt, site = 监护台
    await _发(site, broker, _监护("renew", "s1", c))
    await _发(site, broker, _goto(20.0, "g1", c))
    await _跑(rt, r, c, broker, 10)
    await _发(site, broker, _cmd("halt", {"reason": "operator"}, "h1", c))
    await _发(site, broker, _监护("release", "s2", c))
    await _跑(rt, r, c, broker, 30)
    aborted = [e for e in ears.by.get("event", []) if e["kind"] == "task_aborted"]
    assert aborted and aborted[-1]["data"]["reason"] == "halt"


def test_没给级别时_只有仿真可自主(tmp_path):
    class 别的狗(SimRobot):
        adapter_id = "acme/1.0"
    for hal, want in ((SimRobot(now_ms=lambda: 0), "autonomous"),
                      (别的狗(now_ms=lambda: 0), "supervised")):
        rt = AgentRuntime(transport=MemoryTransport(MemoryBroker(), "dog"), registration=REG,
                          hal=hal, store_dir=tmp_path / want, now_ms=lambda: 0,
                          loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0))
        assert rt.autonomy == want
