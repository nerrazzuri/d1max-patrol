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

from nexwar_agent.bridges.nav2_nav import Nav2NavBackend, _compose_map_odom
from nexwar_agent.navbridge import NavBridgeServer
from nexwar_agent.planning.planner import Planner
from nexwar_contract.errors import ContractError
from nexwar_contract.navbridge import FROM_AGENT, FROM_BRIDGE, encode, parse
from nexwar_patrol.protocol.nav_types import NavStatus, Pose

# ------------------------------------------------------------ 契约


@pytest.mark.parametrize("msg", [
    {"type": "tick", "t_ms": 5, "odom": [0, 0, 0], "vel": [0, 0, 0], "map_odom": None},
    {"type": "goal", "goal_id": 1, "x": 1.0, "y": 2.0, "yaw": 0.5},
    {"type": "goal", "goal_id": 1, "x": 1.0, "y": 2.0, "yaw": 0.5, "via": [[0.5, 0.5], [1.0, 0.5]]},
    {"type": "map", "req": 1, "map_id": "m", "map_version": "", "dir": "/x",
     "nogo": [[[0, 0], [1, 0], [1, 1]]], "speed": [{"poly": [[0, 0], [1, 0], [1, 1]], "mps": 0.3}]},
    {"type": "cmd", "t_ms": 3, "vx": 0.2, "vy": 0.0, "wz": -0.1},
    {"type": "cmd", "t_ms": 3, "vx": -0.15, "vy": 0.0, "wz": 0.0, "goal_id": 4, "src": "beh"},
    {"type": "goal_state", "goal_id": 2, "state": "aborted", "reason": "x"},
])
def test_契约_报文来回不变样(msg):
    assert parse(encode(msg)) == msg


