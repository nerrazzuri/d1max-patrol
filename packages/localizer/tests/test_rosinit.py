"""W41:用 ROS 的几个进程正常停止(SIGTERM)退出码 0,systemd 不记成 failed。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from d1max_localizer import rosinit

SRC = Path(__file__).resolve().parents[1] / "src"


class 假rclpy:
    def __init__(self, *, old: bool = False) -> None:
        self.calls: list[dict] = []
        self.up = False
        self.old = old

    def ok(self) -> bool:
        return self.up

    def init(self, **kw) -> None:
        if self.old and "signal_handler_options" in kw:
            raise TypeError("unexpected keyword")
        self.calls.append(kw)
        self.up = True


def test_起ROS_不装它的信号处理_起过就不再起_老版本照老样子():
    pytest.importorskip("rclpy")
    from rclpy.signals import SignalHandlerOptions
    r = 假rclpy()
    rosinit.init(r)
    rosinit.init(r)
    assert r.calls == [{"args": None, "signal_handler_options": SignalHandlerOptions.NO}]
    old = 假rclpy(old=True)
    rosinit.init(old)
    assert old.calls == [{"args": None}]


def test_停止信号_置位标志():
    ev = rosinit.stop_flag((signal.SIGUSR1,))
    try:
        assert not ev.is_set()
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(50):
            if ev.is_set():
                break
            time.sleep(0.01)
        assert ev.is_set()
    finally:
        signal.signal(signal.SIGUSR1, signal.SIG_DFL)


_LOOP = textwrap.dedent("""
    import sys, rclpy
    from rclpy.executors import SingleThreadedExecutor
    from d1max_localizer import rosinit
    rosinit.init(rclpy)
    stop = rosinit.stop_flag()
    node = rclpy.create_node("d1max_rosinit_test")
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    print("ready", flush=True)
    try:
        while rclpy.ok() and not stop.is_set():
            ex.spin_once(timeout_sec=0.1)
    except rosinit.shutdown_errors():
        pass
    finally:
        ex.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
    sys.exit(0)
""")


def test_真ROS_收到SIGTERM_退出码0():
    """现场原样:rclpy 默认的信号处理让 spin_once 抛 ExternalShutdownException,退出码 1。"""
    pytest.importorskip("rclpy")
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
               ROS_DOMAIN_ID="87")
    p = subprocess.Popen([sys.executable, "-c", _LOOP], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True)
    try:
        assert p.stdout is not None and p.stdout.readline().strip() == "ready"
        p.send_signal(signal.SIGTERM)
        code = p.wait(timeout=20)
    finally:
        if p.poll() is None:
            p.kill()
    assert code == 0, p.stderr.read() if p.stderr else code
