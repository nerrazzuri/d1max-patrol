"""节点接线端到端(W09b,不要 ROS):真的节点(核心、客户端、后端、看管器)+ 假 MOLA 进程 + 假 ROS 层,
对面是代理真的本机桥与 ``BridgeLocalizer``。走一遍:换先验起 MOLA → 人给位置 → 代理信 → MOLA 被杀 →
代理不信 → 自动重启、按最后可信的位置自己重定位 → 代理又信。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest
from d1max_localizer.backend import FRAMES_FILE, PRIOR_FILE
from d1max_localizer.core import Estimate
from d1max_localizer.frames import Frames
from d1max_localizer.main import Node, parse_args

from d1max_agent.bridge_localizer import BridgeLocalizer
from d1max_agent.locbridge import LocBridgeServer

FRAMES = Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
                sensor_height=0.5, sensor_in_base=(0.4043, 0.0))
M = ("estate-1", "7")


class 假ROS:
    """像 MOLA:一起来就按默认原点出位姿;收下重定位就按那个位置出(像跟住了)。"""

    instances: list = []

    def __init__(self, loop, *, lidar_topic, on_scan, on_estimate):
        self.loop, self.on_scan, self.on_estimate = loop, on_scan, on_estimate
        self.pose = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
        self.relocs = []
        self.task = None
        self.stamp = 1000.0
        假ROS.instances.append(self)

    def start(self):
        self.task = self.loop.create_task(self._run())

    def shutdown(self):
        if self.task is not None:
            self.task.cancel()

    async def relocalize(self, p, q, sigma):
        self.relocs.append((p, q, sigma))
        self.pose = (p, q)
        return True

    async def _run(self):
        while True:
            await asyncio.sleep(0.1)
            self.stamp += 0.1
            self.on_scan()
            if self.pose is not None:
                self.on_estimate(Estimate(stamp=self.stamp, p=self.pose[0], q=self.pose[1],
                                          quality=0.95))


async def _等(cond, s=10.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等不到")


@pytest.fixture
async def 台(tmp_path):
    d = Path(tempfile.mkdtemp(prefix="ln", dir="/tmp"))
    prior = tmp_path / "maps" / "m7"
    prior.mkdir(parents=True)
    (prior / PRIOR_FILE).write_bytes(b"mm")
    FRAMES.save(prior / FRAMES_FILE)
    agent = BridgeLocalizer(monotonic=time.monotonic)
    srv = LocBridgeServer(d / "loc.sock", agent)
    agent.link = srv
    agent.on_map(M, str(prior))
    await srv.start()
    pids = tmp_path / "pids"
    pids.mkdir()
    script = tmp_path / "mola.py"
    script.write_text("import os, sys, time, pathlib\n"
                      "pathlib.Path(sys.argv[1], str(os.getpid())).write_text(sys.argv[2])\n"
                      "time.sleep(120)\n")
    args = parse_args(["--socket", str(d / "loc.sock")])
    node = Node(args, ros_factory=假ROS, reconnect_s=0.1,
                command_for=lambda p: [sys.executable, str(script), str(pids), str(p)])
    task = asyncio.get_running_loop().create_task(node.run())
    yield agent, srv, node, pids, prior
    node.stop()
    await asyncio.wait_for(task, 10.0)
    await srv.close()
    shutil.rmtree(d, ignore_errors=True)


async def test_换先验起_MOLA_人给位置_代理信_杀掉之后自己恢复(台):
    agent, srv, node, pids, prior = 台
    await _等(lambda: list(pids.iterdir()))
    [p] = list(pids.iterdir())
    assert p.read_text() == str(prior / PRIOR_FILE)
    await _等(lambda: "等人给初始位置" in agent.why_not(True))
    assert await agent.relocalize(M, (2.0, 1.0, 0.0)) == ""
    await _等(lambda: agent.ok(True))
    e = agent.estimate((0.0, 0.0, 0.0))
    assert (round(e.x, 3), round(e.y, 3)) == (2.0, 1.0)
    os.kill(int(p.name), signal.SIGKILL)                  # MOLA 死了
    await _等(lambda: "定位程序退出了" in agent.why_not(True))
    await _等(lambda: len(list(pids.iterdir())) == 2)     # 退避 1 s 后重起
    ros = 假ROS.instances[-1]
    await _等(lambda: len(ros.relocs) == 2)               # 自己按最后可信的位置重定位
    assert ros.relocs[-1][2] == 1.0
    await _等(lambda: agent.ok(True), s=15.0)
    e = agent.estimate((0.0, 0.0, 0.0))
    assert (round(e.x, 2), round(e.y, 2)) == (2.0, 1.0)


async def test_收尾时_MOLA_一起关掉(台):
    agent, srv, node, pids, prior = 台
    await _等(lambda: list(pids.iterdir()))
    pid = int(next(pids.iterdir()).name)
    node.stop()
    await _等(lambda: not node.supervisor.running)
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