@pytest.mark.parametrize("msg", [
    {"type": "tick", "t_ms": 5, "odom": [0, 0], "vel": [0, 0, 0]},
    {"type": "tick", "t_ms": -1, "odom": [0, 0, 0], "vel": [0, 0, 0]},
    {"type": "goal", "goal_id": 0, "x": 1, "y": 2, "yaw": 0},
    {"type": "goal", "goal_id": 1, "x": float("nan"), "y": 2, "yaw": 0},
    {"type": "goal", "goal_id": 1, "x": 1, "y": 2, "yaw": 0, "via": "x"},
    {"type": "goal", "goal_id": 1, "x": 1, "y": 2, "yaw": 0, "via": [[1, 2, 3]]},
    {"type": "goal", "goal_id": 1, "x": 1, "y": 2, "yaw": 0, "via": [[0, 0]] * 501},
    {"type": "goal", "goal_id": 1, "x": 1, "y": 2, "yaw": 0, "via": [[float("inf"), 0]]},
    {"type": "goal_state", "goal_id": 1, "state": "weird"},
    {"type": "cmd", "t_ms": 1, "vx": 0, "vy": 0, "wz": 0, "extra": 1},
    {"type": "cmd", "t_ms": 1, "vx": 0, "vy": 0, "wz": 0, "src": "smoother"},
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
    from nexwar_contract.navbridge import PROTO
    await send({"type": "hello", "proto": PROTO, "name": "t"})
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
    await send({"type": "path", "goal_id": 1, "pts": [[0.0, 0.0], [1.0, 0.5]]})
    for _ in range(50):
        if srv.plan:
            break
        await asyncio.sleep(0.01)
    assert srv.plan == (1, ((0.0, 0.0), (1.0, 0.5)))
    assert srv.ready and srv.cmd[1:7] == (9, 0.3, 0.0, 0.1, None, "ctl")
    assert srv.goal_states[0]["state"] == "succeeded"
    w.close()
    for _ in range(50):
        if not srv.ready:
            break
        await asyncio.sleep(0.01)
    assert not srv.ready and srv.cmd is None and "已断开" in srv.reason
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
        if m["type"] == "goal" and getattr(self, "auto_plan", True):
            # 真 Nav2 收下目标就规划出路线(转接节点按目标号报);测 P1 的那条自己管
            self.plan = (m["goal_id"], ((m["x"], m["y"]),))
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
    assert ("nav_fallback", {"reason": "Nav2 未就绪(还在启动)"}) in t.events
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
    from nexwar_agent.bridges.nav2_nav import MIN_TRAVEL_MPS
    db = max(t.r.deadband_vx, MIN_TRAVEL_MPS)
    assert t.nav._deband(0.02, 0.0) == (0.0, 0.0)
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
    assert NavStatus.FAILED in t.status and "未连接" in t.nav.fail_reason


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
    assert NavStatus.FAILED not in t2.status, "上一个后端的目标晚到的回执不算本次任务的"


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


async def test_死区按命令算_0点13米每秒_原地转0点15弧度每秒_工单11_02_10(tmp_path):
    """2026-10-29 C40221 实测:前进、后退约 0.13 m/s 以下只踏步不走。原地转:比例 0.10 才实际转
    0.069 rad/s,比例 0.10 = 命令 0.15 rad/s(1.5 rad/s/比例)—— 原来把实际转速 0.07 当命令门槛,
    等于不转(11-02 工单 10)。MPPI 在障碍边上、收尾发的小速度不清零(清零 = 一直不动、「没进展」):
    0.03 以上一律抬到能走起来的最低速度。走着的时候转向不动(走路时小转向照样转)。"""
    from nexwar_agent.bridges.nav2_nav import DEBAND_FLOOR, MIN_TRAVEL_MPS, MIN_TURN_RPS
    link = 假转接()
    t = _台(tmp_path, link)
    assert (MIN_TRAVEL_MPS, MIN_TURN_RPS, DEBAND_FLOOR) == (0.13, 0.15, 0.03)
    assert t.nav._deband(0.02, 0.0) == (0.0, 0.0)
    assert t.nav._deband(0.05, 0.0) == (0.13, 0.0)
    assert t.nav._deband(-0.09, 0.0) == (-0.13, 0.0)
    assert t.nav._deband(0.3, 0.02) == (0.3, 0.02)
    assert t.nav._deband(0.0, -0.05) == (0.0, -0.15)
    assert t.nav._deband(0.0, 0.02) == (0.0, 0.0)


async def test_限速压前进时转向同比例压_弯不变急_工单11_02_9(tmp_path):
    """11-02 回程卡住:Nav2 要 0.87 m/s + 0.7 rad/s(弯半径 1.24 m),代理只把前进压到 0.3、转向照
    旧 → 半径 0.43 m,急拐向方块角,守卫一直拦。压前进就按同一比例压转向,走的还是 Nav2 算的那条弯。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.set_speed(0.3)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    seen = []
    for _ in range(10):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.87, 0.0, 0.7)
        await t.一拍()
        seen.append((t.nav._last_vx, t.nav._last_wz))
    vx, wz = max(seen)
    assert abs(vx - 0.3) < 1e-6, vx
    assert abs(wz / vx - 0.7 / 0.87) < 0.01, ("弯半径要跟 Nav2 的一样", vx, wz)


async def test_送给Nav2的速度按位置变化算_不信厂家里程报的速度(tmp_path):
    """厂家里程低速时原地踏步也报在走(10-29 实测 vx +0.087 m/s 时位置不变)。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    t.nav._v_meas = 0.087                                   # 厂家说在走
    for _ in range(5):
        await t.一拍()
    assert abs(link.kinds("tick")[-1]["vel"][0]) < 0.01, "位置未变:送 0"
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    for _ in range(20):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0.0, 0.0)
        await t.一拍()
    v = link.kinds("tick")[-1]["vel"][0]
    assert 0.25 < v < 0.5, v


async def test_看不见的地方快了就慢下来_看得见的挡才停(tmp_path):
    """2026-10-29 仿真:Nav2 1.0 m/s 时人从侧面(盲区)横穿,5 次撞 5 次 ——
    「看不见的放行」快了不安全。改成:扫过区里有看不见的格子、又快,就降到 UNKNOWN_SLOW_MPS;
    低速照旧放行(不回到 10-28 起不了步)。"""
    from nexwar_agent.bridges.nav2_nav import UNKNOWN_SLOW_MPS
    from nexwar_agent.obstacles import Verdict
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



# ------------------------------------------------------------ 看得清就快(决策 76)


async def _看清(t, n=8):
    for _ in range(n):
        await t.一拍()
    return t.nav._vis_cap(await t.nav._here(), 1)


async def test_看得清就快_空地放到最快(tmp_path):
    from nexwar_agent.bridges.nav2_nav import V_FAST
    t = _台(tmp_path, 假转接())
    await t.start(1.5, 4.0)
    assert await _看清(t) == pytest.approx(V_FAST)


async def test_看得清就快_正前方有箱子按刹车距离降(tmp_path):
    from nexwar_agent.bridges.nav2_nav import V_FAST
    t = _台(tmp_path, 假转接())
    await t.start(1.5, 4.0)
    t.world.dyn["box"] = (3.3, 3.7, 3.7, 4.3)               # 机头前约 1.3 m
    v = await _看清(t)
    assert v < 0.7 * V_FAST, v


async def test_看得清就快_斜前方有人就降_地图上的墙不算(tmp_path):
    from nexwar_agent.bridges.nav2_nav import V_FAST
    walls = [(0.1, 3.2, 11.9, 3.3), (0.1, 4.7, 11.9, 4.8)]  # 1.4 m 走廊:墙在地图里
    t = _台(tmp_path, 假转接(), walls=walls)
    await t.start(1.5, 4.0)
    assert await _看清(t) == pytest.approx(V_FAST), "走廊两边的墙在地图里:不拖慢"
    t2_dir = tmp_path / "b"
    t2_dir.mkdir()
    t2 = _台(t2_dir, 假转接())
    await t2.start(1.5, 4.0)
    t2.world.dyn["person"] = (3.0, 2.9, 3.4, 3.3)           # 前 1.2 m、右 0.9 m:不在扫过区
    v = await _看清(t2)
    assert v < 0.8 * V_FAST, v


@pytest.mark.parametrize("box, want", [
    ((2.6, 3.8, 2.9, 4.2), 0.3),                            # 机头前约 0.6 m:1 m 以内
    ((3.2, 2.1, 3.5, 2.4), 0.6),                            # 离机身边约 1.8 m(前 1.2、右 1.3)
    ((4.2, 6.4, 4.5, 6.7), 1.0),                            # 前 2.2 m、左 2.4 m:2 m 之外
])
async def test_W70_看得清就快_人2米内降到0点6_1米内0点3_别的到1点0(tmp_path, box, want):
    """用户 11-03:「速度提高到 1.0,周围 2 m 有人降到 0.6,1 m 以下 0.3」。"""
    t = _台(tmp_path, 假转接())
    await t.start(1.5, 4.0)
    t.world.dyn["p"] = box
    assert await _看清(t) == pytest.approx(want), box


async def test_看得清就快_限速送给Nav2(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    t.world.dyn["box"] = (3.3, 3.7, 3.7, 4.3)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    for _ in range(10):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.0, 0.0, 0.0)
        await t.一拍()
    sp = link.kinds("tick")[-1].get("speed")
    assert sp is not None and 0 < sp < 0.8, sp


# ------------------------------------------------------------ 站点上看得见(路线、健康)


async def test_走Nav2时_站点地图上画Nav2规划的路线(tmp_path):
    link = 假转接()
    link.plan = None
    link.auto_plan = False
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    assert t.nav.current_path() is None, "Nav2 尚未规划出来"
    link.plan = (gid - 1, ((0.0, 0.0), (1.0, 1.0)))
    assert t.nav.current_path() is None, "别的目标的路线不算"
    link.plan = (gid, ((1.5, 4.0), (3.0, 4.2), (6.0, 4.0)))
    assert t.nav.current_path() == ((1.5, 4.0), (3.0, 4.2), (6.0, 4.0))
    await t.nav.stop()
    await t.跑(1.0)
    assert t.nav.current_path() is None


async def test_设备健康_Nav2就绪没有_为什么(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    assert t.nav.health() == {"id": "nav2", "state": "online", "detail": "Nav2 就绪"}
    link.ready, link.reason = False, "还在启动(生命周期未全部激活)"
    h = t.nav.health()
    assert h["state"] == "offline" and "还在启动" in h["detail"] and "退回" in h["detail"]
    link.connected = False
    assert "未连接" in t.nav.health()["detail"]


def test_路径抽稀_头尾都留_最多256点():
    from nexwar_localizer.nav2bridge import decimate
    pts = [(i * 0.05, 0.0) for i in range(2001)]           # 100 m、每 5 cm 一个点
    out = decimate(pts)
    assert out[0] == [0.0, 0.0] and out[-1] == [100.0, 0.0] and len(out) <= 256
    assert decimate([(0, 0), (0.01, 0), (0.02, 0)]) == [[0.0, 0.0], [0.02, 0.0]]
    assert decimate([]) == []


async def test_运行时_nav2后端的设备健康里有Nav2一条(tmp_path):
    from test_runtime import REG, 钟

    from nexwar_adapter_sim.robot import SimRobot
    from nexwar_agent.runtime import AgentRuntime
    from nexwar_contract.memory_broker import MemoryBroker, MemoryTransport
    c = 钟()
    rt = AgentRuntime(transport=MemoryTransport(MemoryBroker(), "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path / "a", now_ms=c,
                      loaded_map=("m", "1"), home=Pose.from_xy_yaw(0, 0), nav="nav2",
                      obstacles="bridge", monotonic=lambda: c.mono)
    got = await rt.devices.refresh()
    n = [d for d in got if d["id"] == "nav2"]
    assert n and n[0]["state"] == "offline" and "未连接" in n[0]["detail"]


# ---------------------------------------------------------------- W47 外审(R4–R7)

async def test_外审R4_限速区的上限也管Nav2的速度_送给Nav2的也降(tmp_path):
    from test_planned_nav import _区
    link = 假转接()
    t = _台(tmp_path, link, max_vx=1.0)
    await t.start(5, 4)
    await t.nav.set_zones(_区({"id": "children", "kind": "slow", "max_speed_mps": 0.2,
                               "polygon": [[3, 2], [9, 2], [9, 6], [3, 6]]}))
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    for _ in range(15):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.8, 0, 0)
        await t.一拍()
    assert t.nav._last_vx <= 0.2 + 1e-9 and t.r.speed[0] <= 0.2 + 1e-6
    assert link.kinds("tick")[-1]["speed"] <= 0.3 + 1e-9      # 送给 Nav2 的不高于最低档


async def test_外审R5_死区整形不越过任务限速_也不越过转速上限(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4)
    await t.nav.set_speed(0.1)
    await t.nav.goto(Pose.from_xy_yaw(6, 4))
    for _ in range(15):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.5, 0, 0)
        await t.一拍()
    assert abs(t.nav._last_vx) <= 0.1 + 1e-9, "限速低于死区:停下,不抬到死区"
    link2 = 假转接()
    t2 = _台(tmp_path, link2)
    await t2.start(5, 4)
    await t2.nav.set_speed(0.6, z=0.05)
    await t2.nav.goto(Pose.from_xy_yaw(2, 4))
    for _ in range(15):
        link2.now += 0.1
        link2.cmd = (link2.now, 1, 0, 0, 0.5)
        await t2.一拍()
    assert abs(t2.nav._last_wz) <= 0.05 + 1e-9


async def test_外审R6_Nav2往后退时_禁行区提前刹按后退方向查(tmp_path):
    from test_planned_nav import _区, _在
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.set_zones(_区({"id": "pond", "kind": "nogo",
                               "polygon": [[2, 0], [4.365, 0], [4.365, 8], [2, 8]]}))
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    t.nav._v_meas = -0.3
    link.now = 0.0
    link.cmd = (0.0, 1, -0.3, 0, 0)
    await t.nav._drive(_在(5, 4), 0.1)
    assert t.nav._last_vx > -0.2, "身后 17 cm 是禁行区:不许照 0.3 往后退"


async def test_外审R7_旧目标晚到的速度_不驱动新目标(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    a = link.kinds("goal")[-1]["goal_id"]
    await t.nav.stop()
    for _ in range(8):
        await t.一拍()
    link.cmd = (link.now, 1, 0.4, 0, 0)                     # A 的速度晚到(老转接节点:不带目标号)
    await t.nav.goto(Pose.from_xy_yaw(2, 4))               # B 在身后,还没收下、没出速度
    for _ in range(4):
        await t.一拍()
    assert t.nav._last_vx <= 1e-9, "新目标尚无自己的速度:不动"
    b = link.kinds("goal")[-1]["goal_id"]
    link.cmd = (link.now, 1, 0.4, 0, 0, a)                  # 新转接节点:标了是 A 的
    for _ in range(3):
        await t.一拍()
    assert t.nav._last_vx <= 1e-9
    link.cmd = (link.now, 1, -0.3, 0, 0, b)                 # B 自己的:照走
    await t.一拍()
    assert t.nav._last_vx < -0.1


# ---------------------------------------------------------------- W47 复查(N2、N3、N5)

async def test_外审复查N2_很窄的限速区也算上_送给Nav2的不高于区域上限(tmp_path):
    from test_planned_nav import _区
    link = 假转接()
    t = _台(tmp_path, link, max_vx=1.0)
    await t.start(5, 4)
    await t.nav.set_zones(_区({"id": "thin", "kind": "slow", "max_speed_mps": 0.2,
                               "polygon": [[5.02, 2], [5.08, 2], [5.08, 6], [5.02, 6]]}))
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    gid = link.kinds("goal")[-1]["goal_id"]
    sent = []
    for _ in range(6):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.8, 0, 0, gid)
        await t.一拍()
        sent.append(t.nav._last_vx)
    assert 0 < max(sent) <= 0.2 + 1e-9, sent
    assert link.kinds("tick")[-1]["speed"] <= 0.2 + 1e-9


async def test_外审复查N3_没标目标号的速度不认(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(4):                                       # 先起步(头几拍切步态)
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0, 0, gid)
        await t.一拍()
    assert t.nav._last_vx > 0.3
    for _ in range(2):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0, 0, None)            # 转接节点那会儿没有在走的目标
        await t.一拍()
    assert t.nav._last_vx <= 1e-9


