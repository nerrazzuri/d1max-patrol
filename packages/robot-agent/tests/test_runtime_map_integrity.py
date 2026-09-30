"""W09g 正在用的图的完整性,代理这一层:起来时先完整校验再载;校验不过不载 HAL 地图、不给定位先验、
不报 goto / 巡检、**不退回 ``--map``**,事件簿记 ``map_integrity_failed``(连上补投);叫停、换图照收
—— 站点再发一次同一版就是修。"""

from __future__ import annotations

import json

import pytest
from test_runtime_maps import REG, T, _cmd, _跑, 站点, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent import maps as M
from d1max_agent.maps import MapKeeper
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities
from d1max_patrol.protocol.nav_types import Pose

FILES = {"m.pgm": bytes(range(200)), "home.json": b'{"x":1,"y":2,"yaw":0}'}


class 台子:
    def __init__(self, tmp_path):
        self.broker, self.c, self.site = MemoryBroker(), 钟(), 站点()
        self.r = SimRobot(now_ms=self.c)
        self.tmp = tmp_path
        self.keeper = MapKeeper(tmp_path / "agent" / "maps", fetch=self.site.fetch)

    def mk(self):
        return AgentRuntime(transport=MemoryTransport(self.broker, "dog"), registration=REG,
                            hal=self.r, store_dir=self.tmp / "agent", now_ms=self.c,
                            loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                            monotonic=lambda: self.c.mono, maps=self.keeper)

    async def 起(self):
        self.ears = 耳朵()
        st = MemoryTransport(self.broker, "site")
        await st.connect()
        await st.subscribe(f"{T.prefix}/#", self.ears)
        self.rt = self.mk()
        await self.rt.start()
        await self.broker.drain()
        return self

    async def 换(self, version, cid):
        wire = self.site.add("m", version, FILES)
        await self.rt._on_cmd(_cmd("map_activate", wire, cid, self.c))
        await _跑(self.rt, self.broker)
        return wire

    async def 重启(self):
        await self.rt.close()
        self.r.loaded_map = None
        return await self.起()

    def caps(self):
        return Capabilities.from_wire(self.ears.by["capabilities"][-1])

    def events(self, kind):
        return [e["data"] for e in self.ears.by.get("event", []) if e["kind"] == kind]


def _flip(p, at=50):
    b = bytearray(p.read_bytes())
    b[at] ^= 0xFF
    p.write_bytes(bytes(b))


@pytest.fixture
async def t(tmp_path):
    t = await 台子(tmp_path).起()
    await t.换("2", "a1")
    assert t.rt.loaded_map == ("m", "2")
    yield t
    await t.rt.close()


async def test_好的图_重启照常载(t):
    await t.重启()
    assert t.rt.loaded_map == ("m", "2") and t.r.loaded_map[:2] == ("m", "2")
    assert "patrol" in t.caps().tasks and not t.events("map_integrity_failed")


async def test_同尺寸坏了_重启不载_不报自主_不退回命令行的图_记事件(t):
    _flip(t.keeper.dir_of(t.keeper.active()) / "m.pgm")
    await t.重启()
    assert t.r.loaded_map is None, "HAL 没载坏图"
    assert t.rt.loaded_map is None, "不退回 --map 的 m:1"
    caps = t.caps()
    for kind in ("goto", "patrol", "relocalize", "mark_home"):
        assert kind not in caps.tasks, kind
    assert "map_activate" in caps.tasks and "teleop" in caps.tasks, "修、遥控照样能用"
    got = t.events("map_integrity_failed")
    assert len(got) == 1 and (got[0]["map_id"], got[0]["version"]) == ("m", "2")
    assert "m.pgm" in got[0]["reason"] and "sha256" in got[0]["reason"]
    assert "m.pgm" in t.rt.map_problem


async def test_坏了之后_goto_派不进_叫停照收(t):
    from d1max_contract.messages import MapPose
    _flip(t.keeper.dir_of(t.keeper.active()) / "m.pgm")
    await t.重启()
    goto = MapPose(map_id="m", map_version="2", frame_id="map", x=1.0, y=0.0, yaw=0.0)
    await t.rt._on_cmd(_cmd("goto", {"target": goto.to_wire()}, "g1", t.c))
    await t.rt._on_cmd(_cmd("halt", {}, "h1", t.c))
    await t.broker.drain()
    acks = {a["command_id"]: a for a in t.ears.by["cmd/ack"]}
    assert acks["g1"]["result"] != "accepted"
    assert acks["h1"]["result"] == "accepted"


