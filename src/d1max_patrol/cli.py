"""d1max 命令行。

接上真机后的第一条命令应该是:
    d1max --url ws://192.168.144.100:10010 status
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator
from dataclasses import replace

from d1max_patrol.backends.base import NavBackendError, NavTimeoutError
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.loader import ConfigError, load_config
from d1max_patrol.protocol.nav_types import (
    LOC_HEALTHY,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)

log = logging.getLogger(__name__)

DEFAULT_NAV_TIMEOUT_S = 120.0
#: 等定位就绪的上限
LOC_READY_TIMEOUT_S = 60.0
#: 等建图子系统真正进入运行 / 保存完成的上限。
#: 见 tests/contract/conftest.py 的 ready_backend 与 test_nav_contract.py 的
#: _wait_status:这两处都印证了 start_mapping/stop_mapping 返回时只表示指令
#: 被接受,状态机迁移(InitWaitSensor -> MappingReady -> MappingRunning,
#: MappingSaveBegin -> MappingSaveEnd)还要再等一小段时间才真正完成。
MAPPING_TRANSITION_TIMEOUT_S = 15.0
#: 导航终态之后有个短驻留才回落 StandBy,期间再次 goto() 会被拒绝
#: (设备状态机本身的约束,不是竞态)。全逐点执行 walk 在两个航点之间
#: 必须等到这里,道理与 tests/contract/test_nav_contract.py 的
#: test_连续走多个点 完全一致。
NAV_STANDBY_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.02


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="d1max", description="D1 Max 巡检工具")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--url", help="覆盖导航 WebSocket 地址")
    parser.add_argument("--timeout", type=float, default=DEFAULT_NAV_TIMEOUT_S,
                        help="单点导航超时秒数(默认 %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="打印导航/定位/建图状态与速度")
    sub.add_parser("maps", help="列出地图")

    p = sub.add_parser("paths", help="列出某张地图上的路线")
    p.add_argument("map_id")

    p = sub.add_parser("path-save", help="写入一条路线(新增或整条覆盖)")
    p.add_argument("map_id")
    p.add_argument("path_id")
    p.add_argument("--point", action="append", default=[], metavar="名称:x:y[:yaw]",
                   help="航点,可重复。例: --point 配电柜:1.0:2.0:1.57")

    sub.add_parser("map-start", help="开始建图")
    sub.add_parser("map-stop", help="结束建图并保存")

    p = sub.add_parser("load", help="加载定位地图并等到定位就绪")
    p.add_argument("map_id")

    p = sub.add_parser("goto", help="走一个点")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("yaw", type=float, nargs="?", default=0.0)

    p = sub.add_parser("walk", help="按厂商路线全逐点走完")
    p.add_argument("map_id")
    p.add_argument("path_id")

    p = sub.add_parser("speed", help="读或写导航速度")
    p.add_argument("x", type=float, nargs="?")
    p.add_argument("--y", type=float)
    p.add_argument("--z", type=float)

    p = sub.add_parser("sim", help="起一台仿真设备")
    p.add_argument("--port", type=int, default=10010)
    p.add_argument("--seed", action="store_true")

    return parser


# --------------------------------------------------------------------- 骨架


@contextlib.asynccontextmanager
async def _backend(args) -> AsyncIterator[VendorNavBackend]:
    config = load_config(args.config)
    # NavConfig 是 frozen dataclass,覆盖单个字段用 replace 就够了 ——
    # 不要写 type(config.nav)(**{**config.nav.__dict__, ...}) 那种绕法。
    nav = config.nav if args.url is None else replace(config.nav, url=args.url)
    backend = VendorNavBackend(nav, auto_reconnect=False)
    await backend.connect()
    try:
        yield backend
    finally:
        await backend.close()


def main(argv: list[str] | None = None) -> int:
    """进程入口。只负责解析参数、配置日志、起事件循环。

    订正 A: 真正干活的部分挪进 `_amain()`,靠的是它是一个独立协程 ——
    测试可以绕开这里的 `asyncio.run()`,直接 `await _amain(...)`,让它跟
    仿真器 fixture 跑在同一个事件循环上。已实测证伪:正文原样的"同步测试 +
    异步 sim fixture + 这里内部 asyncio.run()"这个组合会挂 —— sim fixture
    的事件循环在同步测试函数体执行期间不转,握手等不到应答,一路挂到
    `connect_timeout_s`(默认 5s)超时,`main()` 于是稳定返回 1。

    `KeyboardInterrupt` 必须在这一层接,不能挪进 `_amain()`:真按 Ctrl+C
    时,异常是从下面这行 `asyncio.run()` 的调用帧里抛出来的(事件循环收到
    信号后把它重新抛到发起 `run()` 的同步代码里),不是从协程体内部抛的 ——
    `_amain()` 内部的 try/except 天生够不着它。`NavTimeoutError` /
    `NavBackendError` / `ConfigError` 三个分支则相反,必须留在 `_amain()`:
    它们是协程体里真实抛出的业务异常,测试也要靠它们(测试直接
    `await _amain(...)`,压根不经过这里的 `asyncio.run()`)。两层各管各的,
    以后不要图省事又挪到一块。
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    if args.command == "sim":
        from d1max_sim.__main__ import main as sim_main

        sim_argv = ["--port", str(args.port)]
        if args.seed:
            sim_argv.append("--seed")
        return sim_main(sim_argv)

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 1


