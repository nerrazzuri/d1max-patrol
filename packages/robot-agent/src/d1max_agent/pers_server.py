"""人员桥,代理这一头(W24)。跟障碍桥同一套规矩(:class:`d1max_agent.obs_server.ObsBridgeServer`),
只换报文(:mod:`d1max_contract.persbridge`)、套接字(``persons.sock``)、交给 :class:`PersonView`。

**跨板**(2026-10-27 用户定:我们的服务在自带的 RK3588 上,人员检测留在厂家 Orin 用它的 GPU):给了
``listen``(``--persons-listen HOST:PORT``)就改在狗内网上听 TCP,本机 Unix 套接字照旧是缺省。跨板时:

- **认密钥**:连上先发 ``challenge {nonce}``(每条连接现给的随机数),检测节点的 ``hello`` 要带
  ``mac = HMAC-SHA256(共享密钥, nonce, 名字)``;对不上回一句、断开。给了 ``peer`` 还要对端 IP
  对得上。
- **截图走桥**:两块板不共用盘 —— 检测节点先发 ``snap {name, jpeg_b64}``、再发引用它的 ``persons``;
  这里写进截图目录(``PersonView.snapshot_dir``,先写临时文件再改名;最多留
  :data:`MAX_REMOTE_SNAPS` 张,代理一直没收走的删最旧的),后面跟本机一样。
- **外参推过去**:检测节点要正在用的那张图的雷达外参测距(本机时它自己读代理的地图目录)。``frames()``
  回 ``(地图号:版本, frames.json 的内容)``;连上推一次,每拍看换没换图,换了再推。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_agent.obs_server import ObsBridgeServer
from d1max_contract import persbridge

log = logging.getLogger(__name__)

#: 跨板时截图目录里最多留几张(检测节点那头本机时也是这个量级)。
MAX_REMOTE_SNAPS = 50
MIN_KEY_BYTES = persbridge.MIN_KEY_BYTES
load_key = persbridge.load_key

Frames = Callable[[], "tuple[str, dict] | None"]


class PersonBridgeServer(ObsBridgeServer):
    BRIDGE: Any = persbridge
    DATA: Any = persbridge.Persons
    DATA_CB = "on_persons"
    WHAT = "人员桥"
    PEER = "人员检测节点"

    def __init__(self, path: Path | str, sink: Any, *, listen: tuple[str, int] | None = None,
                 key: bytes | None = None, peer: str | None = None,
                 frames: Frames | None = None, **kw: Any) -> None:
        super().__init__(path, sink, **kw)
        if listen is not None and (not key or len(key) < MIN_KEY_BYTES):
            raise ValueError(f"跨板人员桥要共享密钥(至少 {MIN_KEY_BYTES} 字节)")
        self.listen = listen
        self.key = key if listen is not None else None
        self.peer = peer
        self.frames = frames
        self._nonces: dict[int, str] = {}
        self._frames_ref: str | None = None

    @property
    def port(self) -> int:
        """跨板时实际听的端口(给的是 0 时系统挑一个;测试用)。"""
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self.listen is None:
            await super().start()
            return
        host, port = self.listen
        self._server = await asyncio.start_server(self._on_conn, host, port,
                                                  limit=self.max_line + 2)
        log.info("人员桥在 %s:%d 等人员检测节点(跨板,认密钥%s)", host, self.port,
                 f"、只认 {self.peer}" if self.peer else "")

    async def close(self) -> None:
        if self.listen is None:
            await super().close()
            return
        self._drop("代理收尾")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def tick(self) -> None:
        super().tick()
        if self.connected:
            self._push_frames()

    # ---- 跨板换的几步

    def _admit(self, writer: asyncio.StreamWriter) -> bool:
        if self.listen is None:
            return super()._admit(writer)
        peer = writer.get_extra_info("peername")
        ip = peer[0] if isinstance(peer, tuple) and peer else None
        if self.peer and ip != self.peer:
            log.warning("人员桥:%s 连上来了(只认 %s),断开", ip, self.peer)
            return False
        nonce = secrets.token_hex(16)
        self._nonces[id(writer)] = nonce
        try:
            writer.write(persbridge.encode(persbridge.Challenge(nonce=nonce)))
        except (ConnectionError, OSError, RuntimeError):
            return False
        return True

    def _hello_problem(self, hello: Any, writer: asyncio.StreamWriter) -> str:
        if self.listen is None:
            return ""
        nonce = self._nonces.pop(id(writer), None)
        assert self.key is not None
        want = persbridge.hello_mac(self.key, nonce, hello.name) if nonce else ""
        if not hello.mac or not want or not hmac.compare_digest(hello.mac, want):
            return "握手签名不对(两块板上的人员桥密钥不一样?)"
        return ""

    def _greeted(self) -> None:
        self._frames_ref = None
        self._push_frames()

    def _other(self, msg: Any) -> None:
        if isinstance(msg, persbridge.Snap):
            self._save_snap(msg)
            return
        super()._other(msg)

    # ---- 外参、截图

    def _push_frames(self) -> None:
        if self.listen is None or self.frames is None or self._writer is None:
            return
        try:
            got = self.frames()
        except Exception as exc:                         # noqa: BLE001 - 读不了下一拍再试
            log.warning("人员桥:外参读不了(先不推):%s", exc)
            return
        if got is None or got[0] == self._frames_ref:
            return
        ref, frames = got
        try:
            self._writer.write(persbridge.encode(persbridge.FramesMsg(ref=ref, frames=frames)))
        except Exception as exc:                         # noqa: BLE001 - 写不过去下一拍再试
            log.warning("人员桥:外参推不过去:%s", exc)
            return
        self._frames_ref = ref
        log.info("人员桥:把图 %s 的外参推给人员检测节点", ref)

    def _save_snap(self, m: Any) -> None:
        d = getattr(self.sink, "snapshot_dir", None)
        if d is None:
            return
        d = Path(d)
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".{m.name}.tmp"
            tmp.write_bytes(m.jpeg())
            os.replace(tmp, d / m.name)
            old = sorted(d.glob("*.jpg"), key=lambda p: (p.stat().st_mtime_ns, p.name))
            for p in old[:-MAX_REMOTE_SNAPS]:
                p.unlink(missing_ok=True)                # 代理一直没收走的:删最旧的
        except OSError:
            log.exception("人员桥:截图 %s 存不下", m.name)
