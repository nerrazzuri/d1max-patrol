"""基站改正数据转发(W09e 决定 1、2):站点主机从自建基站(UM982)读 RTCM3,按帧校验,发给每台在线的狗。

源:``serial:<设备>:<波特率>``(UM982 直连站点主机的 USB 串口;termios 裸模式,不装 pyserial)或
``tcp:<主机>:<端口>``(经网口、串口服务器)。断了隔几秒重连。一个读线程;每读到一批好帧交给 ``publish``
(主程序接到派遣器上,在事件循环里发)。1005 / 1006 解出基站坐标给人看。
"""

from __future__ import annotations

import logging
import math
import os
import socket
import threading
from collections.abc import Callable
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

class SourceError(ValueError):
    """``--rtcm-source`` 写得不对。"""


def parse_source(text: str) -> tuple[str, str, int]:
    """``serial:/dev/ttyUSB0:115200`` → ``("serial", "/dev/ttyUSB0", 115200)``;
    ``tcp:10.0.0.5:5018`` → ``("tcp", "10.0.0.5", 5018)``。"""
    kind, _, rest = text.partition(":")
    where, _, num = rest.rpartition(":")
    if kind not in ("serial", "tcp") or not where or not num.isdigit():
        raise SourceError("改正数据源要写成 serial:<设备>:<波特率> 或 tcp:<主机>:<端口>,"
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
                 on_base_moved: Callable[[float], Any] | None = None) -> None:
        #: 基站坐标跳了 5 cm 以上时叫(W09e 内审应修 3:自测平均模式每次重启漂几米,全队配准对不上)。
        self._on_base_moved = on_base_moved
        self.source = source
        self._kind, self._where, self._n = parse_source(source)
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
        s = socket.create_connection((self._where, self._n), timeout=10)
        s.settimeout(30)                                # 30 s 一个字节都没有:当断了
        with self._fd_lock:
            self._close_fn = s.close
        return lambda: s.recv(READ_BYTES)

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
            log.exception("改正数据发给狗没成")

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
