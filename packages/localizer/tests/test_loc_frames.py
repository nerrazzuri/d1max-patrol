"""MOLA 系的三维位姿 ↔ 地图平面位姿(W09b 决定 1)。合成数据:雷达 X 朝上、Z 朝前(RS-Airy 的装法),
MOLA 的地图系就是建图起点那一刻的雷达系,「上」因为狗站得不完全正而略歪。"""

from __future__ import annotations

import json
import math

import pytest
from d1max_localizer.frames import Frames, calibrate, mat_to_quat, quat_to_mat

from d1max_contract.errors import ContractError

UP_MAP = (0.998, -0.027, 0.053)          # 探路时 coverage2 的先验里估出来的「上」


def _unit(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


def _mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                 for i in range(3))


def _T(a):
    return tuple(tuple(a[j][i] for j in range(3)) for i in range(3))


def _apply(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def _level(up):
    """把 up 转到 +Z 的旋转(测试里自己造一个,跟被测代码的算法无关:绕 up×Z 转)。"""
    u = _unit(up)
    ax = (u[1], -u[0], 0.0)
    s = math.hypot(ax[0], ax[1])
    c = u[2]
    if s < 1e-12:
        return ((1.0, 0, 0), (0, 1.0, 0), (0, 0, 1.0))
    k = (ax[0] / s, ax[1] / s, 0.0)
    K = ((0, -k[2], k[1]), (k[2], 0, -k[0]), (-k[1], k[0], 0))
    K2 = _mul(K, K)
    return tuple(tuple((1.0 if i == j else 0.0) + s * K[i][j] + (1 - c) * K2[i][j]
                       for j in range(3)) for i in range(3))


# 雷达系:X 朝上、Z 朝前;右手系里朝左 = 上 × 前 = -Y
S_UP, S_FWD = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)


def _sensor_in_level(yaw):
    """水平系里朝 yaw 的雷达姿态:把雷达的(前、左、上)轴分别转到水平系的(朝向、朝向左、+Z)。"""
    h = (math.cos(yaw), math.sin(yaw), 0.0)
    left = (-math.sin(yaw), math.cos(yaw), 0.0)
    ez = (0.0, 0.0, 1.0)
    s_left = (0.0, -1.0, 0.0)                    # 上 × 前 = X × Z = -Y
    # R·S_FWD = h, R·s_left = left, R·S_UP = ez  →  R = [h left ez] · [S_FWD s_left S_UP]^T
    A = tuple(tuple((h, left, ez)[j][i] for j in range(3)) for i in range(3))
    B = tuple(tuple((S_FWD, s_left, S_UP)[j][i] for j in range(3)) for i in range(3))
    return _mul(A, _T(B))


def _pose(x, y, yaw, h=0.6, L=None):
    """地图平面上 (x, y, yaw) 处、离地 h 的雷达在 MOLA 系里的位姿。"""
    L = L or _level(UP_MAP)
    p = _apply(_T(L), (x, y, h))
    R = _mul(_T(L), _sensor_in_level(yaw))
    return p, mat_to_quat(R)


def _轨迹(n=400):
    """先直走、再转弯、再直走的一圈(狗大多朝前走,偶尔倒退一小段)。"""
    out, x, y, yaw = [], 0.0, 0.0, 0.3
    for i in range(n):
        v = -0.3 if 150 <= i < 170 else 0.8      # 倒退 20 帧
        w = 0.4 if 200 <= i < 260 else 0.0
        mid = yaw + w * 0.05                     # 这一段位移沿两帧朝向的中间(弦)
        yaw += w * 0.1
        x += v * 0.1 * math.cos(mid)
        y += v * 0.1 * math.sin(mid)
        out.append((x, y, yaw))
    return out


def test_四元数与旋转矩阵来回一样():
    for q in ((0.0, 0.0, 0.0, 1.0), _unit((0.1, -0.6, 0.3, 0.7)),
              _unit((-0.64, -0.02, -0.04, 0.76))):
        back = mat_to_quat(quat_to_mat(q))
        if back[3] * q[3] < 0:
            back = tuple(-c for c in back)
        assert all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(back, q, strict=True))


def test_从建图轨迹标出上_朝前_高度_之后换算回平面位姿():
    traj = _轨迹()
    poses = [_pose(x, y, yaw) for x, y, yaw in traj]
    f = calibrate(poses, sensor_up=S_UP)
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(f.up, _unit(UP_MAP), strict=True))
    assert all(math.isclose(a, b, abs_tol=1e-6)
               for a, b in zip(f.sensor_forward, S_FWD, strict=True))
    assert math.isclose(f.sensor_height, 0.6, abs_tol=1e-6)
    for (x, y, yaw), (p, q) in zip(traj, poses, strict=True):
        gx, gy, gyaw = f.to_map2d(p, q)
        assert math.isclose(gx, x, abs_tol=1e-6) and math.isclose(gy, y, abs_tol=1e-6)
        assert math.isclose(math.remainder(gyaw - yaw, 2 * math.pi), 0.0, abs_tol=1e-6)


