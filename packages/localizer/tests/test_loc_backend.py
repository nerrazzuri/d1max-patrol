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

    async def stop(self):
        self.stops = getattr(self, "stops", 0) + 1
        self.running = False


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
        await _settle()
    assert sup.restarts == ["定位程序卡住了,在重启"], "只重启一回"
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


async def test_换了图_旧_MOLA_出过位姿不算_新图上人给的位置先记着(tmp_path):
    """W09b 内审应修 7:原来换图不清「出过位姿」,人给的位置发给了还在关的旧 MOLA,核心不收、后端却回
    收下,也没记着。"""
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    be.mola_output()
    await be.load_prior(("estate-1", "8"), str(_先验(tmp_path, name="m8")))
    assert await be.relocalize(1.0, 2.0, 0.0, 0.5, req=4) == ""
    assert svc.calls == [], "新图的 MOLA 还没出过位姿:记着"
    await _settle()
    be.mola_output()
    await be.check()
    assert len(svc.calls) == 1


async def test_核心这会儿收不了_MOLA_收下的那次也记着(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    be.mola_output()
    core.prior_loading(M)                                 # 核心在载图(比如重启途中)
    assert await be.relocalize(1.0, 2.0, 0.0, 0.5, req=4) == ""
    core.prior_loaded(M, FLAT)
    core.backend_started()
    c.t += 2.0
    await be.check()
    assert len(svc.calls) == 2, "记着,核心收得了再下发"


async def test_记着的重定位放太久就扔掉(tmp_path):
    from d1max_localizer.backend import PENDING_MAX_S
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    assert await be.relocalize(1.0, 2.0, 0.0, 0.5, req=4) == ""
    c.t += PENDING_MAX_S + 1
    await be.check()
    be.mola_output()
    c.t += 2.0
    await be.check()
    assert svc.calls == []
    core.tick()
    assert [m.reason for m in core.drain() if isinstance(m, State)][-1] == \
        "人给的位置放太久没用上,请重新设位置"


async def test_起_MOLA_出错_不悄悄吞掉(tmp_path):
    """W09b 内审应修 6。"""
    c, core, sup, svc, be = _台(tmp_path)

    async def 炸(prior):
        raise RuntimeError("看管器坏了")
    sup.start = 炸
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    core.tick()
    assert "起 MOLA出错" in [m.reason for m in core.drain() if isinstance(m, State)][-1]


async def _跟踪着(c, core, be, x=3.0):
    """人给过位置、MOLA 稳定出位姿了:有「最后可信的位置」。"""
    be.mola_output()
    assert await be.relocalize(x, 1.0, 0.0, 0.5, req=1) == ""
    stamp = 5000.0
    c.t += 40.0
    for _ in range(5):
        c.t += 0.1
        stamp += 0.1
        core.on_scan()
        core.on_estimate(Estimate(stamp=stamp, p=(x + 0.4043, 1.0, 0.5), q=(0.0, 0.0, 0.0, 1.0),
                                  quality=0.95))


async def test_W34_代理重启后经链接给同一份先验_不重启MOLA(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    d = _先验(tmp_path)
    await be.load_prior(M, str(d))
    await _settle()
    link = tmp_path / "active"
    link.symlink_to(d)
    assert await be.load_prior(M, str(link)) == ""
    await _settle()
    assert len(sup.started) == 1, "字面不同、是同一个文件:不重启"


async def test_W34_同一张图重新载先验_按最后可信的位置自己重定位_不等人(tmp_path):
    """2026-10-08 C40221:代理每次重启,定位器都要人重新给初始位置。"""
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    await _跟踪着(c, core, be)
    other = _先验(tmp_path, "m7-copy")                    # 同一张图,换了个目录给(MOLA 要重启)
    await be.load_prior(M, str(other))
    await _settle()
    assert len(sup.started) == 2
    be.mola_output()
    await be.check()
    p, q, sigma = svc.calls[-1]
    assert len(svc.calls) == 2 and sigma == AUTO_RELOC_SIGMA_M, "自己按最后的位置请重定位"
    core.tick()
    assert "等人给初始位置" not in [m.reason for m in core.drain() if isinstance(m, State)]


async def test_W34_换了别的图_最后的位置作废_照旧等人给(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    await _跟踪着(c, core, be)
    await be.load_prior(("other", "1"), str(_先验(tmp_path, "o1")))
    await _settle()
    be.mola_output()
    await be.check()
    assert len(svc.calls) == 1, "别的图:不按旧位置请"
    core.tick()
    assert [m.reason for m in core.drain() if isinstance(m, State)][-1] == "等人给初始位置"


def _扫(c, core, valid, secs, dt=0.1):
    for _ in range(int(round(secs / dt))):
        c.t += dt
        core.on_scan(valid)
        core.tick()


async def test_W34_雷达被挡住_停掉MOLA不喂_挡着不按卡住重启_揭开马上重起自己重定位(tmp_path):
    """2026-10-08 C40221:罩住前雷达 MOLA 段错误、反复重启(退避到 16 s),揭开以后还要等。"""
    from d1max_localizer.core import LIDAR_MIN_POINTS
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    await _跟踪着(c, core, be)
    _扫(c, core, 50_000, 0.5)
    await be.check()
    _扫(c, core, 100, 0.5)                                # 不到 1 s:还不算
    await be.check()
    await _settle()
    assert getattr(sup, "stops", 0) == 0 and not core.lidar_blocked
    _扫(c, core, 100, 0.7)
    await be.check()
    await _settle()
    assert sup.stops == 1 and core.lidar_blocked
    core.tick()
    st = [m for m in core.drain() if isinstance(m, State)][-1]
    assert st.state == "lost" and "雷达被挡住了" in st.reason, st
    for _ in range(30):                                   # 挡了 3 s:点云在来、MOLA 不吐 —— 不算卡住
        _扫(c, core, 100, 0.1)
        await be.check()
    assert sup.restarts == [] and len(sup.started) == 1
    _扫(c, core, LIDAR_MIN_POINTS + 1, 0.5)               # 恢复不到 1 s:先不起
    await be.check()
    assert len(sup.started) == 1
    _扫(c, core, 50_000, 0.7)
    await be.check()
    await _settle()
    assert len(sup.started) == 2, "恢复了:马上重起,不等退避"
    be.mola_output()
    await be.check()
    assert svc.calls[-1][2] == AUTO_RELOC_SIGMA_M and len(svc.calls) == 2, "按最后可信的位置自己请"


async def test_W34_挡着的时候代理换了先验_恢复了起新的那份(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    _扫(c, core, 100, 1.2)
    await be.check()
    await _settle()
    new = _先验(tmp_path, "m8")
    assert await be.load_prior(("m", "8"), str(new)) == ""
    await _settle()
    assert len(sup.started) == 1, "挡着:不起"
    _扫(c, core, 50_000, 1.2)
    await be.check()
    await _settle()
    assert sup.started[-1] == new / PRIOR_FILE


def test_W34_有效点_有限且离雷达半米外才算():
    import struct
    from types import SimpleNamespace

    from d1max_localizer.backend import valid_points
    pts = [(1.0, 0.0, 0.0), (0.1, 0.1, 0.0), (float("nan"), 0.0, 0.0), (0.0, -3.0, 1.0)]
    data = b"".join(struct.pack("<fffI", *p, 7) for p in pts)        # 每点 16 字节
    assert valid_points(SimpleNamespace(point_step=16, data=data)) == 2
    assert valid_points(SimpleNamespace(point_step=16, data=b"")) == 0


async def test_W34外审_挡着要A_恢复后下一拍之前又要B_只起B_旧的恢复请求不盖掉新图(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    _扫(c, core, 100, 1.2)
    await be.check()
    await _settle()                                       # 挡住:停了 MOLA
    a = _先验(tmp_path, "a")
    await be.load_prior(("a", "1"), str(a))
    _扫(c, core, 50_000, 1.2)                             # 恢复了,还没到下一拍
    b = _先验(tmp_path, "b")
    await be.load_prior(("b", "1"), str(b))
    await _settle()
    await be.check()
    await _settle()
    assert sup.started[1:] == [b / PRIOR_FILE], ("A 的恢复请求不许再起,B 也不重起一遍", sup.started)
    assert core.map_ref == ("b", "1")


async def test_W34外审_挡着先后要A和B_恢复了只起最后要的B(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    _扫(c, core, 100, 1.2)
    await be.check()
    await _settle()
    await be.load_prior(("a", "1"), str(_先验(tmp_path, "a")))
    b = _先验(tmp_path, "b")
    await be.load_prior(("b", "1"), str(b))
    _扫(c, core, 50_000, 1.2)
    await be.check()
    await _settle()
    assert sup.started[1:] == [b / PRIOR_FILE]


async def test_W34外审_两回挡住之间正常换过图_第二回恢复起的是换过的那份(tmp_path):
    c, core, sup, svc, be = _台(tmp_path)
    await be.load_prior(M, str(_先验(tmp_path)))
    await _settle()
    _扫(c, core, 100, 1.2)
    await be.check()
    await _settle()
    a = _先验(tmp_path, "a")
    await be.load_prior(("a", "1"), str(a))               # 第一回挡着时要了 A
    _扫(c, core, 50_000, 1.2)
    await be.check()
    await _settle()
    assert sup.started[-1] == a / PRIOR_FILE
    b = _先验(tmp_path, "b")
    await be.load_prior(("b", "1"), str(b))               # 正常换成 B
    await _settle()
    _扫(c, core, 100, 1.2)                                # 第二回挡住
    await be.check()
    await _settle()
    _扫(c, core, 50_000, 1.2)
    await be.check()
    await _settle()
    assert sup.started[-1] == b / PRIOR_FILE, sup.started
