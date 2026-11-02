"""W68 Super-LIO 后端:载图(没 Super-LIO 点云 = 要重建)、没初值就不起、给位置 = 换初值重启、
卡住/挡过重启按最后可信的位置。不要 ROS:看管器用假的(起的时候照样调后端出命令行)。"""

from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path

import numpy as np
import pytest
from d1max_localizer import slioloc as L
from d1max_localizer.backend import FRAMES_FILE
from d1max_localizer.core import Estimate, LocalizerCore
from d1max_localizer.frames import Frames
from d1max_localizer.slioback import SlioBackend

from d1max_contract.locbridge import Pose, State

R_IL = np.array([-0.017043, -0.999836, 0.006192, -0.999855, 0.017041, -0.000495,
                 0.00039, -0.006199, -0.999981]).reshape(3, 3).T
EXT = (R_IL.tolist(), [0.004279, 0.004176, -0.004436])
FR = Frames(up=(0.0, 0.0, 1.0), sensor_up=(-1.0, 0.0, 0.0), sensor_forward=(0.0, 0.0, 1.0),
            sensor_height=0.2, sensor_in_base=(0.4043, 0.0))
BASE = Path(__file__).resolve().parents[3] / "deploy/superlio/d1max.yaml"
M = ("estate-1", "7")


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class 假看管:
    """起 = 调后端出命令行(真看管器 ``_spawn`` 也是这样)再报起来了。"""

    def __init__(self):
        self.cmds, self.restarts = [], []
        self.running = False
        self.prior = None
        self.backend = None
        self.stops = 0

    async def start(self, prior):
        self.prior = prior
        self.cmds.append(self.backend.command(prior))
        self.running = True
        self.backend.on_up()

    async def restart(self, why):
        self.restarts.append(why)
        self.backend.on_down(why)
        self.cmds.append(self.backend.command(self.prior))
        self.backend.on_up()

    async def stop(self):
        self.stops += 1
        self.running = False


def _装(tmp_path) -> Path:
    root = tmp_path / "superlio"
    src = root / "ws/src/Super-LIO/src/super_lio"
    src.mkdir(parents=True)
    (src / "CMakeLists.txt").write_text("")
    exe = L.reloc_exe(root)
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return root


def _图(tmp_path, name="m7", *, pcd=True) -> Path:
    d = tmp_path / name
    d.mkdir()
    FR.save(d / FRAMES_FILE)
    if pcd:
        g = np.mgrid[-5:5:0.2, -5:5:0.2].reshape(2, -1).T
        L.write_pcd(d / L.MAP_FILE, np.c_[g, np.full(len(g), 1.0)])
    return d


def _台(tmp_path, *, root=None):
    c = 钟()
    core = LocalizerCore(monotonic=c)
    sup = 假看管()
    fits = []
    be = SlioBackend(core, supervisor=sup, ext=EXT, base_config=BASE.read_text(encoding="utf-8"),
                     root=_装(tmp_path) if root is None else root, run_dir=tmp_path / "run",
                     on_map=fits.append, monotonic=c)
    sup.backend = be
    return c, core, sup, be, fits


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _init(cmd) -> list[float]:
    cfg = Path(cmd[cmd.index("--params-file") + 1]).read_text(encoding="utf-8")
    m = re.search(r"lio\.relocation\.init_pose: \[([^\]]*)\]", cfg)
    return [float(v) for v in m.group(1).split(",")]


def _states(core):
    core.tick()
    return [(m.state, m.reason) for m in core.drain() if isinstance(m, State)]


def _est(core, be, x, y, yaw, stamp, q=0.95):
    """按机身位姿造一帧 Super-LIO 的估计(雷达位姿,跟适配层换算后给核心的一样)。"""
    p, R = L.imu_pose(FR, EXT, x, y, yaw)
    lp, lq = L.lidar_pose(p, L.mat_quat(R), EXT)
    core.on_scan()
    core.on_estimate(Estimate(stamp=stamp, p=lp, q=lq, quality=q))


async def test_没有Super_LIO点云的图_说要重建_不起(tmp_path):
    _, _, sup, be, _ = _台(tmp_path)
    why = await be.load_prior(M, str(_图(tmp_path, pcd=False)))
    assert "需要重建" in why
    assert sup.cmds == []