async def _amain(args: argparse.Namespace) -> int:
    """真正干活的地方:建后端连接,分发到对应子命令处理函数。"""
    try:
        async with _backend(args) as backend:
            handler = _COMMANDS[args.command]
            return await handler(backend, args)
    except NavTimeoutError as exc:
        # 单独分支:NavTimeoutError 是 NavBackendError 的子类,必须排在它
        # 前面。这里补一个"超时"字样 —— base.py 里的原始措辞是"超过 Ns",
        # 命令行给用户的提示要一眼看出是"超时"这类可重试的失败。
        print(f"操作超时: {exc}", file=sys.stderr)
        return 1
    except (NavBackendError, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


async def _wait_for(getter, wanted, timeout_s: float) -> bool:
    """轮询直到 `getter()` 落到 `wanted`,超时返回 False(不抛异常)。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return await getter() is wanted


# --------------------------------------------------------------------- 命令


async def _cmd_status(backend, args) -> int:
    nav = await backend.nav_status()
    loc = await backend.loc_status()
    mapping = await backend.mapping_status()
    speed = await backend.get_speed()
    print(f"导航: {nav.value if nav else '未知'}")
    print(f"定位: {loc.value if loc else '未知'}")
    print(f"建图: {mapping.value if mapping else '未知'}")
    print("速度: " + "  ".join(f"{k}={v}" for k, v in sorted(speed.items())))
    return 0


async def _cmd_maps(backend, args) -> int:
    maps = await backend.list_maps()
    if not maps:
        print("没有地图")
        return 0
    for map_id in maps:
        print(map_id)
    return 0


async def _cmd_paths(backend, args) -> int:
    paths = await backend.list_paths(args.map_id)
    if not paths:
        print(f"{args.map_id} 上没有路线")
        return 0
    for path_id, waypoints in paths.items():
        print(f"{path_id}  ({len(waypoints)} 点)")
        for index, wp in enumerate(waypoints, 1):
            p = wp.pose.position
            print(f"  {index}. {wp.name}  x={p.x:.2f} y={p.y:.2f} "
                  f"yaw={wp.pose.yaw:.3f}")
    return 0


def _parse_point(text: str) -> Waypoint:
    parts = text.split(":")
    if len(parts) not in (3, 4):
        raise ValueError(f"航点格式应为 名称:x:y[:yaw],实际为 {text!r}")
    name, x, y = parts[0], float(parts[1]), float(parts[2])
    yaw = float(parts[3]) if len(parts) == 4 else 0.0
    return Waypoint(name, Pose.from_xy_yaw(x, y, yaw))


async def _cmd_path_save(backend, args) -> int:
    try:
        waypoints = [_parse_point(text) for text in args.point]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    await backend.save_path(args.map_id, args.path_id, waypoints)
    print(f"已写入 {args.path_id}: {len(waypoints)} 个点")
    return 0


async def _cmd_map_start(backend, args) -> int:
    await backend.start_mapping()
    # start_mapping() 返回时只表示指令被接受,建图子系统还要经过
    # InitWaitSensor -> MappingReady 才真正进入 MappingRunning。等到这里
    # 再提示用户,免得用户(或紧跟着的脚本)立刻 map-stop 时被设备以
    # "当前 InitWaitSensor 没有建图任务可停止"拒绝。
    if not await _wait_for(backend.mapping_status, MappingStatus.MAPPING_RUNNING,
                           MAPPING_TRANSITION_TIMEOUT_S):
        print(f"建图在 {MAPPING_TRANSITION_TIMEOUT_S}s 内未进入运行状态", file=sys.stderr)
        return 1
    print("已开始建图。走完一圈后执行 d1max map-stop 保存。")
    return 0


async def _cmd_map_stop(backend, args) -> int:
    await backend.stop_mapping()
    # 同上:等到 MappingSaveEnd 真正落地(地图已经写进 store)再提示,
    # 这样紧跟着的 d1max maps 才看得到刚保存的地图。
    if not await _wait_for(backend.mapping_status, MappingStatus.MAPPING_SAVE_END,
                           MAPPING_TRANSITION_TIMEOUT_S):
        print(f"建图保存在 {MAPPING_TRANSITION_TIMEOUT_S}s 内未完成", file=sys.stderr)
        return 1
    print("已结束建图。用 d1max maps 查看保存结果。")
    return 0


async def _cmd_load(backend, args) -> int:
    await backend.load_map(args.map_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOC_READY_TIMEOUT_S
    while loop.time() < deadline:
        loc = await backend.loc_status()
        if loc is not None and loc in LOC_HEALTHY:
            print(f"定位就绪: {loc.value}")
            return 0
        await asyncio.sleep(0.3)
    print(f"定位在 {LOC_READY_TIMEOUT_S}s 内未就绪", file=sys.stderr)
    return 1


async def _goto_and_wait(backend, pose: Pose, timeout_s: float) -> NavStatus:
    # 先订阅、再下发、再把队列交给 wait_nav_terminal —— 订阅与下发之间没有
    # await,不存在让出点,所以终态不会漏。wait_nav_terminal 的 queue= 参数
    # 就是为这个场合加的(Task 11 评审结论)。
    #
    # 旧写法 create_task(wait_nav_terminal()) + await asyncio.sleep(0) 其实也
    # **不漏事件**(实测 200/200 接住终态:subscribe() 是同步的,发生在协程第一个
    # 挂起点之前,所以一次 sleep(0) 就足够把订阅建起来)。换成现在这版不是为了
    # 修竞态,而是因为它不依赖上面这段绕弯的推理,而且省掉了 create_task /
    # cancel / suppress(CancelledError) 那一串(10 行变 3 行)。
    #
    # 另:不要在 cli.py 里另写一份等终态的循环 —— 那是把 base.py 抄第二遍。
    with backend.subscription() as queue:
        await backend.goto(pose)
        return await backend.wait_nav_terminal(timeout_s, queue=queue)


async def _cmd_goto(backend, args) -> int:
    status = await _goto_and_wait(
        backend, Pose.from_xy_yaw(args.x, args.y, args.yaw), args.timeout)
    print(f"导航结束: {status.value}")
    return 0 if status is NavStatus.SUCCEED else 1


async def _cmd_walk(backend, args) -> int:
    """全逐点执行:路线由厂商 App 画,顺序由我们排,一次只下发一个点。"""
    paths = await backend.list_paths(args.map_id)
    if args.path_id not in paths:
        print(f"{args.map_id} 上没有路线 {args.path_id}", file=sys.stderr)
        return 1

    waypoints = paths[args.path_id]
    total = len(waypoints)
    if total == 0:
        print("这条路线上没有航点")
        return 0

    for index, wp in enumerate(waypoints, 1):
        # 上一个点到终态之后,设备状态机有个短驻留才回落 StandBy,期间
        # 再次 goto() 会被拒绝 —— 与 tests/contract/test_nav_contract.py
        # 的 test_连续走多个点 是同一个约束,不是竞态,只许等。
        if not await _wait_for(backend.nav_status, NavStatus.STANDBY, NAV_STANDBY_TIMEOUT_S):
            print(f"导航未在 {NAV_STANDBY_TIMEOUT_S}s 内回到 StandBy,"
                  f"无法前往 {wp.name}", file=sys.stderr)
            return 1
        print(f"[{index}/{total}] 前往 {wp.name}", flush=True)
        status = await _goto_and_wait(backend, wp.pose, args.timeout)
        if status is not NavStatus.SUCCEED:
            print(f"在 {wp.name} 处失败: {status.value}", file=sys.stderr)
            return 1
    print(f"全部完成: {total} 个点")
    return 0


async def _cmd_speed(backend, args) -> int:
    if args.x is None:
        speed = await backend.get_speed()
    else:
        speed = await backend.set_speed(args.x, y=args.y, z=args.z)
    print("  ".join(f"{k}={v}" for k, v in sorted(speed.items())))
    return 0


_COMMANDS = {
    "status": _cmd_status,
    "maps": _cmd_maps,
    "paths": _cmd_paths,
    "path-save": _cmd_path_save,
    "map-start": _cmd_map_start,
    "map-stop": _cmd_map_stop,
    "load": _cmd_load,
    "goto": _cmd_goto,
    "walk": _cmd_walk,
    "speed": _cmd_speed,
}


if __name__ == "__main__":
    sys.exit(main())
