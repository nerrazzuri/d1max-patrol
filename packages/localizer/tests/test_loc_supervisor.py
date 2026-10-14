"""MOLA 子进程看管(W09b 决定 2):起、换先验时重启、自己退了按退避重启、收尾连子孙一起关。
用一个假的「MOLA」(Python 小脚本)代替。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

import pytest
from d1max_localizer import supervisor
from d1max_localizer.supervisor import MolaSupervisor, mola_command

假MOLA = """
import os, sys, time, pathlib
d = pathlib.Path(sys.argv[1])
(d / f"up-{os.getpid()}").write_text(sys.argv[2])
if sys.argv[3] == "crash":
    sys.exit(3)
if sys.argv[3] == "launch_child_segv":             # 像 ros2 launch:底下的节点段错误,外层照样退 0
    print("[INFO] [mola-1]: process started with pid [123]", flush=True)
    print("[ERROR] [mola-1]: process has died [pid 123, exit code -11, cmd 'mola']", flush=True)
    sys.exit(0)
if sys.argv[3] == "quiet_zero":
    sys.exit(0)
if sys.argv[3] == "stubborn":                      # 不理 SIGINT(像收尾慢的 ROS 节点)
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
if sys.argv[3] in ("child", "crash_child"):        # 像 ros2 launch:底下还有子进程
    import subprocess
    c = subprocess.Popen([sys.executable, "-c",        # 不理 SIGINT:只能整组强杀
                          "import os, pathlib, signal, sys, time; "
                          "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                          "pathlib.Path(sys.argv[1], f'child-{os.getpid()}').write_text(''); "
                          "time.sleep(60)", str(d)])
    if sys.argv[3] == "crash_child":
        while not list(d.glob("child-*")):
            time.sleep(0.01)
        sys.exit(4)
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
    # child-* 是孙子进程自己设好「不理 SIGINT」之后写的:之后 SIGINT 杀不了它
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
    assert r.downs == ["定位程序卡住了,0.05 秒后重启"]
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


async def test_要求重启一连来好几次_只重启一回_到点起得来(tmp_path):
    """W09b 内审阻断 1:原来每拍来一次重启,每次都把等着的重起取消、按更长的退避重排,永远起不来。"""
    s, r = _sup(tmp_path, backoff=(0.2,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 1)
    for _ in range(10):
        await s.restart("定位程序卡住了")
        await asyncio.sleep(0.02)
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 2)
    assert len(r.downs) == 1 and s.running
    await s.stop()


async def test_换图连着来_旧的关干净了才起新的_不留孤儿(tmp_path, monkeypatch):
    """W09b 内审应修 5 情形 A:原来取消正在关旧进程的起动,旧进程离了手、跟新的一起跑。"""
    import d1max_localizer.supervisor as sv
    monkeypatch.setattr(sv, "KILL_AFTER_S", 0.3)
    s, r = _sup(tmp_path, mode="stubborn")
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 1)
    t1 = asyncio.get_running_loop().create_task(s.start(Path("/maps/b/prior.mm")))
    await asyncio.sleep(0.05)
    t1.cancel()                                           # 后端原来会这么干
    t2 = asyncio.get_running_loop().create_task(s.start(Path("/maps/c/prior.mm")))
    await asyncio.gather(t1, t2, return_exceptions=True)
    await asyncio.sleep(0.2)
    alive = [p for p in tmp_path.glob("up-*") if _alive(int(p.name[3:]))]
    assert [p.read_text() for p in alive] == ["/maps/c/prior.mm"], "只剩最后那个"
    await s.stop()
    assert not [p for p in tmp_path.glob("up-*") if _alive(int(p.name[3:]))]


async def test_正在关旧的时候收尾_收完就不再起新的(tmp_path, monkeypatch):
    """W09b 内审应修 5 情形 B:原来 stop 立刻返回,起动任务过一会儿又起了一个。"""
    import d1max_localizer.supervisor as sv
    monkeypatch.setattr(sv, "KILL_AFTER_S", 0.3)
    s, r = _sup(tmp_path, mode="stubborn")
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 1)
    t = asyncio.get_running_loop().create_task(s.start(Path("/maps/b/prior.mm")))
    await asyncio.sleep(0.05)
    await s.stop()
    await asyncio.gather(t, return_exceptions=True)
    await s.stop()
    await asyncio.sleep(0.5)
    assert not s.running
    assert not [p for p in tmp_path.glob("up-*") if _alive(int(p.name[3:]))]


