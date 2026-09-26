"""重定位 / 初始位姿(W00c6e):里程锚定接进导航桥、代理。真代理 + 仿真狗,锚定按真狗那样
(``odom_identity=False``):
开机不可信,人给一次位置(或者说「狗在原点」)之后地图位姿 = 锚定 ∘ 里程,走远了不可信、停下等人。"""

from __future__ import annotations

import math

from test_runtime_maps import REG, T, _cmd, _跑, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import MapPose, Status, Telemetry
from d1max_patrol.protocol.nav_types import Pose


async def _台(tmp_path, *, identity=False, home=(0.0, 0.0, 0.0)):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    dog = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=None if home is None else Pose.from_xy_yaw(*home),
                      monotonic=lambda: c.mono, odom_identity=identity, telemetry_period_ms=100)
    await rt.start()
    await broker.drain()
    return broker, c, ears, dog, rt


def _goto(x, y, cid, c):
    return _cmd("goto", {"target": MapPose(map_id="m", map_version="1", frame_id="map", x=x,
                                           y=y, yaw=0.0).to_wire()}, cid, c)


def _ack(ears):
    return ears.by["cmd/ack"][-1]


def _事件(ears, kind):
    return [e["data"] for e in ears.by.get("event", []) if e["kind"] == kind]


def _遥测(ears):
    return Telemetry.from_wire(ears.by["telemetry"][-1])


