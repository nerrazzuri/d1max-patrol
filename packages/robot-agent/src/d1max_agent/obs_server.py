"""本机障碍桥,代理这一头(W11 设计稿 §3)。报文见 :mod:`d1max_contract.obsbridge`。

跟定位桥(:mod:`d1max_agent.locbridge`)同一套规矩:Unix 流式套接字(0600)、**按对端账号认**
(``SO_PEERCRED``,跟代理同一个账号)、第一行这一版的 ``hello``、一次只接一个感知节点(旧的还有声就
拒新的)、一行超长断开、不成形的丢掉;``grid`` 的序号要递增。感知只往这边说,这边只回 ``hello``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import stat
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract import obsbridge
from d1max_contract.errors import ContractError
from d1max_contract.obsbridge import MAX_LINE

log = logging.getLogger(__name__)

HB_DEAD_S = 2.0
HELLO_TIMEOUT_S = 2.0


class ObsBridgeServer:
    #: 哪一座桥(W24:人员桥是它的子类,只换这几样)。
    BRIDGE: Any = obsbridge
    DATA: Any = obsbridge.Grid
    DATA_CB = "on_grid"
    WHAT = "本机障碍桥"
    PEER = "感知节点"

    def __init__(self, path: Path | str, sink: Any, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 allowed_uid: int | None = None) -> None:
        self.path = Path(path)
        #: 报文交给它(:class:`d1max_agent.obstacles.ObstacleView`):
        #: ``on_grid``、``on_connect``、``on_disconnect``。
        self.sink = sink
        self._now = monotonic
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._heard = -1e18
        self._seq = 0

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def start(self) -> None:
        if self.path.exists() or self.path.is_symlink():
            if not stat.S_ISSOCK(os.lstat(self.path).st_mode):
                raise FileExistsError(f"{self.path} 已经有了,而且不是套接字:不动它")
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._on_conn, str(self.path),
                                                       limit=MAX_LINE + 2)
        os.chmod(self.path, 0o600)
        log.info("%s在 %s 等%s", self.WHAT, self.path, self.PEER)

    async def close(self) -> None:
        self._drop("代理收尾")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.path.unlink(missing_ok=True)

    def tick(self) -> None:
        """每拍:感知没声太久就当它断了。"""
        if self._writer is not None and self._now() - self._heard > HB_DEAD_S:
            self._drop(f"{self.PEER} {self._now() - self._heard:.1f} 秒没声,当它断了")

    @staticmethod
    def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
        sock = writer.get_extra_info("socket")
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return None
        return struct.unpack("3i", raw)[1]

    def _sink(self, name: str, *args: Any) -> None:
        try:
            getattr(self.sink, name)(*args)
        except Exception:
            log.exception("%s:%s 处理出错(连接照旧)", self.WHAT, name)

    def _refuse(self, writer: asyncio.StreamWriter, why: str) -> None:
        try:
            writer.write(self.BRIDGE.encode(self.BRIDGE.Error(reason=why[:200])))
        except (ConnectionError, OSError, RuntimeError):
            pass
        writer.close()

    def _busy(self) -> bool:
        return self._writer is not None and self._now() - self._heard <= HB_DEAD_S

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = self._peer_uid(writer)
        if uid != self.allowed_uid:
            log.warning("%s:账号 %s 连上来了(只认 %s),断开", self.WHAT, uid, self.allowed_uid)
            writer.close()
            return
        if self._busy():
            self._refuse(writer, f"已经有一个{self.PEER}连着")
            return
        try:
            first = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)
            hello = self.BRIDGE.parse(first) if first else None
        except (asyncio.TimeoutError, ValueError, ContractError, ConnectionError, OSError) as exc:
            self._refuse(writer, f"握手不对:{exc}"[:200])
            return
        if not isinstance(hello, self.BRIDGE.Hello) or hello.proto != self.BRIDGE.PROTO:
            self._refuse(writer, f"第一行要是 hello(协议版本 {self.BRIDGE.PROTO})")
            return
        if self._busy():
            self._refuse(writer, f"已经有一个{self.PEER}连着")
            return
        self._drop(f"新的{self.PEER}连上来了,旧的没声了")
        self._writer, self._heard, self._seq = writer, self._now(), 0
        try:
            writer.write(self.BRIDGE.encode(self.BRIDGE.Hello(proto=self.BRIDGE.PROTO)))
        except (ConnectionError, OSError, RuntimeError):
            self._drop("回 hello 就发不出去")
            return
        log.info("%s连上了:%s %s", self.PEER, hello.name, hello.version)
        self._sink("on_connect")
        try:
            while self._writer is writer:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("%s发来的一行超过 %d 字节,断开", self.PEER, MAX_LINE)
                    return
                except (ConnectionError, OSError):
                    return
                if not line:
                    return
                self._heard = self._now()
                try:
                    msg = self.BRIDGE.parse(line)
                except ContractError as exc:
                    log.warning("%s发来一行不成形,丢掉:%s", self.PEER, exc)
                    continue
                if isinstance(msg, self.DATA):
                    if msg.seq <= self._seq:
                        continue                         # 旧的、重的
                    self._seq = msg.seq
                    self._sink(self.DATA_CB, msg)
                elif not isinstance(msg, self.BRIDGE.Heartbeat):
                    continue
        finally:
            if self._writer is writer:
                self._drop(f"{self.PEER}断开了")

    def _drop(self, why: str) -> None:
        w, self._writer = self._writer, None
        if w is None:
            return
        log.warning("%s:%s", self.WHAT, why)
        self._sink("on_disconnect")
        w.close()
