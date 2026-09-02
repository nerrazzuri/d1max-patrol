"""契约测试的后端工厂。

这是整个 tests/contract 目录里唯一知道"跑的是哪个实现"的文件。
将来再加后端,只需在 BACKEND_FACTORIES 里加一行。

## 能力集(capabilities)

两条路线的能力天生不一样:厂商后端自己会建图,自建后端不会 —— 它的图是
ROS 侧 slam_toolbox 出的,``start_mapping`` 明确抛错。这不是"没实现",是
**这条路线上建图就不归它管**。

所以契约分成两截:

* 每个后端都必须过的**核心契约** —— 地图列举、路径、定位、导航、速度、事件。
* 只有声明了 ``mapping`` 能力的后端才跑的**建图契约**。

没声明能力的后端要过 ``test_不建图的后端必须明确拒绝建图`` —— 它必须**拒绝**
而不是假装成功。少了这一条,"不支持"就跟"悄悄什么也没干"分不出来了。
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from d1max_patrol.backends.base import NavBackend
from d1max_patrol.backends.local_nav import LocalNavBackend, LocalNavParams
from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.agent_frames import MotionStatus
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus
from d1max_sim.agent_server import SimAgentServer
from d1max_sim.nav_server import SimNavServer
from d1max_sim.pose_server import SimPoseServer

#: 自建后端在契约测试里用的图名。
LOCAL_MAP_ID = "契约图"


@contextlib.asynccontextmanager
async def _vendor_against_sim(workspace: Path) -> AsyncIterator[NavBackend]:
    """厂商后端 + 仿真设备。"""
    sim = SimNavServer(tick_hz=100.0)
    await sim.start()
    backend = VendorNavBackend(NavConfig(
        url=sim.url,
        request_timeout_s=3.0,
        status_poll_interval_s=0.05,
        reconnect_min_s=0.05,
        reconnect_max_s=0.2,
    ))
    try:
        await backend.connect()
        yield backend
    finally:
        await backend.close()
        await sim.stop()


@contextlib.asynccontextmanager
async def _local_against_sim(workspace: Path) -> AsyncIterator[NavBackend]:
    """自建后端 + 仿真旁路进程 + 仿真定位桥。

    参数只调两个,而且都只影响**耗时**、不影响物理:

    * ``settle_s`` 降到 0.02 —— 真机上要等定位追上来,仿真里定位桥被拉到
      200 Hz,没什么可等的。
    * ``pulse_s`` 抬到 1.5 —— 一拍走得多、拍数就少。脉冲时长本来就由
      ``误差/速度`` 算出来(见 ``_pulse_seconds``),抬上限不会过冲,
      只是让"离得远"的那几拍别被切得那么碎。

    死区相关的常数(``min_fwd`` / ``min_yaw``)一个都没动 —— 那才是这条
    路线的承重点,契约测试必须跑在真值上。
    """
    agent = SimAgentServer(port=0)
    await agent.start()
    # 直接摆成站着的,不走 stand() —— 站起在仿真里要 0.6 秒(#36),而契约
    # 测试有几十条,每条都白等一次。站起本身的行为归 test_sidecar_device 管,
    # 这里测的是 NavBackend 的约定。
    agent.motion = MotionStatus.GENERAL
    device = SidecarDeviceBackend("127.0.0.1", agent.port, ack_timeout_s=5.0)
    pose = SimPoseServer(agent, port=0, hz=200.0)
    await pose.start()

    backend = LocalNavBackend(
        device,
        maps_dir=workspace,
        pose_port=pose.port,
        params=LocalNavParams(settle_s=0.02, pulse_s=1.5),
    )
    try:
        await device.connect()
        await device.acquire_control()
        await backend.connect()
        yield backend
    finally:
        await backend.close()
        await device.close()
        await pose.stop()
        await agent.stop()


#: 后端名 -> 上下文管理器工厂。加新后端只改这里和 BACKEND_CAPS。
BACKEND_FACTORIES: dict[
    str, Callable[[Path], contextlib.AbstractAsyncContextManager]
] = {
    "vendor": _vendor_against_sim,
    "local": _local_against_sim,
}

#: 后端名 -> 它自己声明支持的可选能力。
#:
#: ``mapping``   设备自己能建图。
#: ``map_admin`` 设备自己能重命名/删除地图。自建路线的图是仓库里的文件,
#:               增删改交给 git,后端明确拒绝。
#: ``reloc``     设备自己能重置定位。自建路线要人在看板上点 /initialpose ——
#:               自动猜位置猜错了是真的撞上去。
BACKEND_CAPS: dict[str, frozenset[str]] = {
    "vendor": frozenset({"mapping", "map_admin", "reloc"}),
    "local": frozenset(),
}


# --------------------------------------------------------------- 就绪准备


async def _ready_vendor(backend: NavBackend, workspace: Path) -> str:
    """厂商设备自己建一张图。"""
    await backend.start_mapping()
    await _until(backend.mapping_status, MappingStatus.MAPPING_RUNNING)
    await backend.stop_mapping()
    await _until(backend.mapping_status, MappingStatus.MAPPING_SAVE_END)

    maps = await backend.list_maps()
    assert maps, "建图完成后应至少有一张地图"
    return maps[0]


async def _ready_local(backend: NavBackend, workspace: Path) -> str:
    """自建路线的图是**外面给的** —— 这里就地摆一张到工作目录里。

    对应真机上的现实:图由 ROS 侧 slam_toolbox 出、由 ``map_saver_cli``
    落盘,导航后端只负责读。所以"准备就绪"在这条路线上等于"文件到位"。
    """
    _write_map(workspace, LOCAL_MAP_ID, width=20, height=20, resolution=0.05,
               origin=(-0.5, -0.5))
    return LOCAL_MAP_ID


BACKEND_READY: dict[str, Callable] = {
    "vendor": _ready_vendor,
    "local": _ready_local,
}


def _write_map(directory: Path, map_id: str, *, width: int, height: int,
               resolution: float, origin: tuple[float, float]) -> None:
    """写一张 map_saver_cli 形状的空图(全白 = 全空闲)。"""
    directory.mkdir(parents=True, exist_ok=True)
    pgm = directory / f"{map_id}.pgm"
    header = f"P5\n{width} {height}\n255\n".encode("ascii")
    pgm.write_bytes(header + struct.pack(f"{width * height}B",
                                         *([254] * (width * height))))
    (directory / f"{map_id}.yaml").write_text(
        f"image: {map_id}.pgm\n"
        f"mode: trinary\n"
        f"resolution: {resolution}\n"
        f"origin: [{origin[0]}, {origin[1]}, 0]\n"
        f"negate: 0\n"
        f"occupied_thresh: 0.65\n"
        f"free_thresh: 0.25\n",
        encoding="utf-8")


# --------------------------------------------------------------- fixtures


@pytest.fixture(params=sorted(BACKEND_FACTORIES), ids=sorted(BACKEND_FACTORIES))
async def backend_name(request) -> str:
    return request.param


@pytest.fixture
async def nav_backend(backend_name, tmp_path) -> AsyncIterator[NavBackend]:
    async with BACKEND_FACTORIES[backend_name](tmp_path) as backend:
        yield backend


@pytest.fixture
def nav_caps(backend_name) -> frozenset[str]:
    return BACKEND_CAPS[backend_name]


@pytest.fixture
async def mapping_backend(nav_backend, nav_caps) -> NavBackend:
    """只在声明了 ``mapping`` 能力的后端上跑。"""
    if "mapping" not in nav_caps:
        pytest.skip("这个后端不建图 —— 见 test_不建图的后端必须明确拒绝建图")
    return nav_backend


@pytest.fixture
async def mapping_ready(ready_backend, nav_caps) -> tuple[NavBackend, str]:
    """建过图、且定位已就绪 —— 只在会建图的后端上跑。"""
    if "mapping" not in nav_caps:
        pytest.skip("这个后端不建图 —— 见 test_不建图的后端必须明确拒绝建图")
    return ready_backend


@pytest.fixture
async def admin_ready(ready_backend, nav_caps) -> tuple[NavBackend, str]:
    """有图、且能对地图增删改 —— 只在声明了 map_admin 的后端上跑。"""
    if "map_admin" not in nav_caps:
        pytest.skip("这个后端不管地图增删改 —— 见 test_不管地图增删改的后端必须明确拒绝")
    return ready_backend


@pytest.fixture
async def ready_backend(nav_backend, backend_name,
                        tmp_path) -> tuple[NavBackend, str]:
    """有图、且定位已就绪的后端。契约测试的常用起点。

    怎么变成"有图"是各后端自己的事(厂商自己建,自建路线读现成文件),
    ``load_map`` 之后要满足的条件是同一个:定位健康。
    """
    map_id = await BACKEND_READY[backend_name](nav_backend, tmp_path)
    await nav_backend.load_map(map_id)
    await _until(nav_backend.loc_status, LocStatus.CONTINUOUS_LOC)
    return nav_backend, map_id


async def _until(getter, wanted, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")
