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
    assert round(h.pose.yaw, 3) == round(math.pi / 2, 3), "原点带朝向(换电要对准桩)"
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

    async def 在走() -> bool:                               # 没任务也会在走(遥控开着)
        return False
    dog.stopped = 在走
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


async def test_标了原点_正在用的图重启照用_再下发按站点的(tmp_path):
    """站点下发的图(``active.json``)上标了原点,代理重启按标的;之后站点再下发、带着原点,按站点的。"""
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
    # 站点是权威(内审应修 4):再下发这张图、带着站点登记的原点 —— 按站点的,重启也按站点的。
    await rt._on_cmd(_cmd("map_activate", ref | {"home": {"x": 1.0, "y": 1.0, "yaw": 0.3}}, "a2",
                          c))
    await _跑(rt, broker)
    assert (rt.parts.home.pose.position.x, rt.parts.home.pose.yaw) == (1.0, 0.3)
    await rt.close()
    rt = mk()
    await rt.start()
    assert (rt.parts.home.pose.position.x, rt.parts.home.pose.yaw) == (1.0, 0.3)
    # 下发一张带 home.json 的图、站点没带原点:按图里的(以前标过的不许冒出来)。
    ref8 = site.add("m", "8", {"m.pgm": b"8", "home.json": b'{"x":7,"y":7,"yaw":0}'})
    await rt._on_cmd(_cmd("map_activate", ref8, "a3", c))
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "8") and rt.parts.home.pose.position.x == 7.0
    await rt.close()


