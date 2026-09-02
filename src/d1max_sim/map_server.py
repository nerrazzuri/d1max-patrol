"""一台假的地图桥 —— 对应 ``tools/ros2_map_bridge.py``。

**为什么非要有它:** 建图 UI 要展示的核心现象是"图在一格格长出来"。
真机上这个现象来自 slam_toolbox,而 slam_toolbox 要 ROS、要雷达、要一台
真狗。没有这个假桥,整条建图 UI 的路径在真机到场之前一行都测不了 ——
而真机只有一两天,拿来调 UI 的像素是最亏的用法。

``reveal()`` 就是这个现象的开关:调一次,一部分未知格变成已知,下一帧
发出去的图就比上一帧多长一块。UI 那边看到的和真建图时看到的是同一件事。

它跟真桥的差别只有一处,而且是有意的:真桥的降频是"距上次发送不足
1/max_hz 就整帧丢掉",这里是固定周期地发。仿真里没有"图更新得比链路快"
这个问题,补一套降频只会让测试去测仿真器自己的定时器。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from d1max_patrol.protocol.map_frames import encode_hello, encode_map

#: 未知 / 空闲 / 占据。与 ROS 的 OccupancyGrid 约定一致。
UNKNOWN = -1
FREE = 0
OCCUPIED = 100


class SimMapServer:
    """假地图桥:按固定频率把当前这张图发给所有客户端。

    图一开始全是未知(-1),四周一圈墙在 ``reveal()`` 露出来时才可见 ——
    这样"露出来的部分"里既有空地也有障碍,画出来才像张图,而不是一块
    纯色。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        hz: float = 2.0,
        width: int = 40,
        height: int = 40,
        resolution: float = 0.05,
        origin: tuple[float, float] = (-1.0, -1.0),
        topic: str = "/map",
    ) -> None:
        self._host = host
        self._port = port
        self._interval = 1.0 / hz
        self._topic = topic

        self.width = width
        self.height = height
        self.resolution = resolution
        self.origin = origin

        #: 完整的那张图 —— 建好之后应该长什么样。``reveal`` 从这里搬。
        self._truth = _draw_room(width, height)
        #: 当前露出来的那张。发出去的是它。
        self._cells = [UNKNOWN] * (width * height)
        self._revealed = 0.0

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

    async def __aenter__(self) -> SimMapServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # ---------------------------------------------------------------- 造图

    @property
    def cells(self) -> tuple[int, ...]:
        """当前这张图的快照。测试用来对答案。"""
        return tuple(self._cells)

    @property
    def unknown_count(self) -> int:
        return self._cells.count(UNKNOWN)

    def reveal(self, fraction: float) -> None:
        """把前 ``fraction`` 比例的格子从"未知"变成真值。

        按行推进而不是随机撒点:随机撒出来的图是雪花,看不出"扫过了哪里",
        而按行推进跟真建图时"走过一片就亮一片"的观感对得上。

        只增不减 —— 传比当前小的比例不会把已经露出来的图缩回去。真建图
        不会倒着走,UI 上图突然变少只会让人以为程序坏了。
        """
        target = max(self._revealed, min(1.0, max(0.0, fraction)))
        self._revealed = target
        upto = int(round(target * len(self._cells)))
        for index in range(upto):
            self._cells[index] = self._truth[index]

    def set_cell(self, x: int, y: int, value: int) -> None:
        """改一格。行优先,``(0, 0)`` 是图的左下角 —— 与 ROS 一致。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) 超出 {self.width}x{self.height}")
        if not UNKNOWN <= value <= OCCUPIED:
            raise ValueError(f"占据值 {value} 越界,应在 {UNKNOWN}..{OCCUPIED}")
        self._cells[y * self.width + x] = value

    # -------------------------------------------------------------- 客户端

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            writer.write(encode_hello(self._topic))
            await writer.drain()
            pump = asyncio.create_task(self._map_loop(writer))
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

    async def _map_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            line = encode_map(
                self.width, self.height, self.resolution,
                (self.origin[0], self.origin[1], 0.0),
                list(self._cells), int(time.time() * 1000),
            )
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.write(line)
                await writer.drain()
            await asyncio.sleep(self._interval)


def _draw_room(width: int, height: int) -> list[int]:
    """一间四面有墙的空房间 —— 建完图之后该长的样子。"""
    cells = [FREE] * (width * height)
    for x in range(width):
        cells[x] = OCCUPIED
        cells[(height - 1) * width + x] = OCCUPIED
    for y in range(height):
        cells[y * width] = OCCUPIED
        cells[y * width + width - 1] = OCCUPIED
    return cells


__all__ = ["FREE", "OCCUPIED", "UNKNOWN", "SimMapServer"]
