"""W46:GPIO 字符设备(功放 MUTE 脚、风扇测速)。没有真 GPIO,ioctl 换成假的;结构布局对着
``<linux/gpio.h>`` 编出来的 C 程序逐字节核过(下面的期望字节就是 C 那边打出来的)。"""

from __future__ import annotations

import os
import struct
import time

import pytest

from d1max_contract import gpio
from d1max_contract.gpio import (
    EVENT_SIZE,
    FLAG_OUTPUT,
    EdgeCounter,
    GpioError,
    OutputLine,
    chip_path,
    pack_request,
    parse_events,
)


def test_请求结构_跟内核头文件逐字节一样():
    # C:offsets[0]=13, consumer="d1max", flags=OUTPUT, attrs[0]={OUTPUT_VALUES, values=1, mask=1}
    want = {0: 0x0D, 256: 0x64, 257: 0x31, 258: 0x6D, 259: 0x61, 260: 0x78, 288: 0x08,
            296: 0x01, 320: 0x02, 328: 0x01, 336: 0x01, 560: 0x01}
    b = pack_request([13], flags=FLAG_OUTPUT, values=1)
    assert len(b) == 592
    assert {i: x for i, x in enumerate(b) if x} == want


def test_输入请求_带边沿和上拉_不带输出初值():
    b = pack_request([3, 4, 5], flags=gpio.FLAG_INPUT | gpio.FLAG_EDGE_FALLING
                     | gpio.FLAG_BIAS_PULL_UP, event_buffer=192)
    assert struct.unpack_from("<3I", b, 0) == (3, 4, 5)
    assert struct.unpack_from("<QI", b, 288) == (4 | 32 | 256, 0)
    assert struct.unpack_from("<II", b, 560) == (3, 192)


def test_事件解析_尾巴不满一个就丢():
    ev = struct.pack("<QIIII24x", 123, 2, 7, 1, 1)
    assert len(ev) == EVENT_SIZE
    assert parse_events(ev + ev[:10]) == [(7, 123)]


@pytest.mark.parametrize("bad", ["", "gpio1", "/dev/sda", "../gpiochip0", "gpiochip1;x", 3])
def test_芯片名只认gpiochipN(bad):
    with pytest.raises(GpioError):
        chip_path(bad)
    assert chip_path("gpiochip4") == chip_path("/dev/gpiochip4") == "/dev/gpiochip4"


class _假内核:
    """GET_LINE 时把一个管道的读端当成线的 fd 填回去;SET_VALUES 记下来。"""

    def __init__(self, fail=False):
        self.r, self.w = os.pipe()
        self.sets: list[tuple[int, int]] = []
        self.reqs: list[bytearray] = []
        self.fail = fail

    def opener(self, path, flags):
        assert path.startswith("/dev/gpiochip")
        return os.open(os.devnull, os.O_RDONLY)

    def ioctl(self, fd, req, buf):
        if req == gpio.GET_LINE_IOCTL:
            if self.fail:
                raise OSError(16, "Device or resource busy")
            self.reqs.append(bytes(buf))
            struct.pack_into("<i", buf, 588, self.r)
        elif req == gpio.SET_VALUES_IOCTL:
            self.sets.append(struct.unpack("<QQ", buf))
        return 0


def test_输出线_初值进请求_写电平():
    k = _假内核()
    line = OutputLine("gpiochip1", 13, value=True, ioctl=k.ioctl, opener=k.opener)
    assert struct.unpack_from("<Q", k.reqs[0], 328)[0] == 1, "要线时就带初值,不先抖一下"
    line.set(False)
    line.set(True)
    assert k.sets == [(0, 1), (1, 1)]
    line.close()
    with pytest.raises(GpioError):
        line.set(False)
    os.close(k.w)


def test_线被占了_报清楚():
    k = _假内核(fail=True)
    with pytest.raises(GpioError, match="不让要"):
        OutputLine("gpiochip1", 13, value=False, ioctl=k.ioctl, opener=k.opener)


def test_数下降沿_按线分开_取走清零_半个事件拼上():
    k = _假内核()
    c = EdgeCounter("gpiochip2", [3, 4], ioctl=k.ioctl, opener=k.opener)

    def ev(off):
        return struct.pack("<QIIII24x", 1, 0, off, 0, 0)
    data = ev(3) * 5 + ev(4) * 2 + ev(9)
    os.write(k.w, data[:30])
    time.sleep(0.05)
    os.write(k.w, data[30:])
    end = time.monotonic() + 2
    while time.monotonic() < end and sum(c._counts.values()) < 7:
        time.sleep(0.02)
    assert c.take() == {3: 5, 4: 2}, "别的线的事件不算"
    assert c.take() == {3: 0, 4: 0}
    c.close()
    os.close(k.w)