async def test_外审复查N5_刚停下时上一条命令还在后退_往前也要先按后退方向查禁行区(tmp_path):
    from test_planned_nav import _区, _在
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.set_zones(_区({"id": "pond", "kind": "nogo",
                               "polygon": [[2, 0], [4.365, 0], [4.365, 8], [2, 8]]}))
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    gid = link.kinds("goal")[-1]["goal_id"]
    t.nav._v_meas = 0.0
    t.nav._last_vx = -0.3                                     # 刚发过后退,实测还没跟上
    link.now = 0.0
    link.cmd = (0.0, 1, 0.3, 0, 0, gid)
    await t.nav._drive(_在(5, 4), 0.1)
    assert t.nav._last_vx <= 1e-9, "上一条后退可能还在执行:身后 17 cm 是禁行区,这一拍先停"


async def test_外审复查P1_新目标收到自己的规划路线之前_Nav2的速度一律不用(tmp_path):
    """0.2 秒屏障证明不了速度是新目标的;控制器总是拿到新路线之后才出新目标的速度。"""
    link = 假转接()
    link.auto_plan = False
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(5):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0, 0, gid)                # 标的是新目标,可路线还没来
        await t.一拍()
    assert t.nav._last_vx <= 1e-9
    link.plan = (gid - 1, ((5.0, 4.0), (2.0, 4.0)))             # 别的目标的路线:不算
    link.now += 0.1
    link.cmd = (link.now, 1, 0.4, 0, 0, gid)
    await t.一拍()
    assert t.nav._last_vx <= 1e-9
    link.plan = (gid, ((5.0, 4.0), (8.0, 4.0)))                 # 自己的路线来了
    for _ in range(3):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0, 0, gid)
        await t.一拍()
    assert t.nav._last_vx > 0.3


