"""Nav2 转接,代理这一头(W47)。报文见 :mod:`d1max_contract.navbridge`。

照定位桥(:mod:`d1max_agent.locbridge`)的写法,只留要的:

- 代理监听 Unix 流式套接字(0600),转接节点来连;按对端进程账号认(``SO_PEERCRED``),
  不是同一个账号当场断。
- 第一行必须是这一版的 ``hello``;握手之后回一行 ``hello``,再把最近一份 ``map`` 补发过去。
- 一次只接一个;新来的时候旧的还有声就拒新的。
- 发送不等(不 ``drain``):积过 :data:`MAX_WBUF` 当它不读了、断开。
- 收到的:``ready`` → :attr:`ready`/:attr:`reason`;``cmd`` → :attr:`cmd`(带收到的时刻);
  ``goal_state`` → 排进 :attr:`goal_states`(后端每拍取走);``hb`` 只算有声。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import stat
import struct
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.navbridge import FROM_BRIDGE, MAX_LINE, PROTO, encode, parse

log = logging.getLogger(__name__)

HB_DEAD_S = 2.0
HB_EVERY_S = 1.0
HELLO_TIMEOUT_S = 2.0
MAX_WBUF = 512 * 1024


class NavBridgeServer:
    def __init__(self, path: Path | str, *, monotonic: Callable[[], float] = time.monotonic,
                 allowed_uid: int | None = None) -> None:
        self.path = Path(path)
        self._now = monotonic
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._heard = -1e9
        self._hb_seq = 0
        self._next_hb = 0.0
        self._map: dict[str, Any] | None = None
        #: 转接节点说 Nav2 就绪了没有、为什么没有。
        self.ready = False
        self.reason = "Nav2 转接节点没连上"
        #: 最近一条速度:``(收到的单调钟, t_ms, vx, vy, wz, goal_id, src, 到达号)``。
        self.cmd: tuple[Any, ...] | None = None
        #: 收到的报文按到达顺序编号(复查第三轮 P1:速度要比本目标的路线晚到才认)。
        self._rx = 0
        #: 每个目标第一条路线是第几条到的(只留最近几个)。
        self.plan_rx: dict[int, int] = {}
        self.goal_states: deque[dict[str, Any]] = deque(maxlen=64)
        #: Nav2 最近一次规划出的路径:``(目标号, ((x, y), …))``。
        self.plan: tuple[int, tuple[tuple[float, float], ...]] | None = None

    # ------------------------------------------------------------ 起停

    async def start(self) -> None:
        if self.path.exists() or self.path.is_symlink():
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise RuntimeError(f"{self.path} 已存在、又不是套接字,不碰它")
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path),
                                                       limit=MAX_LINE + 2)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.path.unlink(missing_ok=True)

    @property
    def connected(self) -> bool:
        return self._writer is not None and self._now() - self._heard <= HB_DEAD_S

    # ------------------------------------------------------------ 收

    def _peer_ok(self, writer: asyncio.StreamWriter) -> bool:
        sock = writer.get_extra_info("socket")
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except (OSError, AttributeError):
            return False
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid == self.allowed_uid

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self._peer_ok(writer):
            log.warning("Nav2 转接:对端不是同一个账号,断开")
            writer.close()
            return
        if self.connected:
            self._raw(writer, {"type": "error", "reason": "已经有一个转接节点连着"})
            writer.close()
            return
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)
            hello = parse(line, allowed=FROM_BRIDGE)
            if hello["type"] != "hello" or hello["proto"] != PROTO:
                raise ContractError(f"要 hello(proto {PROTO})")
        except (asyncio.TimeoutError, ContractError, ValueError) as exc:
            self._raw(writer, {"type": "error", "reason": str(exc)[:200]})
            writer.close()
            return
        old, self._writer = self._writer, writer
        if old is not None:
            old.close()
        self._heard = self._now()
        self.ready, self.reason = False, "Nav2 还没就绪"
        log.info("Nav2 转接节点连上了:%s %s", hello.get("name", ""), hello.get("version", ""))
        self.send({"type": "hello", "proto": PROTO})
        if self._map is not None:
            self.send(self._map)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:                        # 一行超长
                    log.warning("Nav2 转接:一行太长,断开")
                    break
                if not line:
                    break
                try:
                    self._on(parse(line, allowed=FROM_BRIDGE))
                except ContractError as exc:
                    log.warning("Nav2 转接:丢掉一行:%s", exc)
        finally:
            if self._writer is writer:
                self._writer = None
                self.ready, self.reason = False, "Nav2 转接节点断了"
                self.cmd = None
                log.warning("Nav2 转接节点断了")
            writer.close()

    def _on(self, m: dict[str, Any]) -> None:
        self._heard = self._now()
        self._rx += 1
        t = m["type"]
        if t == "ready":
            self.ready, self.reason = m["ok"], m.get("reason", "") or ("" if m["ok"] else "没就绪")
        elif t == "cmd":
            # 转接节点标了是哪个目标的(外审 R7);老的不标:None
            self.cmd = (self._now(), m["t_ms"], m["vx"], m["vy"], m["wz"], m.get("goal_id"),
                        m.get("src", "ctl"), self._rx)
        elif t == "goal_state":
            self.goal_states.append(m)
        elif t == "path":
            self.plan = (m["goal_id"], tuple((float(x), float(y)) for x, y in m["pts"]))
            if m["goal_id"] not in self.plan_rx:
                self.plan_rx[m["goal_id"]] = self._rx
                while len(self.plan_rx) > 16:
                    self.plan_rx.pop(next(iter(self.plan_rx)))
        elif t == "error":
            log.warning("Nav2 转接节点报错:%s", m["reason"])

    # ------------------------------------------------------------ 发

    def _raw(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        try:
            writer.write(encode(msg))
        except (ContractError, RuntimeError, OSError) as exc:
            log.warning("Nav2 转接:发不出去:%s", exc)

    def send(self, msg: dict[str, Any]) -> bool:
        """发一条;没连上、发送缓冲积太多(它不读了)回 False。"""
        if msg["type"] == "map":
            self._map = msg
        w = self._writer
        if w is None:
            return False
        if w.transport.get_write_buffer_size() > MAX_WBUF:
            log.warning("Nav2 转接节点不读了(发送缓冲 > %d 字节),断开", MAX_WBUF)
            w.close()
            self._writer = None
            self.ready, self.reason = False, "Nav2 转接节点不读了"
            return False
        self._raw(w, msg)
        return True

    def tick(self) -> None:
        """运行时每拍调:心跳。"""
        if self._now() >= self._next_hb:
            self._next_hb = self._now() + HB_EVERY_S
            self._hb_seq += 1
            self.send({"type": "hb", "seq": self._hb_seq})
