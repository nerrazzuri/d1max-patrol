"""W09i 前后雷达:``lidars.json``(几何初值、校验)、合并(拼进前雷达系、配对、降级)、后雷达外参标定
(点到面配准从错的初值收回来、对不上的拒)。点云来自 :mod:`d1max_localizer.lidarsim`
(两台半球雷达)。"""

from __future__ import annotations

import json
import math

import pytest

np = pytest.importorskip("numpy")

from d1max_localizer import calib  # noqa: E402
from d1max_localizer import lidarsim as ls  # noqa: E402
from d1max_localizer.lidars import (  # noqa: E402
    BASELINE_M,
    Lidars,
    LidarsError,
    _rot_about,
    geometry_guess,
    load,
)
from d1max_localizer.merge import Merger, merge_pair, transform, voxel_down  # noqa: E402

UP, FWD = (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)          # C40011:原始系 X 朝下、Z 朝前(#64)


def _世界():
    return ls.room(extra=(ls.Box((5, 3, 0), (5.5, 3.5, 1.0)), ls.Box((8, 5, 0), (8.4, 5.4, 2.0)),
                          ls.Box((3, 6, 0), (4, 6.3, 0.8))))


def _一圈(n=24):
    return [(6 + 3 * math.cos(2 * math.pi * i / n), 4 + 2 * math.sin(2 * math.pi * i / n),
             2 * math.pi * i / n + math.pi / 2) for i in range(n)]


def _扰动(T, dt, deg, axis):
    P = np.eye(4)
    P[:3, :3] = _rot_about(axis, math.radians(deg))
    P[:3, 3] = dt
    return np.asarray(T) @ P


# ------------------------------------------------------------ lidars.json

def test_几何初值_头尾对称就是仿真里的真值():
    G = np.array(geometry_guess(UP, FWD))
    assert np.allclose(G, ls.T_FRONT_REAR, atol=1e-9)
    assert np.linalg.norm(G[:3, 3]) == pytest.approx(BASELINE_M)


def test_没有文件是几何初值_没标定_坏文件不悄悄退回(tmp_path):
    p = tmp_path / "lidars.json"
    lid = load(p, up=UP, forward=FWD)
    assert not lid.calibrated and np.allclose(lid.T_front_rear, ls.T_FRONT_REAR)
    good = Lidars(T_front_rear=geometry_guess(UP, FWD), calibrated=True, residual_m=0.004)
    good.save(p)
    back = load(p, up=UP, forward=FWD)
    assert back.calibrated and back.residual_m == 0.004
    T = geometry_guess(UP, FWD)
    for bad in ({"T_front_rear": T, "calibrated": "yes"},
                {"T_front_rear": [r[:] for r in T[:3]]},
                {"T_front_rear": [[2.0, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
                {"T_front_rear": [[1.0, 0, 0, 5.0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
                {"T_front_rear": [[-1.0, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
                {"T_front_rear": [[float("nan")] * 4] * 4}, []):
        p.write_text(json.dumps(bad))
        with pytest.raises(LidarsError):
            load(p, up=UP, forward=FWD)
    p.write_text("坏")
    with pytest.raises(LidarsError):
        load(p, up=UP, forward=FWD)


# ------------------------------------------------------------ 合并

def test_合并_后雷达的点换进前雷达系_跟同一个世界对得上():
    (Twf, f, r), = ls.scans_along([(4.0, 3.0, 0.4)], _世界(), noise=0.0)
    m = merge_pair(f, r, ls.T_FRONT_REAR, voxel=0.0)
    assert len(m) == len(f) + len(r)
    w = transform(Twf, m)                                     # 放回世界系
    near_ground = np.abs(w[:, 2]) < 0.02
    assert near_ground.mean() > 0.4 and w[:, 2].min() > -0.02, "后雷达的地面点也落在地面上"
    xs = transform(Twf, transform(ls.T_FRONT_REAR, r))[:, 0]
    assert xs.min() < 1.0, "后雷达看见了狗身后的墙(前雷达看不见)"
    assert len(voxel_down(m, 0.05)) < len(m)


def test_合并_配不上的前雷达帧照样出_只有前雷达那份():
    mg = Merger(ls.T_FRONT_REAR, voxel=0.0)
    f = np.array([[1.0, 0.0, 0.0]])
    r = np.array([[0.0, 0.0, 2.0]])
    assert len(mg.on_front(0.0, f)) == 1 and mg.alone == 1
    mg.on_rear(0.03, r)
    assert len(mg.on_front(0.0, f)) == 2 and mg.merged == 1
    assert len(mg.on_front(0.2, f)) == 1, "差 170 ms:配不上"
    got = mg.on_front(0.0, f)
    assert np.allclose(got[1], transform(ls.T_FRONT_REAR, r)[0])


def test_合并_给了运动就补():
    mg = Merger(np.eye(4), voxel=0.0, rel_pose=lambda tf, tr: np.array(
        [[1, 0, 0, 0.1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1.0]]))
    mg.on_rear(0.01, np.array([[0.0, 0.0, 0.0]]))
    got = mg.on_front(0.0, np.array([[5.0, 5.0, 5.0]]))
    assert np.allclose(got[1], [0.1, 0.0, 0.0])


# ------------------------------------------------------------ 标定

@pytest.fixture(scope="module")
def 一圈的帧():
    return ls.scans_along(_一圈(), _世界(), noise=0.01)


@pytest.mark.parametrize("dt,deg,axis", [
    ([0.07, -0.05, 0.05], 5.0, (0.3, 0.5, 0.8)),
    ([-0.10, 0.0, 0.0], 0.0, (0, 0, 1)),
    ([0.0, 0.0, 0.0], 5.0, (1, 0, 0)),
])
def test_标定_从错10厘米5度的初值收回来(一圈的帧, dt, deg, axis):
    T0 = _扰动(ls.T_FRONT_REAR, dt, deg, axis)
    r = calib.calibrate(一圈的帧, T0)
    shift, turn = calib.delta(ls.T_FRONT_REAR, r.T)
    assert r.ok, r.why
    assert shift < 0.01 and turn < 0.3, (shift, turn)
    assert r.inlier > 0.8 and r.median_m < 0.02


def test_标定_装法跟初值差太远_拒(一圈的帧):
    """真机后雷达要是转了 90° 装的,初值离得太远:要么对不上、要么收到很远的地方 —— 都不写。"""
    T0 = _扰动(ls.T_FRONT_REAR, [0, 0, 0], 90.0, (1, 0, 0))
    r = calib.calibrate(一圈的帧, T0)
    assert not r.ok and r.why


def test_标定_后雷达的点是乱的_拒():
    fr = ls.scans_along(_一圈(12), _世界(), noise=0.01)
    rng = np.random.default_rng(3)
    junk = [(Twf, f, rng.uniform(-10, 10, (len(r), 3))) for Twf, f, r in fr]
    r = calib.calibrate(junk, ls.T_FRONT_REAR)
    assert not r.ok and ("对上的点" in r.why or "对不上" in r.why or "残差" in r.why), r.why


def test_标定_没有帧():
    assert not calib.calibrate([], np.eye(4)).ok