def test_外审复查P1_转接节点订控制器的原始输出_不经速度平滑器():
    """平滑器会把旧目标的速度慢慢衰减(OPEN_LOOP),目标换了还在出旧方向的速度。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[3] / "packages" / "localizer" / "src"
           / "nexwar_localizer" / "nav2bridge.py").read_text(encoding="utf-8")
    assert 'create_subscription(Twist, "/cmd_vel_nav"' in src
    assert 'create_subscription(Twist, "/cmd_vel",' not in src


async def test_外审复查P3_MPPI倒车被限速压到死区以下_照最小能走的倒车执行(tmp_path):
    """MPPI 按同一比例缩倒车上限(vx_min / vx_max = 0.3):限速 0.2 时它最多倒 0.06,
    原来落进死区一律清零。"""
    from test_planned_nav import _区
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.set_zones(_区({"id": "lawn", "kind": "slow", "max_speed_mps": 0.2,
                               "polygon": [[3, 2], [9, 2], [9, 6], [3, 6]]}))
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    gid = link.kinds("goal")[-1]["goal_id"]
    sent = []
    for _ in range(6):
        link.now += 0.1
        link.cmd = (link.now, 1, -0.06, 0, 0, gid)
        await t.一拍()
        sent.append(t.nav._last_vx)
    assert min(sent) == pytest.approx(-0.13) and max(abs(v) for v in sent) <= 0.2 + 1e-9


def test_外审复查P3_代理记的倒车比例跟Nav2参数一致():
    from pathlib import Path

    from nexwar_agent.bridges.nav2_nav import MPPI_REVERSE_RATIO
    text = (Path(__file__).resolve().parents[3] / "deploy" / "nav2" / "params.yaml").read_text()
    import re
    vmax = float(re.search(r"\n\s+vx_max:\s*([0-9.]+)", text).group(1))
    vmin = float(re.search(r"\n\s+vx_min:\s*(-?[0-9.]+)", text).group(1))
    assert MPPI_REVERSE_RATIO == pytest.approx(-vmin / vmax)


async def test_复查三P1_路线门槛打开前缓存的速度作废_要比路线晚到的才认(tmp_path):
    """Codex 第三轮:新目标的路线来了,门槛打开,之前缓存的(标的也是新目标的)旧速度不能拿来开车。"""
    link = 假转接()
    link.auto_plan = False
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.goto(Pose.from_xy_yaw(2, 4))                    # 身后的目标
    gid = link.kinds("goal")[-1]["goal_id"]
    link.now += 0.1
    link.cmd = (link.now, 1, 0.4, 0, 0, gid, "ctl", 1)          # 第 1 条到的:上一个目标残留的往前
    link.plan, link.plan_rx = (gid, ((5.0, 4.0), (2.0, 4.0))), {gid: 2}   # 第 2 条到的:路线
    for _ in range(6):                                         # 过了起步那几拍
        link.now += 0.05
        link.cmd = (link.now, *link.cmd[1:])                    # 还是那一条(没新速度来)
        await t.一拍()
    assert t.nav._status is NavStatus.ACTIVE
    assert t.nav._last_vx <= 1e-9, "比路线早到的速度不认"
    for rx in (3, 4, 5):
        link.now += 0.1
        link.cmd = (link.now, 1, -0.3, 0, 0, gid, "ctl", rx)
        await t.一拍()
    assert t.nav._last_vx < -0.1, "路线之后到的照用"


async def test_复查三P4_脱困动作的速度不用等路线_目标号照样要对(tmp_path):
    """BackUp 往往在规划不出来时才做(没有路线):标了 beh 的不过路线门槛;别的目标的照样不认。"""
    link = 假转接()
    link.auto_plan = False
    t = _台(tmp_path, link)
    await t.start(5, 4)
    await t.nav.goto(Pose.from_xy_yaw(8, 4))
    gid = link.kinds("goal")[-1]["goal_id"]
    for rx in (1, 2, 3):
        link.now += 0.1
        link.cmd = (link.now, 1, -0.15, 0, 0, gid - 1, "beh", rx)   # 别的目标的脱困
        await t.一拍()
    assert t.nav._last_vx >= -1e-9
    for rx in (4, 5, 6):
        link.now += 0.1
        link.cmd = (link.now, 1, -0.15, 0, 0, gid, "beh", rx)
        await t.一拍()
    assert t.nav._last_vx < -0.1


def test_复查三_转接服务端_报文按到达编号_记下每个目标第一条路线是第几条到的():
    from nexwar_agent.navbridge import NavBridgeServer
    s = NavBridgeServer("/nonexistent/nav2.sock")
    s._on({"type": "cmd", "t_ms": 1, "vx": 0.1, "vy": 0, "wz": 0, "goal_id": 7, "src": "ctl"})
    s._on({"type": "path", "goal_id": 7, "pts": [[0, 0], [1, 0]]})
    s._on({"type": "path", "goal_id": 7, "pts": [[0, 0], [2, 0]]})     # 重规划:不挪第一条
    s._on({"type": "cmd", "t_ms": 2, "vx": 0.2, "vy": 0, "wz": 0, "goal_id": 7, "src": "beh"})
    assert s.plan_rx[7] == 2 and s.cmd[6:] == ("beh", 4)
    s._on({"type": "cmd", "t_ms": 3, "vx": 0.2, "vy": 0, "wz": 0})       # 老转接节点:不带
    assert s.cmd[6:] == ("ctl", 5)


def test_W48_协议4_目标可以带途经点():
    from nexwar_contract.navbridge import MAX_VIA, PROTO
    assert PROTO == 4 and MAX_VIA >= 500, "路网最多 500 个点,途经点要装得下"


async def test_W48_有路网_交给Nav2的目标带上路网上的途经点_没路网不带(tmp_path):
    from nexwar_contract.routenet import RouteNet
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0))
    assert "via" not in link.kinds("goal")[-1]
    link.goal_states.append({"type": "goal_state", "goal_id": link.kinds("goal")[-1]["goal_id"],
                             "state": "succeeded"})
    await t.跑(1.0)
    net = RouteNet.from_wire({"map_id": "m", "map_version": "", "revision": 1,
                              "nodes": [{"id": "A", "x": 1.5, "y": 5.0},
                                        {"id": "B", "x": 6.0, "y": 5.2}],
                              "edges": [["A", "B"]]})
    t.nav.set_route_net(net)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.5, 0.0))
    g = link.kinds("goal")[-1]
    here = (t.r.x, t.r.y)
    from nexwar_contract.routenet import densify, route
    want = densify(route(net, here, (6.0, 4.5)), step=1.0)
    assert g["via"] == [[round(x, 3), round(y, 3)] for x, y in want] and len(g["via"]) > 2, \
        "沿路每隔不到 1 m 一个途经点:Nav2 跳过一个(1.5 m 内算经过)下一个还在路上,不切角"


async def test_W48复查M1_很长的路网_加密以后途经点也不超过协议上限(tmp_path):
    """外审 W48-M1:按总长算间距、每段各自向上取整,点数可能超 500,报文编不出来。"""
    from nexwar_contract.navbridge import MAX_VIA
    from nexwar_contract.routenet import RouteNet
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    # 300 个点、间距 1.6 m(总长约 478 m):每段都要切成两段,原来会出约 600 个途经点
    nodes = [{"id": f"N{i}", "x": 1.5 + i * 1.6, "y": 4.3} for i in range(300)]
    net = RouteNet.from_wire({"map_id": "m", "map_version": "", "revision": 1, "nodes": nodes,
                              "edges": [[f"N{i}", f"N{i + 1}"] for i in range(299)]})
    t.nav.set_route_net(net)
    await t.nav.goto(Pose.from_xy_yaw(1.5 + 299 * 1.6, 4.0, 0.0))
    g = link.kinds("goal")[-1]
    assert 250 < len(g.get("via", [])) <= MAX_VIA


def test_MPPI采样数按机器人上的算力_工单11_02_12():
    """11-02 C40221 上 controller 几乎每秒「missed its desired rate」:采样 1000 条算不过来。"""
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[3] / "deploy" / "nav2" / "params.yaml").read_text()
    batch = int(re.search(r"\n\s+batch_size:\s*(\d+)", text).group(1))
    assert batch <= 500


async def test_只差朝向的目标_代理自己原地转_不交给Nav2_工单11_02_11(tmp_path):
    """11-02:同一位置换朝向 10°,Smac Lattice「no valid path」、60 s 不动。离目标在容差内只差朝向的,
    代理自己原地转(守卫、地图墙照样管)。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0, 0.0)
    await t.nav.goto(Pose.from_xy_yaw(1.55, 4.0, math.radians(10)))
    assert not link.kinds("goal"), "不交给 Nav2"
    await t.跑(20.0)
    assert NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason)
    assert abs(math.degrees(t.r.yaw) - 10) < 9, math.degrees(t.r.yaw)


async def test_离目标远的照样交给Nav2(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0, 0.0)
    await t.nav.goto(Pose.from_xy_yaw(2.0, 4.0, 0.0))
    assert link.kinds("goal")


async def test_Nav2走到目标附近只差朝向_代理接过来自己转_工单11_02_18(tmp_path):
    """11-02 复测:从 (0,0) 派到 C1 起点,位置到了、差 66°,Nav2 三次没进展、原地卡约 50 s。到了
    xy 容差、只差朝向时代理取消 Nav2 的目标,自己原地转。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0, 0.0)
    await t.nav.goto(Pose.from_xy_yaw(2.5, 4.0, math.radians(66)))
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(60):                                  # Nav2 把它开到目标点,朝向不管
        link.now += 0.1
        dx = 2.5 - t.r.x
        link.cmd = (link.now, 1, 0.3 if dx > 0.05 else 0.0, 0.0, 0.0)
        await t.一拍()
    assert abs(t.r.x - 2.5) < 0.2
    await t.跑(15.0)
    assert link.kinds("cancel") and link.kinds("cancel")[-1]["goal_id"] == gid, "取消 Nav2 的目标"
    assert NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason)
    assert abs(math.degrees(t.r.yaw) - 66) < 9, math.degrees(t.r.yaw)


async def test_W70_原地转到朝向_差不到3度才算到(tmp_path):
    """2026-11-03 C40221:原点朝向 0,转完停在 0.166 rad(9.5°):原来差 0.15 rad 以内就停。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0, math.radians(14))
    await t.nav.goto(Pose.from_xy_yaw(1.55, 4.0, 0.0))
    await t.跑(20.0)
    assert NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason)
    assert abs(math.degrees(t.r.yaw)) < 3.5, math.degrees(t.r.yaw)


async def test_W70_刚中止完_Nav2在取消上一个目标_回原点等它一下_不退回(tmp_path, monkeypatch):
    """2026-11-03 C40221:用户中止任务后马上回原点,Nav2 还在取消上一个目标(转接节点报未就绪),
    代理立刻退回自己的规划导航(倒退 1.3 m、停 20 s)。这种一会儿就好的未就绪,等它。"""
    import nexwar_agent.bridges.nav2_nav as N
    monkeypatch.setattr(N, "NAV2_SETTLE_POLL_S", 0.01)
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0, 0.0)
    link.ready, link.reason = False, "等 Nav2 取消上一次留下的导航目标"
    task = asyncio.get_running_loop().create_task(t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0)))
    await asyncio.sleep(0.05)
    assert not task.done() and not link.kinds("goal")
    link.ready, link.reason = True, ""
    await asyncio.wait_for(task, 2.0)
    assert link.kinds("goal") and t.nav.fallback_reason == ""


