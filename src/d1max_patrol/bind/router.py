"""上装路由器(W42 §8)。

配网服务要让路由器:连现场 WiFi(WiFi 客户端)、说现在走哪条线、忘掉现场 WiFi(清机)、出厂时写热点。
G806w 的 AT 指令只能查、不能设 WiFi 客户端(说明书第 10 章),**程序怎么改要等硬件 / 厂家答复**;
先有一份假的(测试、演示站点用),G806w 那份等答复再写。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from d1max_patrol.bind.paths import read_json, write_json


@dataclass(frozen=True)
class WifiSeen:
    """路由器扫到的一个 WiFi(它只连 2.4 GHz,扫出来的就是它连得上的)。"""

    ssid: str
    signal: int          # dBm(-30 很强、-90 很弱)
    secured: bool        # 要不要密码


@dataclass(frozen=True)
class RouterStatus:
    ssid: str            # WiFi 客户端配的那个(空 = 没配)
    link: str            # 现在走哪条线:wifi / 4g / none


class Router(Protocol):
    def connect_wifi(self, ssid: str, psk: str) -> None: ...
    def forget_wifi(self) -> None: ...
    def status(self) -> RouterStatus: ...
    def set_hotspot(self, ssid: str, psk: str) -> None: ...
    def scan(self) -> list[WifiSeen]: ...


class FakeRouter:
    """假路由器:``reachable`` 里的 WiFi 名算连得上(``None`` = 都连得上),别的连不上(有 4G 就走 4G)。
    状态可以存文件
    (演示站点用:配网服务、演示脚本两个进程看同一份)。"""

    #: 演示、测试用的「附近的 WiFi」。
    NEARBY = (WifiSeen("Office-2.4G", -48, True), WifiSeen("Office-Guest", -61, False),
              WifiSeen("Warehouse", -77, True))

    def __init__(self, *, reachable: set[str] | None = None, has_4g: bool = False,
                 path: Path | None = None, nearby: tuple[WifiSeen, ...] | None = None) -> None:
        self.reachable = reachable
        self.nearby = self.NEARBY if nearby is None else nearby
        self.has_4g = has_4g
        self._path = path
        self._lock = threading.Lock()
        d = read_json(path, {}) if path is not None else {}
        self.ssid: str = d.get("ssid", "")
        self.psk: str = d.get("psk", "")
        self.hotspot: tuple[str, str] = tuple(d.get("hotspot", ("", "")))  # type: ignore[assignment]
        self.calls: list[tuple[str, ...]] = []

    def _save(self) -> None:
        if self._path is not None:
            write_json(self._path, {"ssid": self.ssid, "psk": self.psk,
                                    "hotspot": list(self.hotspot)})

    def connect_wifi(self, ssid: str, psk: str) -> None:
        with self._lock:
            self.ssid, self.psk = ssid, psk
            self.calls.append(("connect", ssid))
            self._save()

    def forget_wifi(self) -> None:
        with self._lock:
            self.ssid, self.psk = "", ""
            self.calls.append(("forget",))
            self._save()

    def status(self) -> RouterStatus:
        with self._lock:
            if self.ssid and (self.reachable is None or self.ssid in self.reachable):
                return RouterStatus(self.ssid, "wifi")
            return RouterStatus(self.ssid, "4g" if self.has_4g else "none")

    def scan(self) -> list[WifiSeen]:
        with self._lock:
            self.calls.append(("scan",))
            return list(self.nearby)

    def set_hotspot(self, ssid: str, psk: str) -> None:
        with self._lock:
            self.hotspot = (ssid, psk)
            self.calls.append(("hotspot", ssid))
            self._save()