async def test_坏了之后_站点再发同一版_重新下_载入_能力回来(t):
    d = t.keeper.dir_of(t.keeper.active())
    _flip(d / "m.pgm")
    await t.重启()
    assert t.rt.loaded_map is None
    await t.换("2", "a2")
    assert (d / "m.pgm").read_bytes() == FILES["m.pgm"], "重新下了"
    assert t.rt.loaded_map == ("m", "2") and t.r.loaded_map[:2] == ("m", "2")
    assert t.caps().tasks["patrol"]["map_version"] == "2"
    assert t.rt.map_problem == ""
    assert t.events("map_activated")[-1]["task_id"] == "map_activate-a2"


async def test_active_json_本身坏了_一样不载不退回(t, tmp_path):
    (tmp_path / "agent" / "maps" / "active.json").write_text("{坏")
    await t.重启()
    assert t.rt.loaded_map is None and t.r.loaded_map is None
    got = t.events("map_integrity_failed")
    assert got and "active.json" in got[-1]["reason"]
    assert "patrol" not in t.caps().tasks


async def test_清单跟声明不一样_不载(t):
    d = t.keeper.dir_of(t.keeper.active())
    man = json.loads((d / "map.json").read_text())
    man["files"] = man["files"][:1]
    (d / "map.json").write_text(json.dumps(man))
    await t.重启()
    assert t.rt.loaded_map is None and t.events("map_integrity_failed")


async def test_没有_active_json_照旧用命令行的图(tmp_path):
    t = await 台子(tmp_path).起()
    try:
        assert t.rt.loaded_map == ("m", "1") and "patrol" in t.caps().tasks
        assert not t.events("map_integrity_failed")
    finally:
        await t.rt.close()


async def test_坏了之后换图提交失败_不把坏的原图载回去(t, monkeypatch):
    _flip(t.keeper.dir_of(t.keeper.active()) / "m.pgm")
    await t.重启()
    loads = []
    real = t.r.load_map

    async def 记(map_id, version, d):
        loads.append(version)
        return await real(map_id, version, d)
    monkeypatch.setattr(t.r, "load_map", 记)

    def 炸(*a, **k):
        raise OSError("盘只读了")
    monkeypatch.setattr(t.keeper, "commit", 炸)
    await t.换("3", "a3")
    assert loads == ["3"], "原来那张校验不过:不载回去"
    assert t.events("map_activate_failed")
    assert t.rt.loaded_map is None


async def test_起来之后查状态_发遥测_不算哈希(t, monkeypatch):
    await t.重启()
    n = []
    real = M._sha256
    monkeypatch.setattr(M, "_sha256", lambda p: (n.append(p), real(p))[1])
    await _跑(t.rt, t.broker, n=30, r=t.r, c=t.c)
    await t.rt._publish_status(force=True)
    await t.rt._publish_caps()
    assert n == []


async def test_读文件出错_也按校验不过处理_不让代理起不来(t, monkeypatch):
    def 读不了(p):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(M, "_sha256", 读不了)
    await t.重启()
    assert t.rt.loaded_map is None
    assert "Input/output" in t.events("map_integrity_failed")[-1]["reason"]


# ------------------------------------------------------------------ 配了定位器的狗


async def test_配了定位器_图坏了不给定位器先验(tmp_path):
    from test_runtime_localizer import 台子 as 定位台
    from test_runtime_localizer import 真狗样
    site = 站点()
    keeper = MapKeeper(tmp_path / "keep", fetch=site.fetch)
    t = 定位台()
    await t.起(tmp_path / "agent", dog=真狗样, maps=keeper)
    try:
        await t.连()
        wire = site.add("m", "2", {"prior.mm": bytes(range(100)), "frames.json": b"{}"})
        await t.rt._on_cmd(_cmd("map_activate", wire, "a1", t.c))
        await _跑(t.rt, t.broker)
        await t.拍(3)
        assert t.rt.loaded_map == ("m", "2")
        _flip(keeper.dir_of(keeper.active()) / "prior.mm")
        await t.rt.close()
        t.loc.priors.clear()
        t.rt = t.新代理()
        await t.rt.start()
        await t.拍(5)
        assert t.rt.loaded_map is None
        assert all((p.map_id, p.map_version) != ("m", "2") for p in t.loc.priors), \
            "坏的先验不给定位器"
        caps = Capabilities.from_wire(t.ears.by["capabilities"][-1])
        assert "relocalize" not in caps.tasks and "goto" not in caps.tasks
    finally:
        await t.收()
