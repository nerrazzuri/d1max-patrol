"""本机定位桥,代理这一头(W09a,W08 决定 2)。报文见 :mod:`d1max_contract.locbridge`。

- 代理监听 Unix 流式套接字(权限 0600),定位器来连;**按对端进程的账号认**(``SO_PEERCRED``):必须跟
  代理同一个账号,别的当场断开 —— 别的进程注入不了假位姿。
- 第一行必须是这一版的 ``hello``,不对回一行 ``error`` 就断;握手之后回一行 ``hello``。
- 一次只接一个定位器:新来的时候旧的还活着(:data:`HB_DEAD_S` 内有声)就拒新的;旧的没声了就断掉旧的、
  接新的。
- 每行最多 ``MAX_LINE`` 字节,超了断开;不成形的一行丢掉、记日志,不断开。``pose``/``status``/``hb`` 的
  序号是一条递增的序列,不比上一条大的丢掉。
- 心跳::meth:`LocBridgeServer.tick`(运行时每拍调)每秒给定位器发一条;定位器 :data:`HB_DEAD_S` 没声
  就当它断了。
- 请求(换先验、重定位)带 ``req`` 号,等它的 ``reply``;没连上、等的时候断了抛
  ``LocalizerUnavailable``,超时抛 ``asyncio.TimeoutError``。
- **发送不等**(不 ``drain``):``tick`` 在每拍的循环里,定位器不读的话等发送会把整个代理卡住(内审
  应修 3)。发不出去的积在发送缓冲里,积过 :data:`MAX_WBUF` 就当定位器坏了、断开。
- 交给 ``sink`` 的回调出了错只记日志,不断开连接。
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d1max_agent.bridge_localizer import LocalizerUnavailable
from d1max_contract.errors import ContractError
from d1max_contract.locbridge import (
    MAX_LINE,
    PROTO,
    Error,
    Heartbeat,
    Hello,
    Pose,
    Reply,
    State,
    encode,
    parse,
)

log = logging.getLogger(__name__)

#: 定位器这么久没声就当它断了(秒)。
HB_DEAD_S = 2.0
HB_EVERY_S = 1.0
HELLO_TIMEOUT_S = 2.0
#: 给定位器的发送缓冲积过这么多(字节)就当它不读了、断开。
MAX_WBUF = 64 * 1024


@dataclass
class _Conn:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    heard: float
    seq: int = 0
    closed: bool = field(default=False)


class LocBridgeServer:
    def __init__(self, path: Path | str, sink: Any, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 allowed_uid: int | None = None) -> None:
        self.path = Path(path)
        #: 报文交给它:``on_connect``、``on_disconnect``、``on_pose``、``on_state``。
        self.sink = sink
        self._now = monotonic
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._server: asyncio.AbstractServer | None = None
        self._conn: _Conn | None = None
        self._req = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._hb_seq = 0
        self._hb_at = -1e18
        self._tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def start(self) -> None:
        if self.path.exists() or self.path.is_symlink():
            if not stat.S_ISSOCK(os.lstat(self.path).st_mode):
                raise FileExistsError(f"{self.path} 已经有了,而且不是套接字:不动它")
            self.path.unlink()                        # 上次没收拾的套接字
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._on_conn, str(self.path),
                                                       limit=MAX_LINE + 2)
        os.chmod(self.path, 0o600)
        log.info("本机定位桥在 %s 等定位器", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            self._drop(self._conn, "代理收尾")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for t in list(self._tasks):
            t.cancel()
        self.path.unlink(missing_ok=True)

    async def tick(self) -> None:
        """每拍:定位器没声太久就当它断了;每秒发一条心跳。不等发送,不让出。"""
        conn = self._conn
        if conn is None:
            return
        now = self._now()
        if now - conn.heard > HB_DEAD_S:
            self._drop(conn, f"定位器 {now - conn.heard:.1f} 秒没声,当它断了")
            return
        if now - self._hb_at >= HB_EVERY_S:
            self._hb_at = now
            self._hb_seq += 1
            self._say(conn, Heartbeat(seq=self._hb_seq))

    async def request(self, make: Callable[[int], Any], timeout_s: float) -> Reply:
        """发一条请求(``make(req)`` 造),等它的回复;``timeout_s`` 管从发到回的全程。"""
        conn = self._conn
        if conn is None:
            raise LocalizerUnavailable("定位器没连上")
        self._req += 1
        req = self._req
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        try:
            self._say(conn, make(req))
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            self._pending.pop(req, None)

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
        sock = writer.get_extra_info("socket")
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return None
        return struct.unpack("3i", raw)[1]

    def _alive(self, conn: _Conn) -> bool:
        return self._now() - conn.heard <= HB_DEAD_S

    def _busy(self) -> bool:
        return self._conn is not None and self._alive(self._conn)

    def _sink(self, name: str, *args: Any) -> None:
        try:
            getattr(self.sink, name)(*args)
        except Exception:
            log.exception("本机定位桥:%s 处理出错(连接照旧)", name)

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = self._peer_uid(writer)
        if uid != self.allowed_uid:
            log.warning("本机定位桥:账号 %s 连上来了(只认 %s),断开", uid, self.allowed_uid)
            writer.close()
            return
        if self._busy():
            self._refuse(writer, "已经有一个定位器连着")
            return
        try:
            first = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)
            hello = parse(first) if first else None
        except (asyncio.TimeoutError, ValueError, ContractError, ConnectionError, OSError) as exc:
            self._refuse(writer, f"握手不对:{exc}"[:200])
            return
        if not isinstance(hello, Hello) or hello.proto != PROTO:
            self._refuse(writer, f"第一行要是 hello(协议版本 {PROTO})")
            return
        if self._busy():                              # 等它握手的时候别的定位器连上了
            self._refuse(writer, "已经有一个定位器连着")
            return
        if self._conn is not None:
            self._drop(self._conn, "新的定位器连上来了,旧的没声了")
        conn = _Conn(reader=reader, writer=writer, heard=self._now())
        self._conn = conn
        log.info("定位器连上了:%s %s", hello.name, hello.version)
        self._say(conn, Hello(proto=PROTO))
        if self._conn is not conn:
            return                                    # 回 hello 就发不出去:已经断了
        self._sink("on_connect")
        try:
            await self._read(conn)
        finally:
            if self._conn is conn:
                self._drop(conn, "定位器断开了")

    async def _read(self, conn: _Conn) -> None:
        while not conn.closed:
            try:
                line = await conn.reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.warning("定位器发来的一行超过 %d 字节,断开", MAX_LINE)
                return
            except (ConnectionError, OSError):
                return
            if not line:
                return
            conn.heard = self._now()
            try:
                msg = parse(line)
            except ContractError as exc:
                log.warning("定位器发来一行不成形,丢掉:%s", exc)
                continue
            if isinstance(msg, Reply):
                fut = self._pending.get(msg.req)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                continue
            seq = getattr(msg, "seq", None)
            if not isinstance(msg, (Pose, State, Heartbeat)) or seq is None:
                continue
            if seq <= conn.seq:
                continue                              # 旧的、重的
            conn.seq = seq
            if isinstance(msg, Pose):
                self._sink("on_pose", msg)
            elif isinstance(msg, State):
                self._sink("on_state", msg)

    def _say(self, conn: _Conn, msg: Any) -> None:
        """写进发送缓冲就走(不等);积多了当它不读、断开。"""
        if conn.closed:
            return
        try:
            conn.writer.write(encode(msg))
        except (ConnectionError, OSError, RuntimeError) as exc:
            self._drop(conn, f"给定位器发不出去:{exc}")
            return
        backlog = conn.writer.transport.get_write_buffer_size()
        if backlog > MAX_WBUF:
            self._drop(conn, f"定位器不读我们发的(积了 {backlog} 字节),断开")

    @staticmethod
    def _refuse(writer: asyncio.StreamWriter, why: str) -> None:
        """回一句错误就断(关的时候把这一句发完,不等)。"""
        try:
            writer.write(encode(Error(reason=why[:200])))
        except (ConnectionError, OSError, RuntimeError):
            pass
        writer.close()

    def _drop(self, conn: _Conn, why: str) -> None:
        conn.closed = True
        if self._conn is conn:
            self._conn = None
            log.warning("本机定位桥:%s", why)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LocalizerUnavailable(why))
            self._sink("on_disconnect")
        conn.writer.close()