async def test_真狗开机没设位置_定位不可信_goto起飞不了_遥测没有位姿(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    assert ears.by["capabilities"][-1]["tasks"]["relocalize"] == {"needs_pose": True}
    await _跑(rt, broker, n=5, r=dog, c=c)
    t = _遥测(ears)
    assert t.pose is None and t.loc_quality == 0.0
    assert t.loc["anchored"] is False and "设位置" in t.loc["reason"]
    assert Status.from_wire(ears.by["status"][-1]).ready.loc_ok is False
    await rt._on_cmd(_goto(1.0, 0.0, "g1", c))
    await _跑(rt, broker, n=40, r=dog, c=c)
    failed = _事件(ears, "task_failed")
    assert failed and "定位" in failed[-1]["reason"], ears.by.get("event")
    o = await dog.odometry()
    assert math.hypot(o.x, o.y) < 0.01, "没动"
    await rt.close()


async def test_设位置_带转角_按地图坐标走到点(tmp_path):
    """人说狗在地图 (10, 5)、朝北(那一刻里程是原点、朝东)。去地图 (10, 7) = 里程上往前 2 m。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("relocalize", {"x": 10.0, "y": 5.0, "yaw": math.pi / 2}, "r1", c))
    await broker.drain()
    assert _ack(ears)["result"] == "accepted", _ack(ears)
    await _跑(rt, broker, n=3, r=dog, c=c)
    ev = _事件(ears, "relocalized")[-1]
    assert (ev["x"], ev["y"], ev["source"]) == (10.0, 5.0, "manual")
    t = _遥测(ears)
    assert (round(t.pose.x, 2), round(t.pose.y, 2)) == (10.0, 5.0) and t.loc["anchored"]
    assert 0.0 < t.loc_quality <= 1.0
    assert Status.from_wire(ears.by["status"][-1]).ready.loc_ok is True
    await rt._on_cmd(_goto(10.0, 7.0, "g2", c))
    for _ in range(40):
        await _跑(rt, broker, n=10, r=dog, c=c)
        if _事件(ears, "task_done") or _事件(ears, "task_failed"):
            break
    assert _事件(ears, "task_done"), ears.by.get("event")
    o = await dog.odometry()
    assert abs(o.x - 2.0) < 0.2 and abs(o.y) < 0.2, (o.x, o.y)
    await rt.close()


async def test_狗在原点_一键设位置_原点没标过拒(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path, home=(3.0, 4.0, 0.0))
    await rt._on_cmd(_cmd("relocalize", {"at_home": True}, "h1", c))
    await _跑(rt, broker, n=3, r=dog, c=c)
    assert _ack(ears)["result"] == "accepted"
    assert _事件(ears, "relocalized")[-1]["source"] == "home"
    t = _遥测(ears)
    assert (round(t.pose.x, 2), round(t.pose.y, 2)) == (3.0, 4.0)
    await rt.close()
    broker, c, ears, dog, rt = await _台(tmp_path / "b", home=None)
    await rt._on_cmd(_cmd("relocalize", {"at_home": True}, "h2", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "no_home"
    await rt.close()


async def test_坐标不像话_地图对不上_在走_都拒(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    for bad in ({"x": "a", "y": 0, "yaw": 0}, {"x": float("nan"), "y": 0, "yaw": 0}, {},
                {"x": 1, "y": 2}, {"at_home": "yes"}):
        await rt._on_cmd(_cmd("relocalize", bad, f"b{len(ears.by.get('cmd/ack', []))}", c))
        await broker.drain()
        assert _ack(ears)["reason"].startswith("payload"), (bad, _ack(ears))
    await rt._on_cmd(_cmd("relocalize", {"x": 1, "y": 2, "yaw": 0, "map_id": "m",
                                         "map_version": "9"}, "b9", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "map_mismatch"
    await rt._on_cmd(_cmd("relocalize", {"x": 0, "y": 0, "yaw": 0}, "a1", c))
    await rt._on_cmd(_goto(5.0, 0.0, "g3", c))
    await _跑(rt, broker, n=20, r=dog, c=c)
    await rt._on_cmd(_cmd("relocalize", {"x": 0, "y": 0, "yaw": 0}, "a2", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "busy", "任务跑着不改位置(W00c6e 内审)"
    await rt.close()


async def test_没有任务_狗在动_不收(tmp_path):
    """遥控开着走(直接下 HAL 速度,不经任务):狗在动就不收。"""
    from d1max_contract.hal import VelocityCommand
    broker, c, ears, dog, rt = await _台(tmp_path)
    await dog.set_velocity(VelocityCommand(seq=1, ttl_ms=5000, frame="base", vx=0.5, vy=0.0,
                                           wz=0.0))
    dog.tick(0.2)
    await rt._on_cmd(_cmd("relocalize", {"x": 0, "y": 0, "yaw": 0}, "m1", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "moving"
    await rt.close()


async def test_走远了丢定位_狗停下_人给了位置接着跑(tmp_path):
    """σ 过线(约走 8 m)就不可信:狗停下等人。人在原地给一次位置,接着把这一趟跑完。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r1", c))
    await rt._on_cmd(_goto(11.0, 0.0, "g4", c))
    lost_at = None
    for _ in range(300):
        await _跑(rt, broker, n=1, r=dog, c=c)
        if "telemetry" not in ears.by:
            continue
        t = _遥测(ears)
        if lost_at is None and t.loc_quality == 0.0 and t.loc["anchored"]:
            lost_at = (await dog.odometry()).x
        if lost_at is not None and await dog.stopped():
            break
    assert lost_at is not None and 7.5 < lost_at < 9.0, lost_at
    await _跑(rt, broker, n=20, r=dog, c=c)
    o = await dog.odometry()
    assert o.x < 9.5 and await dog.stopped(), "丢了就停,不接着往前走"
    assert not _事件(ears, "task_done")
    await rt._on_cmd(_cmd("relocalize", {"x": o.x, "y": o.y, "yaw": o.yaw}, "r2", c))
    await broker.drain()
    assert _ack(ears)["result"] == "accepted"
    assert rt.parts.engine._live.loc_reset_attempts == 0, "人给了位置:丢定位的次数从头算"
    for _ in range(60):
        await _跑(rt, broker, n=10, r=dog, c=c)
        if _事件(ears, "task_done") or _事件(ears, "task_failed"):
            break
    assert _事件(ears, "task_done"), ears.by.get("event")
    assert abs((await dog.odometry()).x - 11.0) < 0.3
    await rt.close()


async def test_仿真狗按原样_开机就可信(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path, identity=None)
    assert ears.by["capabilities"][-1]["tasks"]["relocalize"] == {"needs_pose": False}
    await _跑(rt, broker, n=3, r=dog, c=c)
    t = _遥测(ears)
    assert t.pose is not None and t.loc_quality == 1.0 and t.loc["source"] == "odom_identity"
    await rt.close()


async def test_导航桥报给引擎的位置是地图坐标_不是里程(tmp_path):
    """引擎按它记出发点、来路(W00c6b):锚在 (10, 5)、朝北,狗往前(里程 +x)走 1 m,地图上是 (10, 6)。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    assert await rt.parts.nav.current_pose() is None, "没锚过:报不出"
    await rt._on_cmd(_cmd("relocalize", {"x": 10.0, "y": 5.0, "yaw": math.pi / 2}, "p1", c))
    dog.teleport(1.0, 0.0, 0.0)
    await _跑(rt, broker, n=2, r=dog, c=c)
    p = await rt.parts.nav.current_pose()
    assert (round(p.position.x, 2), round(p.position.y, 2)) == (10.0, 6.0)
    await rt.close()


async def test_断线时安全不安全_看锚定(tmp_path):
    """``continue_if_safe``:定位不可信就停下等,不接着走(以前只看运控的「里程新鲜」)。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    seen = []

    class 假任务:
        kind, task_id, done = "goto", "g", False

        async def on_offline(self, safe):
            seen.append(safe)
    async def 断线一次():
        rt.processor.current = 假任务()                   # 只在判的那一刻挂上
        try:
            await rt._apply_offline_policy()
        finally:
            rt.processor.current = None
    await 断线一次()
    await rt._on_cmd(_cmd("relocalize", {"x": 0, "y": 0, "yaw": 0}, "o1", c))
    await _跑(rt, broker, n=2, r=dog, c=c)
    await 断线一次()
    assert seen == [False, True]
    await rt.close()



async def test_开机时导航桥就报定位不可信_不等第一拍(tmp_path):
    from d1max_patrol.protocol.nav_types import LocStatus
    broker, c, ears, dog, rt = await _台(tmp_path)
    assert await rt.parts.nav.loc_status() is LocStatus.LOC_LOST
    await rt.close()


async def test_换了图_锚定作废_要重新设位置(tmp_path):
    from test_runtime_maps import 站点

    from d1max_agent.maps import MapKeeper
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    site = 站点()
    dog = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono,
                      maps=MapKeeper(tmp_path / "maps", fetch=site.fetch), odom_identity=False)
    await rt.start()
    await rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r", c))
    assert rt.parts.nav.anchor.anchored
    ref = site.add("m", "2", {"m.pgm": b"2"})
    await rt._on_cmd(_cmd("map_activate", ref | {"home": {"x": 0.0, "y": 0.0, "yaw": 0.0}},
                          "a", c))
    await _跑(rt, broker, n=20, r=dog, c=c)
    assert rt.loaded_map == ("m", "2")
    assert not rt.parts.nav.anchor.anchored and "换了地图" in rt.parts.nav.anchor.reason
    await rt.close()


