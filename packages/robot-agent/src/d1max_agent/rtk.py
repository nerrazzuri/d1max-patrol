"""狗上的 RTK 来源(W09e 决定 3、4;决策 21:大概率用自己的,厂家的先不排除 —— 做成接口、配置里选)。

- :class:`OwnRtk`(``--rtk own``):我们自己的串口驱动。读线程按行解 NMEA **GGA**(定位质量、卫星数、
  HDOP、经纬高、差分龄期)与 **GST**(经纬标准差);:meth:`feed` 把站点转来的 RTCM 原样写进串口。
  起来时按配置发一串初始化命令(默认没有 —— 模组命令没核过,真机项)。**用它的前提是停掉厂家驱动**
  (它独占串口),这一步归装机、由人定,这里不碰。
- :class:`VendorRtk`(``--rtk vendor``):读厂家驱动发的 ``/rtk_pvh``。代理没有 ROS,经辅助进程
  ``deploy/d1max-rtk-vendor``(ROS 系统 Python,一行一条 JSON 吐到标准输出)。改正走厂家那一套,
  :meth:`feed` 什么都不做。

两种都给 :meth:`latest`:``{"fix", "lat", "lon", "alt", "sats", "hdop", "std_h_m", "age_s",
"stamp_ms", "stale"}``(``fix`` ∈ ``none|single|dgps|float|fixed``;超过 :data:`STALE_S` 没更新
``stale`` 为真)。
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

log = logging.getLogger(__name__)

#: 多久没更新算过时。
STALE_S = 2.0
#: 串口断了多久再开。
REOPEN_S = 3.0
#: NMEA GGA 的定位质量 → 我们的说法。
_GGA_FIX = {0: "none", 1: "single", 2: "dgps", 4: "fixed", 5: "float", 6: "none"}
#: 厂家 ``/rtk_pvh`` 的 ``pos_type``(Unicore 风格)→ 我们的说法。
_POS_TYPE = {0: "none", 16: "single", 17: "dgps", 18: "dgps", 32: "float", 33: "float",
             34: "float", 48: "fixed", 49: "fixed", 50: "fixed"}


def _nmea_ok(line: str) -> str | None:
    """校验和对的话回 ``*`` 之前、``$`` 之后的那段;不对 None。"""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body, _, cs = line[1:].rpartition("*")
    x = 0
    for ch in body:
        x ^= ord(ch)
    try:
        return body if int(cs[:2], 16) == x else None
    except ValueError:
        return None


def _deg(v: str, hemi: str, width: int) -> float | None:
    """``ddmm.mmmm`` / ``dddmm.mmmm`` + N/S/E/W → 度。"""
    if not v or len(v) < width + 2:
        return None
    try:
        d = int(v[:width]) + float(v[width:]) / 60.0
    except ValueError:
        return None
    return -d if hemi in ("S", "W") else d


def parse_gga(body: str) -> dict[str, Any] | None:
    f = body.split(",")
    if len(f) < 15 or not f[0].endswith("GGA"):
        return None
    try:
        q = int(f[6] or 0)
    except ValueError:
        return None
    fix = _GGA_FIX.get(q, "none")
    lat, lon = _deg(f[2], f[3], 2), _deg(f[4], f[5], 3)
    if fix != "none" and (lat is None or lon is None):
        return None

    def num(s: str) -> float | None:
        try:
            v = float(s)
        except ValueError:
            return None
        return v if math.isfinite(v) else None
    alt, sep = num(f[9]), num(f[11])
    return {"fix": fix, "lat": lat, "lon": lon,
            "alt": None if alt is None else alt + (sep or 0.0),
            "sats": int(f[7]) if f[7].isdigit() else 0, "hdop": num(f[8]),
            "age_s": num(f[13])}


def parse_gst(body: str) -> float | None:
    """GST → 水平标准差(米):纬度、经度标准差的平方和开方。"""
    f = body.split(",")
    if len(f) < 8 or not f[0].endswith("GST"):
        return None
    try:
        slat, slon = float(f[6]), float(f[7])
    except ValueError:
        return None
    return math.hypot(slat, slon)


class _Base:
    kind = ""

    def __init__(self, *, now_ms: Callable[[], int], mono: Callable[[], float]) -> None:
        self._now = now_ms
        self._mono = mono
        self._lock = threading.Lock()
        self._fix: dict[str, Any] | None = None
        self._fix_mono = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    def _set(self, fix: dict[str, Any]) -> None:
        with self._lock:
            self._fix = fix | {"stamp_ms": self._now()}
            self._fix_mono = self._mono()

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            if self._fix is None:
                return None
            return self._fix | {"stale": self._mono() - self._fix_mono > STALE_S}

    def feed(self, data: bytes) -> None:
        raise NotImplementedError

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"rtk-{self.kind}")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._closing()
        if self._thread is not None:
            self._thread.join(5)

    def _closing(self) -> None:
        pass

    def _run(self) -> None:
        raise NotImplementedError


class OwnRtk(_Base):
    kind = "own"

    def __init__(self, device: str, baud: int, *, init: Sequence[str] = (),
                 now_ms: Callable[[], int], mono: Callable[[], float] = time.monotonic,
                 open_port: Callable[[str, int], int] | None = None) -> None:
        super().__init__(now_ms=now_ms, mono=mono)
        from d1max_contract.serialport import BAUDS, open_serial
        if baud not in BAUDS:
            raise ValueError(f"RTK 串口的波特率 {baud} 不认")
        self.device, self.baud, self.init = device, baud, list(init)
        self._open = open_port or open_serial
        self._fd: int | None = None
        self._wlock = threading.Lock()
        self._std: float | None = None
        self.fed_bytes = 0
        self.dropped_bytes = 0

    def feed(self, data: bytes) -> None:
        """站点转来的 RTCM:原样写进模组。串口没开着就丢(过时的改正没用)。"""
        with self._wlock:
            fd = self._fd
            if fd is None:
                self.dropped_bytes += len(data)
                return
            try:
                os.write(fd, data)
                self.fed_bytes += len(data)
            except OSError as exc:
                self.dropped_bytes += len(data)
                self.error = f"写改正没成: {exc}"[:200]

    def _closing(self) -> None:
        with self._wlock:
            fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fd = self._open(self.device, self.baud)
                with self._wlock:
                    self._fd = fd
                self.error = ""
                for cmd in self.init:                   # 初始化命令(真机上核过再配)
                    os.write(fd, (cmd.rstrip("\r\n") + "\r\n").encode("ascii"))
                self._read(fd)
            except Exception as exc:  # noqa: BLE001 —— 串口断了、模组拔了:记下来、隔一会儿再开
                if self._stop.is_set():
                    break
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("RTK 串口 %s:%s;%g s 后再开", self.device, self.error, REOPEN_S)
            finally:
                self._closing()
            self._stop.wait(REOPEN_S)

    def _read(self, fd: int) -> None:
        buf = b""
        while not self._stop.is_set():
            chunk = os.read(fd, 4096)
            if not chunk:
                raise ConnectionError("串口关了")
            buf += chunk
            *lines, buf = buf.split(b"\n")
            if len(buf) > 4096:                          # 一直没换行:不是 NMEA,扔了
                buf = b""
            for raw in lines:
                self._line(raw.decode("ascii", "replace"))

    def _line(self, line: str) -> None:
        body = _nmea_ok(line)
        if body is None:
            return
        if body[2:5] == "GST":
            self._std = parse_gst(body)
            return
        g = parse_gga(body)
        if g is not None:
            self._set(g | {"std_h_m": self._std})


class VendorRtk(_Base):
    """厂家驱动的 ``/rtk_pvh``,经辅助进程(一行一条 JSON)。"""

    kind = "vendor"

    def __init__(self, argv: Sequence[str], *, now_ms: Callable[[], int],
                 mono: Callable[[], float] = time.monotonic) -> None:
        super().__init__(now_ms=now_ms, mono=mono)
        self.argv = list(argv)
        self._proc: subprocess.Popen | None = None

    def feed(self, data: bytes) -> None:
        """厂家模式:改正归厂家那一套,站点转来的不用。"""

    def _closing(self) -> None:
        p = self._proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, text=True,
                                              bufsize=1)
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    self._line(line)
                rc = self._proc.wait()
                if not self._stop.is_set():
                    self.error = f"辅助进程退了(returncode={rc})"
            except Exception as exc:  # noqa: BLE001 —— 起不来:记下来、隔一会儿再起
                self.error = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                self._closing()
            if not self._stop.is_set():
                log.warning("厂家 RTK 辅助进程:%s;%g s 后再起", self.error, REOPEN_S)
            self._stop.wait(REOPEN_S)

    def _line(self, line: str) -> None:
        try:
            d = json.loads(line)
            pt = int(d.get("pos_type", 0))
            lat, lon = float(d["lat"]), float(d["lon"])
        except (ValueError, TypeError, KeyError, AttributeError):
            return
        slat, slon = d.get("lat_std"), d.get("lon_std")
        std = math.hypot(float(slat), float(slon)) if isinstance(slat, (int, float)) and \
            isinstance(slon, (int, float)) else None
        self._set({"fix": _POS_TYPE.get(pt, "none"), "lat": lat, "lon": lon,
                   "alt": d.get("alt"), "sats": int(d.get("svs_num", 0) or 0), "hdop": None,
                   "std_h_m": std, "age_s": d.get("diff_age_s")})
