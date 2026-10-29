"""W47 Nav2 后端(``docs/W47-导航换Nav2-设计.md``):契约、代理这头的转接服务端、
后端在仿真台子上的行为。
Nav2 本身不在这里起(那是 ``tools/navbench.py --backend nav2``);转接节点换成假的。"""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from types import SimpleNamespace as NS

import pytest
from test_obstacle_avoid import 台子

from d1max_agent.bridges.nav2_nav import Nav2NavBackend, _compose_map_odom
from d1max_agent.navbridge import NavBridgeServer
from d1max_agent.planning.planner import Planner
from d1max_contract.errors import ContractError
from d1max_contract.navbridge import FROM_AGENT, FROM_BRIDGE, encode, parse
from d1max_patrol.protocol.nav_types import NavStatus, Pose

# ------------------------------------------------------------ 契约


@pytest.mark.parametrize("msg", [
    {"type": "tick", "t_ms": 5, "odom": [0, 0, 0], "vel": [0, 0, 0], "map_odom": None},
    {"type": "goal", "goal_id": 1, "x": 1.0, "y": 2.0, "yaw": 0.5},
    {"type": "map", "req": 1, "map_id": "m", "map_version": "", "dir": "/x",
     "nogo": [[[0, 0], [1, 0], [1, 1]]], "speed": [{"poly": [[0, 0], [1, 0], [1, 1]], "mps": 0.3}]},
    {"type": "cmd", "t_ms": 3, "vx": 0.2, "vy": 0.0, "wz": -0.1},
    {"type": "goal_state", "goal_id": 2, "state": "aborted", "reason": "x"},
])
def test_契约_报文来回不变样(msg):
    assert parse(encode(msg)) == msg


@pytest.mark.parametrize("msg", [
    {"type": "tick", "t_ms": 5, "odom": [0, 0], "vel": [0, 0, 0]},
    {"type": "tick", "t_ms": -1, "odom": [0, 0, 0], "vel": [0, 0, 0]},
    {"type": "goal", "goal_id": 0, "x": 1, "y": 2, "yaw": 0},
    {"type": "goal", "goal_id": 1, "x": float("nan"), "y": 2, "yaw": 0},
    {"type": "goal_state", "goal_id": 1, "state": "weird"},
    {"type": "cmd", "t_ms": 1, "vx": 0, "vy": 0, "wz": 0, "extra": 1},
    {"type": "nope"},
    {"type": "map", "req": 1, "map_id": "m", "map_version": "", "dir": "/x",
     "speed": [{"poly": [[0, 0], [1, 0], [1, 1]], "mps": -1}]},
])
def test_契约_写错就拒(msg):
    with pytest.raises(ContractError):
        encode(msg)


def test_契约_哪头只许发哪些():
    with pytest.raises(ContractError, match="不该收到"):
        parse(encode({"type": "goal", "goal_id": 1, "x": 0, "y": 0, "yaw": 0}),
              allowed=FROM_BRIDGE)
    with pytest.raises(ContractError, match="不该收到"):
        parse(encode({"type": "cmd", "t_ms": 1, "vx": 0, "vy": 0, "wz": 0}), allowed=FROM_AGENT)


def test_map_odom_换算():
    est = NS(x=3.0, y=-1.0, yaw=1.2)
    odom = (0.7, 0.4, -0.5)
    mx, my, myaw = _compose_map_odom(est, odom)
    c, s = math.cos(myaw), math.sin(myaw)
    assert math.isclose(mx + c * odom[0] - s * odom[1], est.x, abs_tol=1e-9)
    assert math.isclose(my + s * odom[0] + c * odom[1], est.y, abs_tol=1e-9)
    assert math.isclose(math.remainder(myaw + odom[2] - est.yaw, 2 * math.pi), 0, abs_tol=1e-9)


# ------------------------------------------------------------ 转接服务端(真套接字)


@pytest.fixture
def 短目录():
    """Unix 套接字路径有长度上限(108 字节),pytest 的 tmp_path 带长测试名会超。"""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix="nb"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def _client(path):
    r, w = await asyncio.open_unix_connection(str(path))

    async def send(m):
        w.write(encode(m))
        await w.drain()

    async def recv():
        return json.loads(await asyncio.wait_for(r.readline(), 2))
    return r, w, send, recv


async def test_服务端_握手_补发地图_收就绪速度结果_断了就不就绪(短目录):
    srv = NavBridgeServer(短目录 / "nav2.sock")
    await srv.start()
    srv.send({"type": "map", "req": 1, "map_id": "m", "map_version": "", "dir": "/d"})
    r, w, send, recv = await _client(短目录 / "nav2.sock")
    await send({"type": "hello", "proto": 1, "name": "t"})
    assert (await recv())["type"] == "hello"
    assert (await recv())["type"] == "map", "握手完补发最近一份地图"
    assert srv.connected and not srv.ready
    await send({"type": "ready", "ok": True})
    await send({"type": "cmd", "t_ms": 9, "vx": 0.3, "vy": 0.0, "wz": 0.1})
    await send({"type": "goal_state", "goal_id": 1, "state": "succeeded"})
    for _ in range(50):
        if srv.goal_states:
            break
        await asyncio.sleep(0.01)
    assert srv.ready and srv.cmd[1:] == (9, 0.3, 0.0, 0.1)
    assert srv.goal_states[0]["state"] == "succeeded"
    w.close()
    for _ in range(50):
        if not srv.ready:
            break
        await asyncio.sleep(0.01)
    assert not srv.ready and srv.cmd is None and "断了" in srv.reason
    await srv.close()


