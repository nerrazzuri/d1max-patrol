"""W10 运行时:``--nav planned`` 换图载规划栅格、``zones_set`` 下发(收紧立刻换、放宽等空闲、
旧修订拒、
几何版本对不上拒、落盘重启还在)、能力里的区域修订号与能不能规划、狗在禁行区里发事件。"""

from __future__ import annotations

import asyncio
import hashlib
import json

import numpy as np
import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.maps import MapKeeper
from d1max_agent.planning.planner import Planner
from d1max_agent.runtime import AgentRuntime
from d1max_contract.maps import GEOMETRY_FILES
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities, Command
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics
from d1max_contract.transport import Message
from d1max_patrol.protocol.nav_types import Pose

REG = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                   expires_at=10**13)
T = Topics(site_id="s", robot_id="r")


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000
        self.mono = 50.0

    def __call__(self) -> int:
        return self.ms


def 地面():
    h, w = 80, 120                                     # 0.05 m:6 × 4 m
    occ = np.zeros((h, w), dtype=bool)
    occ[:2, :] = occ[-2:, :] = occ[:, :2] = occ[:, -2:] = True
    img = np.where(occ, 0, 254).astype(np.uint8)
    return {"floor.pgm": b"P5\n%d %d\n255\n" % (w, h) + img.tobytes(),
            "floor.yaml": b"resolution: 0.05\norigin: [0.0, 0.0, 0.0]\nnegate: 0\n"}


class 站点:
    def __init__(self):
        self.files: dict[tuple[str, str, str], bytes] = {}

    def add(self, map_id, version, files):
        for n, d in files.items():
            self.files[(map_id, version, n)] = d
        return {"map_id": map_id, "version": version, "files": [
            {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
            for n, d in files.items()]}

    def fetch(self, map_id, version, name):
        yield self.files[(map_id, version, name)]


class 耳朵:
    def __init__(self):
        self.by: dict[str, list] = {}

    async def __call__(self, m):
        self.by.setdefault(T.parse(m.topic)[2], []).append(json.loads(m.payload))


def _cmd(kind, payload, cid, c):
    cmd = Command(command_id=cid, task_id=f"{kind}-{cid}", kind=kind, issued_at=c.ms,
                  expires_at=c.ms + 60_000, control_epoch=1, payload=payload)
    return Message(T.cmd, json.dumps(cmd.to_wire()).encode(), 1, False)


def _zs(rev, zones, ver="2"):
    return {"zones": {"map_id": "m", "map_version": ver, "revision": rev, "zones": zones}}


POND = {"id": "pond", "kind": "nogo", "label": "池子", "polygon": [[3, 1], [4, 1], [4, 2], [3, 2]]}


async def _跑(rt, broker, n=20, r=None, c=None):
    for _ in range(n):
        await rt.step(0.1)
        if r is not None:
            r.tick(0.1)
            c.ms += 100
            c.mono += 0.1
        await asyncio.sleep(0.01)
    await broker.drain()


@pytest.fixture
async def 台(tmp_path):
    broker, c, site = MemoryBroker(), 钟(), 站点()
    r = SimRobot(now_ms=c, max_vx=0.6)
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    keeper = MapKeeper(tmp_path / "agent" / "maps", fetch=site.fetch)

    def mk():
        rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                          store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                          boot_id="b", home=Pose.from_xy_yaw(1, 1, 0), monotonic=lambda: c.mono,
                          maps=keeper, nav="planned")
        rt.parts.nav._planner = Planner(in_process=True)
        return rt
    rt = mk()
    await rt.start()
    yield broker, c, r, ears, rt, site, mk
    await rt.close()


def _caps(ears):
    return Capabilities.from_wire(ears.by["capabilities"][-1])


async def _换图(rt, broker, site, c, ears, ver="2", files=None):
    geo = ({n: b"x" for n in GEOMETRY_FILES} | (地面() if files is None else files)
           | {"home.json": b'{"x":1,"y":1,"yaw":0}'})
    ref = site.add("m", ver, geo)
    await rt._on_cmd(_cmd("map_activate", ref, f"a{ver}", c))
    for _ in range(50):
        await _跑(rt, broker, 2)
        if rt.loaded_map == ("m", ver) and (rt._grid_job is None or rt._grid_job.done()):
            break
    await _跑(rt, broker, 2)


