"""W47 Nav2 转接节点里不碰 ROS 的两样:感知栅格换到此刻机身系、地图烧禁行区。"""

from __future__ import annotations

import math

import numpy as np
from d1max_localizer.nav2bridge import grid_points, occupancy

from d1max_contract.obsbridge import pack_bits


def _grid(size, res, occ_cells, known_all=True):
    occ = np.zeros((size, size), dtype=bool)
    for r, c in occ_cells:
        occ[r, c] = True
    known = np.ones_like(occ) if known_all else occ.copy()
    return {"size": size, "res": res, "occ": pack_bits(occ.ravel().tolist(), size),
            "known": pack_bits(known.ravel().tolist(), size)}


def test_栅格换到此刻机身系_走了转了都对得上():
    size, res = 20, 0.1
    # 行 = 前、列 = 左,中心在机身:行 15、列 10 = 前 0.55 m、左 0.05 m
    m = _grid(size, res, [(15, 10)]) | {"pose": [1.0, 2.0, math.pi / 2]}
    occ, free = grid_points(m, (1.0, 2.0, math.pi / 2))
    assert occ.shape == (1, 2) and np.allclose(occ[0], [0.55, 0.05])
    assert len(free) == size * size - 1
    # 拍下之后狗往前(朝 +y)走了 0.3 m、又左转 90°:那个点在世界里不动
    occ2, _ = grid_points(m, (1.0, 2.3, math.pi))
    # 世界坐标:拍下时朝 +y,前 0.55 左 0.05 → (1.0 - 0.05, 2.0 + 0.55)
    wx, wy = 0.95, 2.55
    nx, ny, nyaw = 1.0, 2.3, math.pi
    bx = math.cos(nyaw) * (wx - nx) + math.sin(nyaw) * (wy - ny)
    by = -math.sin(nyaw) * (wx - nx) + math.cos(nyaw) * (wy - ny)
    assert np.allclose(occ2[0], [bx, by], atol=1e-9)


def test_看不见的格既不算挡也不算空():
    m = _grid(10, 0.1, [(7, 5)], known_all=False) | {"pose": [0, 0, 0]}
    occ, free = grid_points(m, (0, 0, 0))
    assert len(occ) == 1 and len(free) == 0


def test_地图烧禁行区(tmp_path):
    img = np.full((40, 60), 254, dtype=np.uint8)
    (tmp_path / "floor.pgm").write_bytes(b"P5\n60 40\n255\n" + img.tobytes())
    (tmp_path / "floor.yaml").write_text("resolution: 0.05\norigin: [0.0, 0.0, 0.0]\nnegate: 0\n")
    occ, res, origin = occupancy(tmp_path, [])
    assert occ.shape == (40, 60) and occ.max() == 0 and res == 0.05
    occ, _, _ = occupancy(tmp_path, [[[1.0, 0.5], [2.0, 0.5], [2.0, 1.5], [1.0, 1.5]]])
    assert occ[20, 30] == 100 and occ[20, 5] == 0, "多边形里面是挡、外面不动"
