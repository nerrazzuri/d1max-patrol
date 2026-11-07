"""导航 V1 第 2 节:Route Validator V1.0 的几何检查(纯 Python,不碰 ROS)。

沿 Nav2 规划出的路线每 5 cm 放一个「物理车身 + 基础余量」(1.03 × 0.58),算每处离地图墙还剩多少
余量;最窄处 < 0.20 m 就拒绝出发,报出是哪一处。"""
from __future__ import annotations

import math

import numpy as np
import pytest

from nexwar_agent.route_check import check_route, densify, pose_margins
from nexwar_contract.footprint import BASE_HALF_LEN, BASE_HALF_WID, ROUTE_MARGIN

RES = 0.05


def _走廊(width, length=10.0, turn=False):
    """地图:x 方向的直走廊(中心 y = 2.0),宽 ``width``;``turn`` 时在 x = 6 处左转 90° 往 +y。
    返回 (walls, res, origin):walls[行 = y, 列 = x],True = 墙。"""
    n = int(12 / RES)
    yy, xx = np.mgrid[0:n, 0:n]
    cx, cy = (xx + 0.5) * RES, (yy + 0.5) * RES
    free = (np.abs(cy - 2.0) <= width / 2) & (cx <= length)
    if turn:
        free |= (np.abs(cx - 6.0) <= width / 2) & (cy >= 2.0 - width / 2)
        free &= ~((cx > 6.0 + width / 2) & (cy > 2.0 + width / 2))
    return ~free, RES, (0.0, 0.0)


def _直线(x0, x1, y=2.0, yaw=0.0, step=0.025):
    n = int(round((x1 - x0) / step))
    return [[x0 + i * step, y, yaw] for i in range(n + 1)]


def test_常数_三层车身口径():
    assert (BASE_HALF_LEN, BASE_HALF_WID) == pytest.approx((0.515, 0.29))
    assert ROUTE_MARGIN == pytest.approx(0.20)


def test_插值到5厘米_朝向走最短角():
    d = densify([[0.0, 0.0, 3.0], [0.2, 0.0, -3.0]], step=0.05)
    assert len(d) == 5
    assert all(math.dist(a[:2], b[:2]) <= 0.05 + 1e-9 for a, b in zip(d, d[1:], strict=False))
    assert all(abs(p[2]) >= 3.0 - 1e-6 for p in d), "3.0 → -3.0 走过 π,不从 0 绕"


def test_居中直通道_余量等于半宽减车身半宽():
    walls, res, org = _走廊(1.2)
    m = pose_margins(densify(_直线(2.0, 8.0)), walls, res, org)
    assert m.min() == pytest.approx(0.6 - BASE_HALF_WID, abs=0.03)


def test_一米二直通道放行():
    walls, res, org = _走廊(1.2)
    r = check_route(_直线(1.0, 8.0), walls, res, org)
    assert r.ok and r.code == "" and r.margin >= ROUTE_MARGIN


def test_九十厘米通道拒绝_报最窄处和要多宽():
    walls, res, org = _走廊(0.9)
    r = check_route(_直线(1.0, 8.0), walls, res, org)
    assert not r.ok and r.code == "route_too_narrow"
    assert r.margin == pytest.approx(0.45 - BASE_HALF_WID, abs=0.03)
    assert "0.98" in r.why and "离墙只剩" in r.why, r.why


def test_起步那一段不查_机器人本来就站在那儿():
    walls, res, org = _走廊(1.2)
    ys = (np.arange(walls.shape[0])[:, None] + 0.5) * RES
    walls[:, :int(0.75 / RES)] |= np.abs(ys - 2.0) > 0.4
    r = check_route(_直线(1.0, 8.0), walls, res, org)       # 起步时车尾那截窄,走开 0.5 m 后就宽
    assert r.ok, r.why


def test_终点贴墙_报目标点离墙太近():
    walls, res, org = _走廊(1.2)
    r = check_route(_直线(1.0, 9.6), walls, res, org)       # 走廊在 x = 10 封死,车头离墙 0.1 m
    assert not r.ok and r.code == "goal_too_close_to_wall", r
    assert "目标点" in r.why


def test_路线穿墙_余量为负_拒绝():
    walls, res, org = _走廊(1.2)
    r = check_route(_直线(1.0, 8.0, y=2.5), walls, res, org)
    assert not r.ok and r.margin < 0


def test_L弯_按车身朝向扫():
    walls, res, org = _走廊(1.3, length=6.65, turn=True)
    path = _直线(1.0, 6.0) + [[6.0, 2.0 + i * 0.025, math.pi / 2] for i in range(1, 160)]
    r = check_route(path, walls, res, org)
    assert not r.ok, "在拐角原地转 90°:车身对角线扫到墙"
