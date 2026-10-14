"""两版地图的对比图(``map_diff``):合成的栅格,按世界坐标对齐、分类、统计、粗判坐标系、画图。"""

from __future__ import annotations

import struct
import zlib

import numpy as np
import pytest

from d1max_site import map_diff as md

OCC, FREE, UNK = 0, 1, 2


def meta(res=0.1, origin=(0.0, 0.0)):
    return {"resolution": res, "origin": origin, "origin_yaw": 0.0}


def up(g):
    """测试里的栅格按「行号 = 从南往北数」写(好想),交给被测代码前翻成地图服务器的从上往下。"""
    return np.flipud(g)


def room(w=40, h=40, box=(5, 5, 35, 35)):
    g = np.full((h, w), UNK, dtype=np.uint8)
    x0, y0, x1, y1 = box
    g[y0:y1 + 1, x0:x1 + 1] = FREE
    g[y0, x0:x1 + 1] = g[y1, x0:x1 + 1] = OCC
    g[y0:y1 + 1, x0] = g[y0:y1 + 1, x1] = OCC
    return g


def png_pixels(png: bytes) -> np.ndarray:
    assert png.startswith(b"\x89PNG")
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(png):
        n, kind = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[i * (w * 3 + 1) + 1:(i + 1) * (w * 3 + 1)] for i in range(h)]
    return np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(h, w, 3)


def test_一模一样_没有差异_全是相同():
    d = md.diff_grids(room(), meta(), room(), meta())
    c = d.stats["counts"]
    assert c["wall_only_a"] == c["wall_only_b"] == c["free_only_a"] == c["free_only_b"] == 0
    assert c["same_wall"] > 0 and c["same_free"] > 0
    assert d.stats["alignment"] == 1.0 and d.stats["likely_misaligned"] is False


def test_只有一边有的墙_红蓝分清_面积按米算():
    a, b = room(), room()
    b[20, 18:22] = OCC                         # B 里多了 4 格墙
    a[22, 10:12] = OCC                         # A 里有、B 里没有(B 里这两格是空地)
    d = md.diff_grids(up(a), meta(), up(b), meta())
    c = d.stats["counts"]
    assert c["wall_only_b"] == 4 and c["wall_only_a"] == 2
    assert d.stats["area_m2"]["wall_only_b"] == pytest.approx(4 * 0.01)
    px = png_pixels(d.png)
    # 图是从上往下:世界里 (列 18, 行 20 从下数) → 图的行 = 39 - 20
    assert tuple(px[39 - 20, 18]) == md.COLORS["wall_only_b"]
    assert tuple(px[39 - 22, 10]) == md.COLORS["wall_only_a"]


def test_空闲和没扫到的差别_橙和青():
    a, b = room(), room()
    a[2, 10:13] = FREE                        # 房间外 A 多扫了 3 格空地(B 没扫到)
    b[3, 30:32] = FREE                        # 房间外 B 多扫了 2 格
    d = md.diff_grids(up(a), meta(), up(b), meta())
    c = d.stats["counts"]
    assert c["free_only_a"] == 3 and c["free_only_b"] == 2
    px = png_pixels(d.png)
    assert tuple(px[39 - 2, 11]) == md.COLORS["free_only_a"]
    assert tuple(px[39 - 3, 30]) == md.COLORS["free_only_b"]


def test_按世界坐标对齐_两张图的原点不一样_同一堵墙还是同一个位置():
    a = room()
    b = room(w=32, h=32, box=(0, 0, 30, 30))   # B 的左下角在世界 (0.5, 0.5):格子 (0,0) = A 的 (5,5)
    d = md.diff_grids(up(a), meta(), up(b), meta(origin=(0.5, 0.5)))
    c = d.stats["counts"]
    assert c["wall_only_a"] == c["wall_only_b"] == 0 and c["same_wall"] > 0, c
    assert d.stats["left_x"] == 0.0 and d.stats["top_y"] == pytest.approx(4.0)


def test_范围不重合_画布是并集_另一边按没扫到算():
    a = room(w=20, h=20, box=(2, 2, 17, 17))
    b = room(w=20, h=20, box=(2, 2, 17, 17))
    d = md.diff_grids(up(a), meta(), up(b), meta(origin=(5.0, 0.0)))     # B 整体在 A 右边 5 m
    assert d.stats["width"] == 70 and d.stats["height"] == 20
    c = d.stats["counts"]
    assert c["same_wall"] == 0 and c["wall_only_a"] > 0 and c["wall_only_b"] > 0


def test_分辨率不一样_按较细的重采_同一堵墙对得上():
    a = room(w=40, h=40, box=(5, 5, 35, 35))                       # 0.1 m
    b = np.full((20, 20), UNK, dtype=np.uint8)                     # 0.2 m:同一间房
    b[2:18, 2:18] = FREE
    b[2, 2:18] = b[17, 2:18] = OCC
    b[2:18, 2] = b[2:18, 17] = OCC
    d = md.diff_grids(up(a), meta(0.1), up(b), meta(0.2))
    assert d.stats["resolution_m"] == 0.1 and d.stats["same_resolution"] is False
    assert d.stats["alignment"] is not None and d.stats["alignment"] > 0.9


def test_坐标系对不上_粗判认出来_提醒总是在():
    a = room()
    b = np.full((40, 40), UNK, dtype=np.uint8)                     # 同样的墙,但整体转了 90°
    b[5:36, 5] = b[5:36, 35] = OCC
    b[5, 5:36] = OCC
    b[22, 5:36] = OCC                                              # 中间多一道横墙,别的错位
    b[10:30, 12] = OCC
    d = md.diff_grids(up(a), meta(), up(b), meta(origin=(30.0, 30.0)))     # 平移 30 m:完全不重合
    assert d.stats["likely_misaligned"] is True
    assert "坐标系" in d.stats["caveat"]
    ok = md.diff_grids(up(a), meta(), up(a.copy()), meta())
    assert ok.stats["caveat"], "对得上的也带提醒:对比是按各自原点对齐的,没有配准"


def test_墙太少_不下结论():
    a = np.full((20, 20), UNK, dtype=np.uint8)
    a[5, 5:8] = OCC
    d = md.diff_grids(up(a), meta(), up(a.copy()), meta())
    assert d.stats["alignment"] is None and d.stats["likely_misaligned"] is False


def test_范围大得离谱_拒绝():
    a = room(w=10, h=10, box=(1, 1, 8, 8))
    with pytest.raises(md.DiffError):
        md.diff_grids(up(a), meta(), up(a.copy()), meta(origin=(500_000.0, 0.0)))


def test_图太大_按整数倍缩_差异盖过一样():
    a = np.full((3200, 3200), UNK, dtype=np.uint8)
    a[100:3000, 100:3000] = FREE
    b = a.copy()
    b[1000, 1000] = OCC                         # 一格差异,缩 2 倍之后还看得见
    d = md.diff_grids(up(a), meta(), up(b), meta())
    assert d.stats["width"] == 1600 and d.stats["m_per_px"] == pytest.approx(0.2)
    px = png_pixels(d.png)
    assert (px == np.array(md.COLORS["wall_only_b"], dtype=np.uint8)).all(axis=2).sum() == 1
