"""``LocalNavBackend`` 自己的行为 —— 契约测试之外的那一半。

跨后端的共同约定在 ``tests/contract``,这里只测这条路线独有的东西,
其中最要紧的一条是**死区**:2026-09-01 现场机器"站起来但不走"、指令
一条错都不报(清单 #37)。整个脉冲式控制器就是为绕开它才长成这样,
所以必须有一条测试能证明"绕开"这件事真的发生了。
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from d1max_patrol.backends.base import NavRequestError, NavStatusEvent
from d1max_patrol.backends.local_nav import (
    LocalNavBackend,
    LocalNavParams,
    wrap_angle,
)
from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
from d1max_patrol.protocol.agent_frames import MotionStatus
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose
from d1max_sim.agent_server import WALK_DEADBAND, SimAgentServer
from d1max_sim.pose_server import SimPoseServer

MAP_ID = "测试图"

#: 契约测试里那套只影响耗时、不影响物理的参数,这里复用。
FAST = {"settle_s": 0.02, "pulse_s": 1.5}


def write_map(directory: Path, map_id: str = MAP_ID, *, width: int = 20,
              height: int = 20, dark_top_row: bool = False) -> None:
    """写一张 map_saver_cli 形状的图。

    ``dark_top_row`` 把 pgm 的**第一行**涂黑 —— 用来验行序有没有被翻正。
    """
    directory.mkdir(parents=True, exist_ok=True)
    pixels = [254] * (width * height)
    if dark_top_row:
        pixels[:width] = [0] * width
    (directory / f"{map_id}.pgm").write_bytes(
        f"P5\n{width} {height}\n255\n".encode("ascii")
        + struct.pack(f"{width * height}B", *pixels))
    (directory / f"{map_id}.yaml").write_text(
        f"image: {map_id}.pgm\n"
        f"mode: trinary\n"
        f"resolution: 0.05\n"
        f"origin: [-0.5, -0.5, 0]\n"
        f"negate: 0\n"
        f"occupied_thresh: 0.65\n"
        f"free_thresh: 0.25\n",
        encoding="utf-8")


@contextlib.asynccontextmanager
async def rig(tmp_path: Path, *, params: LocalNavParams | None = None,
              with_map: bool = True) -> AsyncIterator[
                  tuple[SimAgentServer, SimPoseServer, LocalNavBackend]]:
    """仿真旁路进程 + 仿真定位桥 + 被测后端,已站立、已加载地图。"""
    agent = SimAgentServer(port=0)
    await agent.start()
    agent.motion = MotionStatus.GENERAL      # 省掉 0.6 秒的站起(#36)
    device = SidecarDeviceBackend("127.0.0.1", agent.port, ack_timeout_s=5.0)
    pose = SimPoseServer(agent, port=0, hz=200.0)
    await pose.start()
    if with_map and not (tmp_path / f"{MAP_ID}.yaml").exists():
        # 测试自己先摆过图(比如要一张涂黑了某行的),就别再覆盖回默认的。
        write_map(tmp_path)
    backend = LocalNavBackend(
        device, maps_dir=tmp_path, pose_port=pose.port,
        params=params or LocalNavParams(**FAST))
    try:
        await device.connect()
        await device.acquire_control()
        await backend.connect()
        await _until(lambda: backend._loc is LocStatus.CONTINUOUS_LOC)
        if with_map:
            await backend.load_map(MAP_ID)
        yield agent, pose, backend
    finally:
        await backend.close()
        await device.close()
        await pose.stop()
        await agent.stop()


async def _until(predicate, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等条件成立超时")


async def _run(backend: LocalNavBackend, pose: Pose,
               timeout_s: float = 20.0) -> NavStatus:
    with backend.subscription() as q:
        await backend.goto(pose)
        return await backend.wait_nav_terminal(timeout_s, queue=q)


# --------------------------------------------------------------- 死区


#: 死区之下和之上各取一个控制量。两条测试只差这一个数。
BELOW_DEADBAND = 0.15
ABOVE_DEADBAND = 0.30


def _floor_params(floor: float) -> LocalNavParams:
    """把控制量钉死在 ``floor``。

    ``fwd_gain=0`` 让比例项恒为 0,于是 ``_saturate`` 每次都取下限 ——
    这样两条测试之间**只有下限这一个变量**,别的一模一样。
    """
    return LocalNavParams(fwd_gain=0.0, min_fwd=floor, max_fwd=0.5, **FAST)


async def test_控制量低于死区时走不动并且判失败(tmp_path):
    """这条是整条自建路线的承重测试。

    现场教训(#37):``Move`` 的量是百分比且有死区,给小了机器一动不动、
    一个字的错都不报。控制器如果按常规做法"误差小就把控制量降下去",
    就会掉进这个静默失败里 —— 它会一直以为自己在走。

    这里把下限压到仿真器死区(``WALK_DEADBAND``)之下,断言两件事:
    机器**确实没动**,而且后端**确实发现了**(判 Failed,不是傻等到超时)。
    """
    assert BELOW_DEADBAND < WALK_DEADBAND, "这条测试的前提是它落在死区里"
    async with rig(tmp_path, params=_floor_params(BELOW_DEADBAND)) as (
            agent, _pose, backend):
        status = await _run(backend, Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert status is NavStatus.FAILED
    assert agent.distance == pytest.approx(0.0), "死区之下不该产生任何位移"


async def test_控制量高于死区就真的走到了(tmp_path):
    """上一条的镜像:只把下限抬到死区之上,别的一个字不改,就走到了。

    两条合起来才是证明 —— 只有失败那条,说不定是控制器本身写坏了。
    """
    assert ABOVE_DEADBAND > WALK_DEADBAND
    async with rig(tmp_path, params=_floor_params(ABOVE_DEADBAND)) as (
            agent, _pose, backend):
        status = await _run(backend, Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert status is NavStatus.SUCCEED
    assert agent.distance > 0.5


async def test_做不到的容差会被参数校验挡住(tmp_path):
    """容差比"死区之下走不动、最短一拍又有下限"所允许的最小一步还小,
    这组参数在物理上就收不了敛。与其现场表现为"永远到不了点",不如构造时炸。
    """
    with pytest.raises(ValueError, match="死区"):
        LocalNavParams(xy_tol=0.01)
    with pytest.raises(ValueError, match="死区"):
        LocalNavParams(yaw_tol=0.01)
    with pytest.raises(ValueError, match="下限不能大于上限"):
        LocalNavParams(min_fwd=0.9, max_fwd=0.5)


# --------------------------------------------------------------- 定位


async def test_导航中定位丢了立刻判失败(tmp_path):
    """定位丢了还接着走就是闭着眼睛开车。"""
    async with rig(tmp_path) as (_agent, pose, backend):
        with backend.subscription() as q:
            await backend.goto(Pose.from_xy_yaw(3.0, 0.0, 0.0))
            await asyncio.sleep(0.05)
            pose.drop_localization()
            status = await backend.wait_nav_terminal(20.0, queue=q)
    assert status is NavStatus.FAILED


async def test_定位桥不发了也会判丢(tmp_path):
    """``drop_localization`` 是"定位器活着但查不到 TF",``freeze`` 是
    "定位器进程没了"。后者没有任何消息可收,只能靠新鲜度超时发现。
    """
    params = LocalNavParams(loc_lost_after_s=0.15, **FAST)
    async with rig(tmp_path, params=params) as (_agent, pose, backend):
        assert await backend.loc_status() is LocStatus.CONTINUOUS_LOC
        pose.freeze()
        await _until(lambda: backend._loc is LocStatus.LOC_LOST)
        assert await backend.loc_status() is LocStatus.LOC_LOST


async def test_跟的是定位桥而不是腿式里程(tmp_path):
    """真理源规则(规范 §3.4)在这条路线上的具体形状。

    把定位往 +x 推 2 米,机器实际还在原点。让它去地图系的 (0, 0):
    它相信自己在 (2, 0),于是会往回走 2 米 —— 里程上机器跑到了 -2 附近。
    要是控制器偷偷信了 odom,它会觉得自己已经到了,一步都不动。
    """
    async with rig(tmp_path) as (agent, pose, backend):
        pose.jump(2.0, 0.0)
        await asyncio.sleep(0.05)
        status = await _run(backend, Pose.from_xy_yaw(0.0, 0.0, 0.0))
    assert status is NavStatus.SUCCEED
    assert agent.x < -1.5, f"应当往回走约 2 米,实际 x={agent.x}"


# --------------------------------------------------------------- 控制循环


async def test_先转到朝向再前进(tmp_path):
    """目标在正后方时,第一拍必须是纯转向 —— 不能带着 0.5 的前进量拐弯。"""
    async with rig(tmp_path) as (agent, _pose, backend):
        await _run(backend, Pose.from_xy_yaw(-1.0, 0.0, 3.14159))
        walks = [args for cmd, args in agent.commands if cmd == "walk"]
    assert walks, "一拍都没发?"
    assert walks[0]["fwd"] == pytest.approx(0.0)
    assert abs(walks[0]["yaw"]) >= 0.3


async def test_每一拍的控制量都在死区之上(tmp_path):
    """精细逼近靠缩短脉冲,不靠降低控制量 —— 这是设计的核心断言。

    走一段很短的距离,末段的误差已经很小了;如果实现里有任何一处"误差小
    就把量降下去",这里就会出现一个落在死区里的控制量。
    """
    async with rig(tmp_path) as (agent, _pose, backend):
        await _run(backend, Pose.from_xy_yaw(0.6, 0.0, 0.8))
        walks = [args for cmd, args in agent.commands if cmd == "walk"]
    assert walks
    for args in walks:
        biggest = max(abs(args["fwd"]), abs(args["lat"]), abs(args["yaw"]))
        assert biggest >= 0.3 - 1e-9, f"这一拍落进死区了: {args}"


async def test_越接近目标脉冲越短(tmp_path):
    """控制量顶着上限不动,所以"走得少"只能来自更短的时长。

    1.4 米是挑出来的:第一拍顶满(1.5 s × 0.6 m/s = 0.9 m),剩下的 0.5 米
    比一整拍能走的少,于是第二拍只能靠缩短时长。距离取整数反而看不出来 ——
    每一拍都顶满上限,末尾那点零头被容差吃掉了。
    """
    async with rig(tmp_path) as (agent, _pose, backend):
        await _run(backend, Pose.from_xy_yaw(1.4, 0.0, 0.0))
        seconds = [args["seconds"] for cmd, args in agent.commands
                   if cmd == "walk" and args["fwd"] > 0]
    assert len(seconds) >= 2
    assert seconds[-1] < seconds[0]


async def test_原地不动会判卡住而不是等到超时(tmp_path):
    """``goal_timeout_s`` 设得很大,如果没有卡住检测,这条会跑满 60 秒。"""
    params = LocalNavParams(fwd_gain=0.0, min_fwd=BELOW_DEADBAND,
                            goal_timeout_s=60.0, stuck_pulses=3, **FAST)
    async with rig(tmp_path, params=params) as (_agent, _pose, backend):
        loop = asyncio.get_running_loop()
        started = loop.time()
        status = await _run(backend, Pose.from_xy_yaw(1.0, 0.0, 0.0))
        elapsed = loop.time() - started
    assert status is NavStatus.FAILED
    assert elapsed < 10.0, f"卡住检测没生效,跑了 {elapsed:.1f}s"


async def test_终态之后状态回落待机(tmp_path):
    """终态是一条事件,不是一个停留的状态 —— 和厂商设备的行为对齐。"""
    async with rig(tmp_path) as (_agent, _pose, backend):
        with backend.subscription() as q:
            await backend.goto(Pose.from_xy_yaw(0.5, 0.0, 0.0))
            assert await backend.wait_nav_terminal(20.0, queue=q) is \
                NavStatus.SUCCEED
            rest = []
            while not q.empty():
                rest.append(q.get_nowait())
        assert any(isinstance(e, NavStatusEvent)
                   and e.status is NavStatus.STANDBY for e in rest)
        assert await backend.nav_status() is NavStatus.STANDBY


async def test_上一次还在跑时再下一个点会被拒(tmp_path):
    async with rig(tmp_path) as (_agent, _pose, backend):
        await backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
        with pytest.raises(NavRequestError):
            await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
        await backend.stop()


async def test_停止之后机器不再收到新的脉冲(tmp_path):
    """``stop()`` 会等当前这一拍走完 —— 脉冲已经在旁路进程手里了,取消
    Python 侧的等待并不能让机器停下来。它保证的是"不再有下一拍"。
    """
    async with rig(tmp_path) as (agent, _pose, backend):
        await backend.goto(Pose.from_xy_yaw(9.0, 0.0, 0.0))
        await asyncio.sleep(0.1)
        await backend.stop()
        sent = len([1 for cmd, _ in agent.commands if cmd == "walk"])
        await asyncio.sleep(0.5)
        assert len([1 for cmd, _ in agent.commands if cmd == "walk"]) == sent
    assert True


# --------------------------------------------------------------- 地图文件


async def test_只有yaml没有pgm的半成品图不列出来(tmp_path):
    """存图中途被打断就会留下这种。列出来只会让上层在 load_map 时才炸。"""
    write_map(tmp_path)
    (tmp_path / "半成品.yaml").write_text("resolution: 0.05\n", encoding="utf-8")
    async with rig(tmp_path) as (_agent, _pose, backend):
        assert await backend.list_maps() == [MAP_ID]


async def test_栅格图的行序被翻正(tmp_path):
    """pgm 从上往下存,OccupancyGrid 从原点(左下)往上存。

    不翻的话地图会上下颠倒,而颠倒的图看着仍然"像一张图" —— 这种错不会
    报任何异常,只会让所有点位偏到别处去。
    """
    write_map(tmp_path, dark_top_row=True)
    async with rig(tmp_path) as (_agent, _pose, backend):
        grid = await backend.get_map_grid(MAP_ID)
    width = grid["info"]["width"]
    height = grid["info"]["height"]
    assert len(grid["data"]) == width * height
    assert grid["data"][:width] == [0] * width, "第一行应是空闲(它原本在图的底部)"
    assert grid["data"][-width:] == [100] * width, "涂黑那行应落到最后一行"


async def test_像素数量对不上会报错(tmp_path):
    """截断的 pgm 必须当场炸,而不是解出一张缺了一角的图。"""
    write_map(tmp_path)
    pgm = tmp_path / f"{MAP_ID}.pgm"
    pgm.write_bytes(pgm.read_bytes()[:-50])
    async with rig(tmp_path) as (_agent, _pose, backend):
        with pytest.raises(NavRequestError, match="像素不足"):
            await backend.get_map_grid(MAP_ID)


async def test_不是P5的图会被拒(tmp_path):
    write_map(tmp_path)
    (tmp_path / f"{MAP_ID}.pgm").write_bytes(b"P2\n2 2\n255\n0 0 0 0\n")
    async with rig(tmp_path) as (_agent, _pose, backend):
        with pytest.raises(NavRequestError, match="P5"):
            await backend.get_map_grid(MAP_ID)


async def test_加载不存在的地图会被拒(tmp_path):
    async with rig(tmp_path) as (_agent, _pose, backend):
        with pytest.raises(NavRequestError):
            await backend.load_map("根本没有这张图")
        assert backend.current_map == MAP_ID, "被拒之后当前地图不该变"


# --------------------------------------------------------------- 小工具


def test_角度归一():
    assert wrap_angle(0.0) == pytest.approx(0.0)
    assert wrap_angle(3.14159 * 3) == pytest.approx(3.14159, abs=1e-4)
    assert wrap_angle(-3.14159 * 3) == pytest.approx(-3.14159, abs=1e-4)
    assert wrap_angle(1.5) == pytest.approx(1.5)


# --------------------------------------------------------------- 限速(W05)


async def test_限速是上限_每一拍的前进量都不超过它(tmp_path):
    """W05。以前 ``set_speed`` 只记一个数,运动循环不读它:设成 0 照样 0.5 往前冲。
    现在它是**上限**:控制量仍由误差和死区决定,但封顶在这个数换算出来的量。"""
    async with rig(tmp_path) as (agent, _pose, backend):
        p = backend.params
        cap_cmd = 0.36                                   # 介于死区 0.30 和 max_fwd 0.50 之间
        got = await backend.set_speed(cap_cmd * p.fwd_speed_mps)
        assert got["x"] == pytest.approx(cap_cmd * p.fwd_speed_mps)
        await _run(backend, Pose.from_xy_yaw(1.5, 0.0, 0.0))
        walks = [args for cmd, args in agent.commands if cmd == "walk" and args["fwd"] > 0]
    assert walks
    assert max(a["fwd"] for a in walks) <= cap_cmd + 1e-9, [a["fwd"] for a in walks]
    assert min(a["fwd"] for a in walks) >= p.min_fwd - 1e-9


async def test_限速低于死区_拒绝而不是回显(tmp_path):
    """0 或者死区以下的速度这台机做不到(#37:0.11 几乎不动)。以前回显"生效了"
    然后照样 0.5 走,上层看到零速、底层在动 —— 这是 D4 那条缺陷。现在明确拒绝。"""
    async with rig(tmp_path) as (_agent, _pose, backend):
        before = await backend.get_speed()
        with pytest.raises(NavRequestError, match="死区"):
            await backend.set_speed(0.1)
        for bad in (0.0, -1.0):                 # 不倒退、不为零:要停用 stop
            with pytest.raises(NavRequestError, match="stop"):
                await backend.set_speed(bad)
        assert await backend.get_speed() == before


async def test_速度按基类契约用米每秒_超过上限就封顶并回报真实值(tmp_path):
    async with rig(tmp_path) as (_agent, _pose, backend):
        p = backend.params
        default = await backend.get_speed()
        assert default == pytest.approx({"x": p.max_fwd * p.fwd_speed_mps, "y": 0.0,
                                         "z": p.max_yaw * p.yaw_speed_rps})
        got = await backend.set_speed(5.0, 0.0, 9.0)
        assert got["x"] == pytest.approx(p.max_fwd * p.fwd_speed_mps)
        assert got["z"] == pytest.approx(p.max_yaw * p.yaw_speed_rps)
        assert got["y"] == 0.0
        with pytest.raises(NavRequestError, match="侧移"):
            await backend.set_speed(0.5, 0.3)


async def test_限速也管转向(tmp_path):
    async with rig(tmp_path) as (agent, _pose, backend):
        p = backend.params
        cap_cmd = 0.36
        await backend.set_speed(p.max_fwd * p.fwd_speed_mps, 0.0, cap_cmd * p.yaw_speed_rps)
        await _run(backend, Pose.from_xy_yaw(0.0, 0.0, 2.5))
        turns = [args for cmd, args in agent.commands if cmd == "walk" and args["yaw"] != 0]
    assert turns
    assert max(abs(a["yaw"]) for a in turns) <= cap_cmd + 1e-9
