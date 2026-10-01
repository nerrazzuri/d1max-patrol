"""W10 规划:读 floor.*、降采样、禁行区栅格化、膨胀、软代价、限速、A*、剪枝、子进程调用。"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from d1max_agent.planning import astar, costmap
from d1max_agent.planning.astar import LETHAL, PlanError, line_cells
from d1max_agent.planning.costmap import CostmapError
from d1max_agent.planning.planner import Planner
from d1max_contract.zones import ZoneSet


def 写图(d, img_top_down: np.ndarray, res=0.05, origin=(0.0, 0.0), extra=""):
    h, w = img_top_down.shape
    (d / "floor.pgm").write_bytes(b"P5\n# c\n%d %d\n255\n" % (w, h) + img_top_down.tobytes())
    (d / "floor.yaml").write_text(
        f"image: floor.pgm\nresolution: {res}\norigin: [{origin[0]}, {origin[1]}, 0.0]\n"
        f"negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\nmode: trinary\n{extra}")


def 空地(h, w):
    return np.zeros((h, w), dtype=bool)


def 区域(*zs, rev=1):
    return ZoneSet.from_wire({"map_id": "m", "map_version": "v", "revision": rev,
                              "zones": list(zs)}).zones


# ------------------------------------------------------------ 读 floor.*

def test_读图_上下翻_三值(tmp_path):
    img = np.full((4, 6), 254, dtype=np.uint8)       # 从上往下
    img[0, 0] = 0          # 最上一行 → 地图里 y 最大那行
    img[3, 5] = 205        # 未知
    写图(tmp_path, img, res=0.1, origin=(-1.0, 2.0))
    free, res, origin = costmap.load_floor(tmp_path)
    assert res == 0.1 and origin == (-1.0, 2.0)
    assert free.shape == (4, 6)
    assert not free[3, 0] and not free[0, 5]         # 占用、未知都不算可通行
    assert free.sum() == 22


def test_读图_negate_与最大值(tmp_path):
    img = np.array([[0, 100]], dtype=np.uint8)
    (tmp_path / "floor.pgm").write_bytes(b"P5 2 1 100\n" + img.tobytes())
    (tmp_path / "floor.yaml").write_text("resolution: 0.1\norigin: [0, 0, 0]\nnegate: 1\n")
    free, _, _ = costmap.load_floor(tmp_path)
    assert free.tolist() == [[True, False]]          # negate:0 是空


@pytest.mark.parametrize("yaml", [
    "resolution: 0.1\n", "origin: [0,0,0]\n", "resolution: x\norigin: [0,0,0]\n",
    "resolution: nan\norigin: [0,0,0]\n", "resolution: 5\norigin: [0,0,0]\n",
    "resolution: 0.1\norigin: [0]\n", "resolution: 0.1\norigin: [0, 0, 0.5]\n",
])
def test_坏yaml(tmp_path, yaml):
    写图(tmp_path, np.full((2, 2), 254, np.uint8))
    (tmp_path / "floor.yaml").write_text(yaml)
    with pytest.raises(CostmapError):
        costmap.load_floor(tmp_path)


@pytest.mark.parametrize("pgm", [b"P2 1 1 255\n0", b"P5 2 2 255\n\x00", b"P5 2",
                                 b"P5 a 2 255\n\0\0",
                                 b"P5 0 1 255\n", b"P5 1 1 256\n\0"])
def test_坏pgm(pgm):
    with pytest.raises(CostmapError):
        costmap.parse_pgm(pgm)


# ------------------------------------------------------------ 降采样、区域、膨胀

def test_降采样只往保守方向():
    free = np.ones((5, 4), dtype=bool)
    free[0, 3] = False
    blocked, res = costmap.downsample(free, 0.05, 0.1)
    assert res == pytest.approx(0.1) and blocked.shape == (3, 2)
    assert blocked[0, 1] and not blocked[0, 0]
    assert blocked[2].all()                          # 补出来的半格当挡


def test_太大拒():
    with pytest.raises(CostmapError):
        costmap.downsample(np.ones((2100, 2000), bool), 0.1, 0.1)


def test_禁行区栅格化_格心在里面或贴着():
    m = costmap.polygon_mask(((1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (1.0, 2.0)), (30, 30), 0.1,
                             (0.0, 0.0))
    assert m[15, 15] and m[10, 10] and m[19, 19]
    assert m[9, 15] and m[20, 15]                    # 贴边一格(半格对角线以内)
    assert not m[8, 15] and not m[21, 15]
    wide = costmap.polygon_mask(((1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (1.0, 2.0)), (30, 30), 0.1,
                                (0.0, 0.0), margin_m=0.3)
    assert wide[7, 15] and not wide[5, 15]
    assert not costmap.polygon_mask(((50, 50), (51, 50), (51, 51)), (30, 30), 0.1, (0, 0)).any()


def test_凹多边形():
    poly = ((0.0, 0.0), (3.0, 0.0), (3.0, 3.0), (1.5, 0.8), (0.0, 3.0))
    m = costmap.polygon_mask(poly, (40, 40), 0.1, (0.0, 0.0))
    assert m[5, 15] and not m[25, 15]


def test_膨胀_按格子实体算净空_软代价往外变便宜():
    """W10 外审 1:致命区按两格之间最近两点的距离算(格子是一整块),不是格心到格心。"""
    blocked = 空地(40, 40)
    blocked[20, 20] = True
    cm = costmap.build(blocked, 0.1, (0.0, 0.0), robot_radius_m=0.52)
    assert cm.lethal[20, 26] and not cm.lethal[20, 27]      # 格边距 0.5 < 0.52、0.6 不是
    assert cm.lethal[24, 24] and not cm.lethal[25, 25]      # (3,3)×0.1 = 0.42、(4,4) = 0.57
    assert cm.lethal[24, 25] and not cm.lethal[25, 26]      # (3,4) = 0.5、(4,5) = 0.64
    on = costmap.build(blocked, 0.1, (0.0, 0.0), robot_radius_m=0.5)
    assert on.lethal[20, 25] and not on.lethal[20, 26], "格边距正好等于半径的算够(< 才致命)"
    assert cm.cost[20, 26] == LETHAL
    assert 0 < cm.cost[20, 29] < cm.cost[20, 28] < cm.cost[20, 27] < LETHAL
    assert cm.cost[20, 33] == 0
    assert np.isinf(cm.speed).all()


def _到实体(blocked, res, x, y):
    """点 (x, y) 到所有挡住的格子(一整块正方形)的最近距离。"""
    rs, cs = np.nonzero(blocked)
    dx = np.maximum(np.abs(x - (cs + 0.5) * res) - res / 2, 0.0)
    dy = np.maximum(np.abs(y - (rs + 0.5) * res) - res / 2, 0.0)
    return float(np.min(np.hypot(dx, dy)))


def test_规划出的路_连线扫过的机身圆离障碍实体够远():
    """W10 外审 1:沿剪枝后的每一段连线密采样,离挡住的格子(实体)都 ≥ 半径 + 余量。"""
    rng = np.random.default_rng(3)
    checked = []
    for k in range(6):
        b = 空地(80, 80)
        b[0, :] = b[-1, :] = b[:, 0] = b[:, -1] = True
        for _ in range(12):
            r0, c0 = rng.integers(5, 70, size=2)
            b[r0:r0 + rng.integers(1, 10), c0:c0 + rng.integers(1, 10)] = True
        R = costmap.ROBOT_RADIUS_M + costmap.INFLATE_MARGIN_M
        cm = costmap.build(b, 0.1, (0.0, 0.0), robot_radius_m=R)
        free = np.argwhere(~cm.lethal)
        s, g = tuple(free[0]), tuple(free[-1])
        try:
            cells = _run(cm, (int(s[0]), int(s[1])), (int(g[0]), int(g[1])))
        except PlanError:
            continue
        pts = [cm.center(r, c) for r, c in cells]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            n = max(2, int(math.hypot(x1 - x0, y1 - y0) / 0.01))
            for t in np.linspace(0, 1, n):
                d = _到实体(b, 0.1, x0 + t * (x1 - x0), y0 + t * (y1 - y0))
                assert d >= R - 1e-9, (k, d)     # 连线只经过不致命的格:格里每一点都够远
        checked.append(k)
    assert len(checked) >= 3, "随机图里至少有几张规划得出来"


def test_区域进代价图():
    zs = 区域({"id": "p", "kind": "nogo", "polygon": [[1, 1], [2, 1], [2, 2], [1, 2]]},
             {"id": "s", "kind": "slow", "polygon": [[0, 0], [3, 0], [3, 0.5], [0, 0.5]],
              "max_speed_mps": 0.3},
             {"id": "t", "kind": "slow", "polygon": [[0, 0], [1, 0], [1, 0.5], [0, 0.5]],
              "max_speed_mps": 0.5})
    cm = costmap.build(空地(40, 40), 0.1, (0.0, 0.0), zs, robot_radius_m=0.3)
    assert cm.hard[15, 15] and cm.lethal[15, 22] and not cm.hard[15, 22]
    assert cm.speed_at(2.5, 0.25) == pytest.approx(0.3)
    assert cm.speed_at(0.5, 0.25) == pytest.approx(0.3)     # 叠着取小
    assert math.isinf(cm.speed_at(2.5, 3.5)) and math.isinf(cm.speed_at(-1, 0))
    wider = costmap.build(空地(40, 40), 0.1, (0.0, 0.0), zs, robot_radius_m=0.3,
                          nogo_margin_m=0.4)
    assert wider.hard[15, 24] and not cm.hard[15, 24] and not wider.hard[15, 25]


def test_格与坐标():
    cm = costmap.build(空地(10, 20), 0.1, (-1.0, 5.0))
    assert cm.cell_of(-1.0, 5.0) == (0, 0) and cm.cell_of(0.95, 5.95) == (9, 19)
    assert cm.cell_of(-1.01, 5.0) is None and cm.cell_of(1.0, 5.0) is None
    assert cm.center(0, 0) == pytest.approx((-0.95, 5.05))


# ------------------------------------------------------------ A*

def _cm(blocked, r=0.2):
    return costmap.build(blocked, 0.1, (0.0, 0.0), robot_radius_m=r)


def _run(cm, s, g, **kw):
    h, w = cm.shape
    return astar.plan(cm.cost.tobytes(), cm.hard.astype(np.uint8).tobytes(), w, h, s, g, **kw)


def _不碰(cm, cells):
    for a, b in zip(cells, cells[1:], strict=False):
        for r, c in line_cells(a, b):
            assert cm.cost[r, c] != LETHAL, (a, b, (r, c))


def test_空地直连():
    cm = _cm(空地(30, 30))
    assert _run(cm, (5, 5), (25, 20)) == [(5, 5), (25, 20)]


def test_绕墙_不穿致命区():
    b = 空地(40, 40)
    b[20, 0:30] = True
    cm = _cm(b)
    cells = _run(cm, (5, 5), (35, 5))
    _不碰(cm, cells)
    assert max(c for _, c in cells) >= 30


def test_U形():
    b = 空地(60, 60)
    b[10, 10:50] = True
    b[10:50, 10] = True
    b[10:50, 49] = True
    cm = _cm(b)
    cells = _run(cm, (30, 30), (5, 30))
    _不碰(cm, cells)
    assert max(r for r, _ in cells) >= 50


def test_没路_终点起点不合法():
    b = 空地(30, 30)
    b[15, :] = True
    cm = _cm(b)
    with pytest.raises(PlanError) as e:
        _run(cm, (5, 5), (25, 5))
    assert e.value.reason == "no_path"
    with pytest.raises(PlanError) as e:
        _run(cm, (5, 5), (15, 5))
    assert e.value.reason == "goal_blocked"
    with pytest.raises(PlanError) as e:
        _run(cm, (15, 5), (5, 5))
    assert e.value.reason == "start_blocked"
    with pytest.raises(PlanError) as e:
        _run(cm, (5, 5), (5, 30))
    assert e.value.reason == "outside"
    with pytest.raises(PlanError) as e:
        _run(cm, (-1, 5), (5, 5))
    assert e.value.reason == "outside"


def test_贴墙起步先走出来():
    b = 空地(30, 30)
    b[10, :] = True
    cm = _cm(b)
    assert cm.cost[11, 5] == LETHAL and not cm.hard[11, 5]
    cells = _run(cm, (11, 5), (25, 5))
    assert cells[0] == (11, 5)
    assert all(cm.cost[r, c] != LETHAL for r, c in cells[1:] if r > 12)
    b2 = 空地(30, 30)
    b2[10, :] = True
    b2[12, :] = True                                 # 夹缝里,走不出致命区
    cm2 = _cm(b2, r=0.5)
    with pytest.raises(PlanError) as e:
        _run(cm2, (11, 5), (25, 5))
    assert e.value.reason == "start_blocked"


def test_斜着不擦致命格的角():
    cost = bytearray(9)
    cost[1] = LETHAL
    cost[3] = LETHAL
    with pytest.raises(PlanError):
        astar.plan(bytes(cost), bytes(9), 3, 3, (0, 0), (2, 2))


def test_走中间():
    b = 空地(40, 40)
    b[10, :] = True
    b[30, :] = True
    cm = _cm(b, r=0.3)
    cells = _run(cm, (20, 2), (20, 37))
    assert all(16 <= r <= 24 for r, _ in cells)


def test_扩展上限与截止():
    cm = _cm(空地(200, 200))
    b = 空地(200, 200)
    b[100, 0:199] = True
    cm = _cm(b)
    with pytest.raises(PlanError) as e:
        _run(cm, (5, 5), (195, 5), max_expansions=100)
    assert e.value.reason == "timeout"
    with pytest.raises(PlanError) as e:
        _run(cm, (5, 5), (195, 5), deadline=0.0)
    assert e.value.reason == "timeout"


def test_连线格():
    assert line_cells((0, 0), (0, 3)) == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert line_cells((0, 0), (2, 2)) == [(0, 0), (0, 1), (1, 0), (1, 1), (1, 2), (2, 1), (2, 2)]
    cs = line_cells((0, 0), (1, 3))
    assert cs == [(0, 0), (0, 1), (0, 2), (1, 1), (1, 2), (1, 3)]      # 过格角
    assert line_cells((3, 3), (0, 0))[-1] == (0, 0)


def test_剪枝不抄近路穿过贵格或致命格():
    """路沿着边绕过中间一块;直连对角线会穿过中间。中间贵(软代价)不许抄,致命更不许。"""
    for mid in (90, LETHAL):
        cost = bytearray(25)
        for r in (1, 2, 3):
            for c in (1, 2, 3):
                cost[r * 5 + c] = mid
        path = [0, 1, 2, 3, 4, 9, 14, 19, 24]          # 上边一行,再右边一列
        out = astar.smooth(path, bytes(cost), 5)
        assert out[0] == 0 and out[-1] == 24 and len(out) >= 3, (mid, out)
        for a, b in zip(out, out[1:], strict=False):
            for r, c in line_cells(divmod(a, 5), divmod(b, 5)):
                assert cost[r * 5 + c] == 0, (mid, out)


def test_剪枝不让软代价变高():
    cost = bytearray(25)
    for i in (6, 7, 8):
        cost[i] = 90                     # 中间一行贵
    path = [0, 5, 10, 15, 16, 17, 18, 19, 14, 9, 4]
    out = astar.smooth(path, bytes(cost), 5)
    assert out[0] == 0 and out[-1] == 4
    for a, b in zip(out, out[1:], strict=False):
        for r, c in line_cells(divmod(a, 5), divmod(b, 5)):
            assert cost[r * 5 + c] == 0


# ------------------------------------------------------------ 调用

def test_规划器_线程里_地图坐标():
    b = 空地(40, 40)
    b[20, 0:30] = True
    cm = _cm(b)
    p = asyncio.run(Planner(in_process=True).plan(cm, (0.53, 0.52), (0.55, 3.5)))
    assert p.points[0] == (0.53, 0.52) and p.points[-1] == (0.55, 3.5)
    assert p.length_m > 5.0
    with pytest.raises(PlanError):
        asyncio.run(Planner(in_process=True).plan(cm, (0.5, 0.5), (50, 0.5)))
    with pytest.raises(PlanError):
        asyncio.run(Planner(in_process=True).plan(cm, (-5, 0.5), (1, 0.5)))
    same = asyncio.run(Planner(in_process=True).plan(cm, (0.51, 0.51), (0.52, 0.52)))
    assert same.points == ((0.51, 0.51), (0.52, 0.52))


def test_规划器_子进程():
    b = 空地(40, 40)
    b[20, 0:30] = True
    cm = _cm(b)
    pl = Planner()
    try:
        async def 两次():
            p = await pl.plan(cm, (0.5, 0.5), (0.5, 3.5))
            with pytest.raises(PlanError) as e:
                await pl.plan(cm, (0.5, 0.5), (0.5, 2.05))
            return p, e.value.reason
        p, reason = asyncio.run(两次())
        assert p.length_m > 5.0 and reason == "goal_blocked"
    finally:
        pl.close()


def test_规划器_超时收手():
    b = 空地(400, 400)
    b[200, 0:399] = True
    cm = _cm(b)
    pl = Planner(in_process=True, timeout_s=0.0)
    with pytest.raises(PlanError) as e:
        asyncio.run(pl.plan(cm, (0.5, 0.5), (0.5, 39.5)))
    assert e.value.reason == "timeout"
