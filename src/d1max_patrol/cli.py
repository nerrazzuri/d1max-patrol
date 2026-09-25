"""d1max 命令行。

接上真机后的第一条命令应该是:
    d1max --url ws://192.168.144.100:10010 status
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from d1max_agent.engine.bundle import (
    BundleGuard,
    BundleState,
    guard_bundle,
    read_state,
)
from d1max_agent.engine.datadir import (
    DATA_ROOT_ENV,
    DEFAULT_DATA_ROOT,
    migrate_slot_data,
)
from d1max_agent.engine.privileged import Privileged, PrivilegedError
from d1max_agent.engine.release import (
    MAX_BOOT_ATTEMPTS,
    GuardAction,
    Layout,
    ReleaseError,
    activate,
    boot_guard,
    current_name,
    installed,
    pack,
    read_manifest,
    read_pending,
    rollback,
    stage,
)
from d1max_patrol.app.identity import SN_ENV, resolve
from d1max_patrol.backends.base import NavBackendError, NavTimeoutError
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.loader import ConfigError, load_config
from d1max_patrol.conformance import DEFAULT_PROBES, nav_host_port, run_conformance
from d1max_patrol.protocol.nav_types import (
    LOC_HEALTHY,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)
from d1max_patrol.recorder import FrameRecorder, default_recording_path

log = logging.getLogger(__name__)

#: 特权助手只认真机上写死的版本根。``release activate --root 别处``(或
#: ``D1MAX_RELEASE_ROOT`` 指到别处)时助手仍会从 /opt/d1max 取单元 —— 那是
#: 另一棵树的单元,装错版本或误报「源不存在」都不该发生,所以根不一致就跳过。
_HELPER_RELEASE_ROOT = Path("/opt/d1max")


def _helper_for(layout: Layout) -> Privileged | None:
    """这条命令该不该走特权助手:助手在、而且版本根就是真机那一个。"""
    priv = Privileged()
    if not priv.present():
        print("提示: 没有特权助手,单元文件没更新;要更新单元请重跑 deploy/install.sh")
        return None
    if Path(layout.root) != _HELPER_RELEASE_ROOT:
        print(f"提示: 版本根是 {layout.root} 而不是 {_HELPER_RELEASE_ROOT},"
              "特权助手只认后者,单元文件没更新")
        return None
    return priv


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
    # 录制**默认开**。理由见 recorder.py: 代码以后能改,真机上没录到的帧
    # 补不回来。要关掉必须显式说出口。
    parser.add_argument("--record", metavar="路径",
                        help="原始帧录制落盘位置(默认 runs/frames/nav-<UTC>.jsonl)")
    parser.add_argument("--no-record", action="store_true",
                        help="关闭原始帧录制(不推荐:现场录像不可复现)")

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

    p = sub.add_parser("conform", help="真机一致性巡检(阶段 0/1,纯只读)")
    p.add_argument("--outdir", help="报告输出目录(默认 runs/conformance/<UTC>)")
    p.add_argument("--allow-motion", action="store_true",
                   help="允许会让机器移动的阶段。默认关闭 —— 现场机器旁边站着人")

    p = sub.add_parser("sim", help="起一台仿真设备")
    p.add_argument("--port", type=int, default=10010)
    p.add_argument("--seed", action="store_true")
    p.add_argument("--full", action="store_true",
                   help="导航之外再起旁路进程和建图桥 —— 彩排整个 app 用这个")

    rel = sub.add_parser("release", help="装机、升级、回滚")
    rel_sub = rel.add_subparsers(dest="release_command", required=True)

    def _root_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=None,
                       help="版本目录的根,默认取 $D1MAX_RELEASE_ROOT 或 /opt/d1max")

    _root_arg(rel_sub.add_parser("list", help="装了哪几版,现在跑哪版"))
    p_pack = rel_sub.add_parser(
        "pack",
        help="把一棵源码树打成能装机的包(**在笔记本上跑,不在狗上跑**)",
        description=(
            "把一棵源码树打成 deploy/install.sh 收得下的包目录。\n"
            "\n"
            "**这条命令跑在笔记本(开发机)上,不跑在狗上。** 它的输入是这个\n"
            "仓库,输出是一个拷到 U 盘、再拿去 install.sh 的目录。狗上跑的是\n"
            "release install / activate / rollback / boot-guard,不是这一条 ——\n"
            "狗上连仓库都没有。\n"
            "\n"
            "打完屏幕上会打出包目录和 content_sha256,**这两个值抄进现场记录栏**。"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_pack.add_argument("src", help="源码树,一般就是这个仓库的根")
    p_pack.add_argument("outdir", help="放包的**父**目录 —— 包建在它底下")
    p_pack.add_argument("--name", default=None,
                        help="包目录名。默认 <今天>-<git 短哈希 8 位>;"
                             "不是 git 仓库就退到整棵树指纹的前 8 位")
    p_pack.add_argument("--force", action="store_true",
                        help="输出目录已经存在且非空也照打(会先把它删掉)")
    p_pack.add_argument("--wheels", default=None,
                        help="离线轮子目录(*.whl):拷进包里,站点下发的版本在狗上"
                             "建 venv 时只从它装(现场的狗上不了网)")
    p_ins = rel_sub.add_parser("install", help="把一个包落进槽里,不切换")
    p_ins.add_argument("package", help="包目录")
    _root_arg(p_ins)
    p_act = rel_sub.add_parser("activate", help="切到某一版,下次起来自检")
    p_act.add_argument("name", help="版本名")
    _root_arg(p_act)
    _root_arg(rel_sub.add_parser("rollback", help="退回上一版"))
    _root_arg(rel_sub.add_parser(
        "boot-guard",
        help="开机守卫:连着两次开机还挂着在途标记就退回去"))
    p_mig = rel_sub.add_parser(
        "migrate-data",
        help="把版本槽里的巡检数据搬到数据根"
             "(W01;装机脚本在停掉服务之后、起新版之前调它)")
    _root_arg(p_mig)
    p_mig.add_argument("--data-root", default=None,
                       help=f"数据根,默认取 ${DATA_ROOT_ENV} 或 {DEFAULT_DATA_ROOT}")

    bun = sub.add_parser("bundle", help="任务包:开机守卫")
    bun_sub = bun.add_subparsers(dest="bundle_command", required=True)
    p_guard = bun_sub.add_parser(
        "guard",
        help="任务包开机守卫:把换链换到一半的局面收拾成能用的终态")
    p_guard.add_argument(
        "--root", default=None,
        help="任务包目录的根,默认取 $D1MAX_BUNDLES_ROOT 或 <版本根>/bundles")

    return parser


# --------------------------------------------------------------------- 骨架


@contextlib.asynccontextmanager
async def _backend(args) -> AsyncIterator[VendorNavBackend]:
    config = load_config(args.config)
    # NavConfig 是 frozen dataclass,覆盖单个字段用 replace 就够了 ——
    # 不要写 type(config.nav)(**{**config.nav.__dict__, ...}) 那种绕法。
    nav = config.nav if args.url is None else replace(config.nav, url=args.url)

    recorder = None
    if not getattr(args, "no_record", False):
        path = getattr(args, "record", None) or default_recording_path(config.runs_dir)
        recorder = FrameRecorder(path)

    backend = VendorNavBackend(nav, auto_reconnect=False, recorder=recorder)
    try:
        await backend.connect()
        try:
            yield backend
        finally:
            await backend.close()
    finally:
        # 录制器必须在**连接失败的路径上也**收尾 —— 连不上的那次录像里
        # 恰好记着 link_up 之前发生了什么,现场排障就靠它。
        if recorder is not None:
            recorder.close()
            if not recorder.failed:
                print(f"原始帧录像: {recorder.path} ({recorder.count} 行)",
                      file=sys.stderr)


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
        # MIN-1: 仿真器是开发期依赖,真机现场装的可能只有 d1max_patrol 一个包。
        # 裸 import 撞上来是一条英文 ModuleNotFoundError 栈,对现场没有意义。
        try:
            from d1max_sim.__main__ import main as sim_main
        except ImportError as exc:
            print(f"仿真器未安装,无法执行 sim 子命令: {exc}", file=sys.stderr)
            print("它随开发依赖一起安装: pip install -e .[dev]", file=sys.stderr)
            return 1

        sim_argv = ["--port", str(args.port)]
        if args.seed:
            sim_argv.append("--seed")
        if args.full:
            sim_argv.append("--full")
        return sim_main(sim_argv)

    if args.command == "release":
        # 跟 sim 同一条道理:装机时机器上根本没有后端可连,boot-guard 更是
        # 在 systemd 把我们的服务拉起来之前就跑 —— 这条支路绝不许碰
        # `_backend()`/`_amain()`,那条路一上来就要建后端连接。
        return _cmd_release(args)

    if args.command == "bundle":
        # 同上:任务包守卫也挂在 systemd 的 ExecStartPre 上,跑在服务被拉起来
        # **之前** —— 那会儿根本没有后端可连。
        return _cmd_bundle(args)

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


async def _cmd_conform(backend, args) -> int:
    """一致性巡检。阶段 0/1 纯只读,不会让机器动。"""
    from datetime import datetime, timezone
    from pathlib import Path

    config = load_config(args.config)
    url = args.url or config.nav.url
    if args.outdir:
        outdir = Path(args.outdir)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outdir = Path(config.runs_dir) / "conformance" / stamp

    host, port = nav_host_port(url)
    # 探测表里把**实际用的**导航地址放在第一条 —— 现场可能改了 IP,
    # 只探默认值等于报告里写了一条假的"不通"。其余候选地址照探:
    # 厂商两份文档对导航地址的说法不一致(见 conformance.DEFAULT_PROBES 的
    # 注释),SDK 又分有线/WiFi 两个入口,一次全扫完才知道这台机器长什么样。
    extra = tuple(p for p in DEFAULT_PROBES if (p[1], p[2]) != (host, port))
    probes = (("nav", host, port), *extra)

    result = await run_conformance(
        backend, url, outdir, backend.recorder,
        allow_motion=args.allow_motion, probes=probes,
    )

    ok = sum(1 for c in result.calls if c.ok)
    print(f"只读接口: {ok}/{len(result.calls)} 成功")
    for p in result.probes:
        print(f"网络 {p.name} {p.host}:{p.port}: {'通' if p.ok else '不通'} — {p.detail}")
    dirty = [d for d in result.diffs if not d.clean]
    print(f"字段对拍: {len(result.diffs)} 种响应,{len(dirty)} 种与文档不一致")
    for d in dirty:
        print(f"  ! {d.resp_func}")
    print(f"报告: {outdir / 'conformance-report.md'}")
    # 退出码只反映"巡检本身有没有跑起来"。字段不一致**不算失败** ——
    # 那正是我们来采集的信息,拿它当错误会让现场以为流程崩了。
    return 0 if ok else 1


# --------------------------------------------------------------------- release


def _release_root(arg: str | None) -> Path:
    """版本根在哪。显式参数赢,其次环境变量,最后真机上的默认路径。"""
    if arg:
        return Path(arg)
    env = os.environ.get("D1MAX_RELEASE_ROOT", "").strip()
    return Path(env) if env else Path("/opt/d1max")


def _cmd_release(args: argparse.Namespace) -> int:
    """release 子命令族。**这条支路不连后端** —— 装机时机器上没有后端可连,

    boot-guard 更是在 systemd 把我们的服务拉起来之前就跑。见 `main()` 里
    在 `asyncio.run(_amain(args))` 之前就把它分派掉的那一段。
    """
    now_ms = int(time.time() * 1000)

    # **pack 排在最前面,而且一个字节都不碰 layout。** 它跑在笔记本上,
    # 那台机器上根本没有 /opt/d1max,连算一次默认版本根都是多余的。
    if args.release_command == "pack":
        return _cmd_release_pack(args, now_ms)

    layout = Layout(root=_release_root(getattr(args, "root", None)))

    if args.release_command == "list":
        pending = read_pending(layout)
        print(f"根       : {layout.root}")
        print(f"现在跑的 : {current_name(layout) or '(还没有)'}")
        for name in installed(layout):
            print(f"装着的   : {name}")
        if pending is not None:
            print(f"在途     : {pending.to} 第 {pending.attempts} 次开机"
                  f"(上一版 {pending.src or '无'})")
        return 0

    if args.release_command == "install":
        try:
            manifest = stage(layout, Path(args.package), now_ms=now_ms)
        except (ReleaseError, OSError) as exc:
            print(f"装不了: {exc}", file=sys.stderr)
            return 2
        print(f"落槽了: {manifest.name} (版本 {manifest.version})")
        return 0

    if args.release_command == "activate":
        # W01b:**先装这一版随包带的单元,再切链。** 单元只引用 current 和几条
        # 稳定路径,新单元配老代码是安全的;反过来(切了链、单元没装上)正是
        # OTA 升上来的机器单元永远停在装机那天的那个坏状态。助手不在(开发机、
        # W01b 之前装的机器)就跳过并说一句怎么补 —— 装机脚本 5/7 自己也会装单元。
        priv = _helper_for(layout)
        if priv is not None:
            try:
                print(f"单元文件: {priv.install_unit(args.name)}")
            except PrivilegedError as exc:
                print(f"切不了: 新版单元文件没装上,没有切换 —— {exc}", file=sys.stderr)
                return 2
        try:
            # 把当下的 SN 记进在途标记 —— 重启后自检第四项拿它比对(§7.2)。
            # 这里要读 D1MAX_SN 而不是零参 resolve():现场设备树/DMI 里常常
            # 没有真 SN,HTTP 服务侧(app/server.py 的 resolve(args.sn, ...),
            # args.sn 默认就是这个环境变量)靠它拿到运维填的真值。两条路
            # 必须用同一个来源定 SN,不然这里记下的是 MAC 兜底值,重启后
            # 自检拿真 SN 一比对不上,把一版好的自动回滚掉。
            pending = activate(layout, args.name, now_ms=now_ms, auto=False,
                               sn=resolve(os.environ.get(SN_ENV)).sn)
        except (ReleaseError, OSError) as exc:
            print(f"切不了: {exc}", file=sys.stderr)
            return 2
        print(f"切到 {pending.to},上一版 {pending.src or '无'}。"
              f"重启之后会自检,没过会自己退回去。")
        print("提示: 升级前那七项检查(precheck)在命令行这条路上没跑,"
              "只有 HTTP 那条路(POST /api/release/activate)才跑。")
        return 0

    if args.release_command == "migrate-data":
        raw = (args.data_root or "").strip()
        env = os.environ.get(DATA_ROOT_ENV, "").strip()
        data_root = Path(raw or env or DEFAULT_DATA_ROOT)
        if os.geteuid() == 0:
            print("注意:正以 root 跑搬迁,拷出来的文件会归 root;"
                  "服务用户以后可能删不掉。装机脚本是用服务用户跑它的。",
                  file=sys.stderr)
        try:
            report = migrate_slot_data(layout, data_root)
        except (ReleaseError, OSError) as exc:
            print(f"搬迁失败:{exc}", file=sys.stderr)
            return 2
        print(f"数据根   : {data_root}")
        print(f"搬过的槽 : {', '.join(report.slots) or '(没有要搬的)'}")
        print(f"拷了 {report.copied} 个文件,跳过 {report.skipped} 个(目标已有)")
        return 0

    if args.release_command == "rollback":
        try:
            back = rollback(layout, now_ms=now_ms)
        except (ReleaseError, OSError) as exc:
            print(f"退不了: {exc}", file=sys.stderr)
            return 2
        # W01b:把退回那一版的单元也装回去。**失败不拦回滚**:回到能跑的代码
        # 比单元一致更要紧,单元向后兼容(只引用 current)。
        priv = _helper_for(layout)
        if priv is not None:
            try:
                print(f"单元文件: {priv.install_unit(back)}")
            except PrivilegedError as exc:
                print(f"退回去了,但 {back} 的单元没装回去: {exc}", file=sys.stderr)
        print(f"退回 {back}。重启生效。")
        return 0

    # boot-guard
    return _cmd_boot_guard(layout, now_ms)


def _cmd_release_pack(args: argparse.Namespace, now_ms: int) -> int:
    """`release pack`。造包的那一头,跑在笔记本上。

    **屏幕上一定要有包目录和 content_sha256 这两行。** 现场记录栏要抄它们:
    包被拷上 U 盘、再从 U 盘拷进狗,中间任何一次拷贝掉了字节,靠的就是这个
    指纹能对得上 —— 而对账的前提是打包那一刻有人把它抄下来了。
    """
    try:
        dest = pack(args.src, args.outdir, name=args.name, now_ms=now_ms,
                    force=args.force, wheels=args.wheels)
        manifest = read_manifest(dest)
    except (ReleaseError, OSError) as exc:
        print(f"打不了包: {exc}", file=sys.stderr)
        return 2
    print(f"包目录         : {dest}")
    print(f"content_sha256 : {manifest.content_sha256}")
    print(f"版本           : {manifest.version}"
          f"(任务包 schema {manifest.requires_mission_schema},"
          f"打包时刻 {manifest.built_at})")
    print("下一步: 把这个目录整个拷到狗上,然后在狗上跑")
    print(f"  sudo bash deploy/install.sh <拷过去的路径>/{dest.name}")
    return 0


def _cmd_boot_guard(layout: Layout, now_ms: int) -> int:
    """开机守卫。**这是自动回滚的第二层,专治"坏到跑不出自检"。**

    第一层是服务里那一遍自检:它需要新版能起来、能跑到那段代码。如果新版坏到
    连进程都起不来,第一层永远不会被执行 —— 那台机器就永远停在坏版本上,而且
    现场没人能通过 HTTP 看到任何东西。

    这个守卫由 systemd 在我们的服务**之前**跑,它自己装在 ``<root>/bin`` 里,
    **不在任何一版的目录内** —— 所以换版本换不掉它,坏版本也带不坏它。它不
    读新版的任何一个字节,只数在途标记上的次数。

    **它永远退 0。** 守卫失败绝不能拦住开机 —— 一个把机器挡在启动之外的
    安全网,比它要防的问题更糟。``boot_guard()`` 自己已经保证任何一条路都
    不抛异常(最坏回 ``GuardAction.BROKEN``),这里不需要再包一层。
    """
    action = boot_guard(layout, now_ms=now_ms)

    话 = {
        GuardAction.OK: "没有在途的升级,链是好的。",
        GuardAction.COUNTED: "有在途的升级,数了一次,让它起。",
        GuardAction.ROLLED_BACK:
            f"连着 {MAX_BOOT_ATTEMPTS} 次开机都没坐实,已退回上一版。",
        GuardAction.REPAIRED: "链断了,已经按在途标记(或盘上最新的一版)修好。",
        GuardAction.GAVE_UP: "装机那一次就没起来,没有上一版可退 —— 请人来看。",
        GuardAction.BROKEN: "盘上一版都没有 —— 这台机器要重装。",
    }
    文本 = (f"[守卫] {话.get(action, action.value)} 现在指着: "
           f"{current_name(layout) or '(没有)'}")
    # GAVE_UP / BROKEN 是"请人来看"的两种,打到 stderr 才不会被日常开机时
    # 收 stdout 的脚本悄悄吞掉。退出码仍然一律是 0(见本函数 docstring)——
    # 改的只是可见性,不是"这次开机算不算数"。
    if action in (GuardAction.GAVE_UP, GuardAction.BROKEN):
        print(文本, file=sys.stderr)
    else:
        print(文本)
    return 0


# --------------------------------------------------------------------- bundle


def _bundles_root(arg: str | None) -> Path:
    """任务包根在哪。显式参数赢,其次环境变量,最后跟着版本根走。

    **默认值必须跟 ``app/server.py`` 的 ``AppContext.bundles_root``
    (``/opt/d1max/bundles``)对得上。** 两边各说各话的话,守卫在一个根下修链、
    服务读的是另一个根 —— 那比守卫根本没挂上还糟:它会报「链是好的」。
    """
    if arg:
        return Path(arg)
    env = os.environ.get("D1MAX_BUNDLES_ROOT", "").strip()
    return Path(env) if env else _release_root(None) / "bundles"


def _cmd_bundle(args: argparse.Namespace) -> int:
    """bundle 子命令族。**这条支路跟 release 一样不连后端。**"""
    return _cmd_bundle_guard(_bundles_root(getattr(args, "root", None)))


def _cmd_bundle_guard(root: Path) -> int:
    """任务包开机守卫。**照 ``_cmd_boot_guard`` 的形状做的**,理由也一样。

    ``apply_bundle`` 连着换两条链(``previous``、``current``),两次之间断电
    盘上就停在 ``current == previous`` 且 ``proven=False`` —— 第 8 卷那条自动
    回退判据随即成立,把盘上唯一那份好包拉黑。``guard_bundle`` 是那条局面
    唯一的出路,而它得有人调:评审复评 finding 4 逮到的正是「修复路径写好了,
    真机上永远不会被执行」,而交付文档已经写着「开机时照着它把链修回一个能用
    的样子」。这条子命令 + ``d1max-patrol.service`` 的 ``ExecStartPre`` 把那句
    话变成真的。

    它顺带**把老盘归一化一次**(见 ``guard_bundle``):截 ``rollbacks``、把
    拉黑事实固化进 ``denied``。升级上来的狗开一次机就正常了。

    **它永远退 0。** 跟版本守卫同一条理由:一个把机器挡在启动之外的安全网,
    比它要防的问题更糟。``guard_bundle()`` 自己保证任何一条路都不抛异常
    (最坏回 ``BundleGuard.BROKEN``),但**底下那句 ``read_state`` 不在那个
    保证里**(评审复评第 3 轮 N3):同一份坏 ``landed.json`` 照样能让它抛,
    于是「永远退 0」这句话不成立,而 ``BROKEN`` 的时候运维本来就只有 stderr
    这一条线索 —— 拿到的却是一坨 traceback,不是那句人话。所以这儿单独包一层:
    **状态读不出来不影响结论,更不该改变退出码。**

    接的是 ``(OSError, ValueError, TypeError)`` 而不是 ``except Exception``:
    这三样正是 ``guard_bundle`` 自己那张兜底网(``bundle._守卫兜底``),而
    ``read_state`` 走的是同一批函数(``_链指向``、``_读记``、``_黑名单``、
    ``_半成品``)。用 ``except Exception`` + ``# noqa`` 把 ruff 的 ``BLE`` 绕
    过去,等于把「哪些坏法是预料之内的」这个信息一起抹掉。
    """
    结论 = guard_bundle(root)

    话 = {
        BundleGuard.OK: "没有换到一半的任务包,两条链是好的。",
        BundleGuard.FINISHED: "有一次换链没换完,已经照标记换完。",
        BundleGuard.UNDONE: "那一版换不过去,两条链已经放回换链之前的样子。",
        BundleGuard.BROKEN:
            "换链换到一半,两个方向都走不通 —— 标记留着,请人来看。",
    }
    try:
        st: BundleState | None = read_state(root)
    except (OSError, ValueError, TypeError):
        st = None
    if st is None:
        尾 = "现在指着: (状态取不到 —— landed.json 或那两条链读不出来)"
    else:
        尾 = (f"现在指着: {st.current or '(没有)'}"
              f"(上一版 {st.previous or '无'})")
    文本 = f"[任务包守卫] {话.get(结论, 结论.value)} {尾}"
    # BROKEN 是"请人来看"那一种,打到 stderr 才不会被日常开机时收 stdout 的
    # 脚本悄悄吞掉。退出码仍然是 0(见本函数 docstring)。
    # **状态读不出来也走 stderr**:那同样是一台该有人看一眼的机器。
    if 结论 is BundleGuard.BROKEN or st is None:
        print(文本, file=sys.stderr)
    else:
        print(文本)
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
    "conform": _cmd_conform,
}


if __name__ == "__main__":
    sys.exit(main())
