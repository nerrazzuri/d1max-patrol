"""一台假的定位桥 —— 对应 ``tools/ros2_pose_bridge.py``。

**它不自己编位姿,而是持 ``SimAgentServer`` 的引用去读。** 这一点是刻意的:
仿真里如果两边各存一份位姿,它们迟早不一致,而且不一致的时候没人会发现 ——
测试会同时"通过",因为两边各自自洽。真机上定位和运动是同一台机器的两个观测,
仿真里也必须是同一份状态的两个视图。

它与真机的差别只有一处,而且是有意的:真机上定位器的位姿和 SDK 的腿式里程会
**慢慢分开**(腿式里程会漂),仿真里两者恒等。要测交叉校验就用 ``jump()``
人为把它们拉开 —— 靠"仿真自然漂移"来测漂移处理,测的是仿真器自己的随机数。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from d1max_patrol.protocol.pose_frames import (
    DEFAULT_HZ,
    encode_hello,
    encode_pose,
    encode_pose_lost,
)

from .agent_server import SimAgentServer


class SimPoseServer:
    """假定位桥:把 ``SimAgentServer`` 的位姿按固定频率往外发。"""

    def __init__(
        self,
        agent: SimAgentServer,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        map_frame: str = "loc_map",
        base_frame: str = "base_link",
        hz: float = DEFAULT_HZ,
    ) -> None:
        self._agent = agent
        self._host = host
        self._port = port
        self._map_frame = map_frame
        self._base_frame = base_frame
        self._interval = 1.0 / hz

        #: True 时改发 ``ok:false`` —— 模拟定位器还活着但查不到 TF。
        self.lost = False
        self._reason = "sim"

        #: True 时干脆不发 —— 模拟定位器进程挂了。这两种要分开测:
        #: 前者上层看得见"我丢了",后者上层只能靠超时发现。
        self.frozen = False
        #: 相对 agent 位姿的偏移。``jump()`` 往里写,用来测交叉校验。
        self.offset_x = 0.0
        self.offset_y = 0.0

        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # -------------------------------------------------------------- 生命周期

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("还没启动")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for writer in list(self._clients):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> SimPoseServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # -------------------------------------------------------------- 故障注入

    def drop_localization(self, reason: str = "sim: lookup failed") -> None:
        """开始发 ``ok:false``。定位器活着,但查不到 TF。"""
        self.lost = True
        self._reason = reason

    def restore_localization(self) -> None:
        self.lost = False

    def freeze(self) -> None:
        """停止发送。模拟定位器进程挂掉 —— 上层只能靠超时发现。"""
        self.frozen = True

    def thaw(self) -> None:
        self.frozen = False

    def jump(self, dx: float, dy: float) -> None:
        """位姿跳变。定位器重定位到错的地方时就是这个样子。"""
        self.offset_x += dx
        self.offset_y += dy

    # -------------------------------------------------------------- 客户端

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            writer.write(encode_hello(self._map_frame, self._base_frame))
            await writer.drain()
            pump = asyncio.create_task(self._pose_loop(writer))
            self._tasks.add(pump)
            try:
                # 单向协议:客户端什么都不该发。读到 EOF 就收摊。
                await reader.read()
            finally:
                pump.cancel()
                self._tasks.discard(pump)
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _pose_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            if not self.frozen:
                ts = int(time.time() * 1000)
                if self.lost:
                    line = encode_pose_lost(self._reason, ts)
                else:
                    line = encode_pose(
                        self._agent.x + self.offset_x,
                        self._agent.y + self.offset_y,
                        self._agent.yaw,
                        ts,
                    )
                with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                    writer.write(line)
                    await writer.drain()
            await asyncio.sleep(self._interval)


__all__ = ["SimPoseServer"]