async def test_服务端_握手不对就断(短目录):
    srv = NavBridgeServer(短目录 / "nav2.sock")
    await srv.start()
    r, w, send, recv = await _client(短目录 / "nav2.sock")
    await send({"type": "hello", "proto": 99})
    assert (await recv())["type"] == "error"
    assert not srv.connected
    await srv.close()


# ------------------------------------------------------------ 后端(假转接节点)


class 假转接:
    def __init__(self, ready=True):
        self.connected, self.ready, self.reason = True, ready, ""
        self.cmd = None
        self.goal_states: deque = deque()
        self.sent: list[dict] = []
        self.now = 0.0

    def _now(self):
        return self.now

    def send(self, m):
        self.sent.append(m)
        return True

    def tick(self):
        pass

    def kinds(self, k):
        return [m for m in self.sent if m["type"] == k]


def _台(tmp_path, link, **kw):
    t = 台子(tmp_path, **kw)
    old = t.nav
    nav = Nav2NavBackend(t.r, now_ms=t.c, map_id="m", planner=Planner(in_process=True), link=link)
    nav.obstacles, nav.guard, nav.on_event, nav.emit = (old.obstacles, old.guard, old.on_event,
                                                         old.emit)
    t.nav = nav
    nav.load_grid(tmp_path)
    return t


async def test_Nav2没就绪_这一段退回自己的规划_照样到(tmp_path):
    link = 假转接(ready=False)
    link.reason = "还在启动"
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    assert "还在启动" in t.nav.fallback_reason and not link.kinds("goal")
    assert ("nav_fallback", {"reason": "Nav2 没就绪(还在启动)"}) in t.events
    await t.跑(60.0)
    assert NavStatus.SUCCEED in t.status and not t.crashed


async def test_Nav2就绪_交目标_按它的速度走_它说到了就到(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    assert link.kinds("map") and link.kinds("map")[-1]["dir"] == str(tmp_path)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0))
    g = link.kinds("goal")[-1]
    assert (g["x"], g["y"]) == (6.0, 4.0) and t.nav.fallback_reason == ""
    for _ in range(30):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0.0, 0.0)
        await t.一拍()
    assert t.r.x > 2.0, "按 Nav2 的速度往前走了"
    ticks = link.kinds("tick")
    assert ticks and ticks[-1]["map_odom"] is not None and ticks[-1]["t_ms"] > ticks[0]["t_ms"]
    assert link.kinds("grid"), "感知栅格送过去了"
    link.goal_states.append({"type": "goal_state", "goal_id": g["goal_id"], "state": "succeeded"})
    await t.跑(2.0)
    assert NavStatus.SUCCEED in t.status


