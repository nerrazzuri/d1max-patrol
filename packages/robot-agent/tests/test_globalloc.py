"""W41 全局重定位:点云套全图(粗找 + 细调)、认不认、核对定位器给的位置;现场数据回放(有就跑)。"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from d1max_agent.globalloc import (
    MIN_FIT,
    Candidate,
    GlobalLocError,
    Matcher,
    decide,
    judge,
)

RES = 0.05


def _房间(w=16.0, h=12.0, extra=()):
    """外墙一圈 + 内部的墙、柱子(米);行从下往上。"""
    H, W = int(h / RES), int(w / RES)
    occ = np.zeros((H, W), dtype=bool)
    occ[:2, :] = occ[-2:, :] = occ[:, :2] = occ[:, -2:] = True
    for x0, y0, x1, y1 in extra:
        occ[int(y0 / RES):int(math.ceil(y1 / RES)), int(x0 / RES):int(math.ceil(x1 / RES))] = True
    return occ, ~occ


#: 不对称:一道隔墙、两根柱子、一个柜子
不对称 = ((6.0, 0.0, 6.2, 7.0), (10.0, 4.0, 10.4, 4.4), (3.0, 9.0, 3.3, 9.3),
          (12.0, 9.5, 15.0, 10.1))


def _扫(occ, x, y, yaw, *, rng=9.0, rays=720, noise=0.0, seed=0):
    """从 (x, y, yaw) 往四周打射线,打到占用格就记一个点(狗身系)。"""
    rs = np.random.default_rng(seed)
    out = []
    H, W = occ.shape
    for i in range(rays):
        a = 2 * math.pi * i / rays
        for d in np.arange(0.3, rng, RES / 2):
            px, py = x + d * math.cos(yaw + a), y + d * math.sin(yaw + a)
            r, c = int(py / RES), int(px / RES)
            if not (0 <= r < H and 0 <= c < W):
                break
            if occ[r, c]:
                out.append((d * math.cos(a) + rs.normal(0, noise),
                            d * math.sin(a) + rs.normal(0, noise)))
                break
    return out


@pytest.fixture(scope="module")
def 图():
    occ, free = _房间(extra=不对称)
    return occ, Matcher(occ, free, RES, (0.0, 0.0))


@pytest.mark.parametrize("pose", [(3.0, 3.0, 0.4), (8.5, 8.0, -2.0), (13.5, 2.5, 2.9)])
def test_找回真实位置_差不到15厘米和3度(图, pose):
    occ, m = 图
    pts = _扫(occ, *pose, noise=0.02)
    cands = m.search(pts)
    best, why = decide(cands)
    assert best is not None, (why, cands[:3])
    assert math.hypot(best.x - pose[0], best.y - pose[1]) < 0.15, (best, pose)
    assert abs(math.remainder(best.yaw - pose[2], 2 * math.pi)) < math.radians(3), (best, pose)
    assert best.fit > 0.7
    assert m.fit(pts, pose) > 0.7
    assert m.fit(pts, (pose[0] + 2.0, pose[1], pose[2])) < 0.5, "错的位置贴合度低"


def test_对称的空房间_说不准是哪一处_不认():
    occ, free = _房间(w=10.0, h=10.0)
    m = Matcher(occ, free, RES, (0.0, 0.0))
    best, why = decide(m.search(_扫(occ, 5.0, 5.0, 0.3)))
    assert best is None and "差不多像" in why, why


def test_看到的太少_说清楚():
    occ, free = _房间(extra=不对称)
    m = Matcher(occ, free, RES, (0.0, 0.0))
    with pytest.raises(GlobalLocError, match="太少"):
        m.search([(1.0, 0.0)] * 5)
    with pytest.raises(GlobalLocError, match="没有占用格"):
        Matcher(np.zeros((10, 10), bool), np.ones((10, 10), bool), RES, (0.0, 0.0))


def test_认不认_贴合度太低不认_第二名太近不认():
    assert decide([])[0] is None
    assert decide([Candidate(0, 0, 0, MIN_FIT - 0.01)])[0] is None
    b, why = decide([Candidate(0, 0, 0, 0.8), Candidate(5, 0, 0, 0.75)])
    assert b is None and "差不多像" in why
    b, _ = decide([Candidate(0, 0, 0, 0.8), Candidate(5, 0, 0, 0.6)])
    assert b == Candidate(0, 0, 0, 0.8)


def test_核对_对得上不动_锁到远处的错解按对的那处重定位_认不出只标不可信():
    good = [Candidate(2.4, -2.8, 3.0, 0.72), Candidate(0.5, -0.5, -1.5, 0.41)]
    assert judge(0.70, (2.4, -2.8, 3.0), good).ok
    v = judge(0.44, (-2.5, -2.0, 3.0), good)                 # 现场:MOLA 锁到 5 m 外,贴合度 0.44
    assert not v.ok and v.reloc == good[0] and "锁错了" in v.reason
    near = judge(0.50, (2.5, -2.7, 3.0), good)               # 就在对的那处附近:只是差一点,不管
    assert near.ok
    two = [Candidate(2.4, -2.8, 3.0, 0.72), Candidate(-0.8, 0.5, 1.3, 0.70)]
    v = judge(0.40, (5.0, 5.0, 0.0), two)
    assert not v.ok and v.reloc is None and "请重新设位置" in v.reason
    assert judge(0.30, (5.0, 5.0, 0.0), [Candidate(1, 1, 0, 0.35)]).ok, "哪儿都不像:不下结论"


FIELD = Path(__file__).resolve().parents[3] / "runs" / "field-c40221" / "site-20261026"


@pytest.mark.skipif(not (FIELD / "site/maps/office-c40221/2/floor.pgm").exists(),
                    reason="现场数据只在开发机上(runs/ 不进 git)")
def test_现场数据_C40221_20261026_跟现场脚本一致():
    """现场那次笔记本上的脚本算出来的位置(whereami.py):本模块要找到同一处(±0.2 m、±3°)。"""
    m = Matcher.from_dir(FIELD / "site/maps/office-c40221/2")
    ref = {"scan-20261026.npz": (-2.85, -3.79, 83.0), "scan-20261026d.npz": (1.32, -0.01, -5.5),
           "scan-20261026h.npz": (2.40, 0.86, -93.0), "scan-20261026l.npz": (3.22, -2.16, -87.5)}
    for name, (x, y, deg) in ref.items():
        best, why = decide(m.search(np.load(FIELD / name)["xy"]))
        assert best is not None, (name, why)
        assert math.hypot(best.x - x, best.y - y) < 0.2, (name, best)
        assert abs(math.remainder(best.yaw - math.radians(deg), 2 * math.pi)) < math.radians(3)


def test_工单10_人给的跟定位器的冲突_按两处贴合度判信谁():
    from d1max_agent.globalloc import TRUST_FIT, WRONG_GAP, resolve_conflict
    assert resolve_conflict(TRUST_FIT + 0.1, 0.2) == "localizer"
    assert resolve_conflict(0.2, TRUST_FIT + 0.1) == "anchor"
    assert resolve_conflict(TRUST_FIT + 0.05, TRUST_FIT + 0.05 - WRONG_GAP / 2) == "", "差得不够"
    assert resolve_conflict(TRUST_FIT - 0.05, 0.0) == "", "哪处都不够像:不下结论"