async def test_W70_Nav2一直取消不完_等够了才退回自己的规划(tmp_path, monkeypatch):
    import nexwar_agent.bridges.nav2_nav as N
    monkeypatch.setattr(N, "NAV2_SETTLE_POLL_S", 0.01)
    monkeypatch.setattr(N, "NAV2_SETTLE_S", 0.1)
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0, 0.0)
    link.ready, link.reason = False, "等 Nav2 取消上一次留下的导航目标"
    await asyncio.wait_for(t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0)), 2.0)
    assert "取消上一次" in t.nav.fallback_reason and not link.kinds("goal")


async def test_W70_中止那一拍卡住_未在送时钟_回原点自己送上时钟(tmp_path, monkeypatch):
    """2026-11-03 C40221 11:58:01:中止后马上回原点,「Nav2 未就绪(代理未在送时钟)」→ 退回自己的
    规划(倒退 1.3 m、停 20 s)。处理中止那一拍卡了 2 s 多,时钟没送;goto 自己先送上时钟再等。"""
    import nexwar_agent.bridges.nav2_nav as N
    monkeypatch.setattr(N, "NAV2_SETTLE_POLL_S", 0.01)
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0, 0.0)
    link.ready, link.reason = False, "代理未在送时钟"
    send = link.send

    def 收到时钟就绪(m):
        if m["type"] == "tick":
            link.ready, link.reason = True, ""
        return send(m)
    link.send = 收到时钟就绪
    await asyncio.wait_for(t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0)), 2.0)
    assert link.kinds("goal") and t.nav.fallback_reason == ""


async def test_W70_只差朝向_转身会扫到墙_被挡10秒就停在这儿算到_不干等(tmp_path):
    """11-03 C40221:原点旁边有椅子、桌子,要转 170°,守卫一直拦,停了一分多钟不动。"""
    link = 假转接()
    # 机头左前方 0.45 m 处一根柱子:转大半圈一定扫到
    t = _台(tmp_path, link, walls=[(1.85, 4.35, 1.95, 4.45)])
    await t.start(1.5, 4.0, 0.0)
    await t.nav.goto(Pose.from_xy_yaw(1.55, 4.0, math.radians(170)))
    await t.跑(5.0)
    assert NavStatus.SUCCEED not in t.status, "被挡不到 10 s 先等"
    await t.跑(8.0)
    assert NavStatus.SUCCEED in t.status, (t.status, t.nav.fail_reason)
    assert abs(math.degrees(t.r.yaw)) < 120, "确实被挡住、没转过去"
    assert not t.crashed


async def test_W70_Nav2失败而狗没挪窝_说规划不出_照旧再试一次(tmp_path):
    """11-03 C40221:原点被椅子占了,Nav2 一直 no valid path,站点上看不到原因。15:00 又一例:第一次
    空等 2 分钟,再发一次 35 s 就到了 —— 没挪窝的说清楚原因,但照旧让引擎再试一次。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    await t.跑(0.5)
    link.goal_states.append({"type": "goal_state", "goal_id": gid, "state": "aborted",
                             "reason": "no valid path"})
    await t.跑(1.0)
    assert NavStatus.FAILED in t.status and "规划不出到目标的路线" in t.nav.fail_reason
    assert not t.nav.fail_final


async def test_W70_Nav2失败但走了一大段_照旧让引擎重试(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(30):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.4, 0.0, 0.0)
        await t.一拍()
    assert t.r.x > 2.0
    link.goal_states.append({"type": "goal_state", "goal_id": gid, "state": "aborted",
                             "reason": "x"})
    await t.跑(1.0)
    assert NavStatus.FAILED in t.status and not t.nav.fail_final


async def test_W70_目标点上有箱子_Nav2失败_说目标点被占_不再重试(tmp_path):
    """11-03 C40221:椅子放得离原点太近,原点本身被占了;Nav2 只说 no valid path。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    t.world.dyn["box"] = (2.8, 3.8, 3.2, 4.2)               # 正好压在目标 (3.0, 4.0) 上
    await _看清(t)
    await t.nav.goto(Pose.from_xy_yaw(3.0, 4.0, 0.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    await t.跑(0.5)
    link.goal_states.append({"type": "goal_state", "goal_id": gid, "state": "aborted",
                             "reason": "no valid path"})
    await t.跑(1.0)
    assert NavStatus.FAILED in t.status and "目标点被占" in t.nav.fail_reason
    assert t.nav.fail_final


async def test_W70_Nav2卡住不给速度也不回结果_过20秒重发同一个目标_最多两次(tmp_path):
    """11-03 15:00 C40221:Nav2 的 action server、清图服务都确认超时,原地空等到 120 s;
    重发一次就好了。"""
    from nexwar_agent.bridges.nav2_nav import NAV2_RESENDS, NAV2_STALL_S
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0))
    first = link.kinds("goal")[-1]
    await t.跑(NAV2_STALL_S - 3)
    assert len(link.kinds("goal")) == 1, "没到时候不重发"
    await t.跑(5.0)
    goals = link.kinds("goal")
    assert len(goals) == 2 and goals[-1]["goal_id"] != first["goal_id"]
    assert (goals[-1]["x"], goals[-1]["y"]) == (first["x"], first["y"])
    assert link.kinds("cancel")[-1]["goal_id"] == first["goal_id"]
    await t.跑((NAV2_STALL_S + 1) * (NAV2_RESENDS + 1))
    assert len(link.kinds("goal")) == 1 + NAV2_RESENDS, "最多重发两次"


async def test_W70_Nav2一直在给速度_不重发(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0, 0.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(250):                                    # 25 s 一直有速度
        link.now += 0.1
        link.cmd = (link.now, gid, 0.2, 0.0, 0.0)
        await t.一拍()
    assert len(link.kinds("goal")) == 1


async def test_W70_离终点不到半米_15秒没再靠近_停在这儿算到(tmp_path):
    """11-03 17:16 商场 P8:终点离地图障碍 0.26 m(Nav2 的致命带里),机器人离终点 0.3 m
    前后来回 2 分钟。"""
    from nexwar_agent.bridges.nav2_nav import NEAR_STALL_S
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(1.85, 4.0, 0.0))     # 离终点 0.35 m,朝向已对上
    gid = link.kinds("goal")[-1]["goal_id"]
    n = int((NEAR_STALL_S - 2) * 10)
    for i in range(n):                                      # Nav2 一直给前后来回的小速度
        link.now += 0.1
        link.cmd = (link.now, gid, 0.15 if i % 10 < 5 else -0.15, 0.0, 0.0)
        await t.一拍()
    assert NavStatus.SUCCEED not in t.status, "还不到时候"
    for i in range(60):
        link.now += 0.1
        link.cmd = (link.now, gid, 0.15 if i % 10 < 5 else -0.15, 0.0, 0.0)
        await t.一拍()
    assert NavStatus.SUCCEED in t.status
    assert link.kinds("cancel")[-1]["goal_id"] == gid


def _卡在台阶边(t, roll):
    """仿真狗:朝向跟台阶边差不到 25° 时腿在边上打滑,往前走不动;机身按 ``roll`` 歪着。"""
    from nexwar_contract.hal import ImuSample
    real = t.r.tick

    def tick(dt):
        x, y = t.r.x, t.r.y
        real(dt)
        if abs(math.remainder(t.r.yaw, 2 * math.pi)) < math.radians(25) and t.r.x > x:
            t.r.x, t.r.y = x, y

    async def imu():
        return ImuSample(stamp_ms=t.c(), roll=roll, pitch=0.06, shock_g=0.0, load=None,
                         valid=True)
    t.r.tick = tick
    t.r.imu = imu


