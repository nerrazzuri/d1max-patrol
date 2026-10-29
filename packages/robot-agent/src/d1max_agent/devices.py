"""设备健康(W40,上装 V1.2):每台设备在线、离线、降级、故障(``d1max_contract.devices``),随遥测
每 10 s 带给站点。

收这几样:
- HAL 报的(``device_health``):上装的继电器板、喇叭、喇叭警笛;
- RTK:固定解在线;浮点、差分、单点、没有定位算降级;一直没数据、好一会儿没更新算离线;
- 在录的每一路相机:在录在线,断了故障(带 ffmpeg 的原因,口令打过码);
- 发件箱那块盘(上装 V1.2 的 1TB SSD):挂载点没挂上故障;剩不到 10% 降级;
- 网络上的设备(``links``:路由器、热像这类):TCP 连得上在线,连不上离线。只看「连不连得上」,
  不登录、不发东西。

收一次可能要等网络设备的超时(最多 :data:`CONNECT_TIMEOUT_S`):在后台跑(:meth:`refresh`),
遥测只带最近收好的那一份,不卡遥测。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from d1max_contract.devices import device

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 1.0
#: 发件箱那块盘剩不到这么多算降级。
LOW_FREE = 0.10
_RTK_FIX = {"float": "浮点解", "dgps": "差分", "single": "单点(没收到改正?)"}


def tcp_reachable(host: str, port: int, timeout: float = CONNECT_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_links(spec: str) -> dict[str, tuple[str, int]]:
    """``router=192.168.10.1:80,thermal=192.168.10.64:554`` → ``{名: (主机, 端口)}``。写错了抛
    ``ValueError``(代理不起,说清楚)。"""
    out: dict[str, tuple[str, int]] = {}
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        name, eq, addr = part.partition("=")
        host, colon, port = addr.rpartition(":")
        if not eq or not colon or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"--links 写成 名字=主机:端口,逗号隔开:{part!r}")
        device(f"link:{name}", "online")                 # 名字不对这里就抛
        out[name] = (host, int(port))
    return out


def camera_links(cameras: dict[str, dict[str, Any]]) -> dict[str, tuple[str, int]]:
    """``cameras.json`` 里的相机(热像)按地址查连不连得上(RTSP 缺省 554)。"""
    out = {}
    for name, v in cameras.items():
        u = urlsplit(v["url"])
        try:
            port = u.port or (322 if u.scheme == "rtsps" else 554)
        except ValueError:
            continue
        if u.hostname:
            out[name] = (u.hostname, port)
    return out


class DeviceHealth:
    def __init__(self, *, hal: Any, rtk: Any = None, recorder: Any = None,
                 outbox: Path | None = None, outbox_mount: str = "",
                 links: dict[str, tuple[str, int]] | None = None,
                 reach: Callable[[str, int], bool] = tcp_reachable,
                 ismount: Callable[[str], bool] = os.path.ismount,
                 disk_usage: Callable[[Path], Any] = shutil.disk_usage) -> None:
        self.hal, self.rtk, self.recorder = hal, rtk, recorder
        self.outbox, self.outbox_mount = outbox, outbox_mount
        self.links = dict(links or {})
        self._reach, self._ismount, self._disk_usage = reach, ismount, disk_usage
        #: 最近收好的一份(还没收过是 None)。
        self.last: list[dict[str, Any]] | None = None
        #: 上装主控板的温度、风扇(W46,:class:`~d1max_agent.boardhealth.BoardMonitor`)。
        self.board: Any = None
        #: 热像镜头被挡(W46,:class:`~d1max_agent.lens.LensWatch`)。
        self.lens: Any = None
        #: Nav2 后端(W47):就绪没有(``health()``)。
        self.nav2: Any = None

    def wanted(self) -> bool:
        """有没有东西可报(一样都没有就不往遥测里带)。"""
        return bool(callable(getattr(self.hal, "device_health", None)) or self.rtk is not None
                    or self.recorder is not None or self.outbox is not None or self.links
                    or self.board is not None or self.lens is not None
                    or self.nav2 is not None)

    async def refresh(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        fn = getattr(self.hal, "device_health", None)
        if callable(fn):
            try:
                out += list(fn())
            except Exception:                              # 别的照报
                log.exception("上装的设备健康读不出来")
        if self.rtk is not None:
            out.append(self._rtk())
        if self.recorder is not None:
            held = getattr(self.recorder, "held", set())
            for cam, why in self.recorder.camera_health().items():
                if cam in held:
                    out.append(device(f"camera:{cam}", "degraded", "上装过热,暂停录这一路"))
                    continue
                out.append(device(f"camera:{cam}", "error" if why else "online", why or "在录"))
        if self.board is not None:
            try:
                out += list(self.board.health())
            except Exception:                              # 别的照报
                log.exception("板子温度的设备健康读不出来")
        if self.lens is not None:
            out.append(self.lens.health())
        if self.nav2 is not None:
            out.append(self.nav2.health())
        if self.outbox is not None:
            out.append(self._disk())
        if self.links:
            names = list(self.links)
            ok = await asyncio.gather(*(asyncio.to_thread(self._reach, *self.links[n])
                                        for n in names))
            for n, up in zip(names, ok, strict=True):
                host, port = self.links[n]
                out.append(device(f"link:{n}", "online" if up else "offline",
                                  f"{host}:{port}" if up else f"连不上 {host}:{port}"))
        self.last = out
        return out

    def _rtk(self) -> dict[str, Any]:
        fix = self.rtk.latest()
        err = str(getattr(self.rtk, "error", "") or "")
        if fix is None:
            return device("rtk", "offline", err or "还没收到过定位")
        if fix.get("stale"):
            return device("rtk", "offline", err or "好一会儿没更新了")
        f = fix.get("fix")
        hd = fix.get("heading_deg")
        tail = f",朝向 {hd:.0f}°" if isinstance(hd, (int, float)) else ""
        if f == "fixed":
            return device("rtk", "online", "固定解" + tail)
        if f in _RTK_FIX:
            return device("rtk", "degraded", _RTK_FIX[f] + tail)
        return device("rtk", "degraded", "没有定位(天线、遮挡?)")

    def _disk(self) -> dict[str, Any]:
        assert self.outbox is not None
        if self.outbox_mount and not self._ismount(self.outbox_mount):
            return device("disk", "error", f"{self.outbox_mount} 没挂上(证据不往系统盘写)")
        try:
            u = self._disk_usage(self.outbox)
        except OSError as exc:
            return device("disk", "error", f"读不了盘:{exc}")
        free = u.free / u.total if u.total else 0.0
        gb = u.free / 2**30
        if free < LOW_FREE:
            return device("disk", "degraded", f"只剩 {free:.0%}({gb:.1f} GB)")
        return device("disk", "online", f"剩 {gb:.0f} GB")
