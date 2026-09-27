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


def _walls_and_person(frames):
    """屋子的墙 + 一个跟着狗走的人(狗后面 0.5 m、右边 0.3 m,离地 0.8–1.6 m)。回:合起来的点云、
    雷达走过的位置、逐帧扫描 [(雷达位置, 这一帧的点)](都在 MOLA 系)、狗身中心走过的路(地图平面)。"""
    L = np.asarray(frames.level_matrix())
    walls = []
    for h in np.linspace(0.8, 1.6, 9):
        xs, ys = np.arange(0, 10, 0.02), np.arange(0, 6, 0.02)
        walls += [np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                  np.c_[xs, np.full_like(xs, 6.0), np.full_like(xs, h)],
                  np.c_[np.zeros_like(ys), ys, np.full_like(ys, h)],
                  np.c_[np.full_like(ys, 10.0), ys, np.full_like(ys, h)]]
    walls = np.vstack(walls)
    rng = np.random.default_rng(3)
    floor = np.c_[rng.uniform([0, 0], [10, 6], (20000, 2)), np.zeros(20000)]
    a = np.linspace(0, 2 * np.pi, 48, endpoint=False)
    pillar = np.vstack([np.c_[5.0 + 0.15 * np.cos(a), 1.2 + 0.15 * np.sin(a), np.full(48, h)]
                        for h in np.linspace(0.8, 1.6, 9)])   # 路和墙之间一根柱子(一直在)
    xs = np.linspace(1, 9, 80)
    sensor = np.c_[xs, np.full(80, 3.0), np.full(80, 0.6)]
    scans, people = [], []
    for x in xs:
        a = np.linspace(0, 2 * np.pi, 24, endpoint=False)
        person = np.c_[x - 0.5 + 0.15 * np.cos(a), 2.7 + 0.15 * np.sin(a), np.full(24, 1.2)]
        person = np.vstack([person + [0, 0, dz] for dz in (-0.4, 0.0, 0.4)])
        people.append(person)
        scans.append((np.array([x, 3.0, 0.6]) @ L, np.vstack([walls, floor, pillar, person]) @ L))
    P = np.vstack([walls, floor, pillar, *people])
    return P @ L, sensor @ L, scans, np.c_[xs - 0.4, np.full(80, 3.0)]


def test_跟着狗走的人_真射线清得掉_走过的路和右边都是可通行_墙还在():
    """W09c1 真数据(newdog2)上踩到的:人跟着狗走,他站过的地方连成一道「墙」,模拟射线碰到就停、永远
    清不掉,走过的路被判成墙、右半边全成了「没扫到」。逐帧扫描的真射线:那一刻人不在的地方射线穿过去。"""
    f = _frames()
    P, T, scans, body = _walls_and_person(f)
    g = G.render(P, T, f, scans=scans, body_path=body)
    at = lambda x, y: g.image[g.cell_of(x, y)]            # noqa: E731
    for x in (2.0, 4.5, 7.0):
        assert at(x - 0.5, 2.7) == G.FREE, "人站过的地方"
        assert at(x, 1.5) == G.FREE, "人右边那半间屋"
        assert at(x - 0.4, 3.0) == G.FREE, "狗身中心走过的路"
    for x, y in ((5.0, 0.0), (5.0, 6.0), (0.0, 3.0), (10.0, 3.0), (5.0, 1.05)):
        assert at(x, y) == G.OCCUPIED, (x, y)            # (5, 1.05):柱子朝路的那一面
    # 柱子后面的墙点跟柱子在同一个方位:每个方位只打最近的那个,不然射线穿过柱子把它清掉;
    # 地面的点不打射线(它只说明地面上空,说明不了障碍层那个高度是空的)
    old = G.render(P, T, f)                               # 模拟射线:人那道「墙」还在
    assert old.image[old.cell_of(4.0, 2.7)] != G.FREE


def test_狗身子占过的格子一律可通行_身宽两边各_0_25_m():
    """没有一条射线的时候,可通行只来自狗身子走过的路;路上的障碍点(比如当时开着的门)也不算。"""
    f = _frames()
    P, T, _, body = _walls_and_person(f)
    L = np.asarray(f.level_matrix())
    door = np.c_[np.full(50, 4.6), np.full(50, 3.0), np.linspace(0.8, 1.6, 50)] @ L
    g = G.render(np.vstack([P, door]), T, f, scans=[], body_path=body)
    at = lambda x, y: g.image[g.cell_of(x, y)]            # noqa: E731
    for x in (1.0, 4.6, 8.0):
        assert at(x, 3.0) == G.FREE, x
    r, c = g.cell_of(4.6, 3.0)
    assert (g.image[r - 1:r + 2, c - 1:c + 2] == G.FREE).all(), "门那几格(换算后可能差一格)"
    assert at(4.0, 3.2) == G.FREE, "中心线旁边 0.2 m"
    assert at(4.0, 3.4) != G.FREE, "0.4 m 以外不是身子"
    assert g.cell_of(4.0, 1.5) is None or at(4.0, 1.5) != G.FREE, "没有射线:别处都不知道"
