"""基站改正数据转发(W09e 决定 1、2):站点主机从自建基站(UM982)读 RTCM3,按帧校验,发给每台在线的狗。

源:``serial:<设备>:<波特率>``(UM982 直连站点主机的 USB 串口;termios 裸模式,不装 pyserial)、
``tcp:<主机>:<端口>``(经网口、串口服务器),或 ``ntrip:<主机>:<端口>/<挂载点>``(W46,决策 74:商业 CORS
或自建基站的 caster;账号在站点目录的 ``ntrip.json`` 里,0600,不进命令行)。断了隔几秒重连。

NTRIP 按 1.0 的写法要(``GET /<挂载点> HTTP/1.0`` + Basic 账号),回 ``ICY 200 OK`` 或 ``HTTP/1.x 200``
都认,2.0 的分块传输也拆。配了站点的大概位置就每 ``GGA_EVERY_S`` 秒回报一句 GGA(虚拟基站一类的网络
按它算改正)。一个读线程;每读到一批好帧交给 ``publish``
(主程序接到派遣器上,在事件循环里发)。1005 / 1006 解出基站坐标给人看。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d1max_contract.rtcm import RtcmFramer, ecef_to_llh, msg_type, station_ecef
from d1max_contract.serialport import BAUDS as _BAUDS
from d1max_contract.serialport import open_serial

log = logging.getLogger(__name__)

#: 断了多久再连。
RECONNECT_S = 3.0
#: 一次读多少。
READ_BYTES = 4096
#: 基站坐标跳这么多算挪了(W09e 内审应修 3)。
BASE_MOVED_M = 0.05
#: NTRIP:多久回报一次大概位置(GGA)。
GGA_EVERY_S = 10.0
#: NTRIP:这么久一个字节都没有,当断了。
NTRIP_IDLE_S = 30.0
_MOUNT = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")

class SourceError(ValueError):
    """``--rtcm-source`` 写得不对。"""


@dataclass(frozen=True)
class NtripAccount:
    """NTRIP 账号,加上站点的大概位置(给了才回报 GGA)。``repr`` 不带密码。"""

    user: str
    password: str = field(repr=False)
    lat: float | None = None
    lon: float | None = None
    alt: float = 0.0


def load_ntrip(path: str | Path) -> NtripAccount:
    """读 ``ntrip.json``:``{"user", "password", "lat", "lon", "alt"}``,文件要 0600(里面有密码)。"""
    p = Path(path)
    try:
        mode = p.stat().st_mode
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"NTRIP 要账号:在 {p} 写 ntrip.json(user、password,"
                          "可选 lat、lon、alt),chmod 600") from exc
    except (OSError, ValueError) as exc:
        raise SourceError(f"{p} 读不了: {exc}") from exc
    if mode & 0o077:
        raise SourceError(f"{p} 里有密码,权限要 0600(chmod 600)")
    if not isinstance(d, dict) or not all(isinstance(d.get(k), str) and d.get(k)
                                          for k in ("user", "password")):
        raise SourceError(f"{p} 要有 user、password 两个字符串")
    try:
        lat = None if d.get("lat") is None else float(d["lat"])
        lon = None if d.get("lon") is None else float(d["lon"])
        alt = float(d.get("alt", 0.0))
    except (TypeError, ValueError) as exc:
        raise SourceError(f"{p} 的 lat、lon、alt 要是数字") from exc
    if (lat is None) != (lon is None) or (lat is not None and not (
            -90 <= lat <= 90 and -180 <= lon <= 180)):           # type: ignore[operator]
        raise SourceError(f"{p} 的 lat、lon 要一起给,在 ±90、±180 之内")
    return NtripAccount(d["user"], d["password"], lat, lon, alt)


def gga_sentence(lat: float, lon: float, alt: float, utc_s: float) -> str:
    """一句 ``$GPGGA``(单点解、12 颗星),回报给 caster 的大概位置。"""
    t = time.gmtime(utc_s)

    def dm(v: float, width: int) -> str:
        a = abs(v)
        d = int(a)
        return f"{d:0{width}d}{(a - d) * 60:08.5f}"
    body = (f"GPGGA,{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}.00,"
            f"{dm(lat, 2)},{'N' if lat >= 0 else 'S'},{dm(lon, 3)},{'E' if lon >= 0 else 'W'},"
            f"1,12,1.0,{alt:.1f},M,0.0,M,,")
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n"


class _Chunks:
    """HTTP 分块传输拆包:喂进字节,吐出数据部分。"""

    def __init__(self) -> None:
        self._buf = b""
        self._left = 0                 # 这一块还剩多少数据
        self._skip = 0                 # 数据后面的 \r\n

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        while self._buf:
            if self._skip:
                n = min(self._skip, len(self._buf))
                self._buf, self._skip = self._buf[n:], self._skip - n
                continue
            if self._left:
                n = min(self._left, len(self._buf))
                out += self._buf[:n]
                self._buf, self._left = self._buf[n:], self._left - n
                if not self._left:
                    self._skip = 2
                continue
            line, sep, rest = self._buf.partition(b"\r\n")
            if not sep:
                if len(self._buf) > 64:
                    raise ConnectionError("分块的长度行不对")
                break
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError as exc:
                raise ConnectionError("分块的长度行不对") from exc
            self._buf = rest
            if size == 0:
                raise ConnectionError("caster 结束了数据流")
            self._left = size
        return bytes(out)


def parse_source(text: str) -> tuple[str, str, int]:
    """``serial:/dev/ttyUSB0:115200`` → ``("serial", "/dev/ttyUSB0", 115200)``;
    ``tcp:10.0.0.5:5018`` → ``("tcp", "10.0.0.5", 5018)``;
    ``ntrip:caster:2101/RTCM32`` → ``("ntrip", "caster/RTCM32", 2101)``。"""
    if text.startswith("ntrip:"):
        hostport, slash, mount = text[len("ntrip:"):].partition("/")
        host, _, num = hostport.rpartition(":")
        if not slash or not _MOUNT.fullmatch(mount) or not _HOST.fullmatch(host) \
                or not num.isdigit() or not 0 < int(num) < 65536:
            raise SourceError("NTRIP 源要写成 ntrip:<主机>:<端口>/<挂载点>,"
                              f"给的是 {text!r}")
        return "ntrip", f"{host}/{mount}", int(num)
    kind, _, rest = text.partition(":")
    where, _, num = rest.rpartition(":")
    if kind not in ("serial", "tcp") or not where or not num.isdigit():
        raise SourceError("改正数据源要写成 serial:<设备>:<波特率>、tcp:<主机>:<端口> 或 "
                          "ntrip:<主机>:<端口>/<挂载点>,"
                          f"给的是 {text!r}")
    n = int(num)
    if kind == "serial" and n not in _BAUDS:
        raise SourceError(f"波特率 {n} 不认,只认 {sorted(_BAUDS)}")
    if kind == "tcp" and not 0 < n < 65536:
        raise SourceError(f"端口 {n} 不对")
    return kind, where, n


class RtcmRelay:
    def __init__(self, source: str, *, publish: Callable[[bytes], Any],
                 now_ms: Callable[[], int],
                 on_base_moved: Callable[[float], Any] | None = None,
                 ntrip: NtripAccount | None = None, wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        #: 基站坐标跳了 5 cm 以上时叫(W09e 内审应修 3:自测平均模式每次重启漂几米,全队配准对不上)。
        self._on_base_moved = on_base_moved
        self.source = source
        self._kind, self._where, self._n = parse_source(source)
        if self._kind == "ntrip" and ntrip is None:
            raise SourceError("NTRIP 源要账号(站点目录里的 ntrip.json)")
        self._ntrip = ntrip
        self._wall, self._mono = wall, mono
        self._publish = publish
        self._now = now_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._framer = RtcmFramer()
        self._lock = threading.Lock()
        self._types: dict[int, int] = {}
        self._last_ms: int | None = None
        self._base: tuple[float, float, float] | None = None
        self._base_ecef: tuple[float, float, float] | None = None
        self._fd_lock = threading.Lock()
        self.connected = False
        self.error = ""
        self._close_fn: Callable[[], None] | None = None

    # ------------------------------------------------------------ 线程

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="rtcm-relay")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._close_source()
        if self._thread is not None:
            self._thread.join(5)

    def _close_source(self) -> None:
        """换出来再关:收尾线程与读线程各关一次同一个 fd 的话,中间 fd 号被复用就关错了(内审小)。"""
        with self._fd_lock:
            fn, self._close_fn = self._close_fn, None
        if fn is not None:
            try:
                fn()
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                read = self._open()
                self.connected, self.error = True, ""
                log.info("基站改正数据源连上了:%s", self.source)
                while not self._stop.is_set():
                    data = read()
                    if not data:
                        raise ConnectionError("源关了")
                    self.feed(data)
            except Exception as exc:  # noqa: BLE001 —— 什么错都记下来、重连,读线程不许死
                if self._stop.is_set():
                    break
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("基站改正数据源断了(%s),%g s 后重连", self.error, RECONNECT_S)
            finally:
                self.connected = False
                self._close_source()
            self._stop.wait(RECONNECT_S)

    def _open(self) -> Callable[[], bytes]:
        if self._kind == "serial":
            fd = open_serial(self._where, self._n)
            with self._fd_lock:
                self._close_fn = lambda: os.close(fd)
            return lambda: os.read(fd, READ_BYTES)
        if self._kind == "ntrip":
            return self._open_ntrip()
        s = socket.create_connection((self._where, self._n), timeout=10)
        s.settimeout(30)                                # 30 s 一个字节都没有:当断了
        with self._fd_lock:
            self._close_fn = s.close
        return lambda: s.recv(READ_BYTES)

    def _open_ntrip(self) -> Callable[[], bytes]:
        acc = self._ntrip
        assert acc is not None
        host, _, mount = self._where.partition("/")
        s = socket.create_connection((host, self._n), timeout=10)
        with self._fd_lock:
            self._close_fn = s.close
        auth = base64.b64encode(f"{acc.user}:{acc.password}".encode()).decode()
        s.sendall((f"GET /{mount} HTTP/1.0\r\nHost: {host}:{self._n}\r\n"
                   "User-Agent: NTRIP d1max-site/1.0\r\n"
                   f"Authorization: Basic {auth}\r\nAccept: */*\r\n\r\n").encode())
        s.settimeout(10)
        head = b""
        while b"\r\n\r\n" not in head and not head.startswith(b"ICY 200 OK\r\n"):
            d = s.recv(1024)
            if not d:
                raise ConnectionError("caster 没回就断了")
            head += d
            if len(head) > 16384:
                raise ConnectionError("caster 回的头太长")
        status = head.split(b"\r\n", 1)[0].decode("latin-1").strip()
        if status.startswith("ICY 200"):
            rest = head[len(b"ICY 200 OK\r\n"):]
            rest = rest[2:] if rest.startswith(b"\r\n") else rest
            chunked = None
        else:
            hdr, _, rest = head.partition(b"\r\n\r\n")
            if status.startswith("SOURCETABLE"):
                raise ConnectionError(f"挂载点不对(caster 回了挂载点列表):/{mount}")
            code = status.split(" ")[1] if status.startswith("HTTP/") and " " in status else ""
            if code == "401":
                raise ConnectionError("NTRIP 账号密码不对(401)")
            if code != "200":
                raise ConnectionError(f"caster 拒了:{status[:80]}")
            chunked = _Chunks() if b"transfer-encoding: chunked" in hdr.lower() else None
        s.settimeout(1.0)
        pending = [chunked.feed(rest) if chunked is not None else rest]
        state = {"gga": 0.0, "heard": self._mono()}

        def read() -> bytes:
            while True:
                if pending[0]:
                    out, pending[0] = pending[0], b""
                    return out
                if acc.lat is not None and self._mono() >= state["gga"]:
                    state["gga"] = self._mono() + GGA_EVERY_S
                    s.sendall(gga_sentence(acc.lat, acc.lon or 0.0, acc.alt,
                                           self._wall()).encode())
                try:
                    d = s.recv(READ_BYTES)
                except TimeoutError:
                    if self._stop.is_set():
                        return b""
                    if self._mono() - state["heard"] > NTRIP_IDLE_S:
                        raise ConnectionError(f"caster {NTRIP_IDLE_S:.0f} s 没发数据") from None
                    continue
                if not d:
                    return b""
                state["heard"] = self._mono()
                d = chunked.feed(d) if chunked is not None else d
                if d:
                    return d
        return read

    # ------------------------------------------------------------ 处理

    def feed(self, data: bytes) -> None:
        """一段字节:切帧,好帧一批发出去。"""
        frames = self._framer.feed(data)
        if not frames:
            return
        with self._lock:
            for f in frames:
                t = msg_type(f)
                self._types[t] = self._types.get(t, 0) + 1
                ecef = station_ecef(f)
                if ecef is not None:
                    if self._base_ecef is not None and \
                            math.dist(ecef, self._base_ecef) > BASE_MOVED_M:
                        moved = math.dist(ecef, self._base_ecef)
                        log.warning("基站坐标跳了 %.2f m", moved)
                        if self._on_base_moved is not None:
                            try:
                                self._on_base_moved(moved)
                            except Exception:
                                log.exception("基站坐标跳了,报告警没成")
                    self._base_ecef = ecef
                    self._base = ecef_to_llh(*ecef)
            self._last_ms = self._now()
        try:
            self._publish(b"".join(frames))
        except Exception:
            # 发不出去这一批就丢了(过时的改正没用)
            log.exception("改正数据发给机器人没成")

    def stats(self) -> dict[str, Any]:
        with self._lock:
            age = None if self._last_ms is None else round((self._now() - self._last_ms) / 1000, 1)
            base = None if self._base is None else {
                "lat": round(self._base[0], 8), "lon": round(self._base[1], 8),
                "alt": round(self._base[2], 3)}
            return {"source": self.source, "connected": self.connected, "error": self.error,
                    "frames": self._framer.frames, "bad": self._framer.bad,
                    "age_s": age, "types": {str(k): v for k, v in sorted(self._types.items())},
                    "base": base}
