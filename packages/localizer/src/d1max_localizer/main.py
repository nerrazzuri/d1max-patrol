"""定位器节点入口(W09b):``d1max-localizer``,狗上跑在 ROS 的系统 Python 里(启动脚本
``deploy/d1max-localizer-start``),跟代理同一个账号(本机桥按账号认)。

接线:ROS 层把点云时刻、MOLA 的每帧估计喂给核心 → 客户端发给代理;代理请换先验 → 后端(重)起 MOLA;
代理请重定位 → 后端调 MOLA 的服务;每 :data:`TICK_S` 看一次门(卡住重启、重启后自己重定位)并把攒下的
报文发出去。MOLA 要等代理发来换先验才起。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from d1max_localizer.backend import MolaBackend
from d1max_localizer.client import BridgeClient
from d1max_localizer.core import LocalizerCore
from d1max_localizer.supervisor import MolaSupervisor, mola_command

log = logging.getLogger("d1max_localizer")

TICK_S = 0.1
DEFAULT_SOCKET = Path("/var/lib/d1max/agent/loc.sock")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="d1max-localizer", description="狗上的定位器节点(W09b)")
    p.add_argument("--socket", type=Path, default=DEFAULT_SOCKET,
                   help=f"代理的本机定位桥(默认 {DEFAULT_SOCKET})")
    p.add_argument("--prior-dir", type=Path, default=None,
                   help="代理没给先验目录时(狗按启动参数载的图)用的先验目录:"
                        "里面要有 prior.mm、frames.json")
    p.add_argument("--lidar-topic", default="/front_lidar")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


class Node:
    """把核心、客户端、后端、看管器、ROS 层接起来;``ros_factory`` 可以换成假的(测试)。"""

    def __init__(self, args: argparse.Namespace, *, ros_factory: Callable[..., Any],
                 command_for: Callable[[Path], Sequence[str]] | None = None,
                 reconnect_s: float = 1.0) -> None:
        self.args = args
        self.core = LocalizerCore(monotonic=time.monotonic)
        loop = asyncio.get_running_loop()
        self.ros = ros_factory(loop, lidar_topic=args.lidar_topic, on_scan=self.core.on_scan,
                               on_estimate=self._on_estimate)
        self.supervisor = MolaSupervisor(
            command_for=command_for or (lambda prior: mola_command(prior,
                                                                    lidar_topic=args.lidar_topic)),
            on_up=lambda: self.backend.on_up(), on_down=lambda why: self.backend.on_down(why))
        self.backend = MolaBackend(self.core, supervisor=self.supervisor,
                                   reloc_service=self.ros.relocalize,
                                   default_prior_dir=args.prior_dir)
        self.client = BridgeClient(args.socket, self.core, self.backend, reconnect_s=reconnect_s)
        self._stop = asyncio.Event()

    def _on_estimate(self, e: Any) -> None:
        self.backend.mola_output()
        self.core.on_estimate(e)
        self.client.flush()

    async def run(self) -> None:
        self.ros.start()
        link = asyncio.get_running_loop().create_task(self.client.run())
        try:
            while not self._stop.is_set():
                await self.backend.check()
                self.client.flush()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), TICK_S)
        finally:
            self.client.stop()
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(link, 2.0)
            await self.supervisor.stop()
            self.ros.shutdown()

    def stop(self) -> None:
        self._stop.set()


async def _main(args: argparse.Namespace) -> None:
    from d1max_localizer.mola import MolaRos  # 只在这里 import ROS
    node = Node(args, ros_factory=MolaRos)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, node.stop)
    await node.run()


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
