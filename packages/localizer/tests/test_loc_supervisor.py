"""MOLA 子进程看管(W09b 决定 2):起、换先验时重启、自己退了按退避重启、收尾连子孙一起关。
用一个假的「MOLA」(Python 小脚本)代替。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from d1max_localizer.supervisor import MolaSupervisor, mola_command

假MOLA = """
import os, sys, time, pathlib
d = pathlib.Path(sys.argv[1])
(d / f"up-{os.getpid()}").write_text(sys.argv[2])
if sys.argv[3] == "crash":
    sys.exit(3)
if sys.argv[3] == "child":                         # 像 ros2 launch:底下还有子进程
    import subprocess
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (d / f"child-{c.pid}").write_text("")
time.sleep(60)
"""


class 记:
    def __init__(self):
        self.ups, self.downs = [], []

    def up(self):
        self.ups.append(1)

    def down(self, why):
        self.downs.append(why)


def _sup(tmp_path, mode="ok", **kw):
    script = tmp_path / "fake_mola.py"
    script.write_text(假MOLA)
    r = 记()

    def cmd(prior):
        return [sys.executable, str(script), str(tmp_path), str(prior), mode]
    return MolaSupervisor(command_for=cmd, on_up=r.up, on_down=r.down, **kw), r


async def _等(cond, n=300):
    for _ in range(n):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等不到")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_起来_换先验就重启_关的时候连子进程一起关(tmp_path):
    s, r = _sup(tmp_path, mode="child")
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 1 and list(tmp_path.glob("child-*")))
    assert r.ups == [1] and s.running
    first = int(next(tmp_path.glob("up-*")).name[3:])
    child = int(next(tmp_path.glob("child-*")).name[6:])
    await s.start(Path("/maps/b/prior.mm"))
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 2)
    assert not _alive(first) and not _alive(child), "换先验:旧的连子进程一起关掉"
    assert sorted(p.read_text() for p in tmp_path.glob("up-*")) == ["/maps/a/prior.mm",
                                                                     "/maps/b/prior.mm"]
    assert r.downs == [], "我们自己关的不算它退出"
    await s.stop()
    assert not s.running
    for p in tmp_path.glob("up-*"):
        assert not _alive(int(p.name[3:]))


async def test_自己退了_说原因_按退避重启(tmp_path):
    s, r = _sup(tmp_path, mode="crash", backoff=(0.05, 0.1, 0.2))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(r.downs) >= 3)
    assert "退出码 3" in r.downs[0] and "秒后重启" in r.downs[0]
    await s.stop()
    n = len(list(tmp_path.glob("up-*")))
    await asyncio.sleep(0.4)
    assert len(list(tmp_path.glob("up-*"))) == n, "关了就不再重启"


async def test_要求重启_关掉重起同一个先验(tmp_path):
    s, r = _sup(tmp_path, backoff=(0.05,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 1)
    await s.restart("定位程序卡住了")
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 2)
    assert [p.read_text() for p in tmp_path.glob("up-*")] == ["/maps/a/prior.mm"] * 2
    assert r.downs == ["定位程序卡住了"]
    await s.stop()


def test_MOLA_的启动命令():
    cmd = mola_command(Path("/var/lib/d1max/agent/maps/m/3/prior.mm"), lidar_topic="/front_lidar")
    assert cmd[:4] == ["ros2", "launch", "mola_lidar_odometry", "ros2-lidar-odometry.launch.py"]
    args = dict(a.split(":=", 1) for a in cmd[4:])
    assert args["start_mapping_enabled"] == "False"
    assert args["mola_initial_map_mm_file"] == "/var/lib/d1max/agent/maps/m/3/prior.mm"
    assert args["lidar_topic_name"] == "/front_lidar"
    assert args["use_rviz"] == args["use_mola_gui"] == "False"
    assert args["ignore_lidar_pose_from_tf"] == "True"