def test_标定说得出它有多可信():
    """光看轨迹分不出狗是大多朝前走还是大多倒着走:给了装法上「朝前」的轴就拿它核,对不上说出来。"""
    f, why = calibrate([_pose(x, y, yaw) for x, y, yaw in _轨迹()], sensor_up=S_UP,
                       forward_hint=S_FWD, explain=True)
    assert "朝前" in why and "倒着走" not in why
    f, why = calibrate(_poses_backwards_mostly(), sensor_up=S_UP, forward_hint=S_FWD, explain=True)
    assert "倒着走" in why
    assert all(math.isclose(a, b, abs_tol=1e-6)
               for a, b in zip(f.sensor_forward, S_FWD, strict=True)), "按装法翻过来"


def test_雷达装得有点歪_给的上不准_转过一圈也估得出():
    """装法上说「上」是 X,实际歪了 8°:狗转一整圈,「上」在地图系里的平均还是竖直的,再反过来把雷达系里
    的「上」校准。"""
    tilt = math.radians(8.0)
    true_up = (math.cos(tilt), math.sin(tilt), 0.0)       # 雷达系里真正的「上」
    fwd = (0.0, 0.0, 1.0)
    left = (true_up[1] * fwd[2] - true_up[2] * fwd[1], true_up[2] * fwd[0] - true_up[0] * fwd[2],
            true_up[0] * fwd[1] - true_up[1] * fwd[0])
    B = tuple(tuple((fwd, left, true_up)[j][i] for j in range(3)) for i in range(3))
    L = _level(UP_MAP)
    poses = []
    for i in range(360):                                  # 原地转一整圈,边转边往前挪一点
        yaw = math.radians(i)
        h = (math.cos(yaw), math.sin(yaw), 0.0)
        lf = (-math.sin(yaw), math.cos(yaw), 0.0)
        A = tuple(tuple((h, lf, (0.0, 0.0, 1.0))[j][i2] for j in range(3)) for i2 in range(3))
        R = _mul(_T(L), _mul(A, _T(B)))
        poses.append((_apply(_T(L), (0.02 * i * math.cos(yaw), 0.02 * i * math.sin(yaw), 0.6)),
                      mat_to_quat(R)))
    f = calibrate(poses, sensor_up=S_UP)
    assert all(math.isclose(a, b, abs_tol=1e-3) for a, b in zip(f.up, _unit(UP_MAP), strict=True))
    assert all(math.isclose(a, b, abs_tol=1e-3) for a, b in zip(f.sensor_up, true_up, strict=True))


def _poses_backwards_mostly():
    out, x = [], 0.0
    for i in range(100):
        x -= 0.05 if i < 80 else -0.05          # 大多在倒退
        out.append(_pose(x, 0.0, 0.0))
    return out


def test_平面位姿反算成_MOLA_的三维初值():
    f = calibrate([_pose(x, y, yaw) for x, y, yaw in _轨迹()], sensor_up=S_UP)
    for x, y, yaw in ((0.0, 0.0, 0.0), (12.5, -3.0, 2.0), (-7.0, 40.0, -2.9)):
        p, q = f.to_mola(x, y, yaw)
        ep, eq = _pose(x, y, yaw)
        assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(p, ep, strict=True))
        gx, gy, gyaw = f.to_map2d(p, q)
        assert (round(gx, 6), round(gy, 6)) == (round(x, 6), round(y, 6))
        assert math.isclose(math.remainder(gyaw - yaw, 2 * math.pi), 0.0, abs_tol=1e-6)


def test_雷达不在狗身中心_平面位姿是狗身中心的():
    """雷达装在头上、狗身中心前 0.3 m:报给代理的是狗身中心(导航、原点都按狗身算)。"""
    f = calibrate([_pose(x, y, yaw) for x, y, yaw in _轨迹()], sensor_up=S_UP)
    f = f.with_sensor_in_base(0.3, 0.0)
    p, q = _pose(10.0, 5.0, math.pi / 2)         # 雷达在 (10, 5)、朝北
    x, y, yaw = f.to_map2d(p, q)
    assert (round(x, 6), round(y, 6)) == (10.0, 4.7)
    p2, q2 = f.to_mola(x, y, yaw)
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(p2, p, strict=True))


def test_存成_json_读回来一样_坏的拒收(tmp_path):
    f = calibrate([_pose(x, y, yaw) for x, y, yaw in _轨迹()], sensor_up=S_UP)
    f = f.with_sensor_in_base(0.25, -0.02)
    path = tmp_path / "frames.json"
    f.save(path)
    assert Frames.load(path) == f
    good = json.loads(path.read_text())
    for k, bad in (("up", [0.0, 0.0, 0.0]), ("up", [1.0, "x", 0.0]),
                   ("sensor_height", float("nan")), ("sensor_forward", [1.0, 0.0]),
                   ("version", 99)):
        d = dict(good)
        d[k] = bad
        path.write_text(json.dumps(d))
        with pytest.raises(ContractError):
            Frames.load(path)
    d = dict(good)
    del d["sensor_up"]
    path.write_text(json.dumps(d))
    with pytest.raises(ContractError):
        Frames.load(path)
