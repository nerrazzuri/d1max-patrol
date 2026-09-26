"""配了定位器的代理(W09a):真代理 + 仿真狗 + 仿真定位器,走真的本机定位桥(Unix 套接字)。

没配定位器的狗照旧用里程锚定(W00c6e),行为不变。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest
from test_relocalize import _ack, _事件, _遥测
from test_runtime_maps import REG, T, _cmd, 耳朵, 钟

from d1max_adapter_sim.localizer import SimLocalizer
from d1max_adapter_sim.robot import SimRobot
from d1max_agent.bridge_localizer import SETTLE_FIXES, BridgeLocalizer
from d1max_agent.engine.machine import RunState
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import MapPose
from d1max_patrol.protocol.nav_types import Pose


class 台子:
    async def 起(self, tmp_path, *, localizer="bridge", frame=(0.0, 0.0, 0.0)):
        self.d = Path(tempfile.mkdtemp(prefix="lr", dir="/tmp"))
        self.broker, self.c = MemoryBroker(), 钟()
        self.ears = 耳朵()
        st = MemoryTransport(self.broker, "site")
        await st.connect()
        await st.subscribe(f"{T.prefix}/#", self.ears)
        self.dog = SimRobot(now_ms=self.c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
        self.rt = AgentRuntime(
            transport=MemoryTransport(self.broker, "dog"), registration=REG, hal=self.dog,
            store_dir=tmp_path, now_ms=self.c, loaded_map=("m", "1"), boot_id="b",
            home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: self.c.mono,
            telemetry_period_ms=100, localizer=localizer, loc_socket=self.d / "loc.sock")
        await self.rt.start()
        self.loc = SimLocalizer(self.dog, self.d / "loc.sock", frame=frame)
        return self

    async def 拍(self, n=1):
        for _ in range(n):
            await self.loc.step()
            await asyncio.sleep(0.005)                 # 让本机桥把这一帧读进去
            await self.rt.step(0.1)
            self.dog.tick(0.1)
            self.c.ms += 100
            self.c.mono += 0.1
            await asyncio.sleep(0.005)
        await self.broker.drain()

    async def 连(self):
        """连上、换好先验、第一帧稳下来(内审应修 6:连上之后的第一帧也要稳)。"""
        assert await self.loc.connect(), self.loc.error
        await self.拍(SETTLE_FIXES + 3)

    async def 收(self):
        await self.loc.close()
        await self.rt.close()
        shutil.rmtree(self.d, ignore_errors=True)


@pytest.fixture
async def 台(tmp_path):
    t = await 台子().起(tmp_path)
    yield t
    await t.收()


def _goto(x, y, cid, c):
    return _cmd("goto", {"target": MapPose(map_id="m", map_version="1", frame_id="map", x=x,
                                           y=y, yaw=0.0).to_wire()}, cid, c)


async def test_配了定位器_没连上不可信_连上换好先验_按它的位置走到点(台):
    t = 台
    assert isinstance(t.rt.parts.nav.anchor, BridgeLocalizer)
    assert t.ears.by["capabilities"][-1]["tasks"]["relocalize"] == {"needs_pose": True}
    await t.拍(2)
    loc = _遥测(t.ears).loc
    assert loc["localizer"] == "bridge" and "没连上" in loc["reason"]
    await t.连()
    assert [(p.map_id, p.map_version, p.dir) for p in t.loc.priors] == [("m", "1", "")]
    loc = _遥测(t.ears).loc
    assert loc["reason"] == "" and loc["anchored"] and loc["source"] == "scan_match", loc
    await t.rt._on_cmd(_goto(2.0, 0.0, "g1", t.c))
    await t.拍(80)
    assert _事件(t.ears, "task_done"), t.ears.by.get("event")
    assert abs(t.dog.x - 2.0) < 0.3


async def test_定位器断了_停车暂停_连回来接着走(台):
    t = 台
    await t.连()
    await t.rt._on_cmd(_goto(4.0, 0.0, "g1", t.c))
    await t.拍(15)
    assert t.dog.x > 0.3, "走起来了"
    await t.loc.close()
    await t.拍(25)                                      # 过 2 s:桥当它断了
    assert _遥测(t.ears).pose is None, "定位不可信:遥测不带地图位姿(W08 决定 3)"
    assert await t.dog.stopped()
    assert t.rt.parts.engine.state is RunState.PAUSED
    x = t.dog.x
    await t.拍(10)
    assert t.dog.x == x, "丢着定位不走"
    await t.连()
    await t.拍(120)
    assert _事件(t.ears, "task_done"), t.ears.by.get("event")


def _记修正量(t, monkeypatch):
    got = []
    real = t.rt.parts.engine.relocalized

    def 记(delta, **kw):
        got.append((delta, kw))
        real(delta, **kw)
    monkeypatch.setattr(t.rt.parts.engine, "relocalized", 记)
    return got


async def test_定位器自信地跳错_抓住停下_请它在推算的位置附近重定位_对回来接着走(台, monkeypatch):
    """跳错被交叉校验抓住 → 导航桥停车、引擎按丢定位暂停并重置 → 重置 = 请定位器在最后可信的位置
    (按里程推到此刻)附近重定位(W08 决定 4)→ 对回来,修正量约为零 → 接着走完。丢定位的次数不从头算
    (不是人给的,内审应修 4)。"""
    t = 台
    await t.连()
    got = _记修正量(t, monkeypatch)
    await t.rt._on_cmd(_goto(4.0, 0.0, "g1", t.c))
    await t.拍(10)
    t.loc.jump(0.8, 0.0, flag=False)                     # 不报 jump:自信地跳错
    await t.拍(1)
    assert not t.rt.parts.nav.anchor.ok(True)            # 对不上;引擎同一拍就请它重定位了
    assert t.rt.parts.engine.state is RunState.PAUSED or await t.dog.stopped()
    await t.拍(SETTLE_FIXES + 5)
    [r] = t.loc.relocs
    assert r.sigma_xy == 1.0 and abs(r.x - t.dog.x) < 0.2, (r, t.dog.x)
    [(d, kw)] = got
    assert kw == {"reset_attempts": False} and d is not None and abs(d[0]) < 0.15, got
    assert t.rt.parts.engine._live.loc_reset_attempts == 1
    await t.拍(80)
    assert _事件(t.ears, "task_done"), t.ears.by.get("event")
    assert abs(t.dog.x - 4.0) < 0.3


async def test_定位器冻住了_狗在走_抓住停下_重置之后接着走(台):
    """内审阻断 1:匹配线程死了、定时器还在重发最后那个位置。以前相邻两帧只差 0.1 m、在线内,狗拿着
    停住的位置一直往前走。"""
    t = 台
    await t.连()
    await t.rt._on_cmd(_goto(4.0, 0.0, "g1", t.c))
    await t.拍(10)
    x0 = t.dog.x
    t.loc.freeze()
    for _ in range(10):
        await t.拍(1)
        if not t.rt.parts.nav.anchor.ok(True):
            break
    assert not t.rt.parts.nav.anchor.ok(True)
    assert t.dog.x - x0 < 0.8, "冻住之后没走出多远就停"
    await t.拍(SETTLE_FIXES + 5)
    assert t.loc.relocs and t.loc.frozen is None, "重置请它重定位,它对回来了"
    await t.拍(80)
    assert _事件(t.ears, "task_done"), t.ears.by.get("event")
    assert abs(t.dog.x - 4.0) < 0.3


async def test_定位器卡了一下一批一起到_不当跳_接着走(台, monkeypatch):
    """内审应修 5:以前一批一起到的全配此刻的里程,第一帧当跳、修正量算错、来路被挪错。"""
    t = 台
    await t.连()
    got = _记修正量(t, monkeypatch)
    await t.rt._on_cmd(_goto(4.0, 0.0, "g1", t.c))
    await t.拍(10)
    t.loc.stall(6)
    sources = []
    for _ in range(12):
        await t.拍(1)
        assert t.rt.parts.nav.anchor.ok(True), t.rt.parts.nav.anchor.why_not(True)
        sources.append(t.rt.parts.nav.anchor.source)
    assert "dead_reckoning" in sources and sources[-1] == "scan_match", "卡着的时候在推算"
    assert got == [] and not t.loc.relocs
    await t.拍(60)
    assert _事件(t.ears, "task_done"), t.ears.by.get("event")


async def test_地图系跟里程系不重合_走到点_人给位置的修正量方向对(tmp_path, monkeypatch):
    """内审应修 8:仿真里地图系就是里程系,修正量 compose 的先后反了也看不出。这里地图系 ← 里程系
    转了 0.8 rad、平移 (3, -2)。"""
    from d1max_agent.localization import compose
    frame = (3.0, -2.0, 0.8)
    t = await 台子().起(tmp_path, frame=frame)
    try:
        t.loc.offset = [0.25, 0.0, 0.0]                  # 定位器一直偏 0.25 m(地图系里)
        await t.连()
        got = _记修正量(t, monkeypatch)
        tx, ty, _ = compose(frame, (1.5, 0.0, 0.0))
        await t.rt._on_cmd(_goto(tx, ty, "g1", t.c))
        await t.拍(80)
        assert _事件(t.ears, "task_done"), t.ears.by.get("event")
        assert abs(t.dog.x - 1.25) < 0.3 and abs(t.dog.y) < 0.3, (t.dog.x, t.dog.y)
        truth = t.loc.truth()
        await t.rt._on_cmd(_cmd("relocalize", {"x": truth[0], "y": truth[1], "yaw": truth[2]},
                                "r1", t.c))
        await t.拍(SETTLE_FIXES + 3)
        [(d, kw)] = got
        assert kw == {"reset_attempts": True}
        assert abs(d[0] + 0.25) < 0.02 and abs(d[1]) < 0.02 and abs(d[2]) < 0.01, d
    finally:
        await t.收()


async def test_设位置经定位器_不在线拒_拒了说原因_收下之后稳下来(台):
    t = 台
    await t.rt._on_cmd(_cmd("relocalize", {"x": 1.0, "y": 0.0, "yaw": 0.0}, "r0", t.c))
    await t.broker.drain()
    assert _ack(t.ears)["reason"].startswith("localizer_unavailable"), _ack(t.ears)
    await t.连()
    t.loc.refuse_reloc = "初值离地图太远"
    await t.rt._on_cmd(_cmd("relocalize", {"x": 1.0, "y": 0.0, "yaw": 0.0}, "r1", t.c))
    await t.broker.drain()
    assert _ack(t.ears)["reason"] == "localizer_refused: 初值离地图太远"
    t.loc.refuse_reloc = ""
    await t.rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r2", t.c))
    await t.broker.drain()
    assert _ack(t.ears)["result"] == "accepted", _ack(t.ears)
    r = t.loc.relocs[-1]
    assert (r.x, r.y, r.yaw, r.sigma_xy) == (0.0, 0.0, 0.0, 0.5)
    await t.拍(1)
    assert not t.rt.parts.nav.anchor.ok(True), "定位器刚重定位:等它稳下来"
    await t.拍(SETTLE_FIXES + 2)
    assert t.rt.parts.nav.anchor.ok(True)
    assert _事件(t.ears, "relocalized")[-1]["source"] == "manual"
    await t.rt._on_cmd(_cmd("relocalize", {"x": 3.0, "y": 0.0, "yaw": 0.0}, "r3", t.c))
    await t.broker.drain()
    assert _ack(t.ears)["result"] == "accepted", "收下了;对不对得上看之后"
    await t.拍(2)
    assert "初值附近对不上" in t.rt.parts.nav.anchor.why_not(True)


async def test_换图的时候不收设位置(台):
    """内审应修 2:给的是这张图上的位置,换完图就作废;等定位器回复的时候换了图,以前还会卡住。"""
    t = 台
    await t.连()
    t.rt._switching = True
    await t.rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r1", t.c))
    await t.broker.drain()
    t.rt._switching = False
    assert _ack(t.ears)["reason"] == "busy: 在换图"
    assert not t.loc.relocs


async def test_标原点用定位器的位置(台):
    t = 台
    await t.连()
    for i in range(1, 16):                               # 按走路的速度挪过去(一拍 0.1 m 上下):
        t.dog.teleport(0.1 * i, -0.04 * i, 0.02 * i)      # 定位器那一帧比里程早到一拍,差得不多
        await t.拍(1)
    await t.拍(2)
    await t.rt._on_cmd(_cmd("mark_home", {}, "h1", t.c))
    await t.broker.drain()
    a = _ack(t.ears)
    assert a["result"] == "accepted", a["reason"]
    assert (round(a["data"]["x"], 2), round(a["data"]["y"], 2)) == (1.5, -0.6)


async def test_换图请定位器换先验_升级前检查有定位器这一项(台):
    from d1max_contract.maps import MapRef
    t = 台
    await t.连()
    [(name, src)] = [(n, s) for n, s in t.rt.precheck_sources if n == "定位器"]
    assert (await src()).ok
    ref = MapRef.from_wire({"map_id": "m", "version": "2", "files": [
        {"name": "m.pgm", "size": 1, "sha256": "0" * 64}]})
    t.rt._switch_map(ref)
    await t.拍(SETTLE_FIXES + 3)
    assert (t.loc.priors[-1].map_id, t.loc.priors[-1].map_version) == ("m", "2")
    assert t.rt.parts.nav.anchor.ok(True), "定位器在新图上出位姿了、稳下来了"
    await t.loc.close()
    await t.拍(25)
    got = await src()
    assert not got.ok and "没连上" in got.detail


async def test_没配定位器的狗_照旧里程锚定_不开套接字(tmp_path):
    from d1max_agent.localization import OdomAnchor
    t = await 台子().起(tmp_path, localizer="anchor")
    try:
        assert isinstance(t.rt.parts.nav.anchor, OdomAnchor)
        assert not (t.d / "loc.sock").exists()
        assert not [n for n, _ in t.rt.precheck_sources if n == "定位器"]
    finally:
        await t.rt.close()
        shutil.rmtree(t.d, ignore_errors=True)


async def test_定位器连着却一声不吭_两秒后当它断了(台):
    """本机桥的心跳看门(每拍 tick):连接没断、定位器卡死不发 —— 不能一直当它连着、只报
    「没来位姿」。"""
    t = 台
    await t.连()
    for _ in range(25):                                  # 只走代理,定位器什么都不发
        await t.rt.step(0.1)
        t.c.mono += 0.1
        await asyncio.sleep(0.005)
    assert "没连上" in t.rt.parts.nav.anchor.why_not(True)


async def test_换图时告诉定位器这张图在狗上的目录(tmp_path):
    from test_runtime_maps import 站点

    from d1max_agent.maps import MapKeeper
    from d1max_contract.maps import MapRef
    t = 台子()
    site = 站点()
    t.keeper = MapKeeper(tmp_path / "keep", fetch=site.fetch)
    await t.起(tmp_path / "agent")
    try:
        t.rt.maps = t.keeper
        await t.连()
        ref = MapRef.from_wire(site.add("m", "3", {"m.pgm": b"3"}))
        t.rt._switch_map(ref)
        await t.拍(3)
        assert t.loc.priors[-1].dir == str(t.keeper.dir_of(ref))
    finally:
        await t.收()


async def test_定位不可信时遥测不带地图位姿(台):
    """W08 决定 3:定位不好的时候,遥测不带 ``frame_id "map"`` 的位姿(站点按位姿选狗、手机画位置都不该
    拿一个不可信的位置)。定位器连着、σ 过线:照样有估计,但不发。"""
    t = 台
    await t.连()
    assert _遥测(t.ears).pose is not None
    t.loc.sigma_xy = 1.5
    await t.拍(3)
    assert "偏差" in _遥测(t.ears).loc["reason"]
    assert _遥测(t.ears).pose is None