async def test_Super_LIO没装_说未装(tmp_path):
    _, _, sup, be, _ = _台(tmp_path, root=tmp_path / "nothing")
    why = await be.load_prior(M, str(_图(tmp_path)))
    assert "Super-LIO 未装" in why
    assert sup.cmds == []


async def test_新图没初值_不起_等人给_地图交给套图打分(tmp_path):
    _, core, sup, be, fits = _台(tmp_path)
    assert await be.load_prior(M, str(_图(tmp_path))) == ""
    await _settle()
    assert sup.cmds == []
    assert _states(core)[-1] == ("initializing", "等人给初始位置")
    assert core.can_relocalize()
    [fit] = fits
    assert isinstance(fit, L.MapFit)


async def test_人给位置_按IMU位姿起_对上了出位姿(tmp_path):
    c, core, sup, be, _ = _台(tmp_path)
    await be.load_prior(M, str(_图(tmp_path)))
    assert await be.relocalize(1.0, 2.0, 0.5, 0.5, req=7, human=True) == ""
    await _settle()
    [cmd] = sup.cmds
    assert cmd[0].endswith("relocation_node")
    p, R = L.imu_pose(FR, EXT, 1.0, 2.0, 0.5)
    assert _init(cmd) == pytest.approx(L.init_param(p, R), abs=1e-6)
    _est(core, be, 1.02, 2.0, 0.5, 10.0)
    core.tick()
    [pose] = [m for m in core.drain() if isinstance(m, Pose)]
    assert pose.reloc_id == 7 and pose.jump
    assert (pose.x, pose.y) == pytest.approx((1.02, 2.0), abs=1e-3)


async def test_起来以后每拍查_不会反复重启(tmp_path):
    c, core, sup, be, _ = _台(tmp_path)
    await be.load_prior(M, str(_图(tmp_path)))
    await be.relocalize(1.0, 2.0, 0.5, 0.5, req=7, human=True)
    for i in range(30):
        c.t += 0.1
        _est(core, be, 1.0, 2.0, 0.5, 10.0 + 0.1 * i)
        core.tick()
        await be.check()
        await _settle()
    assert len(sup.cmds) == 1


async def test_跟着的时候人再给位置_换初值重启(tmp_path):
    c, core, sup, be, _ = _台(tmp_path)
    await be.load_prior(M, str(_图(tmp_path)))
    await be.relocalize(1.0, 2.0, 0.5, 0.5, req=7, human=True)
    await _settle()
    await be.relocalize(-3.0, 0.0, math.pi, 0.5, req=8, human=True)
    await _settle()
    assert len(sup.cmds) == 2
    p, R = L.imu_pose(FR, EXT, -3.0, 0.0, math.pi)
    assert _init(sup.cmds[1]) == pytest.approx(L.init_param(p, R), abs=1e-6)


async def test_卡住重启_按最后可信的位置起_不按开机给的(tmp_path):
    c, core, sup, be, _ = _台(tmp_path)
    await be.load_prior(M, str(_图(tmp_path)))
    await be.relocalize(0.0, 0.0, 0.0, 0.5, req=7, human=True)
    await _settle()
    for i in range(80):                                   # 走到 x = 4
        c.t += 0.1
        _est(core, be, 0.05 * (i + 1), 0.0, 0.0, 10.0 + 0.1 * i)
        core.tick()
    for _ in range(40):                                   # 点云在来、它不吐了
        c.t += 0.1
        core.on_scan()
        core.tick()
    await be.check()
    await _settle()
    assert sup.restarts and len(sup.cmds) == 2
    p, R = L.imu_pose(FR, EXT, 4.0, 0.0, 0.0)
    assert _init(sup.cmds[1])[:2] == pytest.approx(L.init_param(p, R)[:2], abs=0.06)


async def test_同一张图再载_有最后可信的位置就直接起(tmp_path):
    c, core, sup, be, _ = _台(tmp_path)
    d = _图(tmp_path)
    await be.load_prior(M, str(d))
    await be.relocalize(1.0, 2.0, 0.5, 0.5, req=7, human=True)
    await _settle()
    for i in range(30):
        c.t += 0.1
        _est(core, be, 1.0, 2.0, 0.5, 10.0 + 0.1 * i)
        core.tick()
    sup.running = False                                   # 代理重启后再让载一次
    assert await be.load_prior(M, str(d)) == ""
    await _settle()
    assert len(sup.cmds) == 2
    assert _init(sup.cmds[1]) == pytest.approx(_init(sup.cmds[0]), abs=0.01)
