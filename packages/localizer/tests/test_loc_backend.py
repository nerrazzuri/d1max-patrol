"""MOLA 后端(W09b 决定 2):找先验、起 MOLA、重定位换算、卡住重启、重启后自己重定位;位姿与质量配对。
不要 ROS:看管器与重定位服务都用假的。"""

from __future__ import annotations

import asyncio
import math

import pytest
from d1max_localizer.backend import FRAMES_FILE, PRIOR_FILE, MolaBackend, Pairer
from d1max_localizer.core import AUTO_RELOC_SIGMA_M, STALL_S, Estimate, LocalizerCore
from d1max_localizer.frames import Frames, mat_to_quat

from d1max_contract.locbridge import Pose, State

FLAT = Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
              sensor_height=0.5, sensor_in_base=(0.4043, 0.0))
M = ("estate-1", "7")


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class 假看管:
    def __init__(self):
        self.started, self.restarts = [], []
        self.running = False
        self.prior = None
        self.backend = None

    async def start(self, prior):
        self.started.append(prior)
        self.prior, self.running = prior, True
        self.backend.on_up()

    async def restart(self, why):
        self.restarts.append(why)
        self.backend.on_down(why)
        self.backend.on_up()


class 假服务:
    def __init__(self):
        self.calls = []
        self.accept = True

    async def __call__(self, p, q, sigma):
        self.calls.append((p, q, sigma))
        return self.accept


def _先验(tmp_path, name="m7", frames=FLAT):
    d = tmp_path / name
    d.mkdir()
    (d / PRIOR_FILE).write_bytes(b"mm")
    if frames is not None:
        frames.save(d / FRAMES_FILE)
    return d


