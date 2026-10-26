"""起 ROS(rclpy)而**不让它接管停止信号**(W41)。

2026-10-26 现场:``d1max-persons``、``d1max-localizer``、``d1max-obstacles`` 正常
``systemctl stop`` 都退出码非 0,systemd 记成 failed。原因是 rclpy 默认自己装 SIGINT / SIGTERM 的
处理:收到就关掉 ROS 的上下文,``spin_once`` 接着抛 ``ExternalShutdownException``(退出码 1);定位器
那种「rclpy 在线程里转、asyncio 接信号」的,两边抢同一个信号,进程直接 abort(退出码 134)。

现在:rclpy 不装信号处理(:func:`init`),停止信号由程序自己接(:func:`stop_flag` 或 asyncio 的
``add_signal_handler``),收到就退出主循环、收尾、退出码 0。万一 rclpy 还是先关了上下文(别处
先 init 过),主循环照样把 ``ExternalShutdownException`` 当正常结束(:data:`SHUTDOWN_ERRORS`)。
"""

from __future__ import annotations

import signal
import threading
from typing import Any


def init(rclpy: Any = None) -> None:
    """``rclpy.init``,不装信号处理;已经起过就不再起。老的 rclpy 没这个选项就照老样子起。"""
    if rclpy is None:
        import rclpy
    if rclpy.ok():
        return
    try:
        from rclpy.signals import SignalHandlerOptions
        rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
    except (ImportError, TypeError):
        rclpy.init(args=None)


def stop_flag(sigs: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)) -> threading.Event:
    """收到停止信号就置位的标志(只能在主线程里调)。"""
    ev = threading.Event()

    def _set(*_: Any) -> None:
        ev.set()
    for s in sigs:
        signal.signal(s, _set)
    return ev


def shutdown_errors() -> tuple[type[BaseException], ...]:
    """主循环里当「正常结束」的异常:ROS 的上下文被关了、Ctrl-C。"""
    out: list[type[BaseException]] = [KeyboardInterrupt]
    try:
        from rclpy.executors import ExternalShutdownException
        out.append(ExternalShutdownException)
    except ImportError:
        pass
    return tuple(out)
