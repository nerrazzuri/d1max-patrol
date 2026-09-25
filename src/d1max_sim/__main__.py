"""python -m d1max_sim —— 起一台仿真 D1 Max。

默认只起导航那一台(CLI 够用了)。``--full`` 连同仿真旁路进程一起起,端口取真机的默认值,
给 ``d1max-agent --hal d1max --sidecar 127.0.0.1:8090`` 这类联调用。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from d1max_patrol.protocol.nav_types import Pose, Waypoint

from .agent_server import SimAgentServer
from .nav_server import SimNavServer

#: ``--full`` 用的端口,跟真机一样:对不齐就得两边都带参数,而每多一个参数就多一处
#: "现场跟在家不一样"。
FULL_NAV_PORT = 10010
FULL_AGENT_PORT = 8090


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max_sim", description="D1 Max 自主导航仿真器")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10010)
    p.add_argument("--tick-hz", type=float, default=50.0)
    p.add_argument("--seed", action="store_true",
                   help="预置一张地图与一条三点巡检路径,便于演示")
    p.add_argument("--agent-port", type=int, default=0,
                   help="同时起一台仿真旁路进程(patrol_agent 的 TCP 口),0 为不起")
    p.add_argument("--full", action="store_true",
                   help=f"导航({FULL_NAV_PORT})与旁路({FULL_AGENT_PORT})一起起并预置地图")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _seed(server: SimNavServer) -> None:
    map_id = server.store.create_map("demo_map")
    server.store.set_path(map_id, "demo_route", [
        Waypoint("P1_配电柜", Pose.from_xy_yaw(2.0, 0.0, 0.0)),
        Waypoint("P2_水泵", Pose.from_xy_yaw(2.0, 2.0, 1.5708)),
        Waypoint("P3_出口", Pose.from_xy_yaw(0.0, 2.0, 3.1416)),
    ])


async def _serve(args: argparse.Namespace) -> None:
    server = SimNavServer(host=args.host, port=args.port, tick_hz=args.tick_hz)
    if args.seed:
        _seed(server)
    await server.start()
    print(f"导航: {server.url}")
    print(f"注入: {server.control_url}  (发送 {{\"cmd\": \"help\"}} 查看命令)")

    extra: list[SimAgentServer] = []
    try:
        if args.agent_port:
            agent = SimAgentServer(args.host, args.agent_port)
            await agent.start()
            extra.append(agent)
            print(f"旁路进程: {args.host}:{agent.port}  (motion={agent.motion.value},"
                  f" 站起来要自己发命令,跟真机一样)")
        await asyncio.Event().wait()
    finally:
        for one in reversed(extra):
            await one.stop()
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.full:
        # 只在没显式指定时才顶上去:``--full --port 10011`` 得听后面那个的。
        if args.port == build_parser().get_default("port"):
            args.port = FULL_NAV_PORT
        args.agent_port = args.agent_port or FULL_AGENT_PORT
        args.seed = True
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