# ------------------------------------------------------------ W00c6e 内审修复


async def test_里程不新鲜过_锚定作废_回来了也不可信(tmp_path):
    """内审阻断 1:运控 / 旁路重启期间里程不新鲜,回来时里程可能归零了(离原点不到 1 m、只差朝向,
    跳变查不出来)。
    不新鲜过就作废,要人重新给位置。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r", c))
    for yaw in (0.0, 0.4, 0.8):                             # 慢慢挪、慢慢转(一拍转太多算跳)
        dog.teleport(0.5, 0.0, yaw)
        await _跑(rt, broker, n=1, r=dog, c=c)
    assert rt.parts.nav.anchor.anchored
    dog.inject_loc_lost(True)
    await _跑(rt, broker, n=2, r=dog, c=c)
    dog.inject_loc_lost(False)
    dog.teleport(0.0, 0.0, 0.0)          # 里程归零:平移 0.5 m、朝向差 0.8 rad —— 都在跳变线以内
    await _跑(rt, broker, n=3, r=dog, c=c)
    a = rt.parts.nav.anchor
    assert not a.anchored and "里程" in a.reason
    assert _遥测(ears).pose is None
    await rt.close()


async def test_里程只转了朝向_也当跳了(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r", c))
    await _跑(rt, broker, n=2, r=dog, c=c)
    dog.teleport(0.0, 0.0, 2.0)                              # 一拍转了 2 rad:转不了这么快
    await _跑(rt, broker, n=2, r=dog, c=c)
    assert not rt.parts.nav.anchor.anchored and "跳" in rt.parts.nav.anchor.reason
    await rt.close()


async def test_遥控开着走的距离也算(tmp_path):
    """导航桥每拍都喂里程,不管是谁在让狗动(遥控直接下 HAL 速度)。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r", c))
    await _跑(rt, broker, n=2, r=dog, c=c)
    s0 = rt.parts.nav.anchor.sigma_xy
    for i in range(1, 9):
        dog.teleport(i * 0.5, 0.0, 0.0)
        await _跑(rt, broker, n=1, r=dog, c=c)
    assert rt.parts.nav.anchor.sigma_xy > s0 + 3.0 * 2.0 / 9.0
    await rt.close()


