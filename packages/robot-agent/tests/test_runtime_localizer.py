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
    async def 起(self, tmp_path, *, localizer="bridge"):
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
        self.loc = SimLocalizer(self.dog, self.d / "loc.sock")
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
        assert await self.loc.connect(), self.loc.error
        await self.拍(3)

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


async def test_定位器自信地跳错_跟里程对不上_停下等它稳下来_修正量给引擎(台, monkeypatch):
    t = 台
    await t.连()
    got = []
    real = t.rt.parts.engine.relocalized

    def 记(delta):
        got.append(delta)
        real(delta)
    monkeypatch.setattr(t.rt.parts.engine, "relocalized", 记)
    await t.rt._on_cmd(_goto(4.0, 0.0, "g1", t.c))
    await t.拍(10)
    t.loc.jump(0.8, 0.0, flag=False)                     # 不报 jump:自信地跳错
    await t.拍(2)
    assert t.rt.parts.nav.anchor.why_not(True).startswith("定位器跟里程对不上")
    assert t.rt.parts.engine.state is RunState.PAUSED or await t.dog.stopped()
    await t.拍(SETTLE_FIXES + 5)
    assert len(got) == 1 and got[0] is not None and abs(got[0][0] - 0.8) < 0.15, got


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
    await t.拍(3)
    assert (t.loc.priors[-1].map_id, t.loc.priors[-1].map_version) == ("m", "2")
    assert t.rt.parts.nav.anchor.ok(True), "定位器在新图上出位姿了"
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
