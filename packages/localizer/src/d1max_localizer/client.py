"""本机定位桥,定位器这一头(W09b 决定 1;代理那一头与报文见 W09a)。

- 连代理的 Unix 套接字,第一行 ``hello``,等代理回 ``hello``;断了 :data:`RECONNECT_S` 后重连,连上
  把当前状态再发一遍(:meth:`LocalizerCore.hello`)。
- 发:核心攒下的 ``pose``/``status``(:meth:`flush`,适配层每喂一帧调一次)、每 :data:`HB_EVERY_S`
  一条心跳。序号由这里统一编(``pose``/``status``/``hb`` 在代理那边是一条递增的序列)。发送不等
  (不 ``drain``):代理不读、积过 :data:`MAX_WBUF` 就断开重连。
- 收:``set_prior`` 与 ``relocalize`` **当场回「收下没有」**(W09a 契约:回复要快,代理重定位只等 1 s);
  真正的活由后端做(换先验 = 重启 MOLA,载图的进度走 ``status initializing``)。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import Any, Protocol

from d1max_contract.errors import ContractError
from d1max_contract.locbridge import (
    MAX_LINE,
    PROTO,
    Error,
    Heartbeat,
    Hello,
    Relocalize,
    Reply,
    SetPrior,
    encode,
    parse,
)
from d1max_localizer.core import LocalizerCore

log = logging.getLogger(__name__)

RECONNECT_S = 1.0
HB_EVERY_S = 1.0
HELLO_TIMEOUT_S = 2.0
#: 后端「收下没有」最多等这么久(代理重定位只等 1 s)。
ACK_TIMEOUT_S = 0.8
MAX_WBUF = 64 * 1024


class Backend(Protocol):
    async def load_prior(self, map_ref: tuple[str, str], dir: str) -> str:
        """换先验:收下回空串(之后自己去载,载好了调核心的 ``prior_loaded``),不收回原因。要快。"""

    async def relocalize(self, x: float, y: float, yaw: float, sigma: float, *,
                         req: int | None = None, human: bool = True) -> str:
        """按平面位姿重定位:MOLA 收下(或者先记着,它起来再下发)回空串,不收回原因;收下时由后端告诉
        核心(``relocalized``)。要快。"""


class BridgeClient:
    def __init__(self, path: Path | str, core: LocalizerCore, backend: Backend, *,
                 name: str = "d1max-localizer", version: str = "",
                 reconnect_s: float = RECONNECT_S, hb_every_s: float = HB_EVERY_S) -> None:
        self.path = Path(path)
        self.core = core
        self.backend = backend
        self.name, self.version = name, version
        self.reconnect_s, self.hb_every_s = reconnect_s, hb_every_s
        self._writer: asyncio.StreamWriter | None = None
        self._seq = 0
        self._stop = asyncio.Event()
        self.error = ""

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    def flush(self) -> None:
        """让核心判一遍状态,把攒下的报文发出去;没连上就丢(位姿过时了没用;状态连上时再发一遍)。"""
        self.core.tick()
        msgs = self.core.drain()
        if self.connected:
            for m in msgs:
                self._send(m)

    def stop(self) -> None:
        self._stop.set()
        if self._writer is not None:
            self._writer.close()

    async def run(self) -> None:
        """一直连着;断了过一会儿重连。"""
        while not self._stop.is_set():
            try:
                await self._session()
            except (OSError, ConnectionError) as exc:
                self.error = f"连不上本机定位桥:{exc}"
                log.warning("%s", self.error)
            except Exception:
                log.exception("本机定位桥这一趟出错了,过一会儿重连")
            finally:
                if self._writer is not None:
                    self._writer.close()
                    self._writer = None
            try:
                await asyncio.wait_for(self._stop.wait(), self.reconnect_s)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ 内部

    async def _session(self) -> None:
        reader, writer = await asyncio.open_unix_connection(str(self.path), limit=MAX_LINE + 2)
        self._writer = writer
        writer.write(encode(Hello(proto=PROTO, name=self.name, version=self.version)))
        line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)
        got = parse(line) if line else None
        if not isinstance(got, Hello):
            self.error = got.reason if isinstance(got, Error) else "代理没回 hello"
            log.warning("本机定位桥握手不成:%s", self.error)
            return
        self.error = ""
        log.info("连上本机定位桥了")
        for m in self.core.hello():
            self._send(m)
        hb = asyncio.get_running_loop().create_task(self._heartbeat())
        try:
            await self._read(reader)
        finally:
            hb.cancel()

    async def _heartbeat(self) -> None:
        while self.connected:
            self._send(Heartbeat(seq=1))
            await asyncio.sleep(self.hb_every_s)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while not self._stop.is_set():
            try:
                line = await reader.readline()
            except (ValueError, ConnectionError, OSError):
                return
            if not line:
                return
            try:
                msg = parse(line)
            except ContractError as exc:
                log.warning("代理发来一行不成形,丢掉:%s", exc)
                continue
            if isinstance(msg, SetPrior):
                why = await self._ask(self.backend.load_prior((msg.map_id, msg.map_version),
                                                              msg.dir))
                self._send(Reply(req=msg.req, ok=not why, reason=why))
            elif isinstance(msg, Relocalize):
                self._send(Reply(req=msg.req, **await self._reloc(msg)))
            elif isinstance(msg, Error):
                self.error = msg.reason
                log.warning("代理说:%s", msg.reason)
            self.flush()

    async def _reloc(self, m: Relocalize) -> dict[str, Any]:
        if self.core.map_ref != (m.map_id, m.map_version):
            return {"ok": False, "reason": "定位器载的不是这张图"}
        if not self.core.can_relocalize():
            return {"ok": False, "reason": "先验还没载好"}
        why = await self._ask(self.backend.relocalize(m.x, m.y, m.yaw, m.sigma_xy, req=m.req,
                                                      human=True))
        if why:
            return {"ok": False, "reason": why}
        return {"ok": True, "reason": ""}

    @staticmethod
    async def _ask(coro: Any) -> str:
        try:
            return await asyncio.wait_for(coro, ACK_TIMEOUT_S)
        except asyncio.TimeoutError:
            return "定位程序没及时答应"
        except Exception as exc:
            log.exception("后端出错")
            return f"定位程序出错:{exc}"[:200]

    def _send(self, msg: Any) -> None:
        w = self._writer
        if w is None or w.is_closing():
            return
        if hasattr(msg, "seq"):
            self._seq += 1
            msg = dataclasses.replace(msg, seq=self._seq)
        try:
            w.write(encode(msg))
        except (ConnectionError, OSError, RuntimeError) as exc:
            log.warning("给代理发不出去:%s", exc)
            w.close()
            return
        if w.transport.get_write_buffer_size() > MAX_WBUF:
            log.warning("代理不读我们发的,断开重连")
            w.close()