async def test_Nav2放弃_FAILED写清楚_取消时告诉它(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    await t.跑(0.5)
    link.goal_states.append({"type": "goal_state", "goal_id": gid, "state": "aborted",
                             "reason": "规划不出"})
    await t.跑(1.0)
    assert NavStatus.FAILED in t.status and "规划不出" in t.nav.fail_reason
    await t.跑(1.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    await t.跑(0.5)
    await t.nav.stop()
    assert link.kinds("cancel")[-1]["goal_id"] == link.kinds("goal")[-1]["goal_id"]


async def test_速度太旧发零_低于死区的整形(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    await t.跑(0.5)
    link.now = 10.0
    link.cmd = (9.0, 1, 0.4, 0.0, 0.0)                     # 1 s 前的
    await t.一拍()
    assert abs(t.r.speed[0]) < 1e-9, "太旧的速度不执行"
    from d1max_agent.bridges.nav2_nav import MIN_TRAVEL_MPS
    db = max(t.r.deadband_vx, MIN_TRAVEL_MPS)
    assert t.nav._deband(db * 0.4, 0.0) == (0.0, 0.0)
    assert t.nav._deband(-db * 0.6, 0.0) == (-db, 0.0)
    link.cmd = (10.0, 2, db * 0.7, 0.0, 0.0)
    await t.一拍()
    assert t.nav.fail_reason == "" and NavStatus.FAILED not in t.status, "抬到死区,HAL 不拒"


async def test_守卫只拦真看到的挡_拦久了报被挡(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    t.world.dyn["box"] = (2.6, 3.6, 3.0, 4.4)              # 机头前 0.6 m
    await t.跑(0.5)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    for _ in range(70):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.5, 0.0, 0.0)            # Nav2 硬要往前冲
        await t.一拍()
    assert not t.crashed, "守卫拦住了"
    assert [k for k, _ in t.events].count("nav_blocked") == 1


async def test_转接节点断了_走着的这一段FAILED(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    await t.跑(0.5)
    link.connected = False
    await t.跑(1.0)
    assert NavStatus.FAILED in t.status and "没连上" in t.nav.fail_reason


async def test_目标号跨后端不重复_旧目标的晚到回执不认(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    old = link.kinds("goal")[-1]["goal_id"]
    await t.nav.stop()
    await t.跑(1.0)
    (tmp_path / "b").mkdir()
    t2 = _台(tmp_path / "b", link)
    await t2.start(1.5, 4.0)
    await t2.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    new = link.kinds("goal")[-1]["goal_id"]
    assert new != old
    link.goal_states.append({"type": "goal_state", "goal_id": old, "state": "canceled"})
    await t2.跑(0.5)
    assert NavStatus.FAILED not in t2.status, "上一个后端的目标晚到的回执不算这一趟的"


async def test_原地转会扫到地图上的墙就不转_空地照转(tmp_path):
    """2026-10-29 仿真:0.85 m 门里 Nav2 的脱困原地转(Spin)扫到门框。门框在地图里:代理按地图核一道,
    不靠雷达、不靠 Nav2 的配置。"""
    link = 假转接()
    t = _台(tmp_path, link, walls=[(6.0, 0.1, 6.2, 3.575), (6.0, 4.425, 6.2, 7.9)])
    await t.start(6.1, 4.0)                                # 正卡在门里
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    for _ in range(30):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.0, 0.0, 1.0)            # Nav2 要原地转
        await t.一拍()
    assert abs(t.r.yaw) < 0.05 and not t.crashed, "门里不许原地转"
    await t.nav.stop()
    await t.跑(1.0)
    t.r.teleport(3.0, 4.0, 0.0)                            # 空地
    await t.跑(1.0)
    await t.nav.goto(Pose.from_xy_yaw(1.0, 4.0))
    for _ in range(10):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.0, 0.0, 1.0)
        await t.一拍()
    assert abs(t.r.yaw) > 0.3, "空地照转"


async def test_死区至少按真机实测_0点13米每秒_0点07弧度每秒(tmp_path):
    """2026-10-29 C40221 实测:前进、后退约 0.13 m/s 以下只踏步不走;转 0.07 rad/s 以下不转。"""
    from d1max_agent.bridges.nav2_nav import MIN_TRAVEL_MPS, MIN_TURN_RPS
    link = 假转接()
    t = _台(tmp_path, link)
    assert (MIN_TRAVEL_MPS, MIN_TURN_RPS) == (0.13, 0.07)
    assert t.nav._deband(0.05, 0.0) == (0.0, 0.0)
    assert t.nav._deband(0.08, 0.0) == (0.13, 0.0)
    assert t.nav._deband(-0.09, 0.0) == (-0.13, 0.0)
    assert t.nav._deband(0.3, 0.02) == (0.3, 0.0)
    assert t.nav._deband(0.0, -0.05) == (0.0, -0.07)


async def test_送给Nav2的速度按位置变化算_不信厂家里程报的速度(tmp_path):
    """厂家里程低速时原地踏步也报在走(10-29 实测 vx +0.087 m/s 时位置不变)。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    t.nav._v_meas = 0.087                                   # 厂家说在走
    for _ in range(5):
        await t.一拍()
    assert abs(link.kinds("tick")[-1]["vel"][0]) < 0.01, "位置没变:送 0"
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    for _ in range(20):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0.0, 0.0)
        await t.一拍()
    v = link.kinds("tick")[-1]["vel"][0]
    assert 0.25 < v < 0.5, v


async def test_看不见的地方快了就慢下来_看得见的挡才停(tmp_path):
    """2026-10-29 仿真:Nav2 1.0 m/s 时人从侧面(盲区)横穿,5 次撞 5 次 —— 「看不见的放行」快了不安全。
    改成:扫过区里有看不见的格子、又快,就降到 UNKNOWN_SLOW_MPS;低速照旧放行(不回到 10-28 起不了步)。"""
    from d1max_agent.bridges.nav2_nav import UNKNOWN_SLOW_MPS
    from d1max_agent.obstacles import Verdict
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    calls = []

    def guard(vx, wz):
        calls.append(vx)
        if abs(vx) > UNKNOWN_SLOW_MPS + 1e-9:
            return Verdict(False, "看不见 3 处", hits=(), unknown=3)
        return Verdict(True)
    t.nav._guard_check = guard
    for _ in range(15):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.9, 0.0, 0.0)
        await t.一拍()
    assert calls and calls[-1] == pytest.approx(UNKNOWN_SLOW_MPS), calls[-3:]
    assert 0.2 < t.r.speed[0] <= UNKNOWN_SLOW_MPS + 1e-6, "慢下来、不停"
    t.nav._guard_check = lambda vx, wz: Verdict(False, "扫过区有挡 2 处", hits=((1.0, 0.0),))
    for _ in range(5):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.9, 0.0, 0.0)
        await t.一拍()
    assert abs(t.r.speed[0]) < 0.05, "看得见的挡:停"