async def test_里程不新鲜_拒(tmp_path):
    """锚过、偏差也小,但此刻运控里程不新鲜(旁路断了):这一刻的位姿是旧的,不标。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    await _设位置(rt, broker, dog, c)
    dog.inject_loc_lost(True)
    await rt._on_cmd(_cmd("mark_home", {}, "o1", c))
    await broker.drain()
    assert _ack(ears)["reason"].startswith("loc_poor") and "里程" in _ack(ears)["reason"], \
        _ack(ears)
    assert rt.parts.home.pose.position.x == 0.0
    await rt.close()


async def test_在跑任务_拒忙_原点不变(tmp_path):
    """内审应修 3:goto 刚收下、狗还没起步(停着)也不标 —— 不然原点变成半路上的一个点,这一趟返航
    还去老原点、站点的默认待命点却已经是新点。"""
    from d1max_contract.messages import MapPose
    broker, c, ears, dog, rt = await _台(tmp_path)
    await _设位置(rt, broker, dog, c)
    await rt._on_cmd(_cmd("goto", {"target": MapPose(map_id="m", map_version="1", frame_id="map",
                                                     x=6.0, y=3.0, yaw=0.0).to_wire()}, "g", c))
    await rt._on_cmd(_cmd("mark_home", {}, "b1", c))
    await broker.drain()
    assert _ack(ears)["reason"].startswith("busy") and "任务" in _ack(ears)["reason"], _ack(ears)
    await _跑(rt, broker, n=2, r=dog, c=c)                  # 已经在跑了(不在排队)

    async def 停着() -> bool:                               # 驻留、两段之间:狗一时停着
        return True
    dog.stopped = 停着
    assert rt.processor.current is not None and rt.processor.current.kind == "goto"
    await rt._on_cmd(_cmd("mark_home", {}, "b2", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "busy: 在跑任务 goto-g", _ack(ears)
    assert rt.parts.home.pose.position.x == 0.0
    assert not _事件(ears, "home_marked")
    await rt.close()


async def test_在换图_在切版本_拒忙(tmp_path):
    import asyncio
    broker, c, ears, dog, rt = await _台(tmp_path)
    await _设位置(rt, broker, dog, c)
    for slot, what in (("_map_job", "换图"), ("_release_job", "版本")):
        job = asyncio.get_running_loop().create_task(asyncio.sleep(30))
        setattr(rt, slot, job)
        await rt._on_cmd(_cmd("mark_home", {}, f"j{slot}", c))
        await broker.drain()
        assert _ack(ears)["reason"].startswith("busy") and what in _ack(ears)["reason"], \
            _ack(ears)
        job.cancel()
        setattr(rt, slot, None)
    assert rt.parts.home.pose.position.x == 0.0
    await rt.close()


async def test_遥控开着_可以标(tmp_path):
    """「开过去再标」:遥控把狗开到充电桩前,租约还开着就点标原点 —— 收。"""
    import json

    from test_runtime_maps import T

    from d1max_contract.messages import Command
    from d1max_contract.teleop import teleop_grant_payload
    from d1max_contract.transport import Message
    broker, c, ears, dog, rt = await _台(tmp_path)
    rt._video_live = lambda: True
    await _设位置(rt, broker, dog, c)
    g = Command(command_id="t1", task_id="teleop-1", kind="teleop", issued_at=c.ms,
                expires_at=c.ms + 60_000, control_epoch=1, priority=100,
                payload=teleop_grant_payload(lease_epoch=1, operator="gina", lease_ttl_ms=5000))
    await rt._on_cmd(Message(T.cmd, json.dumps(g.to_wire()).encode(), 1, False))
    await _跑(rt, broker, n=3, r=dog, c=c)
    assert rt.processor.current is not None and rt.processor.current.kind == "teleop"
    await rt._on_cmd(_cmd("mark_home", {}, "t2", c))
    await broker.drain()
    assert _ack(ears)["result"] == "accepted", _ack(ears)
    await rt.close()


async def test_没有正在用的图_按启动参数载的图_标了重启照用(tmp_path):
    """内审阻断 1:部署脚本按 ``D1MAX_MAP``/``D1MAX_HOME`` 起(没有站点下发的 ``active.json``,也可以
    没配地图保管),新现场第一次标原点走的就是这条路。以前只改了内存、回执照样收下,重启回到
    ``--home``。"""
    from test_runtime_maps import REG

    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryTransport
    from d1max_patrol.protocol.nav_types import Pose
    broker, c, ears, dog, rt = await _台(tmp_path)
    await _设位置(rt, broker, dog, c, x=4.0, y=4.0, yaw=0.7)
    await rt._on_cmd(_cmd("mark_home", {}, "p1", c))
    await broker.drain()
    assert _ack(ears)["result"] == "accepted"
    await rt.close()
    again = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                         store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b2",
                         home=Pose.from_xy_yaw(0.0, 0.0, 0.0), monotonic=lambda: c.mono,
                         odom_identity=False)
    h = again.parts.home
    assert (round(h.pose.position.x, 2), round(h.pose.position.y, 2)) == (4.0, 4.0)
    assert round(h.pose.yaw, 3) == 0.7, "朝向也落盘"
    assert h.map_id == "m"


async def test_落不了盘_拒_原点不变_不发事件(tmp_path):
    """内审应修 6:先落盘再改内存;盘满了就拒(以前异常冒出去、没有回执,内存里的原点已经换了)。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    await _设位置(rt, broker, dog, c)

    def 盘满(*a, **k):
        raise OSError(28, "No space left on device")
    rt.homes.put = 盘满
    await rt._on_cmd(_cmd("mark_home", {}, "f1", c))
    await broker.drain()
    assert _ack(ears)["reason"].startswith("persist_failed"), _ack(ears)
    assert rt.parts.home.pose.position.x == 0.0
    await _跑(rt, broker, n=2, r=dog, c=c)
    assert not _事件(ears, "home_marked")
    await rt.close()


def test_原点簿_按图号版本记_删_坏文件当没有(tmp_path):
    from d1max_agent.homes import MAX_ENTRIES, HomeBook
    b = HomeBook(tmp_path / "homes.json")
    assert b.get("m", "7") is None
    b.put("m", "7", (1.0, 2.0, 0.5), now_ms=1)
    b.put("m", "8", (3.0, 4.0, -0.5), now_ms=2)
    assert HomeBook(tmp_path / "homes.json").get("m", "7") == (1.0, 2.0, 0.5)
    assert b.get("m", "8") == (3.0, 4.0, -0.5) and b.get("n", "7") is None
    b.put("m", "7", None, now_ms=3)
    assert b.get("m", "7") is None and b.get("m", "8") == (3.0, 4.0, -0.5), "只删这一张"
    for i in range(MAX_ENTRIES + 5):
        b.put("x", str(i), (float(i), 0.0, 0.0), now_ms=10 + i)
    assert b.get("x", str(MAX_ENTRIES + 4)) is not None and b.get("x", "0") is None, "只留最近的"
    (tmp_path / "homes.json").write_text("{坏")
    assert b.get("m", "8") is None
    b.put("m", "9", (0.0, 0.0, 0.0), now_ms=99)
    assert b.get("m", "9") == (0.0, 0.0, 0.0)
