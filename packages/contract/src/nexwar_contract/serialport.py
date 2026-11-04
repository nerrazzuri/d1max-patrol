"""裸模式串口(W09e):站点读基站、狗读写 RTK 模组都用。只用标准库(termios),不装 pyserial。"""

from __future__ import annotations

import os
import termios

BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
         57600: termios.B57600, 115200: termios.B115200, 230400: termios.B230400,
         460800: termios.B460800, 921600: termios.B921600}


def open_serial(path: str, baud: int) -> int:
    """裸模式打开串口(8N1、不回显、不改字节),回文件描述符。"""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                                   # iflag
        attrs[1] = 0                                   # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                                   # lflag
        attrs[4] = attrs[5] = BAUDS[baud]
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except BaseException:
        os.close(fd)
        raise
    return fd


__all__ = ["BAUDS", "open_serial"]