async def test_W71_沿着台阶边打滑走不动_机身歪着_退一点转大角度再上_再交回Nav2(tmp_path):
    """11-03 商场 P2→P3 用户:「轮胎和阶梯角度不够大,轮胎一直卡在阶梯边缘出不来…
    要把角度调大才能出来」。"""
    from nexwar_agent.bridges.nav2_nav import EDGE_TURN_RAD
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    _卡在台阶边(t, roll=math.radians(-4.8))
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0, 0.0))
    first = link.kinds("goal")[-1]
    yaws, track = [], []
    for _ in range(250):
        link.now += 0.1
        gid = link.kinds("goal")[-1]["goal_id"]
        link.cmd = (link.now, gid, 0.4, 0.0, 0.0)
        await t.一拍()
        yaws.append(t.r.yaw)
        track.append((t.r.x, t.r.yaw))
    turned = next(i for i, (_, y) in enumerate(track) if y > EDGE_TURN_RAD * 0.8)
    backed = next(i for i, (x, _) in enumerate(track) if x < 1.4)
    assert turned < backed, "用户:「困着的时候应该先转角度 再后退」"
    assert link.kinds("cancel") and link.kinds("cancel")[0]["goal_id"] == first["goal_id"]
    assert max(yaws) > EDGE_TURN_RAD * 0.8, "左边低(roll < 0):往左转大角度"
    goals = link.kinds("goal")
    assert len(goals) >= 2 and (goals[-1]["x"], goals[-1]["y"]) == (9.0, 4.0), "脱完交回 Nav2"
    assert t.r.x > 2.0, "转过来就上去了"


async def test_W71_走不动但机身是平的_不做台阶脱困(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    _卡在台阶边(t, roll=0.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0, 0.0))
    for _ in range(100):
        link.now += 0.1
        link.cmd = (link.now, link.kinds("goal")[-1]["goal_id"], 0.4, 0.0, 0.0)
        await t.一拍()
    assert not link.kinds("cancel") and abs(t.r.yaw) < 0.05


async def test_W71_机器人自己四周都被判成挡_Nav2失败_说机器人被困_不说目标点被占(tmp_path):
    """11-03 17:28 商场 P2→P3:机身歪着,身边一圈都被判成凸起,Nav2「Starting point in lethal space」,
    代理却报「目标点被占」。"""
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    for k, box in {"前": (2.25, 3.5, 2.45, 4.5), "后": (0.55, 3.5, 0.75, 4.5),
                   "左": (1.0, 4.5, 2.0, 4.7), "右": (1.0, 3.3, 2.0, 3.5)}.items():
        t.world.dyn[k] = box
    await _看清(t)
    await t.nav.goto(Pose.from_xy_yaw(8.0, 4.0, 0.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    await t.跑(0.5)
    link.goal_states.append({"type": "goal_state", "goal_id": gid, "state": "aborted",
                             "reason": "no valid path"})
    await t.跑(1.0)
    assert NavStatus.FAILED in t.status and "被困" in t.nav.fail_reason, t.nav.fail_reason
    assert "目标点被占" not in t.nav.fail_reason and not t.nav.fail_final


async def test_W71_目标点贴着地图墙_交给Nav2之前挪到最近能站的点(tmp_path):
    """11-03 商场:站点查可达时把贴墙的点挪进来算(intercept_reach.snap),说「到得了」;机器人却把原点
    交给 Nav2,Nav2 判终点在致命带里规划失败。自己的规划早就会挪(工单 20),Nav2 这条路也挪。"""
    from nexwar_agent.bridges.planned_nav import GOAL_SNAP_M
    link = 假转接()
    t = _台(tmp_path, link, walls=[(0.1, 4.7, 11.9, 4.8)])
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.3, 0.3))               # 离墙面 0.4 m:站点照收
    g = link.kinds("goal")[-1]
    assert g["y"] < 4.3 and math.dist((g["x"], g["y"]), (6.0, 4.3)) <= GOAL_SNAP_M + 1e-6, g
    assert g["yaw"] == pytest.approx(0.3), "朝向不变"
    await t.nav.stop()
    await t.跑(1.0)
    await t.跑(1.0)
    await t.nav.goto(Pose.from_xy_yaw(6.0, 3.0, 0.0))            # 空地上的点原样
    g = link.kinds("goal")[-1]
    assert (g["x"], g["y"]) == (6.0, 3.0)


async def test_W72_离终点还远_原地打转30秒没进展_报被挡_走起来了报恢复(tmp_path):
    """用户 11-03:导航卡住几十秒要给站点告警(站点一分钟报 P2,恢复了自动解除);11-03 绕人贴着沙发,
    Nav2 自己原地转了 70 秒,守卫没拦,站点什么都不知道。"""
    from nexwar_agent.bridges.nav2_nav import NAV2_NO_PROGRESS_S
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0, 0.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    blocked = lambda: [d for k, d in t.events if k == "nav_blocked"]   # noqa: E731
    for _ in range(int((NAV2_NO_PROGRESS_S - 3) * 10)):    # Nav2 一直给原地转的速度
        link.now += 0.1
        link.cmd = (link.now, gid, 0.0, 0.0, 0.6)
        await t.一拍()
    assert blocked() == [], "还不到时候"
    for _ in range(50):
        link.now += 0.1
        link.cmd = (link.now, gid, 0.0, 0.0, 0.6)
        await t.一拍()
    [b] = blocked()
    assert "没向目标靠近" in b["reason"] and b["waited_s"] >= NAV2_NO_PROGRESS_S
    for _ in range(40):                                     # 转回来、往前走起来了
        link.now += 0.1
        wz = -0.3 if t.r.yaw > 0.05 else (0.3 if t.r.yaw < -0.05 else 0.0)
        link.cmd = (link.now, gid, 0.5, 0.0, wz)
        await t.一拍()
    assert ("nav_unblocked" in [k for k, _ in t.events]), "有进展了:报恢复,站点那条自动解除"
    assert len(blocked()) == 1


async def test_W72_一直在往前走_绕远路也不算卡住(tmp_path):
    from nexwar_agent.bridges.nav2_nav import NAV2_NO_PROGRESS_S
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(1.5, 1.0, 0.0))     # 目标在身后
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(int((NAV2_NO_PROGRESS_S + 10) * 10)):   # 往前(背离目标)绕远路一直在走
        link.now += 0.1
        link.cmd = (link.now, gid, 0.3, 0.0, 0.0)
        await t.一拍()
        if t.r.x > 10.5:
            break
    assert [d for k, d in t.events if k == "nav_blocked"] == []


