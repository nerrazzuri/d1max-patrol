"""``d1max-agent`` 的入口(W00b 决定 3、4)。

装配:Transport(``mqtt://`` 走 Paho,``memory://`` 走进程内 broker —— 给演示与联调)
× HAL(``sim`` 现在;``d1max`` 归 W00d)× 引擎(``assembly.build_engine``)× ``AgentRuntime``。
一切都跑在 ``LoopBridge`` 的事件循环线程里,``asyncio.sleep(period)`` 每拍 ``step`` 一次。

``--legacy-http host:port`` 时在**同一进程**里起老的 ``AppServer``,与运行时共用同一台引擎、
同一对后端:手机与值守屏照旧连它,站点经 MQTT 派的任务和手机发起的任务走同一台引擎、
同一套资源仲裁。两个进程都想拿 sidecar 控制权会出事,所以必须同一进程。
``server.py`` 自己不改,只是组装权从它的 ``main()`` 移到这里;旧入口 ``d1max-app`` 与
``d1max-patrol.service`` 原样保留,等 W00c 手机改连站点后退役。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_agent import AGENT_VERSION
from d1max_agent.assembly import EngineParts, build_engine
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.registration import Registration
from d1max_contract.transport import Transport
from d1max_patrol.protocol.nav_types import Pose

log = logging.getLogger(__name__)


def wall_ms() -> int:
    return int(time.time() * 1000)


def _map_arg(text: str) -> tuple[str, str]:
    map_id, sep, version = text.rpartition(":")
    if not sep or not map_id or not version:
        raise argparse.ArgumentTypeError(f"--map 要写成 <map_id>:<version>,收到 {text!r}")
    return map_id, version


def _home_arg(text: str) -> Pose:
    parts = text.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"--home 要写成 x,y,yaw,收到 {text!r}")
    try:
        x, y, yaw = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--home 里不是数: {text!r}") from exc
    return Pose.from_xy_yaw(x, y, yaw)


def _transport_arg(text: str) -> str:
    if text == "memory://" or text.startswith(("mqtt://", "mqtts://")):
        return text
    raise argparse.ArgumentTypeError(
        f"--transport 只认 mqtt://host[:port]、mqtts://host[:port] 或 memory://,收到 {text!r}")


def _hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"--legacy-http 要写成 host:port,收到 {text!r}")
    return host, int(port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max-agent",
                                description=f"D1 Max 机器人代理 {AGENT_VERSION}")
    p.add_argument("--transport", type=_transport_arg, required=True,
                   help="mqtt://host[:port]、mqtts://host[:port] 或 memory://(进程内,演示用)")
    p.add_argument("--hal", choices=("sim",), default="sim", help="品牌适配器;d1max 归 W00d")
    p.add_argument("--registration", type=Path, required=True, help="站点签发的注册文件")
    p.add_argument("--store-dir", type=Path, required=True, help="幂等记录、事件簿、代次落盘的目录")
    p.add_argument("--runs-root", type=Path, required=True, help="引擎归档目录(数据根下的 runs)")
    p.add_argument("--map", type=_map_arg, required=True, help="已加载地图 <map_id>:<version>")
    p.add_argument("--home", type=_home_arg, default=None,
                   help="原点 x,y,yaw;不给的话预飞检查 home 那一项红,goto 一律 failed")
    p.add_argument("--period", type=float, default=0.1, help="每拍间隔(秒)")
    p.add_argument("--legacy-http", type=_hostport, default=None,
                   help="同一进程里托管老的 HTTP 面(手机/值守屏用),host:port")
    p.add_argument("--pin", default=None, help="legacy HTTP 绑到非本机地址时必须给")
    p.add_argument("--sn", default=None, help="机身序列号(legacy HTTP 的身份用)")
    return p


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


@dataclass
class Assembled:
    bridge: Any
    runtime: AgentRuntime
    parts: EngineParts
    hal: Any
    broker: MemoryBroker | None
    server: Any
    ctx: Any
    period_s: float
    _stop: threading.Event

    def start(self) -> None:
        """在 bridge 的循环里起运行时,并开始每拍 step。legacy HTTP 一起起。"""
        self.bridge.call(self.runtime.start, timeout_s=30.0)
        self.bridge.spawn(lambda: self._drive())
        if self.server is not None:
            self.server.start(postcheck=False)

    async def _drive(self) -> None:
        dt = self.period_s
        while not self._stop.is_set():
            tick = getattr(self.hal, "tick", None)
            if tick is not None:                 # sim 自己要推进;真 HAL 没有 tick
                tick(dt)
            try:
                await self.runtime.step(dt)
            except Exception:
                log.exception("运行时一拍炸了,下一拍继续")
            await asyncio.sleep(dt)

    def stop(self) -> None:
        self._stop.set()
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                log.exception("legacy HTTP 停不干净")
        try:
            self.bridge.call(self.runtime.close, timeout_s=10.0)
        except Exception:
            log.exception("运行时关不干净")
        self.bridge.stop()


def _make_transport(url: str, client_id: str, broker: MemoryBroker | None) -> Transport:
    if url == "memory://":
        assert broker is not None
        return MemoryTransport(broker, client_id)
    from d1max_contract.paho_transport import PahoTransport
    return PahoTransport(url, client_id=client_id)


def build(args: argparse.Namespace) -> Assembled:
    from d1max_patrol.app.bridge import LoopBridge

    registration = Registration.load(args.registration)
    broker = MemoryBroker() if args.transport == "memory://" else None
    bridge = LoopBridge()
    bridge.start()

    def _assemble() -> tuple[Any, EngineParts, AgentRuntime]:
        if args.hal == "sim":
            from d1max_adapter_sim.robot import SimRobot
            hal: Any = SimRobot(now_ms=wall_ms)
        else:  # pragma: no cover - 只有 sim
            raise SystemExit(f"不认识的 HAL {args.hal!r}")
        parts = build_engine(hal, runs_root=args.runs_root, now_ms=wall_ms, map_id=args.map[0],
                             home=args.home)
        transport = _make_transport(args.transport, registration.robot_id, broker)
        runtime = AgentRuntime(transport=transport, registration=registration, hal=hal,
                               store_dir=args.store_dir, now_ms=wall_ms, loaded_map=args.map,
                               parts=parts)
        return hal, parts, runtime

    async def _in_loop():
        return _assemble()

    hal, parts, runtime = bridge.call(_in_loop, timeout_s=30.0)

    server = ctx = None
    if args.legacy_http is not None:
        server, ctx = _legacy_http(args, bridge, parts, registration)
    return Assembled(bridge=bridge, runtime=runtime, parts=parts, hal=hal, broker=broker,
                     server=server, ctx=ctx, period_s=args.period, _stop=threading.Event())


def _legacy_http(args: argparse.Namespace, bridge: Any, parts: EngineParts,
                 registration: Registration) -> tuple[Any, Any]:
    """老的 AppServer,共用同一台引擎与后端。组装照 ``server.main()`` 的样子,只是零件换成
    桥出来的 nav/device 与已经装好的 engine。"""
    from d1max_patrol.app.identity import resolve
    from d1max_patrol.app.mapping import MappingConfig, MappingOrchestrator
    from d1max_patrol.app.procs import ProcManager
    from d1max_patrol.app.server import AppContext, AppServer, _make_teleop, check_exposure
    from d1max_patrol.backends.map_bridge import MapBridgeClient

    host, port = args.legacy_http
    check_exposure(host, args.pin)
    store = Path(args.store_dir)
    procs = ProcManager(store / "logs")
    teleop = bridge.call(lambda: _make_teleop(parts.device, parts.engine, {}))
    mapping = MappingOrchestrator(procs, MappingConfig(bags_dir=store / "bags",
                                                       maps_dir=store / "maps"))
    who = resolve(args.sn or registration.robot_id, None)
    ctx = AppContext(bridge=bridge, engine=parts.engine, nav=parts.nav, device=parts.device,
                     maps=MapBridgeClient("127.0.0.1", 8092), procs=procs, teleop=teleop,
                     mapping=mapping, missions_dir=store / "missions",
                     runs_root=Path(args.runs_root), video={}, identity=who)
    server = AppServer(ctx, host=host, port=port, pin=args.pin)
    return server, ctx


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    assembled = build(args)
    assembled.start()
    print(f"d1max-agent 起来了:transport={args.transport} hal={args.hal} "
          f"robot={assembled.runtime.registration.robot_id}"
          + (f" legacy-http={assembled.server.url}" if assembled.server else ""))
    try:
        while not assembled._stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C,收尾中……")
    finally:
        assembled.stop()
    return 0


if __name__ == "__main__":      # pragma: no cover - 入口
    sys.exit(main())