async def test_命令起不来_说原因_按退避再试(tmp_path):
    """W09b 内审应修 6:原来 FileNotFoundError 被吞了,状态一直「在载入先验」、不再重试。"""
    r = 记()
    s = MolaSupervisor(command_for=lambda p: [str(tmp_path / "没有这个程序")], on_up=r.up,
                       on_down=r.down, backoff=(0.05,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(r.downs) >= 2)
    assert "起不来" in r.downs[0] and "秒后重启" in r.downs[0]
    assert r.ups == []
    await s.stop()


async def test_它自己退了_底下剩的也收掉(tmp_path):
    """MOLA 自己退了、launch 底下的节点还挂着:整组收掉(W09b 内审小问题 3)。"""
    s, r = _sup(tmp_path, mode="crash_child", backoff=(5.0,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: r.downs)
    child = int(next(tmp_path.glob("child-*")).name[6:])
    await _等(lambda: not _alive(child))
    await s.stop()


async def test_两次起动同时来_串着做_只剩后来那个(tmp_path, monkeypatch):
    """W09b 内审应修 5:不串着做的话,两次起动都去关同一个旧进程、各起一个新的,前一个成了孤儿。"""
    import d1max_localizer.supervisor as sv
    monkeypatch.setattr(sv, "KILL_AFTER_S", 0.3)
    s, r = _sup(tmp_path, mode="stubborn")
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: len(list(tmp_path.glob("up-*"))) == 1)
    await asyncio.gather(s.start(Path("/maps/b/prior.mm")), s.start(Path("/maps/c/prior.mm")))
    await asyncio.sleep(0.3)
    alive = [p for p in tmp_path.glob("up-*") if _alive(int(p.name[3:]))]
    assert [p.read_text() for p in alive] == ["/maps/c/prior.mm"]
    await s.stop()


async def test_W34_底下的节点段错误_外层launch退0_说清是谁怎么死的(tmp_path):
    """2026-10-08 C40221:罩住前雷达 MOLA 段错误,日志写「退出码 0」(外层 ros2 launch 的)。"""
    s, r = _sup(tmp_path, mode="launch_child_segv", backoff=(30.0,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: r.downs)
    assert "mola-1 退出码 -11" in r.downs[0] and "SIGSEGV" in r.downs[0], r.downs
    await s.stop()


async def test_W34_外层退0又没说谁死了_不写成正常的退出码0(tmp_path):
    s, r = _sup(tmp_path, mode="quiet_zero", backoff=(30.0,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: r.downs)
    assert "外层 ros2 launch 退出码 0" in r.downs[0] and "可能是被杀或崩了" in r.downs[0]
    await s.stop()


# ---------------------------------------------------------------- W35:MOLA 日志逐行到
# 2026-10-13 C40221:MOLA 自己打 11:46:11 的那行,我们日志 11:46:44 才出来
# —— 子进程往管道写时整块缓冲。

慢慢说 = """
import sys, time
print("[mola-cli-1] 第一行 " + sys.argv[1])
{flush}
time.sleep(float(sys.argv[2]))
print("[mola-cli-1] 第二行")
"""


def _说话的(tmp_path, *, flush: bool, sleep_s: float):
    script = tmp_path / "talker.py"
    script.write_text(慢慢说.format(flush="sys.stdout.flush()" if flush else ""))
    r = 记()

    def cmd(prior):                                       # 不加 -u:往管道写就整块缓冲
        return [sys.executable, str(script), str(prior), str(sleep_s)]
    return MolaSupervisor(command_for=cmd, on_up=r.up, on_down=r.down, backoff=(30.0,)), r


def _mola_lines(caplog):
    return [rec.getMessage() for rec in caplog.records if rec.name == "mola"]


async def test_W35_子进程说一行_我们这边马上记_不等它退(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="mola")
    s, r = _说话的(tmp_path, flush=True, sleep_s=20.0)
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: any("第一行" in m for m in _mola_lines(caplog)), n=400)   # 最多约 8 s
    assert s.running and not any("第二行" in m for m in _mola_lines(caplog)), "它还没退、还在睡"
    await s.stop()


async def test_W35_子进程是_Python_不刷也逐行到(tmp_path, caplog):
    """ros2 launch 本身是 Python:环境里给 PYTHONUNBUFFERED,它往管道写也逐行。"""
    caplog.set_level(logging.INFO, logger="mola")
    s, r = _说话的(tmp_path, flush=False, sleep_s=20.0)
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: any("第一行" in m for m in _mola_lines(caplog)), n=400)
    assert s.running
    await s.stop()


async def test_W35_超长的一行_读的不死_后面的照样记(tmp_path, caplog):
    """一行超过读缓冲上限:原来 readline 抛错、读的任务死了,管道写满 MOLA 就卡在写上。"""
    caplog.set_level(logging.INFO, logger="mola")
    script = tmp_path / "long.py"
    script.write_text("import sys, time\n"
                      "sys.stdout.write('x' * (4 << 20) + '\\n')\n"
                      "print('[mola-cli-1] 长行之后', flush=True)\n"
                      "time.sleep(20)\n")
    s = MolaSupervisor(command_for=lambda p: [sys.executable, str(script)],
                       on_up=lambda: None, on_down=lambda why: None, backoff=(30.0,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: any("长行之后" in m for m in _mola_lines(caplog)), n=400)
    assert s.running
    await s.stop()


@pytest.mark.skipif(not (shutil.which("stdbuf") and shutil.which("grep") and shutil.which("sh")),
                    reason="要 coreutils stdbuf、grep、sh")
async def test_W35_孙子进程是_C_程序_套了_stdbuf_也逐行到(caplog):
    """像 ros2 launch 底下的 mola-cli:sh 底下的 grep(C stdio,往管道写整块缓冲)。stdbuf 靠
    LD_PRELOAD + 环境变量,孙子进程继承到 → 第一行不等它退就到。"""
    caplog.set_level(logging.INFO, logger="mola")
    s = MolaSupervisor(
        command_for=lambda p: ["sh", "-c", "(echo '[mola-cli-1] 第一行'; sleep 20) | grep mola"],
        on_up=lambda: None, on_down=lambda why: None, backoff=(30.0,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: any("第一行" in m for m in _mola_lines(caplog)), n=400)
    assert s.running
    await s.stop()


def _which(*missing):
    return lambda name, path=None: None if name in missing else f"/usr/bin/{name}"


def test_W35_有_stdbuf_就套上_逐行缓冲(monkeypatch):
    monkeypatch.setattr(supervisor.shutil, "which", _which())
    cmd = mola_command(Path("/m/prior.mm"))
    assert supervisor.line_buffered(cmd) == ["/usr/bin/stdbuf", "-oL", "-eL", *cmd]


def test_W35_要起的程序找不到_不套_照旧报起不来(monkeypatch):
    monkeypatch.setattr(supervisor.shutil, "which", _which("ros2"))
    cmd = mola_command(Path("/m/prior.mm"))
    assert supervisor.line_buffered(cmd) == cmd


def test_W35_没有_stdbuf_原样起_只说一回(monkeypatch, caplog):
    monkeypatch.setattr(supervisor.shutil, "which", _which("stdbuf"))
    monkeypatch.setattr(supervisor, "_warned_no_stdbuf", False)
    caplog.set_level(logging.WARNING, logger=supervisor.__name__)
    cmd = mola_command(Path("/m/prior.mm"))
    assert supervisor.line_buffered(cmd) == cmd
    assert supervisor.line_buffered(cmd) == cmd
    assert sum("stdbuf" in rec.getMessage() for rec in caplog.records) == 1


async def test_W35_起的时候真套上了_环境里也给了不缓冲(monkeypatch):
    seen = {}

    async def fake_exec(*argv, env=None, **kw):
        seen["argv"], seen["env"] = list(argv), env
        raise OSError("不真起")
    monkeypatch.setattr(supervisor.shutil, "which", _which())
    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", fake_exec)
    s = MolaSupervisor(command_for=lambda p: mola_command(p), on_up=lambda: None,
                       on_down=lambda why: None, backoff=(30.0,), env={"PATH": "/x"})
    await s.start(Path("/m/prior.mm"))
    assert seen["argv"] == ["/usr/bin/stdbuf", "-oL", "-eL", *mola_command(Path("/m/prior.mm"))]
    assert seen["env"]["PATH"] == "/x"
    assert seen["env"]["PYTHONUNBUFFERED"] == "1"
    assert seen["env"]["RCUTILS_LOGGING_BUFFERED_STREAM"] == "0"
    await s.stop()


async def test_W35_套了_stdbuf_照样认出底下谁死了(tmp_path):
    """解析不变:套上 stdbuf(这台机器上有就真套)认「process has died」照旧。"""
    assert supervisor.shutil.which("stdbuf"), "开发机上该有 coreutils 的 stdbuf"
    s, r = _sup(tmp_path, mode="launch_child_segv", backoff=(30.0,))
    await s.start(Path("/maps/a/prior.mm"))
    await _等(lambda: r.downs)
    assert "mola-1 退出码 -11" in r.downs[0] and "SIGSEGV" in r.downs[0], r.downs
    await s.stop()
