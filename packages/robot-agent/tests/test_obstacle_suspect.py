"""可疑格子(W29 复查,决策 42):感知节点滤雨点滤掉的孤立凸起点,**这一帧可疑就当挡**,代理滚动记忆里
旧帧看见过的「空」不许补上来。整条链路:感知节点分类 → 障碍桥报文(``suspect``)→ 代理记忆 → 守卫。"""

from __future__ import annotations

import numpy as np
from d1max_localizer.obstacles import Config, classify_full

from d1max_agent.obstacles import ObstacleGuard, ObstacleView
from d1max_contract.obsbridge import Grid, pack_bits

H = 0.5


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _地面(x0=0.5, x1=3.0, y=0.6, step=0.05):
    xs, ys = np.meshgrid(np.arange(x0, x1, step), np.arange(-y, y, step))
    return np.stack([xs.ravel(), ys.ravel(), np.full(xs.size, -H)], 1)


def _报文(pts, seq, cfg):
    occ, known, sus = classify_full(pts, H, cfg)
    n = cfg.size
    d = {"seq": seq, "stamp_ns": 0, "res": cfg.res, "size": n, "occ": pack_bits(occ, n),
         "known": pack_bits(known, n)}
    if any(sus):
        d["suspect"] = pack_bits(sus, n)
    return Grid(**d)


def _台():
    c = 钟()
    v = ObstacleView(monotonic=c)
    v.on_connect()
    v.note_odom(0.0, 0.0, 0.0)
    return c, v


def test_先空地_再来一帧同格地面点加一个凸起点_守卫不放行():
    cfg = Config()
    c, v = _台()
    g = ObstacleGuard()
    v.on_grid(_报文(_地面(), 1, cfg))
    assert g.check(0.3, 0.0, 0.0, v, (0.0, 0.0, 0.0)).ok, "空地:放行"
    c.t += 0.1
    v.note_odom(0.0, 0.0, 0.0)
    pole = np.array([[0.92, 0.02, -H + 0.4]])            # 机身前沿 0.45 m 处:一个孤零零的高点
    v.on_grid(_报文(np.vstack([_地面(), pole]), 2, cfg))
    verdict = g.check(0.3, 0.0, 0.0, v, (0.0, 0.0, 0.0))
    assert not verdict.ok, "这一帧可疑:当挡,旧帧的空补不上来"
    c.t += 0.1
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_报文(_地面(), 3, cfg))                    # 下一帧没了(雨滴)
    assert g.check(0.3, 0.0, 0.0, v, (0.0, 0.0, 0.0)).ok, "雨滴一闪就没:接着走"


def test_可疑的帧过去了_更新的帧看见是空_按新的():
    cfg = Config()
    c, v = _台()
    v.on_grid(_报文(np.vstack([_地面(), [[1.5, 0.0, -H + 0.4]]]), 1, cfg))
    c.t += 0.1
    v.note_odom(0.0, 0.0, 0.0)
    v.on_grid(_报文(_地面(), 2, cfg))
    hit, unknown = v.lookup([(1.5, 0.0)], (0.0, 0.0, 0.0))
    assert hit == [] and unknown == []


def test_报文_可疑位图长度不对_拒():
    import pytest

    from d1max_contract.errors import ContractError
    n = 10
    ok = {"seq": 1, "stamp_ns": 0, "res": 0.1, "size": n, "occ": pack_bits([False] * 100, n),
          "known": pack_bits([True] * 100, n)}
    assert Grid(**ok).suspect_bits() == bytes(100), "没带:全 0"
    with pytest.raises(ContractError):
        Grid(**ok, suspect=pack_bits([False] * 400, 20))
    with pytest.raises(ContractError):
        Grid(**ok, suspect=7)
