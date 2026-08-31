"""python -m d1max_sim —— 起一台仿真 D1 Max 导航设备。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from d1max_patrol.protocol.nav_types import Pose, Waypoint

from .nav_server import SimNavServer


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max_sim", description="D1 Max 自主导航仿真器")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10010)
    p.add_argument("--tick-hz", type=float, default=50.0)
    p.add_argument("--seed", action="store_true",
                   help="预置一张地图与一条三点巡检路径,便于演示")
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
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
