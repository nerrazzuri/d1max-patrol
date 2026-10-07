"""全狗限速(W29,决策 41):站点下雨、雷暴时发 ``speed_cap``。真代理 + 仿真狗:能力里报、收下马上用上
(在走的 goto 也慢下来)、取消、到点自己取消并重发能力、载荷不对拒、不进幂等记录(重投照收);
限得比死区还低按死区;定位器自己能找回来的,丢定位等 30 秒(里程锚定等人给位置,还是 10 分钟)。"""

from __future__ import annotations

from test_deter import _台
from test_runtime_maps import _cmd, _跑

from d1max_agent.bridges.hal_nav import AUTO_RELOCALIZE_WAIT_S, HUMAN_RELOCALIZE_WAIT_S
from d1max_contract.messages import MapPose


async def _限(rt, broker, ears, c, v, cid, ttl=600):
    await rt._on_cmd(_cmd("speed_cap", {"max_speed_mps": v, "ttl_s": ttl}, cid, c))
    await broker.drain()
    return ears.by["cmd/ack"][-1]


def _能力(ears):
    return ears.by["capabilities"][-1]["tasks"]["speed_cap"]


async def _速度(rt, broker, dog, c, n=10):
    x0 = (await dog.odometry()).x
    await _跑(rt, broker, n=n, r=dog, c=c, settle_s=0)
    return ((await dog.odometry()).x - x0) / (n * 0.1)


async def test_收下马上用上_在走的goto也慢下来_取消就快回去(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    try:
        assert _能力(ears) == {"max_speed_mps": None}
        target = MapPose(map_id="m", map_version="1", frame_id="map", x=30.0, y=0.0,
                         yaw=0.0).to_wire()
        await rt._on_cmd(_cmd("goto", {"target": target}, "g1", c))
        await broker.drain()
        await _跑(rt, broker, n=20, r=dog, c=c, settle_s=0)
        fast = await _速度(rt, broker, dog, c)
        assert fast > 0.5, fast
        ack = await _限(rt, broker, ears, c, 0.3, "s1")
        assert ack["result"] == "accepted"
        await _跑(rt, broker, n=10, r=dog, c=c, settle_s=0)
        slow = await _速度(rt, broker, dog, c)
        assert slow <= 0.33, slow
        cap = _能力(ears)
        assert cap["max_speed_mps"] == 0.3 and 590 <= cap["left_s"] <= 600
        await _限(rt, broker, ears, c, None, "s2")
        await _跑(rt, broker, n=10, r=dog, c=c, settle_s=0)
        assert await _速度(rt, broker, dog, c) > 0.5
        assert _能力(ears) == {"max_speed_mps": None}
    finally:
        await rt.close()


async def test_到点自己取消_重发能力_重投照收_载荷不对拒(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    try:
        await _限(rt, broker, ears, c, 0.3, "s1", ttl=5)
        await _跑(rt, broker, n=2, r=dog, c=c, settle_s=0)
        assert _能力(ears)["max_speed_mps"] == 0.3
        c.mono += 6
        await _跑(rt, broker, n=2, r=dog, c=c, settle_s=0)
        assert _能力(ears) == {"max_speed_mps": None}, "站点没续:到点取消,能力重发"
        assert (await _限(rt, broker, ears, c, 0.3, "s1"))["result"] == "accepted", \
            "不进幂等记录:同一条重投照收"
        for bad in ({"max_speed_mps": 0.05, "ttl_s": 60}, {"max_speed_mps": 0.3, "ttl_s": 0},
                    {"max_speed_mps": 0.3}, {"max_speed_mps": "fast", "ttl_s": 60}):
            await rt._on_cmd(_cmd("speed_cap", bad, f"b{len(str(bad))}", c))
            await broker.drain()
            ack = ears.by["cmd/ack"][-1]
            assert ack["result"] == "rejected" and ack["reason"].startswith("payload"), bad
    finally:
        await rt.close()


async def test_限得比死区还低_按死区():
    from types import SimpleNamespace

    from d1max_agent.bridges.hal_nav import HalNavBackend
    caps = SimpleNamespace(max_vx=1.0, max_wz=1.5, deadband_vx=0.2)
    nav = HalNavBackend.__new__(HalNavBackend)
    nav._caps, nav._vmax, nav.speed_cap = caps, 1.0, 0.1
    assert nav._vlim() == 0.2
    nav.speed_cap = 0.3
    assert nav._vlim() == 0.3
    nav._vmax = 0.25
    assert nav._vlim() == 0.25, "任务给的更低:按任务的"


def test_定位器自己能找回来的_丢定位等30秒_里程锚定等人10分钟():
    from types import SimpleNamespace

    from d1max_agent.bridge_localizer import BridgeLocalizer
    from d1max_agent.bridges.hal_nav import HalNavBackend
    nav = HalNavBackend.__new__(HalNavBackend)
    nav.anchor = SimpleNamespace(identity=False, AUTO_RECOVERS=BridgeLocalizer.AUTO_RECOVERS)
    assert nav.RELOCALIZE_WAIT_S == AUTO_RELOCALIZE_WAIT_S == 30.0
    nav.anchor = SimpleNamespace(identity=False)
    assert nav.RELOCALIZE_WAIT_S == HUMAN_RELOCALIZE_WAIT_S
    nav.anchor = SimpleNamespace(identity=True)
    assert nav.RELOCALIZE_WAIT_S is None


def test_限着速时_近障从更远就开始减速():
    import math
    from types import SimpleNamespace

    from d1max_agent.bridges.planned_nav import NEAR_SLOW_FROM_M, PlannedNavBackend
    nav = PlannedNavBackend.__new__(PlannedNavBackend)
    nav._caps = SimpleNamespace(max_vx=1.0, max_wz=1.5, deadband_vx=0.2)
    nav._vmax = 1.0
    nav.guard = SimpleNamespace(margin=0.05, body_len=0.93, body_wid=0.48)
    nav.obstacles = SimpleNamespace(near_ahead=lambda *a: NEAR_SLOW_FROM_M + 0.5)
    nav.speed_cap = math.inf
    assert nav._near_cap(1) == math.inf, "不限速:2.5 m 外的障碍不减速"
    nav.speed_cap = 0.3
    assert nav._near_cap(1) < 0.3, "下雨限着速:2.5 m 就开始减速"
