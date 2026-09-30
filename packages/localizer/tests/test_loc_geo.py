"""W09e 建图时的地理配准:合成轨迹 + 已知旋转平移的 RTK 固定解 → 解出 ``geo.json``。"""

from __future__ import annotations

import math
import random

import pytest
from d1max_localizer.frames import Frames
from d1max_localizer.geo import MIN_PAIRS, fit_rigid2d, georeference, read_rtk

from d1max_contract.geo import enu_to_llh

F = Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
           sensor_height=0.5, sensor_in_base=(0.4, 0.0))
LAT0, LON0 = 5.4141, 100.3288


def _q(yaw):
    return (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))


def _loop(n=200):
    """绕一个 30 × 20 m 的矩形走一圈(雷达位姿,MOLA 系 = 地图平面,z 朝上)。"""
    pts = []
    for i in range(n):
        s = i / n * 100.0
        if s < 30:
            x, y, yaw = s, 0.0, 0.0
        elif s < 50:
            x, y, yaw = 30.0, s - 30, math.pi / 2
        elif s < 80:
            x, y, yaw = 30 - (s - 50), 20.0, math.pi
        else:
            x, y, yaw = 0.0, 20 - (s - 80), -math.pi / 2
        pts.append((100.0 + i * 0.2, (x, y, 0.5), _q(yaw)))
    return pts


def _rtk(traj, yaw_deg, tx, ty, *, ant=(0.0, 0.0), noise=0.0, fix="fixed", dt=0.03, seed=1):
    rnd = random.Random(seed)
    th = math.radians(yaw_deg)
    rows = []
    for t, p, q in traj:
        x, y, yaw = F.to_map2d(p, q)
        c, s = math.cos(yaw), math.sin(yaw)
        ax, ay = x + c * ant[0] - s * ant[1], y + s * ant[0] + c * ant[1]
        e = math.cos(th) * ax - math.sin(th) * ay + tx + rnd.gauss(0, noise)
        n = math.sin(th) * ax + math.cos(th) * ay + ty + rnd.gauss(0, noise)
        lat, lon = enu_to_llh(e, n, LAT0, LON0)
        rows.append({"t": t + dt, "fix": fix, "lat": lat, "lon": lon, "alt": 12.0})
    return rows


def test_拟合_已知旋转平移():
    src = [(0, 0), (10, 0), (10, 5), (3, 8)]
    th = math.radians(30)
    dst = [(math.cos(th) * x - math.sin(th) * y + 4, math.sin(th) * x + math.cos(th) * y - 2)
           for x, y in src]
    a, tx, ty, rms = fit_rigid2d(src, dst)
    assert math.degrees(a) == pytest.approx(30) and (tx, ty) == pytest.approx((4, -2))
    assert rms == pytest.approx(0, abs=1e-9)


def test_配准_解出来的朝向与平移_误差5厘米以内():
    traj = _loop()
    rows = _rtk(traj, 37.0, 0.0, 0.0, noise=0.02)
    g, why = georeference(traj, F, rows)
    assert g is not None, why
    assert g.yaw_deg == pytest.approx(37.0, abs=0.2) and g.pairs == len(traj)
    assert g.rms_m < 0.05
    # 地图上一个点 → 经纬度 → 回地图
    x, y = 12.0, 7.0
    lat, lon = enu_to_llh(*g.map_to_enu(x, y), g.lat0, g.lon0)
    assert g.llh_to_map(lat, lon) == pytest.approx((x, y), abs=1e-6)
    # 起点(地图原点附近)在 ENU 里的位置 ≈ 第一个固定解(ENU 原点)减去它自己的地图坐标转过去
    th = math.radians(g.yaw_deg)
    x0, y0, _ = F.to_map2d(traj[0][1], traj[0][2])
    assert (math.cos(th) * x0 - math.sin(th) * y0 + g.tx,
            math.sin(th) * x0 + math.cos(th) * y0 + g.ty) == pytest.approx((0, 0), abs=0.05)


def test_天线杆臂_算进去():
    traj = _loop()
    ant = (0.3, 0.15)
    rows = _rtk(traj, 10.0, 0.0, 0.0, ant=ant)
    g, _ = georeference(traj, F, rows, antenna_in_base=ant)
    assert g.rms_m < 0.01 and g.antenna_in_base == ant
    g2, _ = georeference(traj, F, rows)                   # 不给杆臂:残差大
    assert g2 is None or g2.rms_m > 0.1


def test_只用固定解():
    traj = _loop()
    rows = _rtk(traj, 0.0, 0, 0, fix="float")
    g, why = georeference(traj, F, rows)
    assert g is None and "固定解" in why


def test_时刻对不上的不配():
    traj = _loop()
    rows = _rtk(traj, 0.0, 0, 0, dt=500.0)                 # 另一趟的 RTK:时刻跟轨迹都对不上
    g, why = georeference(traj, F, rows)
    assert g is None and "对得上时刻" in why


def test_范围太小_朝向定不准不写():
    traj = [(100 + i * 0.1, (i * 0.02, 0.0, 0.5), _q(0.0)) for i in range(MIN_PAIRS + 10)]
    g, why = georeference(traj, F, _rtk(traj, 0.0, 0, 0))
    assert g is None and "范围" in why


def test_残差太大不写():
    traj = _loop()
    g, why = georeference(traj, F, _rtk(traj, 20.0, 0, 0, noise=0.5))
    assert g is None and "残差" in why


def test_读rtk_jsonl_坏行跳过(tmp_path):
    p = tmp_path / "rtk.jsonl"
    p.write_text('{"t": 1, "lat": 5, "lon": 100, "fix": "fixed"}\n坏行\n{"t": "x"}\n')
    assert len(read_rtk(p)) == 1 and read_rtk(tmp_path / "none") == []


def test_两个钟差一千秒_给了钟差照样配上():
    """W09e 内审应修 1:轨迹是雷达消息头的钟,RTK 是代理收到的时刻(本机钟)。"""
    traj = _loop()
    rows = _rtk(traj, 37.0, 0.0, 0.0, dt=1000.0)
    g, why = georeference(traj, F, rows)
    assert g is None and "对得上时刻" in why
    g, why = georeference(traj, F, rows, clock_offset_s=1000.0)
    assert g is not None and g.yaw_deg == pytest.approx(37.0, abs=0.1), why


def test_建图时的基站坐标记进geo():
    traj = _loop()
    rows = [r | {"base_ecef": [-1.1e6, 6.2e6, 6.0e5]} for r in _rtk(traj, 0.0, 0, 0)]
    g, _ = georeference(traj, F, rows)
    assert g.base_ecef == (-1.1e6, 6.2e6, 6.0e5)


def test_rtk_jsonl_里有NaN的行跳过(tmp_path):
    p = tmp_path / "rtk.jsonl"
    p.write_text('{"t": 1, "lat": NaN, "lon": 100}\n{"t": 1, "lat": 5, "lon": 100}\n')
    assert len(read_rtk(p)) == 1