async def test_W72_换了新目标_进展从头算_不拿上一个目标的时刻误报(tmp_path):
    from nexwar_agent.bridges.nav2_nav import NAV2_NO_PROGRESS_S
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(5.0, 4.0, 0.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(int((NAV2_NO_PROGRESS_S - 5) * 10)):     # 原地转了 25 秒(还没报)
        link.now += 0.1
        link.cmd = (link.now, gid, 0.0, 0.0, 0.6)
        await t.一拍()
    await t.nav.stop()
    await t.跑(1.0)
    await t.nav.goto(Pose.from_xy_yaw(1.5, 9.0, 0.0))       # 换成更远的目标
    gid = link.kinds("goal")[-1]["goal_id"]
    for _ in range(80):                                     # 新目标上转了 8 秒
        link.now += 0.1
        link.cmd = (link.now, gid, 0.0, 0.0, 0.6)
        await t.一拍()
    assert [d for k, d in t.events if k == "nav_blocked"] == []


def _残影(tmp_path, x, y):
    """在 floor.pgm 上 (x, y) 点一格挡(扫图时的人、摊位留下的残影;现场其实是空的)。"""
    import numpy as np
    from obs_world import RES
    raw = (tmp_path / "floor.pgm").read_bytes()
    w, h = map(int, raw.split(b"\n")[1].split())
    body = raw[len(b"\n".join(raw.split(b"\n")[:3])) + 1:]
    img = np.frombuffer(body, dtype=np.uint8).reshape(h, w).copy()
    img[h - 1 - int(y / RES), int(x / RES)] = 0
    (tmp_path / "floor.pgm").write_bytes(b"P5\n%d %d\n255\n" % (w, h) + img.tobytes())


async def test_现场1105_站在地图残影上_Nav2说起点致命_机身底下临时当空_说清楚再试(tmp_path):
    """11-05 商场原点、广场窄口:机器人站的那格在静态图上是挡(残影),Nav2「Starting point in lethal
    space」55 次,倒车脱困也被挡;原来报「身边一圈都被判成障碍」。机器人站着的地方一定是空的。"""
    link = 假转接()
    t = _台(tmp_path, link)
    _残影(tmp_path, 1.5, 4.0)
    t.nav.load_grid(tmp_path)
    await t.start(1.5, 4.0)
    await _看清(t)
    await t.nav.goto(Pose.from_xy_yaw(8.0, 4.0, 0.0))
    gid = link.kinds("goal")[-1]["goal_id"]
    await t.跑(0.5)
    link.goal_states.append({"type": "goal_state", "goal_id": gid, "state": "aborted",
                             "reason": "GridBased: failed to create plan, invalid use: "
                                       "Starting point in lethal space!"})
    await t.跑(1.0)
    why = t.nav.fail_reason
    assert NavStatus.FAILED in t.status and "残影" in why and "清理" in why, why
    assert "一圈" not in why and not t.nav.fail_final, "临时擦了,引擎再试一次"
    m = link.kinds("map")[-1]
    [c] = m["clear"]
    assert math.dist(c[:2], (1.5, 4.0)) < 0.2 and len(c) == 5, c


async def test_现场1105_站在禁行区边上_Nav2说起点致命_说先挪出来_不擦禁行区(tmp_path):
    from nexwar_contract.zones import Zone, ZoneSet
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.set_zones(ZoneSet(map_id="m", map_version="1", revision=1, zones=(
        Zone(id="z", kind="nogo", label="栏杆", polygon=((1.3, 3.8), (3.0, 3.8), (3.0, 3.0),
                                                        (1.3, 3.0))),)))
    await _看清(t)
    t.nav._target = Pose.from_xy_yaw(8.0, 4.0, 0.0)
    t.r.teleport(1.5, 3.7, 0.0)                       # 机身压着禁行区边
    await t.跑(0.2)
    await t.nav._nav2_failed({"state": "aborted",
                              "reason": "Starting point in lethal space!"}, await t.nav._here())
    why = t.nav.fail_reason
    assert "禁行区" in why and "挪" in why and "残影" not in why, why
    assert not link.kinds("map")[-1].get("clear"), "禁行区不擦"


# ------------------------------------------------------------ W74 跟随:走着换目标


async def test_W74_走着换目标_Nav2收到新目标号_旧的取消_不在走就不换(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    assert await t.nav.retarget(Pose.from_xy_yaw(5.0, 4.0)) is False, "没在走:不换(要 goto)"
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0))
    await t.跑(0.3)
    g1 = link.kinds("goal")[-1]
    assert await t.nav.retarget(Pose.from_xy_yaw(7.0, 5.0, 0.5)) is True
    g2 = link.kinds("goal")[-1]
    assert g2["goal_id"] > g1["goal_id"] and (g2["x"], g2["y"], g2["yaw"]) == (7.0, 5.0, 0.5)
    assert "via" not in g2, "跟人不走路网"
    assert link.kinds("cancel")[-1]["goal_id"] == g1["goal_id"]
    assert t.nav._target.position.x == 7.0
    assert NavStatus.FAILED not in t.status


async def test_W74_换的新目标在禁行区里_不换(tmp_path):
    from nexwar_contract.zones import ZoneSet
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.set_zones(ZoneSet.from_wire({"map_id": "m", "map_version": "1", "revision": 1,
                                             "zones": [
        {"id": "z", "kind": "nogo", "polygon": [[8, 3], [9, 3], [9, 5], [8, 5]]}]}))
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0))
    await t.跑(0.3)
    n = len(link.kinds("goal"))
    assert await t.nav.retarget(Pose.from_xy_yaw(8.5, 4.0)) is False
    assert len(link.kinds("goal")) == n


async def test_W74_自己的规划_走着换目标_从这儿重新规划过去(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    assert await t.nav.retarget(Pose.from_xy_yaw(5.0, 4.0)) is False
    await t.nav.goto(Pose.from_xy_yaw(6.0, 4.0, 0.0))
    await t.跑(1.0)
    assert await t.nav.retarget(Pose.from_xy_yaw(4.0, 6.0, 1.5)) is True
    await t.跑(40.0)
    assert NavStatus.SUCCEED in t.status and not t.crashed
    assert math.dist((t.r.x, t.r.y), (4.0, 6.0)) < 0.4, "到的是新目标"


# ---------------------------------------- 导航 V1 第 1 节:D 守卫(SAFE / SLOW / STOP)


def _按余量答(full_hits, soft_ok_at):
    """假守卫:按全余量查 → 给 full_hits;
    按软余量(多留的比全余量少)查、速度不超过 soft_ok_at → 放行。"""
    from nexwar_agent.obstacles import Verdict
    calls = []

    def guard(vx, wz, g=None):
        soft = g is not None and g.extra < 0.3 - 1e-9
        calls.append((round(vx, 3), round(wz, 3), "soft" if soft else "full"))
        if soft and abs(vx) <= soft_ok_at + 1e-9:
            return Verdict(True)
        return Verdict(False, "扫过区有挡", hits=full_hits)
    return guard, calls


async def test_V1_D守卫_挡的全是地图墙_降速少留余量走过去_不硬停(tmp_path):
    """导航 V1 设计第 1 节(仿真 runs/nav-sweep-ab-1107 的 D 组):
    全余量查到的挡全在地图墙边上(墙不会动)
    → SLOW:余量跟速度挂钩、前进和转向同比例压到 0.3 再查,过得去就按 0.3 走,不停、不报被挡。"""
    link = 假转接()
    walls = [(0.1, 4.6, 11.9, 4.7)]                          # 机身左边 0.6 m 有一道地图墙
    t = _台(tmp_path, link, walls=walls)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    guard, calls = _按余量答(full_hits=((1.0, 0.62),), soft_ok_at=0.3)   # 挡在墙上
    t.nav._guard_check = guard
    for _ in range(12):                                     # 再久机身转偏了,假挡点就离开墙了
        link.now += 0.1
        link.cmd = (link.now, 1, 0.6, 0.0, 0.3)
        await t.一拍()
    assert ("soft" in {c[2] for c in calls}), calls[-4:]
    assert 0.2 < t.r.speed[0] <= 0.3 + 1e-6, t.r.speed
    assert t.r.speed[1] == pytest.approx(0.3 * 0.3 / 0.6, abs=0.02), "转向同比例压,走的还是那条弯"
    assert "nav_blocked" not in [k for k, _ in t.events]


async def test_V1_D守卫_挡里有地图之外的东西_按全余量停(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link, walls=[(0.1, 4.6, 11.9, 4.7)])
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    # 一个在墙上、一个在路中间
    guard, calls = _按余量答(full_hits=((1.0, 0.62), (1.0, 0.0)), soft_ok_at=0.6)
    t.nav._guard_check = guard
    for _ in range(10):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.6, 0.0, 0.0)
        await t.一拍()
    assert abs(t.r.speed[0]) < 0.05, "人、箱子:停"
    assert "soft" not in {c[2] for c in calls}, "有地图之外的挡就不试软余量"