async def test_没有规划栅格_不宣告能自主(台):
    broker, c, r, ears, rt, site, mk = 台
    caps = _caps(ears)
    assert caps.tasks["zones_set"] == {"rev": 0, "enforced": True, "plan_ok": False,
                                       "problem": "没有规划栅格"}
    assert "goto" not in rt.processor.supported and "patrol" not in rt.processor.supported


async def test_换图载规划栅格_能规划(台):
    broker, c, r, ears, rt, site, mk = 台
    await _换图(rt, broker, site, c, ears)
    assert rt.parts.nav.plan_ok
    z = _caps(ears).tasks["zones_set"]
    assert z == {"rev": 0, "enforced": True, "plan_ok": True}
    assert {"goto", "patrol"} <= rt.processor.supported


async def test_规划栅格坏了_记原因(台):
    broker, c, r, ears, rt, site, mk = 台
    await _换图(rt, broker, site, c, ears, files={"floor.pgm": b"P2 1 1 1\n0",
                                                 "floor.yaml": b"resolution: 0.05\n"})
    z = _caps(ears).tasks["zones_set"]
    assert z["plan_ok"] is False and "载不了" in z["problem"]
    assert "goto" not in rt.processor.supported


async def test_下发区域_收紧立刻换_落盘_重启还在(台):
    broker, c, r, ears, rt, site, mk = 台
    await _换图(rt, broker, site, c, ears)
    await rt._on_cmd(_cmd("zones_set", _zs(1, [POND]), "z1", c))
    await _跑(rt, broker, 2)
    ack = ears.by["cmd/ack"][-1]
    assert ack["result"] == "accepted" and ack["data"] == {"revision": 1, "deferred": False}
    assert rt.parts.nav.zones.revision == 1
    assert _caps(ears).tasks["zones_set"]["rev"] == 1
    # 同一修订重发:收下不动;旧修订、内容不同的同修订拒
    await rt._on_cmd(_cmd("zones_set", _zs(1, [POND]), "z1b", c))
    await _跑(rt, broker, 1)
    assert ears.by["cmd/ack"][-1]["result"] == "accepted"
    await rt._on_cmd(_cmd("zones_set", _zs(1, []), "z1c", c))
    await _跑(rt, broker, 1)
    assert ears.by["cmd/ack"][-1]["reason"] == "stale"
    await rt._on_cmd(_cmd("zones_set", _zs(0, []), "z0", c))
    await _跑(rt, broker, 1)
    assert ears.by["cmd/ack"][-1]["reason"] == "stale"
    await rt._on_cmd(_cmd("zones_set", _zs(2, [POND], ver="1"), "zv", c))
    await _跑(rt, broker, 1)
    assert ears.by["cmd/ack"][-1]["reason"] == "map_mismatch"
    await rt._on_cmd(_cmd("zones_set", {"zones": {"map_id": "m"}}, "zb", c))
    await _跑(rt, broker, 1)
    assert ears.by["cmd/ack"][-1]["reason"].startswith("payload")
    await rt.close()
    rt2 = mk()
    await rt2.start()
    try:
        for _ in range(50):
            await _跑(rt2, broker, 2)
            if rt2.parts.nav.plan_ok:
                break
        assert rt2._zones.revision == 1 and rt2.parts.nav.zones.revision == 1
        assert rt2.parts.nav.nogo_at(3.5, 1.5) is not None
    finally:
        await rt2.close()


