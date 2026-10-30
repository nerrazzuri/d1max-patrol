"""代理跟本机配网服务(W42,``d1max_patrol.bind``)说话:本机 unix 套接字,一行一个 JSON。

- 遥测里带 WiFi 状态(``net.wifi``:连的哪个、走 WiFi 还是 4G、有没有等站点确认的改动);
- 站点发来的 ``unbind``(解绑 = 恢复出厂)、``wifi_confirm``(确认 / 不同意换 WiFi)转给它。

配网服务没起、套接字不在:``net()`` 回 ``None``(遥测不带),命令回原因(站点记进命令账)。
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

SOCK = Path("/run/d1max/bind.sock")
#: WiFi 状态缓存多久(遥测每秒一份,不用每份都去问)。
CACHE_S = 2.0


class BindLink:
    def __init__(self, path: Path = SOCK, *, timeout_s: float = 3.0) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self._cache: tuple[float, dict[str, Any] | None] = (0.0, None)

    def _ask(self, msg: dict[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout_s)
            s.connect(str(self.path))
            s.sendall(json.dumps(msg).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        d = json.loads(buf or b"{}")
        return d if isinstance(d, dict) else {}

    def net(self) -> dict[str, Any] | None:
        at, val = self._cache
        if time.monotonic() - at < CACHE_S:
            return val
        try:
            d = self._ask({"op": "net"})
            val = d.get("net") if isinstance(d.get("net"), dict) else None
        except (OSError, ValueError):
            val = None
        self._cache = (time.monotonic(), val)
        return val

    def _cmd(self, msg: dict[str, Any]) -> str:
        try:
            d = self._ask(msg)
        except (OSError, ValueError) as exc:
            return f"bind_unavailable: {exc}"[:200]
        self._cache = (0.0, None)
        return "" if d.get("ok") else str(d.get("error") or "bind_failed")[:200]

    def unbind(self, cert: Any) -> str:
        return self._cmd({"op": "unbind", "cert": cert})

    def wifi_confirm(self, cid: Any, approve: Any) -> str:
        return self._cmd({"op": "wifi_confirm", "id": cid, "approve": approve is True})