async def test_V1_D守卫_降到蠕行还过不去就停(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link, walls=[(0.1, 4.6, 11.9, 4.7)])
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    guard, calls = _按余量答(full_hits=((1.0, 0.62),), soft_ok_at=0.1)   # 0.2 也过不去
    t.nav._guard_check = guard
    for _ in range(10):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.6, 0.0, 0.0)
        await t.一拍()
    assert abs(t.r.speed[0]) < 0.05
    tried = {c[0] for c in calls if c[2] == "soft"}
    assert {0.3, 0.2} <= tried, ("一档档压到蠕行都试过", tried)


def test_V1_D守卫_软余量跟速度挂钩():
    """蠕行 0.2 m/s 多留 0.05 m、0.6 m/s 以上多留到全余量 0.3 m;
    原地转 0.15 rad/s 多留 0.05 rad、0.6 以上 0.2。"""
    from nexwar_agent.bridges.nav2_nav import soft_guard
    from nexwar_agent.obstacles import ObstacleGuard
    full = ObstacleGuard()
    assert soft_guard(full, 0.2, 0.0).extra == pytest.approx(0.05)
    assert soft_guard(full, 0.4, 0.0).extra == pytest.approx(0.175)
    assert soft_guard(full, 0.9, 0.0).extra == pytest.approx(full.extra)
    assert soft_guard(full, 0.0, 0.15).extra_rad == pytest.approx(0.05)
    assert soft_guard(full, 0.0, 1.0).extra_rad == pytest.approx(full.extra_rad)
    g = soft_guard(full, 0.2, 0.0)
    assert (g.body_len, g.body_wid, g.margin, g.latency, g.decel) == \
        (full.body_len, full.body_wid, full.margin, full.latency, full.decel), "只改多留的,别的照旧"


async def test_V1_看不见的只降速时_转向同比例压(tmp_path):
    from nexwar_agent.bridges.nav2_nav import UNKNOWN_SLOW_MPS
    from nexwar_agent.obstacles import Verdict
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))

    def guard(vx, wz, g=None):
        if abs(vx) > UNKNOWN_SLOW_MPS + 1e-9:
            return Verdict(False, "看不见 3 处", hits=(), unknown=3)
        return Verdict(True)
    t.nav._guard_check = guard
    for _ in range(15):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.6, 0.0, 0.4)
        await t.一拍()
    assert t.r.speed[0] == pytest.approx(UNKNOWN_SLOW_MPS, abs=0.02)
    assert t.r.speed[1] == pytest.approx(0.4 * UNKNOWN_SLOW_MPS / 0.6, abs=0.02)


# ---------------------------------------- 导航 V1 第 1 节:遮挡限速(看不见的区域主动限速)


async def _遮挡(t, n=25):
    # 车后面从雷达看过去是看不见的;等 2.5 s,打开阴影之前那几帧(全看得见)过了记忆
    t.per.shadow = True
    for _ in range(n):
        await t.一拍()
    return t.nav._occ_cap(await t.nav._here(), 1)


async def test_V1_遮挡限速_前方路边停着车_车后面的阴影可能冒出人_限速(tmp_path):
    """仿真 ab_parking_delay300(D 组 s8):人从两车之间冲出,0.42 m/s 撞上。车后面那片看不见的、
    跟机身走廊之间没有挡:按「反应 + 刹车」限速。"""
    from nexwar_agent.bridges.nav2_nav import V_FAST
    t = _台(tmp_path, 假转接())
    await t.start(1.5, 4.0)
    t.world.dyn["car"] = (2.6, 4.45, 3.2, 5.4)              # 机头前 0.6 m 起、走廊左边 0.1 m 外
    v = await _遮挡(t)
    # 人最早从车的远端(机头前约 1.2 m)冒出来:反应 0.5 s + 0.5 m/s² 刹车,停在它前面 0.1 m → 约 0.84
    assert 0.75 < v < 0.9 < V_FAST, v
    t.world.dyn["car"] = (2.1, 4.45, 2.5, 5.4)              # 车挨着机头:冒出点 0.5 m 内
    assert await _遮挡(t) == pytest.approx(0.2 + 0.31, abs=0.12)


async def test_V1_遮挡限速_空地不限(tmp_path):
    from nexwar_agent.bridges.nav2_nav import V_FAST
    t = _台(tmp_path, 假转接())
    await t.start(1.5, 4.0)
    assert await _遮挡(t) == pytest.approx(V_FAST)


async def test_V1_遮挡限速_走廊两边是地图墙_墙外的阴影不算(tmp_path):
    from nexwar_agent.bridges.nav2_nav import V_FAST
    t = _台(tmp_path, 假转接(), walls=[(0.1, 3.2, 11.9, 3.3), (0.1, 4.7, 11.9, 4.8)])
    await t.start(1.5, 4.0)
    assert await _遮挡(t) == pytest.approx(V_FAST)


async def test_V1_遮挡限速_E1按距离分档_关掉不限(tmp_path, monkeypatch):
    from nexwar_agent.bridges import nav2_nav
    t = _台(tmp_path, 假转接())
    await t.start(1.5, 4.0)
    t.world.dyn["car"] = (2.6, 4.45, 3.2, 5.4)
    monkeypatch.setattr(nav2_nav, "OCC_ALGO", "E1")
    assert await _遮挡(t) == pytest.approx(0.4)             # 阴影离机身 1–2 m
    monkeypatch.setattr(nav2_nav, "OCC_ALGO", "")
    assert t.nav._occ_cap(await t.nav._here(), 1) == pytest.approx(nav2_nav.V_FAST)


async def test_V1_遮挡限速_算进看得清就快_也送给Nav2(tmp_path):
    link = 假转接()
    t = _台(tmp_path, link)
    await t.start(1.5, 4.0)
    t.world.dyn["car"] = (2.6, 4.45, 3.2, 5.4)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    for _ in range(10):
        link.now += 0.1
        link.cmd = (link.now, 1, 0.9, 0.0, 0.0)
        await t.一拍()
    occ = t.nav._occ_cap(await t.nav._here(), 1)
    assert t.nav._vis <= occ + 1e-6, (t.nav._vis, occ)


async def test_V1_遮挡限速_锥形视野_机头两侧刚刚看见过的不算_不一直压蠕行(tmp_path):
    """C40221 实测视野是锥形(机头两侧 1 m 内此刻看不见):往前走时那片刚刚看见过,不当阴影。"""
    from nexwar_agent.bridges.nav2_nav import V_FAST
    link = 假转接()
    t = _台(tmp_path, link, cone=True)
    await t.start(1.5, 4.0)
    first = t.nav._occ_cap(await t.nav._here(), 1)
    await t.nav.goto(Pose.from_xy_yaw(9.0, 4.0))
    for _ in range(40):                                     # 往前走 2 m 左右
        link.now += 0.1
        link.cmd = (link.now, 1, 0.5, 0.0, 0.0)
        await t.一拍()
    later = t.nav._occ_cap(await t.nav._here(), 1)
    assert later > 0.8 * V_FAST, (first, later)


async def test_V1_遮挡限速_车停在侧面1米外_车后的阴影也要限速(tmp_path, monkeypatch):
    """仿真 DE2 ab_parking_delay300 s33:人从横向 1.46 m 外的车后冒出,E2 只看 1.0 m 以内,
    遮挡限速一次没起作用,撞上。看到 2.0 m 以后(DE3)200 趟 0 碰撞。
    车摆在机头前 1.1–1.5 m、走廊边外 1.0–1.3 m:车后的阴影在视野锥里、离走廊边 1.0 m 以外。"""
    from nexwar_agent.bridges import nav2_nav
    t = _台(tmp_path, 假转接())
    await t.start(1.5, 4.0)
    t.world.dyn["car"] = (3.1, 5.29, 3.5, 5.6)
    v = await _遮挡(t)
    assert v < nav2_nav.V_FAST - 0.01, v
    monkeypatch.setattr(nav2_nav, "OCC_E2_SIDE_M", 1.0)     # 老的 1.0 m:看不到,不限
    assert t.nav._occ_cap(await t.nav._here(), 1) == pytest.approx(nav2_nav.V_FAST)
