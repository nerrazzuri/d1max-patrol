"""二维规划栅格(W09c1):按 frames.json 的平面画。合成数据:一间 10 × 6 m 的屋子,MOLA 系是 X 朝上的
雷达系(跟 RS-Airy 一样),地图平面系里的墙、狗走过的路已知。"""

from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")

from d1max_localizer import grid as G  # noqa: E402
from d1max_localizer.frames import Frames  # noqa: E402

# 雷达 X 朝上(MOLA 系里的「上」略歪)、Z 朝前
UP = (0.998, -0.027, 0.053)


def _frames():
    n = math.sqrt(sum(c * c for c in UP))
    up = tuple(c / n for c in UP)
    return Frames(up=up, sensor_up=(1.0, 0.0, 0.0), sensor_forward=(0.0, 0.0, 1.0),
                  sensor_height=0.6)


def _room(frames, *, ghost=True):
    """地图平面系里造点(x, y, 高度),再转回 MOLA 系。屋子 x∈[0,10]、y∈[0,6];狗沿 y=3 从 x=1
    走到 9。"""
    rng = np.random.default_rng(0)
    pts = []
    for h in np.linspace(0.0, 2.4, 13):                  # 四面墙
        xs = np.arange(0, 10, 0.02)
        ys = np.arange(0, 6, 0.02)
        pts += [np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                np.c_[xs, np.full_like(xs, 6.0), np.full_like(xs, h)],
                np.c_[np.zeros_like(ys), ys, np.full_like(ys, h)],
                np.c_[np.full_like(ys, 10.0), ys, np.full_like(ys, h)]]
    fl = rng.uniform([0, 0], [10, 6], (40000, 2))        # 地面
    pts.append(np.c_[fl, np.zeros(len(fl))])
    # 狗走过的地方站了个人
    pts.append(np.c_[np.full(200, 4.0), np.full(200, 3.0), np.linspace(0.8, 1.6, 200)])
    if ghost:                                            # 远处玻璃的反射
        pts.append(np.c_[np.full(300, 40.0), np.linspace(0, 6, 300), np.full(300, 1.2)])
    P = np.vstack(pts)
    traj = np.c_[np.linspace(1, 9, 80), np.full(80, 3.0), np.full(80, 0.6)]
    L = np.asarray(frames.level_matrix())
    return P @ L, traj @ L                               # 地图平面系 → MOLA 系(L 是正交阵)


def test_墙是占据_屋里走过的地方是可通行_远处的鬼影不进来():
    f = _frames()
    P, T = _room(f)
    g = G.render(P, T, f)
    at = lambda x, y: g.image[g.cell_of(x, y)]            # noqa: E731
    for x, y in ((5.0, 0.0), (5.0, 6.0), (0.0, 3.0), (10.0, 3.0)):
        assert at(x, y) == G.OCCUPIED, (x, y)
    for x, y in ((2.0, 3.0), (5.0, 2.0), (8.0, 4.5), (4.0, 3.0)):
        assert at(x, y) == G.FREE, (x, y)                 # 那个人:射线穿过得多,判可通行
    assert g.cell_of(40.0, 3.0) is None, "离路线 12 m 以外的鬼影不在图上"


def test_栅格的坐标跟定位的平面同一个系():
    """路线上每一点(就是定位器会报的位置)都落在可通行的格子上。"""
    f = _frames()
    P, T = _room(f, ghost=False)
    g = G.render(P, T, f)
    q = G.level(T, f)
    for x, y, _ in q[::7]:
        assert g.image[g.cell_of(x, y)] == G.FREE


def test_写出来是_map_server_的格式(tmp_path):
    f = _frames()
    P, T = _room(f, ghost=False)
    g = G.render(P, T, f)
    pgm, yml = G.write(g, tmp_path / "floor")
    raw = pgm.read_bytes()
    header, body = raw.split(b"\n255\n", 1)
    w, h = (int(v) for v in header.split(b"\n")[1].split())
    assert header.startswith(b"P5") and len(body) == w * h == g.image.size
    y = yml.read_text()
    assert "image: floor.pgm" in y and "resolution: 0.05" in y and "mode: trinary" in y
    ox, oy = (float(v) for v in y.split("origin: [")[1].split("]")[0].split(",")[:2])
    assert (round(ox, 3), round(oy, 3)) == (round(g.origin[0], 3), round(g.origin[1], 3))
    assert -0.5 < ox < 0.0 and -0.5 < oy < 0.0, "屋角在原点附近"
