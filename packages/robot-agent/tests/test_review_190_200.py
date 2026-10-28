"""外审 2026-10-29(PR #190–#200,reviews/2026-10-29-PR190-200-50b51a8-review.md)的复现:
R1、R2、R4–R7。
R3(IMU 安装角)在 localizer 包的 test_loc_imu.py。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
from types import SimpleNamespace as NS

import numpy as np
import pytest
import test_bridge_localizer as b

from d1max_contract.hal import Odometry
from d1max_contract.locbridge import Relocalize

# ---------------------------------------------------------------- R1 原地转:还在转的那股劲也查


def _前左有个点():
    from d1max_agent.obstacles import ObstacleView
    from d1max_contract.obsbridge import Grid, pack_bits
    v = ObstacleView(monotonic=lambda: 0.0, latency_s=0.0)
    v.note_odom(0, 0, 0)
    n, res = 80, 0.05
    occ = np.zeros((n, n), bool)
    occ[int(0.375 / res + n / 2), int(0.325 / res + n / 2)] = True     # 机身左前 (0.375, 0.325)
    v.on_grid(Grid(seq=1, stamp_ns=0, res=res, size=n, occ=pack_bits(occ.ravel().tolist(), n),
                   known=pack_bits([True] * (n * n), n), check="ok"))
    return v


def test_外审R1_正在往左快转_新命令往右慢转_左边那股劲扫到的也要查():
    from d1max_agent.obstacles import ObstacleGuard
    g, v = ObstacleGuard(), _前左有个点()
    assert g.check(0.0, -0.32, 0.0, v, (0, 0, 0)).ok                  # 只看新命令:往右,放行
    assert not g.check(0.0, 1.5, 0.0, v, (0, 0, 0)).ok
    # 上一拍发的 +1.5 还在执行(延迟里接着转、再刹停):这条往右慢转也要拦
    assert not g.check(0.0, -0.32, 0.0, v, (0, 0, 0), w_now=(1.5,)).ok
    # 实测在转(里程报的转速)同样算
    assert not g.check(0.0, -0.32, 0.0, v, (0, 0, 0), w_now=(0.0, 1.5)).ok
    # 本来就往右转:左边那点不扫到
    assert g.check(0.0, -0.32, 0.0, v, (0, 0, 0), w_now=(-0.3,)).ok


def _左后看不见():
    import test_obstacle_avoid as ob

    from d1max_agent.obstacles import ObstacleView
    n = 80
    c = [(i - n / 2 + 0.5) * 0.05 for i in range(n)]
    known = [(r, k) for r in range(n) for k in range(n)
             if not (abs(c[r] + 0.4) < 0.1 and abs(c[k] - 0.4) < 0.1)]
    v = ObstacleView(monotonic=lambda: 0.0)
    v.note_odom(0, 0, 0)
    v.on_grid(ob._grid(cells_known=known, size=n, res=0.05))
    return v


def test_复查N1_里程转速一点点反向噪声_不把反向那一侧也查上():
    """复查 N1:里程报 −0.001 rad/s 的噪声,原来也按完整角余量(0.2 rad)查反向那一侧,左后一小块
    看不见就把往左对准一直挡住。噪声以下的不算在转;还在转的那股劲只按延迟 + 刹停算,不再加余量。"""
    from d1max_agent.obstacles import ObstacleGuard
    g, v = ObstacleGuard(), _左后看不见()
    assert g.check(0.0, 0.4, 0.0, v, (0, 0, 0)).ok
    for w in ((0.0, -0.001), (0.0, -0.03), (-0.02, 0.0)):
        assert g.check(0.0, 0.4, 0.0, v, (0, 0, 0), w_now=w).ok, w
    assert not g.check(0.0, 0.4, 0.0, v, (0, 0, 0), w_now=(-1.0,)).ok      # 真在往右转:照样拦


async def test_外审R1_导航把实测转速和上一拍发的转速都交给守卫():
    from d1max_agent.bridges.planned_nav import PlannedNavBackend
    seen = []

    class 守卫:
        def check(self, vx, wz, v_meas, view, pose, *, w_now=()):
            seen.append(tuple(w_now))
            return NS(ok=True, reason="", hits=(), unknown=0)

    nav = PlannedNavBackend.__new__(PlannedNavBackend)
    nav.guard, nav.obstacles = 守卫(), None
    nav._v_meas = nav._w_meas = nav._last_wz = 0.0
    nav._odom_pose = (0.0, 0.0, 0.0)
    nav._odom_seen(NS(x=0.0, y=0.0, yaw=0.0, vx=0.0, wz=0.9))
    nav._last_wz = 1.5
    nav._guard_check(0.0, -0.32)
    assert set(seen[-1]) == {0.9, 1.5}


# ---------------------------------------------------------------- R2 重启后朝向那条线照旧


async def test_外审R2_代理重启_最后可信位置的朝向线跟着落盘_错的朝向稳下来也不认(tmp_path):
    keep = tmp_path / "anchor.json"
    c = b.钟()
    loc, _ = b._代理(c, keep)
    await b._接上(loc, (0.0, 0.0, 0.0))
    b._稳(loc, 10.0, 5.0, 0.0)
    assert loc._anchor_yaw == 0.5
    c.t += 0.1
    resumed, _ = b._代理(c, keep)
    await b._接上(resumed, (0.0, 0.0, 0.0))
    assert resumed._anchor_yaw == 0.5
    for _ in range(b.SETTLE_FIXES + 3):
        resumed.on_pose(b._位姿(10.0, 5.0, 0.8))                     # 位置对、朝向错 46°
    assert not resumed.ok(True)


async def test_外审R2_老格式的盘上没记朝向线_按位置线推(tmp_path):
    from d1max_agent.bridge_localizer import (
        ANCHOR_TRUSTED_M,
        ANCHOR_TRUSTED_YAW_RAD,
        ANCHOR_YAW_RAD,
    )
    keep = tmp_path / "anchor.json"
    c = b.钟()
    loc, _ = b._代理(c, keep)
    await b._接上(loc, (0.0, 0.0, 0.0))
    b._稳(loc, 10.0, 5.0, 0.0)
    for line, want in ((ANCHOR_TRUSTED_M, ANCHOR_TRUSTED_YAW_RAD), (3.0, ANCHOR_YAW_RAD)):
        d = json.loads(keep.read_text(encoding="utf-8"))
        d.pop("anchor_yaw", None)
        d["anchor_m"] = line
        keep.write_text(json.dumps(d), encoding="utf-8")
        again, _ = b._代理(c, keep)
        await b._接上(again, (0.0, 0.0, 0.0))
        assert again._anchor_yaw == want


# ------------------------------------------------ R4 两处核对 / 稳下来的核对:算的时候动了


def _假狗(view):
    async def odometry():
        x, y, yaw = view.odom[-1][1]
        return Odometry(stamp_ms=0, frame_id="odom", x=x, y=y, yaw=yaw, vx=0.0, wz=0.0,
                        valid=True)
    return NS(odometry=odometry)


async def _对不上的场面(tmp_path, *, conflict=True):
    from test_globalloc import RES, _房间, _扫, 不对称

    from d1max_agent.globalloc import GlobalLoc, Matcher
    from d1max_agent.obstacles import ObstacleView
    from d1max_agent.runtime import AgentRuntime
    c, link, loc = await b._就绪()
    true = (3.0, 3.0, 0.4)
    if conflict:
        await loc.relocalize(b.M, true)                              # 人给对了、定位器锁到 7 m 那儿
        req = [r for r in link.sent if isinstance(r, Relocalize)][-1]
        loc.on_pose(b._位姿(7.0, 3.0, 0.4, jump=True, reloc_id=req.req))
        for _ in range(b.SETTLE_FIXES + 3):
            loc.on_pose(b._位姿(7.0, 3.0, 0.4))
        assert loc.conflict_poses() is not None
    else:
        b._稳(loc, 7.0, 3.0, 0.4)                                     # 定位器锁错、自己说可信
    occ, free = _房间(extra=不对称)
    pts = _扫(occ, *true, noise=0.02)
    view = ObstacleView(monotonic=c)
    for _ in range(21):
        c.t += 0.1
        view.note_odom(0.0, 0.0, 0.0)
        loc.update((0.0, 0.0, 0.0), True)
    view.on_wall(NS(points=lambda: pts))
    glob = GlobalLoc(view)
    glob.set_map(b.M, tmp_path)
    glob._m = (b.M, Matcher(occ, free, RES, (0.0, 0.0)))
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.parts = NS(nav=NS(anchor=loc))
    rt.loaded_map, rt.globalloc, rt.hal = b.M, glob, _假狗(view)
    rt._mono, rt._scan_relocs = c, 0
    rt.events = NS(emit=lambda k, d: None)
    return c, link, loc, view, glob, rt


def _算的时候走了60厘米(c, loc, view):
    for i in range(1, 7):
        c.t += 0.1
        loc.update((0.1 * i, 0.0, 0.0), True)
        view.note_odom(0.1 * i, 0.0, 0.0)


async def test_外审R4_两处核对算的时候机器人走了_结论作废不提交(tmp_path):
    c, link, loc, view, glob, rt = await _对不上的场面(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    real = glob.fits

    async def 慢(points, poses):
        entered.set()
        await release.wait()
        return await real(points, poses)
    glob.fits = 慢
    job = asyncio.create_task(rt._conflict_check())
    await asyncio.wait([job, asyncio.create_task(entered.wait())],
                       return_when=asyncio.FIRST_COMPLETED)
    assert entered.is_set(), "没走到核对那一步"
    _算的时候走了60厘米(c, loc, view)
    release.set()
    await job
    assert len([r for r in link.sent if isinstance(r, Relocalize)]) == 1   # 只有人给的那次


async def test_外审R4_两处核对算的时候没动_照样按人给的那处提交(tmp_path):
    c, link, loc, view, glob, rt = await _对不上的场面(tmp_path)
    await rt._conflict_check()
    sent = [r for r in link.sent if isinstance(r, Relocalize)]
    assert len(sent) == 2 and math.hypot(sent[-1].x - 3.0, sent[-1].y - 3.0) < 1e-6


async def test_外审R4_稳下来后的点云核对算的时候走了_不按认出的那处重定位(tmp_path):
    c, link, loc, view, glob, rt = await _对不上的场面(tmp_path, conflict=False)
    real = glob.check
    entered, release = asyncio.Event(), asyncio.Event()

    async def 慢(points, pose):
        entered.set()
        await release.wait()
        return await real(points, pose)
    glob.check = 慢
    job = asyncio.create_task(rt._scan_check(c.t - 10.0))
    await asyncio.wait([job, asyncio.create_task(entered.wait())],
                       return_when=asyncio.FIRST_COMPLETED)
    assert entered.is_set(), "没走到核对那一步"
    _算的时候走了60厘米(c, loc, view)
    release.set()
    await job
    assert not [r for r in link.sent if isinstance(r, Relocalize)]
    assert not loc.scan_disagree


async def test_外审R4_稳下来后的点云核对没动_照样认出对的那处(tmp_path):
    c, link, loc, view, glob, rt = await _对不上的场面(tmp_path, conflict=False)
    await rt._scan_check(c.t - 10.0)
    sent = [r for r in link.sent if isinstance(r, Relocalize)]
    assert len(sent) == 1 and math.hypot(sent[0].x - 3.0, sent[0].y - 3.0) < 0.3


# ---------------------------------------------------------------- R5 / R6 拍照


async def test_外审R5_拍照失败的原因不带相机口令(tmp_path):
    from d1max_agent.bridges.rtsp_media import FfmpegCamera
    from d1max_patrol.backends.base import MediaError
    ff = tmp_path / "ffmpeg"
    ff.write_text("#!/bin/sh\n"
                  'echo "rtsp://admin:FAKE_SECRET_190@10.0.0.9/front: Connection refused" >&2\n'
                  "exit 1\n")
    ff.chmod(0o755)
    cam = FfmpegCamera(["-i", "rtsp://admin:FAKE_SECRET_190@10.0.0.9/front"], now_ms=lambda: 0,
                       ffmpeg=str(ff))
    with pytest.raises(MediaError) as exc:
        await cam.grab()
    assert "FAKE_SECRET_190" not in str(exc.value)
    assert "Connection refused" in str(exc.value)


async def _起一个卡住的(tmp_path):
    from d1max_agent.bridges.rtsp_media import FfmpegCamera
    pid, ff = tmp_path / "pid", tmp_path / "ffmpeg"
    ff.write_text(f"#!/bin/sh\necho $$ > {pid}\nexec sleep 30\n")
    ff.chmod(0o755)
    cam = FfmpegCamera([], now_ms=lambda: 0, ffmpeg=str(ff), timeout_s=5.0)
    task = asyncio.create_task(cam.grab())
    for _ in range(200):
        if pid.exists() and pid.read_text().strip():
            break
        await asyncio.sleep(0.01)
    return cam, task, int(pid.read_text())


def _活着(p):
    try:
        os.kill(p, 0)
    except ProcessLookupError:
        return False
    with contextlib.suppress(ChildProcessError):
        if os.waitpid(p, os.WNOHANG)[0] == p:
            return False
    return True


async def test_外审R6_拍照被取消_ffmpeg_跟着收掉(tmp_path):
    cam, task, p = await _起一个卡住的(tmp_path)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert not _活着(p)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(p, signal.SIGKILL)


async def test_外审R6_关掉取图源_还在抽帧的_ffmpeg_收掉(tmp_path):
    from d1max_patrol.backends.base import MediaError
    cam, task, p = await _起一个卡住的(tmp_path)
    try:
        await cam.close()
        with pytest.raises((MediaError, asyncio.CancelledError)):
            await asyncio.wait_for(task, 2.0)
        await asyncio.sleep(0.1)
        assert not _活着(p)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(p, signal.SIGKILL)


# ---------------------------------------------------------------- R7 提前绕看出终点被盖住:马上报


async def test_外审R7_提前绕看出终点被盖住_马上报_不等走到跟前(tmp_path):
    import test_obstacle_avoid as ob

    from d1max_patrol.protocol.nav_types import Pose
    t = ob.台子(tmp_path, rear=True, cone=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(7.0, 4.0))
    await t.跑(1.0)
    t.world.dyn["box"] = (6.7, 3.6, 7.3, 4.4)
    covered_at = told_at = None
    for _ in range(400):
        await t.一拍()
        if t.nav._goal_covered and covered_at is None:
            covered_at = t.c.s()
        told = [d for k, d in t.events if k == "nav_blocked"]
        if told:
            told_at = t.c.s()
            assert "终点被障碍盖住" in told[0]["reason"]
            break
    assert covered_at is not None and told_at is not None
    assert told_at - covered_at < 0.5
    # 后来真被挡住:不再重复报
    for _ in range(100):
        await t.一拍()
    assert len([1 for k, _ in t.events if k == "nav_blocked"]) == 1


async def test_外审R7_提前报过终点被盖住_没真被挡就停了_站点那边的被挡清掉(tmp_path):
    import test_obstacle_avoid as ob

    from d1max_patrol.protocol.nav_types import Pose
    t = ob.台子(tmp_path, rear=True, cone=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(7.0, 4.0))
    await t.跑(1.0)
    t.world.dyn["box"] = (6.7, 3.6, 7.3, 4.4)
    for _ in range(100):
        await t.一拍()
        if [1 for k, _ in t.events if k == "nav_blocked"]:
            break
    assert t.nav._blocked_since is None                              # 还没走到跟前
    await t.nav.stop()
    kinds = [k for k, _ in t.events]
    assert kinds.count("nav_unblocked") == 1
    assert kinds.index("nav_unblocked") > kinds.index("nav_blocked")


@pytest.mark.parametrize("bias", [0.0, -0.001])
async def test_复查N1_里程转速带噪声_原地往左对准照样对得上(tmp_path, bias):
    import dataclasses

    import test_obstacle_avoid as ob

    from d1max_patrol.protocol.nav_types import NavStatus, Pose
    n = 80
    c = [(i - n / 2 + 0.5) * 0.05 for i in range(n)]
    known = [(r, k) for r in range(n) for k in range(n)
             if not (abs(c[r] + 0.4) < 0.1 and abs(c[k] - 0.4) < 0.1)]

    def 格():
        return ob._grid(cells_known=known, cells_occ=[(26, 40)], size=n, res=0.05)
    t = ob.台子(tmp_path, rear=True)
    await t.start(3, 4)
    t.view.frames.clear()
    t.view.on_grid(格())
    t._feed = lambda: t.view.on_grid(格())
    original = t.r.odometry

    async def 带噪声():
        return dataclasses.replace(await original(), wz=bias)
    t.r.odometry = 带噪声
    await t.nav.goto(Pose.from_xy_yaw(3, 4, 0.2))
    await t.跑(24)
    assert NavStatus.SUCCEED in t.status and t.r.yaw > 0


async def test_复查N3_取图源关的时候ffmpeg正在起_起来了也收掉(tmp_path):
    from d1max_agent.bridges.rtsp_media import FfmpegCamera
    from d1max_patrol.backends.base import MediaError
    pid, ff = tmp_path / "pid", tmp_path / "ffmpeg"
    ff.write_text(f"#!/bin/sh\necho $$ > {pid}\nexec sleep 30\n")
    ff.chmod(0o755)
    cam = FfmpegCamera([], now_ms=lambda: 0, ffmpeg=str(ff), timeout_s=10.0)
    task = asyncio.create_task(cam.grab())
    try:
        await asyncio.sleep(0)                                       # 正卡在起进程那一步
        assert not cam._procs
        await cam.close()
        with pytest.raises(MediaError):
            await asyncio.wait_for(task, 3.0)
        for _ in range(100):
            if pid.exists() and pid.read_text().strip():
                break
            await asyncio.sleep(0.01)
        if pid.exists() and pid.read_text().strip():
            await asyncio.sleep(0.1)
            assert not _活着(int(pid.read_text()))
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if pid.exists() and pid.read_text().strip():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid.read_text()), signal.SIGKILL)


async def test_复查N4_提前报过又真被挡住_停下这一段_站点那边的被挡清掉(tmp_path):
    import test_obstacle_avoid as ob

    from d1max_patrol.protocol.nav_types import Pose
    t = ob.台子(tmp_path, rear=True, cone=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(7.0, 4.0))
    await t.跑(1.0)
    t.world.dyn["box"] = (6.7, 3.6, 7.3, 4.4)
    for _ in range(150):
        await t.一拍()
        if t.nav._blocked_told and t.nav._blocked_since is not None:
            break
    assert t.nav._blocked_told and t.nav._blocked_since is not None
    await t.nav.stop()
    kinds = [k for k, _ in t.events]
    assert kinds.count("nav_blocked") == 1 and kinds.count("nav_unblocked") == 1


async def test_复查N4_被挡放弃这一段_不发不挡了_引擎还要重试同一个点(tmp_path):
    import test_obstacle_avoid as ob

    from d1max_patrol.protocol.nav_types import NavStatus, Pose
    t = ob.台子(tmp_path, rear=True, cone=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(7.0, 4.0))
    await t.跑(1.0)
    t.world.dyn["box"] = (6.7, 3.6, 7.3, 4.4)
    await t.跑(40.0)
    assert NavStatus.FAILED in t.status
    assert "nav_unblocked" not in [k for k, _ in t.events]


async def test_复查N4_放弃这一段以后_箱子挪走重试成功_补发不挡了(tmp_path):
    import test_obstacle_avoid as ob

    from d1max_patrol.protocol.nav_types import NavStatus, Pose
    t = ob.台子(tmp_path, rear=True, cone=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(7.0, 4.0))
    await t.跑(1.0)
    t.world.dyn["box"] = (6.7, 3.6, 7.3, 4.4)
    await t.跑(40.0)
    assert NavStatus.FAILED in t.status
    assert "nav_unblocked" not in [k for k, _ in t.events]
    del t.world.dyn["box"]
    for _ in range(310):                                             # 感知照常刷新,临时障碍过期
        await t.一拍()
    begin = len(t.status)
    await t.nav.goto(Pose.from_xy_yaw(7.0, 4.0))
    await t.跑(30.0)
    assert NavStatus.SUCCEED in t.status[begin:]
    kinds = [k for k, _ in t.events]
    assert "nav_unblocked" in kinds[kinds.index("nav_blocked"):]


async def test_复查N4_放弃以后_改去别处一路畅通走起来了_当场补发不挡了(tmp_path):
    import test_obstacle_avoid as ob

    from d1max_patrol.protocol.nav_types import NavStatus, Pose
    t = ob.台子(tmp_path, rear=True, cone=True)
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(7.0, 4.0))
    await t.跑(1.0)
    t.world.dyn["box"] = (6.7, 3.6, 7.3, 4.4)
    await t.跑(40.0)
    assert NavStatus.FAILED in t.status
    del t.world.dyn["box"]
    for _ in range(310):
        await t.一拍()
    await t.nav.set_speed(0.08, 0.0, 1.5)
    x = t.r.x
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    for _ in range(200):
        await t.一拍()
    assert t.r.x - x > 1.0 and t.status[-1] is NavStatus.ACTIVE
    assert [k for k, _ in t.events].count("nav_unblocked") == 1, "走起来了就清,不等到终点"
    await t.跑(70.0)
    assert [k for k, _ in t.events].count("nav_unblocked") == 1, "终点不再重复清"