async def test_放宽_跑任务时等空闲再换(台):
    broker, c, r, ears, rt, site, mk = 台
    await _换图(rt, broker, site, c, ears)
    await rt._on_cmd(_cmd("zones_set", _zs(1, [POND]), "z1", c))
    await _跑(rt, broker, 2)
    r.teleport(1.0, 1.0, 0.0)
    goto = Command(command_id="g1", task_id="t1", kind="goto", issued_at=c.ms,
                   expires_at=c.ms + 60_000, control_epoch=1,
                   payload={"target": {"schema": "1.0", "map_id": "m", "map_version": "2",
                                       "frame_id": "map", "x": 5.0, "y": 3.0, "yaw": 0.0}})
    await rt._on_cmd(Message(T.cmd, json.dumps(goto.to_wire()).encode(), 1, False))
    await _跑(rt, broker, 5, r, c)
    assert not rt._tasks_idle(), rt.parts.engine.snapshot.reason
    await rt._on_cmd(_cmd("zones_set", _zs(2, []), "z2", c))          # 删禁行区:放宽
    await _跑(rt, broker, 1)
    ack = ears.by["cmd/ack"][-1]
    assert ack["result"] == "accepted" and ack["data"] == {"revision": 2, "deferred": True}
    assert rt.parts.nav.zones.revision == 1
    z = _caps(ears).tasks["zones_set"]
    assert z["rev"] == 1 and z["pending_rev"] == 2
    for _ in range(60):
        await _跑(rt, broker, 5, r, c)
        if rt._tasks_idle():
            break
    await _跑(rt, broker, 2, r, c)
    assert rt.parts.nav.zones.revision == 2 and rt._zones_pending is None
    assert _caps(ears).tasks["zones_set"]["rev"] == 2


async def test_狗在新禁行区里_发事件带图号(台):
    broker, c, r, ears, rt, site, mk = 台
    await _换图(rt, broker, site, c, ears)
    r.teleport(1.0, 1.0, 0.0)
    goto = Command(command_id="g1", task_id="t1", kind="goto", issued_at=c.ms,
                   expires_at=c.ms + 60_000, control_epoch=1,
                   payload={"target": {"schema": "1.0", "map_id": "m", "map_version": "2",
                                       "frame_id": "map", "x": 5.0, "y": 1.0, "yaw": 0.0}})
    await rt._on_cmd(Message(T.cmd, json.dumps(goto.to_wire()).encode(), 1, False))
    await _跑(rt, broker, 20, r, c)
    o = await r.odometry()
    await rt._on_cmd(_cmd("zones_set", _zs(1, [{
        "id": "bed", "kind": "nogo", "label": "花坛",
        "polygon": [[o.x - 0.5, 0.2], [o.x + 0.5, 0.2], [o.x + 0.5, 2], [o.x - 0.5, 2]]}]),
        "z1", c))
    await _跑(rt, broker, 10, r, c)
    ev = [e for e in ears.by.get("event", []) if e["kind"] == "inside_nogo"]
    assert ev and ev[0]["data"]["zone"] == "bed" and ev[0]["data"]["map_version"] == "2"


async def test_直线桥_报不守区域(tmp_path):
    broker, c = MemoryBroker(), 钟()
    r = SimRobot(now_ms=c)
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono)
    await rt.start()
    try:
        await _跑(rt, broker, 2)
        assert _caps(ears).tasks["zones_set"] == {"rev": 0, "enforced": False}
        await rt._on_cmd(_cmd("zones_set", _zs(1, [POND], ver="1"), "z1", c))
        await _跑(rt, broker, 2)
        assert ears.by["cmd/ack"][-1]["result"] == "accepted"
        assert _caps(ears).tasks["zones_set"]["rev"] == 1
        assert {"goto", "patrol"} <= rt.processor.supported
    finally:
        await rt.close()


async def test_读规划栅格期间来了新区域_读完不拿旧的盖回去(台):
    """内审阻断 1。"""
    import time as _time
    broker, c, r, ears, rt, site, mk = 台
    nav = rt.parts.nav
    real = nav.load_grid

    def 慢(d):
        _time.sleep(0.4)
        real(d)
    nav.load_grid = 慢
    geo = {n: b"x" for n in GEOMETRY_FILES} | 地面() | {"home.json": b'{"x":1,"y":1,"yaw":0}'}
    ref = site.add("m", "2", geo)
    await rt._on_cmd(_cmd("map_activate", ref, "a2", c))
    for _ in range(100):
        await _跑(rt, broker, 1)
        if rt.loaded_map == ("m", "2") and rt._grid_job is not None \
                and not rt._grid_job.done():
            break
    await rt._on_cmd(_cmd("zones_set", _zs(1, [POND]), "z1", c))
    for _ in range(100):
        await _跑(rt, broker, 1)
        if rt._grid_job.done():
            break
    assert nav.plan_ok and nav.zones is not None and nav.zones.revision == 1
    assert nav.nogo_at(3.5, 1.5) is not None