def _台(tmp_path, default_dir=None):
    c = 钟()
    core = LocalizerCore(monotonic=c)
    sup, svc = 假看管(), 假服务()
    be = MolaBackend(core, supervisor=sup, reloc_service=svc, default_prior_dir=default_dir,
                     monotonic=c)
    sup.backend = be
    return c, core, sup, svc, be


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_换先验_找得到就起_MOLA_起来了等人给初值(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    d = _先验(tmp_path)
    assert await be.load_prior(M, str(d)) == ""
    await _settle()
    assert sup.started == [d / PRIOR_FILE]
    core.tick()
    assert [m.reason for m in core.drain() if isinstance(m, State)][-1] == "等人给初始位置"
    assert await be.load_prior(M, str(d)) == "", "同一张图、在跑:当没事"
    await _settle()
    assert len(sup.started) == 1


@pytest.mark.parametrize("what,want", [("no_dir", "没有给先验目录"), ("no_mm", "没有 prior.mm"),
                                       ("no_frames", "frames"), ("bad_frames", "frames")])
async def test_换先验_找不到_说原因_不动现在的(tmp_path, what, want):
    c, core, sup, svc, be = _台(tmp_path)
    if what == "no_dir":
        d = ""
    else:
        d = _先验(tmp_path, frames=None if what == "no_frames" else FLAT)
        if what == "no_mm":
            (d / PRIOR_FILE).unlink()
        if what == "bad_frames":
            (d / FRAMES_FILE).write_text("{}")
    why = await be.load_prior(M, str(d))
    assert want in why, why
    assert sup.started == []


async def test_代理没给目录_用本机配的默认先验(tmp_path):
    d = _先验(tmp_path)
    c, core, sup, svc, be = _台(tmp_path, default_dir=d)
    assert await be.load_prior(M, "") == ""
    await _settle()
    assert sup.started == [d / PRIOR_FILE]


async def test_重定位_平面位姿换成_MOLA_的三维初值(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    be.mola_output()
    assert await be.relocalize(1.0, 2.0, math.pi / 2, 0.5, req=4) == ""
    [(p, q, sigma)] = svc.calls
    # 雷达在狗身前 0.4 m,狗朝北:雷达在狗身中心北边 0.4 m
    assert (round(p[0], 4), round(p[1], 4), round(p[2], 4)) == (1.0, 2.4043, 0.5)
    assert sigma == 0.5
    assert core.want_reloc() is None and core.can_relocalize()
    core.tick()
    assert [m.reason for m in core.drain() if isinstance(m, State)][-1] != "等人给初始位置", \
        "收下了就告诉核心"
    svc.accept = False
    assert "没收下" in await be.relocalize(1.0, 2.0, 0.0, 0.5)


async def test_MOLA_刚起来还没出过位姿_人给的位置先记着_出了第一帧再下发(tmp_path):
    """2026-09-27 实跑:MOLA 还没收到点云时收下的重定位,被它第一帧点云上的初始定位盖掉了。"""
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    assert await be.relocalize(1.0, 2.0, 0.0, 0.5, req=4) == "", "先记着,回收下"
    assert svc.calls == []
    await be.check()
    assert svc.calls == [], "MOLA 还没出过位姿"
    be.mola_output()
    await be.check()
    assert len(svc.calls) == 1
    await be.check()
    assert len(svc.calls) == 1, "下发过了就不再发"


async def test_卡住了就重启_重启之后按最后可信的位置自己重定位(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    be.mola_output()
    assert await be.relocalize(3.0, 1.0, 0.0, 0.5, req=1) == ""
    stamp = 5000.0
    c.t += 40.0                                          # 过了重定位之后的稳定期
    for _ in range(5):
        c.t += 0.1
        stamp += 0.1
        core.on_scan()
        core.on_estimate(Estimate(stamp=stamp, p=(3.4043, 1.0, 0.5), q=(0.0, 0.0, 0.0, 1.0),
                                  quality=0.95))
    for _ in range(int(STALL_S * 10) + 3):               # 点云在来,MOLA 不出了
        c.t += 0.1
        core.on_scan()
        core.tick()
        await be.check()
    assert sup.restarts == ["定位程序卡住了,在重启"]
    assert len(svc.calls) == 1, "重启之后 MOLA 还没出过位姿:先不请"
    be.mola_output()
    await be.check()
    await be.check()
    assert len(svc.calls) == 2, "出了第一帧就自己请重定位,只请一次"
    p, q, sigma = svc.calls[-1]
    assert (round(p[0], 3), round(p[1], 3)) == (3.404, 1.0) and sigma == AUTO_RELOC_SIGMA_M
    core.tick()
    assert [m.state for m in core.drain() if isinstance(m, State)][-1] != "initializing"


async def test_自己重定位没成_过一会儿再试(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    be.mola_output()
    assert await be.relocalize(0.0, 0.0, 0.0, 0.5, req=1) == ""
    c.t += 40.0
    core.on_scan()
    for stamp in (1.0, 1.1):                              # 重定位后第一帧带跳变,第二帧才算可信
        core.on_estimate(Estimate(stamp=stamp, p=(0.4043, 0.0, 0.5), q=(0.0, 0.0, 0.0, 1.0),
                                  quality=0.95))
    be.on_down("定位程序退出了")
    be.on_up()
    be.mola_output()
    svc.calls.clear()
    svc.accept = False                                    # 刚起来,服务还没好
    await be.check()
    assert len(svc.calls) == 1
    await be.check()
    assert len(svc.calls) == 1, "别一拍一请"
    c.t += 1.5
    svc.accept = True
    await be.check()
    assert len(svc.calls) == 2 and core.want_reloc() is None


def test_位姿跟质量配对_谁先到都行_太久没配上的丢掉():
    got = []
    pr = Pairer(got.append, max_gap_s=0.2)
    q = mat_to_quat(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    pr.pose(10.0, (1.0, 0.0, 0.0), q, at=0.0)
    pr.quality(0.9, at=0.01)
    pr.quality(0.8, at=0.11)                              # 质量先到
    pr.pose(10.1, (1.1, 0.0, 0.0), q, at=0.12)
    pr.pose(10.2, (1.2, 0.0, 0.0), q, at=0.2)            # 这帧的质量没来
    pr.pose(10.3, (1.3, 0.0, 0.0), q, at=0.5)
    pr.quality(0.7, at=0.51)
    pr.quality(0.6, at=0.6)                               # 质量来了,位姿 0.3 s 后才来:不配
    pr.pose(10.4, (1.4, 0.0, 0.0), q, at=0.9)
    assert [(e.stamp, e.quality) for e in got] == [(10.0, 0.9), (10.1, 0.8), (10.3, 0.7)]
    assert all(isinstance(e, Estimate) for e in got)
    assert not [x for x in got if isinstance(x, Pose)]


async def test_记着的重定位_换了图就作废(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    assert await be.relocalize(1.0, 2.0, 0.0, 0.5, req=4) == ""      # MOLA 还没出过位姿:记着
    await be.load_prior(("estate-1", "8"), str(_先验(tmp_path, name="m8")))
    await _settle()
    be.mola_output()
    await be.check()
    assert svc.calls == [], "旧图上给的位置不能拿到新图上用"