async def test_里程无效时_不收设位置_说里程读不到(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    dog.inject_loc_lost(True)
    await _跑(rt, broker, n=2, r=dog, c=c)
    await rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "v", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "odom_invalid"
    await rt.close()


async def test_在原点设位置_朝向也照原点的(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path, home=(3.0, 4.0, 0.7))
    await rt._on_cmd(_cmd("relocalize", {"at_home": True}, "h", c))
    await _跑(rt, broker, n=3, r=dog, c=c)
    assert abs(_遥测(ears).pose.yaw - 0.7) < 1e-3
    await rt.close()


async def test_任务跑着_狗停着驻留_不收设位置_丢定位暂停时收(tmp_path):
    """内审阻断 2:以前只看狗停没停 —— 巡检在点上驻留时点了「在原点」,当场改了下一段怎么走。"""
    from d1max_contract.mission import Action, Mission, MissionWaypoint, Policy
    from d1max_patrol.protocol.nav_types import Pose as P
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, "r", c))
    m = Mission(mission="d", map_id="m", policy=Policy(), waypoints=(
        MissionWaypoint(name="a", pose=P.from_xy_yaw(1.0, 0.0),
                        actions=(Action(type="dwell", seconds=30.0),)),
        MissionWaypoint(name="b", pose=P.from_xy_yaw(2.0, 0.0))))
    await rt._on_cmd(_cmd("patrol", {"mission": m.to_wire(), "map_version": "1"}, "p", c))
    for _ in range(200):
        await _跑(rt, broker, n=1, r=dog, c=c)
        if await dog.stopped() and abs((await dog.odometry()).x - 1.0) < 0.15:
            break
    await rt._on_cmd(_cmd("relocalize", {"at_home": True}, "r2", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "busy", _ack(ears)
    await rt.close()


async def test_goto报的剩余距离按地图坐标(tmp_path):
    """内审应修 2:以前按原始里程算 —— 锚在 (10, 5) 时报 12 m,真距离 2 m。"""
    broker, c, ears, dog, rt = await _台(tmp_path)
    await rt._on_cmd(_cmd("relocalize", {"x": 10.0, "y": 5.0, "yaw": 0.0}, "r", c))
    await rt._on_cmd(_goto(12.0, 5.0, "g", c))
    await _跑(rt, broker, n=10, r=dog, c=c)
    prog = _事件(ears, "task_progress")
    assert prog and prog[0]["distance_m"] <= 2.1, prog[:2]
    await rt.close()
