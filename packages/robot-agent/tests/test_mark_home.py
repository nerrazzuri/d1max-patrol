"""在当前位置标原点(W00c6f):狗用此刻锚定后的地图位姿当原点,定位不好就拒。真代理 + 仿真狗,
锚定按真狗那样。"""

from __future__ import annotations

import math

from test_relocalize import _ack, _事件, _台
from test_runtime_maps import _cmd, _跑


async def _设位置(rt, broker, dog, c, x=2.0, y=3.0, yaw=0.0):
    await rt._on_cmd(_cmd("relocalize", {"x": x, "y": y, "yaw": yaw}, f"r{x}{y}", c))
    await _跑(rt, broker, n=2, r=dog, c=c)


async def test_定位好_原点换成狗此刻的地图位姿_回执带位置(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    assert "mark_home" in ears.by["capabilities"][-1]["tasks"]
    await _设位置(rt, broker, dog, c, yaw=math.pi / 2)
    await rt._on_cmd(_cmd("mark_home", {"name": "dock"}, "m1", c))
    await _跑(rt, broker, n=2, r=dog, c=c)
    ack = [a for a in ears.by["cmd/ack"] if a["command_id"] == "m1"][-1]
    assert ack["result"] == "accepted", ack
    d = ack["data"]
    assert (d["map_id"], d["map_version"]) == ("m", "1")
    assert (round(d["x"], 2), round(d["y"], 2)) == (2.0, 3.0)
    assert round(d["yaw"], 3) == round(math.pi / 2, 3)
    assert 0.0 < d["sigma_m"] <= 0.5
    h = rt.parts.home
    assert h is not None
    assert (round(h.pose.position.x, 2), round(h.pose.position.y, 2)) == (2.0, 3.0)
    ev = _事件(ears, "home_marked")[-1]
    assert ev["name"] == "dock" and round(ev["x"], 2) == 2.0
    await rt.close()


async def test_没设位置_走远了偏差大_在走_都拒(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("mark_home", {}, "n1", c))
    await broker.drain()
    assert _ack(ears)["reason"].startswith("loc_poor") and "设位置" in _ack(ears)["reason"], \
        _ack(ears)
    await _设位置(rt, broker, dog, c, x=0.0, y=0.0)
    for i in range(1, 6):                                   # 一步 0.4 m 挪 2 m(不算跳):σ 过 0.5 m
        dog.teleport(i * 0.4, 0.0, 0.0)
        await _跑(rt, broker, n=1, r=dog, c=c)
    await rt._on_cmd(_cmd("mark_home", {}, "n2", c))
    await broker.drain()
    assert _ack(ears)["reason"].startswith("loc_poor") and "偏差" in _ack(ears)["reason"]
    await _设位置(rt, broker, dog, c, x=2.0, y=0.0)
    from d1max_contract.messages import MapPose
    await rt._on_cmd(_cmd("goto", {"target": MapPose(map_id="m", map_version="1", frame_id="map",
                                                     x=6.0, y=0.0, yaw=0.0).to_wire()}, "g", c))
    await _跑(rt, broker, n=20, r=dog, c=c)
    await rt._on_cmd(_cmd("mark_home", {}, "n3", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "moving"
    assert rt.parts.home.pose.position.x == 0.0, "都没换"
    await rt.close()


async def test_名字不像话拒(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    await _设位置(rt, broker, dog, c)
    await rt._on_cmd(_cmd("mark_home", {"name": "a b/c"}, "x1", c))
    await broker.drain()
    assert _ack(ears)["reason"].startswith("payload")
    await rt.close()


async def test_仿真按原样_可以标(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path, identity=None)
    dog.teleport(1.0, -1.0, 0.0)
    await _跑(rt, broker, n=2, r=dog, c=c)
    await rt._on_cmd(_cmd("mark_home", {}, "s1", c))
    await broker.drain()
    assert _ack(ears)["result"] == "accepted" and round(_ack(ears)["data"]["x"], 2) == 1.0
    await rt.close()


async def test_标了原点_记进正在用的那张图_重启照用(tmp_path):
    """狗上正在用的图的 ``active.json`` 里记着站点给的原点;标了新的就改它,代理重启按它来。"""
    from test_runtime_maps import REG, 站点, 耳朵, 钟

    from d1max_adapter_sim.robot import SimRobot
    from d1max_agent.maps import MapKeeper
    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_patrol.protocol.nav_types import Pose
    broker, c, site = MemoryBroker(), 钟(), 站点()
    r = SimRobot(now_ms=c)
    ears = 耳朵()
    keeper = MapKeeper(tmp_path / "agent" / "maps", fetch=site.fetch)

    def mk():
        return AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                            store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                            boot_id="b", home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono,
                            maps=keeper)
    st = MemoryTransport(broker, "site")
    await st.connect()
    from test_runtime_maps import T
    await st.subscribe(f"{T.prefix}/#", ears)
    rt = mk()
    await rt.start()
    ref = site.add("m", "7", {"m.pgm": b"7"})
    await rt._on_cmd(_cmd("map_activate", ref | {"home": {"x": 0.0, "y": 0.0, "yaw": 0.0}}, "a", c))
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "7")
    r.teleport(3.0, 1.0, 0.0)
    await _跑(rt, broker, n=2, r=r, c=c)
    await rt._on_cmd(_cmd("mark_home", {}, "h", c))
    await broker.drain()
    assert _ack(ears)["result"] == "accepted"
    await rt.close()
    rt = mk()
    await rt.start()
    assert rt.loaded_map == ("m", "7")
    assert (round(rt.parts.home.pose.position.x, 2), round(rt.parts.home.pose.position.y, 2)) == \
        (3.0, 1.0)
    await rt.close()


def test_换原点只改正在用的那张图(tmp_path):
    import json

    from d1max_agent.maps import MapKeeper
    from d1max_contract.maps import MapRef
    k = MapKeeper(tmp_path / "maps", fetch=lambda *a: iter(()))
    active = {"map_id": "m", "version": "7", "files": [], "home": {"x": 0, "y": 0, "yaw": 0}}
    (tmp_path / "maps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "maps" / "active.json").write_text(json.dumps(active))
    f = [{"name": "m.pgm", "size": 1, "sha256": "0" * 64}]
    other = MapRef.from_wire({"map_id": "m", "version": "8", "files": f})
    assert k.set_home(other, (5.0, 5.0, 0.0)) is False
    assert json.loads((tmp_path / "maps" / "active.json").read_text())["home"]["x"] == 0
    cur = MapRef.from_wire({"map_id": "m", "version": "7", "files": f})
    assert k.set_home(cur, (5.0, 6.0, 0.5)) is True
    assert json.loads((tmp_path / "maps" / "active.json").read_text())["home"] == \
        {"x": 5.0, "y": 6.0, "yaw": 0.5}
