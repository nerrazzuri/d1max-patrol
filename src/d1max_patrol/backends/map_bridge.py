"""地图桥的客户端 —— 只读一条 TCP/JSONL 流,把占据栅格推给上层。

**和定位桥客户端(``LocalNavBackend`` 里那半截)的关键区别:坏帧的处置。**

定位帧坏了是安全问题:位姿错一米,机器人就撞上去了,所以那边宁可判丢
定位、停车。地图帧坏了只是**画面**少一帧 —— 建图 UI 上图晚长半秒,没有
任何东西会因此动起来。所以这里的规矩是:记一笔,继续读下一行。

这条差异是有意的,不是抄漏了。见 ``local_nav._pump_frames`` 的对照。

本类不是 ``NavBackend``:它不导航、不下任何指令,只是一路遥测。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from d1max_patrol.backends.base import EventEmitter, NavConnectionError
from d1max_patrol.protocol.map_frames import (
    PROTO_VERSION,
    MapFrame,
    MapHello,
    MapProtocolError,
    decode_frame,
)

#: 等 hello 的耐心。桥一连上就发 hello,拖这么久多半是连错了端口 ——
#: 比如连到一个只 accept 不说话的服务上,不设上限就会永远挂在那里。
HELLO_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class MapUpdate:
    """来了一张新图。"""

    frame: MapFrame


class MapBridgeClient(EventEmitter[MapUpdate]):
    """连地图桥,把每一帧广播给订阅者,同时留一份 ``latest``。

    ``latest`` 是给"半路进来的人"用的:UI 刷新时不必等下一帧才有图看。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8092) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._hello: MapHello | None = None
        self._latest: MapFrame | None = None
        self._bad_frames = 0

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> None:
        """连上并把 hello 校完。版本对不上宁可连不上,不留半死不活的状态。"""
        if self._writer is not None:
            return
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port)
        except OSError as exc:
            raise NavConnectionError(
                f"连不上地图桥 {self._host}:{self._port}: {exc}。"
                f"先确认 ROS 侧的 tools/ros2_map_bridge.py 在跑") from exc
        try:
            hello = await self._read_hello(reader)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise
        self._hello = hello
        self._reader, self._writer = reader, writer
        self._pump = asyncio.create_task(self._pump_frames())

    async def _read_hello(self, reader: asyncio.StreamReader) -> MapHello:
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            # Python 3.10 的 asyncio.TimeoutError 还不是内建那个的别名。
            raise NavConnectionError(
                f"地图桥 {self._host}:{self._port} 连上了但 {HELLO_TIMEOUT_S:g}s "
                f"内没发 hello") from exc
        except OSError as exc:
            raise NavConnectionError(f"读地图桥 hello 出错: {exc}") from exc
        if not line:
            raise NavConnectionError("地图桥连上就断了,没发 hello")
        try:
            frame = decode_frame(line)
        except MapProtocolError as exc:
            raise NavConnectionError(f"地图桥的第一帧不认识: {exc}") from exc
        if not isinstance(frame, MapHello):
            raise NavConnectionError(f"地图桥的第一帧应是 hello,实际是 {frame!r}")
        if frame.proto != PROTO_VERSION:
            raise NavConnectionError(
                f"地图桥协议版本 {frame.proto},本端要 {PROTO_VERSION}")
        return frame

    async def close(self) -> None:
        """关掉。没连上时调用是合法的 —— 收尾路径不该还要先判断一次。"""
        pump, self._pump = self._pump, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ---------------------------------------------------------------- 查询

    @property
    def latest(self) -> MapFrame | None:
        return self._latest

    @property
    def connected(self) -> bool:
        return self._writer is not None and self._pump is not None

    @property
    def topic(self) -> str:
        """桥在盯哪个话题。没连上时是空串。"""
        return self._hello.topic if self._hello else ""

    @property
    def bad_frames(self) -> int:
        """至今丢掉了多少帧坏数据。坏帧不抛,但也不能不留痕迹。"""
        return self._bad_frames

    # ---------------------------------------------------------------- 收流

    async def _pump_frames(self) -> None:
        reader = self._reader
        assert reader is not None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    self._pump = None       # 桥关了连接,connected 立刻变假
                    return
                try:
                    frame = decode_frame(line)
                except MapProtocolError:
                    # 地图只是画面,不是安全关键路径:坏一帧记一笔接着读。
                    # 定位桥那边相反 —— 位姿错了是会撞车的,那边要判丢定位。
                    self._bad_frames += 1
                    continue
                if isinstance(frame, MapFrame):
                    self._latest = frame
                    self.emit(MapUpdate(frame))
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            self._pump = None

    def __repr__(self) -> str:  # pragma: no cover - 只为调试好看
        return (f"<MapBridgeClient {self._host}:{self._port} "
                f"connected={self.connected}>")


__all__ = ["HELLO_TIMEOUT_S", "MapBridgeClient", "MapUpdate"]
