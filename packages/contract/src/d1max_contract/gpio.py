"""Linux GPIO 字符设备(uAPI v2,内核 5.10 起有),纯 Python,不装 libgpiod 的 Python 包(W46)。

上装 V2 用到两样:
- **功放 MUTE 脚**:一路输出(:class:`OutputLine`)。
- **风扇测速(FG)**:几路输入,数下降沿(:class:`EdgeCounter`);FG 是开集电极,要上拉。

结构布局、命令号照 ``<linux/gpio.h>``(``gpio_v2_line_request`` 592 字节、
``gpio_v2_line_event`` 48 字节),测试里对着头文件的偏移核过。
引脚是哪个芯片、第几根线,装机时用 ``gpioinfo`` 看,写进配置。
"""

from __future__ import annotations

import os
import re
import select
import struct
import threading
from collections.abc import Callable, Iterable
from typing import Any

GET_LINE_IOCTL = 0xC250B407
SET_VALUES_IOCTL = 0xC010B40F
GET_VALUES_IOCTL = 0xC010B40E

FLAG_ACTIVE_LOW = 1 << 1
FLAG_INPUT = 1 << 2
FLAG_OUTPUT = 1 << 3
FLAG_EDGE_RISING = 1 << 4
FLAG_EDGE_FALLING = 1 << 5
FLAG_BIAS_PULL_UP = 1 << 8

ATTR_OUTPUT_VALUES = 2
MAX_LINES = 64
REQUEST_SIZE = 592
EVENT_SIZE = 48
_OFF_CONSUMER, _OFF_CONFIG, _OFF_NUM_LINES, _OFF_EVENT_BUF, _OFF_FD = 256, 288, 560, 564, 588
_CHIP = re.compile(r"(?:/dev/)?gpiochip[0-9]{1,3}")


class GpioError(OSError):
    """GPIO 配置不对或者内核不让要这根线。"""


def chip_path(chip: str) -> str:
    """``gpiochip1`` 或 ``/dev/gpiochip1`` → ``/dev/gpiochip1``。
    别的写法不认(不许指到别的设备文件)。"""
    if not isinstance(chip, str) or not _CHIP.fullmatch(chip):
        raise GpioError(f"GPIO 芯片要写成 gpiochipN,给的是 {chip!r}")
    return chip if chip.startswith("/dev/") else f"/dev/{chip}"


def pack_request(offsets: Iterable[int], *, flags: int, consumer: str = "d1max",
                 values: int | None = None, event_buffer: int = 0) -> bytearray:
    """拼 ``struct gpio_v2_line_request``。
    ``values`` 给了就带一条「输出初值」属性(第 i 位 = 第 i 根线)。"""
    offs = list(offsets)
    if not 0 < len(offs) <= MAX_LINES or any(not 0 <= o < 2 ** 32 for o in offs):
        raise GpioError(f"线号错误:{offs}")
    buf = bytearray(REQUEST_SIZE)
    struct.pack_into(f"<{len(offs)}I", buf, 0, *offs)
    name = consumer.encode()[:31]
    buf[_OFF_CONSUMER:_OFF_CONSUMER + len(name)] = name
    struct.pack_into("<QI", buf, _OFF_CONFIG, flags, 0 if values is None else 1)
    if values is not None:                       # attrs[0]:{id, pad, values} + mask
        struct.pack_into("<IIQQ", buf, _OFF_CONFIG + 32, ATTR_OUTPUT_VALUES, 0, values,
                         (1 << len(offs)) - 1)
    struct.pack_into("<II", buf, _OFF_NUM_LINES, len(offs), event_buffer)
    return buf


def request_fd(buf: bytes | bytearray) -> int:
    return struct.unpack_from("<i", buf, _OFF_FD)[0]


def parse_events(data: bytes) -> list[tuple[int, int]]:
    """``struct gpio_v2_line_event`` 一串 → [(线号, 时刻 ns)]。不满一个的尾巴丢掉。"""
    out = []
    for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
        ts, _id, offset = struct.unpack_from("<QII", data, i)
        out.append((offset, ts))
    return out


def _ioctl(fd: int, req: int, buf: bytearray) -> Any:
    import fcntl
    return fcntl.ioctl(fd, req, buf, True)


def _request(chip: str, buf: bytearray, *, ioctl: Callable[..., Any],
             opener: Callable[[str, int], int]) -> int:
    path = chip_path(chip)
    try:
        cfd = opener(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        raise GpioError(f"打不开 {path}: {exc}") from exc
    try:
        ioctl(cfd, GET_LINE_IOCTL, buf)
    except OSError as exc:
        raise GpioError(f"{path} 不让要这几根线(被别的程序占了、线号错误?): {exc}") from exc
    finally:
        os.close(cfd)
    return request_fd(buf)


class OutputLine:
    """一根输出线,进程活着就一直占着。``value`` 是电平(真 = 高)。"""

    def __init__(self, chip: str, line: int, *, value: bool, consumer: str = "d1max",
                 ioctl: Callable[..., Any] = _ioctl,
                 opener: Callable[[str, int], int] = os.open) -> None:
        self.chip, self.line = chip, line
        self._ioctl = ioctl
        self.fd = _request(chip, pack_request([line], flags=FLAG_OUTPUT, consumer=consumer,
                                              values=int(bool(value))),
                           ioctl=ioctl, opener=opener)
        self.value = bool(value)

    def set(self, value: bool) -> None:
        if self.fd < 0:
            raise GpioError("GPIO 已关闭")
        try:
            self._ioctl(self.fd, SET_VALUES_IOCTL, bytearray(struct.pack("<QQ", int(value), 1)))
        except OSError as exc:
            raise GpioError(f"GPIO {self.chip}:{self.line} 无法写入: {exc}") from exc
        self.value = bool(value)

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)


class EdgeCounter:
    """同一个芯片上几根输入线,数下降沿(一个读线程)。:meth:`take` 取走上次以来每根线的个数。"""

    READ_TIMEOUT_S = 0.2

    def __init__(self, chip: str, lines: Iterable[int], *, pull_up: bool = True,
                 consumer: str = "d1max-fan", ioctl: Callable[..., Any] = _ioctl,
                 opener: Callable[[str, int], int] = os.open) -> None:
        self.chip = chip
        self.lines = list(lines)
        flags = FLAG_INPUT | FLAG_EDGE_FALLING | (FLAG_BIAS_PULL_UP if pull_up else 0)
        self.fd = _request(chip, pack_request(self.lines, flags=flags, consumer=consumer,
                                              event_buffer=64 * len(self.lines)),
                           ioctl=ioctl, opener=opener)
        self._counts = dict.fromkeys(self.lines, 0)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error = ""
        self._t = threading.Thread(target=self._read, name=f"gpio-edges-{chip}", daemon=True)
        self._t.start()

    def _read(self) -> None:
        tail = b""
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.fd], [], [], self.READ_TIMEOUT_S)
                if not r:
                    continue
                data = os.read(self.fd, EVENT_SIZE * 64)
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    self.error = f"无法读取 {self.chip} 的边沿: {exc}"
                return
            if not data:
                return
            data, tail = tail + data, b""
            whole = len(data) - len(data) % EVENT_SIZE
            data, tail = data[:whole], data[whole:]
            with self._lock:
                for offset, _ts in parse_events(data):
                    if offset in self._counts:
                        self._counts[offset] += 1

    def take(self) -> dict[int, int]:
        with self._lock:
            out, self._counts = self._counts, dict.fromkeys(self.lines, 0)
        return out

    def close(self) -> None:
        self._stop.set()
        self._t.join(1.0)
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)
