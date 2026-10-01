"""W11 感知核心:外参(RS-Airy 原始系 X 竖直、Z 朝前)、地面拟合与自检、高度带、落差、看不见、
机身自己的点、净空距离与许可、发出去(假套接字)、后雷达合并。"""

from __future__ import annotations

import json
import math
import socket
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from d1max_localizer.obstacles import (  # noqa: E402
    Config,
    LineClient,
    Mount,
    Perception,
    SelfCheck,
    active_frames,
    classify,
    clear_distance,
    fit_ground,
    permit,
)

from d1max_contract.obsbridge import Grid, unpack_bits  # noqa: E402

#: RS-Airy 原始系:X 朝上、Z 朝前(``tools/lio/README.md``);建图标出来的 frames.json 就是这样。
AIRY = SimpleNamespace(sensor_up=(1.0, 0.0, 0.0), sensor_forward=(0.0, 0.0, 1.0),
                       sensor_in_base=(0.4043, 0.0))
H = 0.40                                           # 雷达离地


def 场景(*, box=None, drop=None, hole=None, step=0.05, rng=6.0, back=False):
    """狗身系(z 相对雷达)的点:前半球的地面 + 可选的箱子、落差(地面低下去)、空洞(打不到东西)。"""
    xs = np.arange(-rng if back else 0.3, rng, step)
    ys = np.arange(-rng, rng, step)
    X, Y = np.meshgrid(xs, ys)
    pts = np.stack([X.ravel(), Y.ravel(), np.full(X.size, -H)], 1)
    if hole is not None:
        (x0, x1), (y0, y1) = hole
        keep = ~((pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1))
        pts = pts[keep]
    if drop is not None:
        (x0, x1), (y0, y1) = drop
        m = (pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)
        pts[m, 2] = -H - 0.4
    if box is not None:
        (x0, x1), (y0, y1), top = box
        bx, by, bz = np.meshgrid(np.arange(x0, x1, step), np.arange(y0, y1, step),
                                 np.arange(-H, -H + top, step))
        pts = np.vstack([pts, np.stack([bx.ravel(), by.ravel(), bz.ravel()], 1)])
    return pts


def 到雷达系(m: Mount, base):
    R = np.array([m.fwd, m.left, m.up])
    b = base.copy()
    b[:, 0] -= m.x
    b[:, 1] -= m.y
    return b @ R


def _cell(cfg, x, y):
    n = cfg.size
    return int(math.floor(x / cfg.res + n / 2)) * n + int(math.floor(y / cfg.res + n / 2))


def test_外参_Airy原始系_转回狗身系():
    m = Mount.from_frames(AIRY)
    assert m.fwd == (0.0, 0.0, 1.0) and m.up == (1.0, 0.0, 0.0)
    assert np.allclose(m.left, (0.0, -1.0, 0.0))
    # 雷达系里正前方 2 m、上方 0.1 m 的点 → 狗身系 (2 + 0.4043, 0, 0.1)
    got = m.to_base(np.array([[0.1, 0.0, 2.0]]))
    assert np.allclose(got, [[2.4043, 0.0, 0.1]])
    back = m.mirrored()
    assert np.allclose(back.to_base(np.array([[0.0, 0.0, 1.0]])), [[-1.4043, 0.0, 0.0]])
    # 「前」带一点「上」的分量也能拉正(标定出来不是严格正交)
    m2 = Mount.from_frames(SimpleNamespace(sensor_up=(1.0, 0.0, 0.0),
                                           sensor_forward=(0.05, 0.0, 1.0), sensor_in_base=None))
    assert abs(sum(a * b for a, b in zip(m2.fwd, m2.up, strict=True))) < 1e-12
    with pytest.raises(ValueError):
        Mount.from_frames(SimpleNamespace(sensor_up=(0, 0, 0), sensor_forward=(0, 0, 1),
                                          sensor_in_base=None))


def test_地面拟合_高度与法向():
    n, h, frac = fit_ground(场景())
    assert n[2] > 0.999 and abs(h - H) < 0.01 and frac > 0.9
    assert fit_ground(np.zeros((5, 3))) is None


def test_自检_过与不过():
    ok = SelfCheck(frames=3)
    for _ in range(3):
        ok.feed(场景())
    assert ok.check == "ok" and abs(ok.height - H) < 0.01
    tilted = 场景()
    a = math.radians(10)
    tilted[:, 2] += tilted[:, 0] * math.tan(a)                  # 外参歪了 10°
    bad = SelfCheck(frames=3)
    for _ in range(3):
        bad.feed(tilted)
    assert bad.check == "extrinsic_bad" and "°" in bad.reason
    high = 场景()
    high[:, 2] -= 1.0                                           # 离地 1.4 m
    bad2 = SelfCheck(frames=3)
    for _ in range(3):
        bad2.feed(high)
    assert bad2.check == "extrinsic_bad" and "离地" in bad2.reason
    wall = np.vstack([场景(step=0.5), np.stack(np.meshgrid(np.array([1.0]), np.arange(-3, 3, 0.02),
                                                            np.arange(-0.39, 1.0, 0.02)),
                                                -1).reshape(-1, 3)])
    bad3 = SelfCheck(frames=3)
    for _ in range(3):
        bad3.feed(wall)
    assert bad3.check == "extrinsic_bad" and "地面点" in bad3.reason, bad3.reason
    still = SelfCheck(frames=3)
    still.feed(场景())
    assert still.check == "initializing" and still.height is None


def test_分类_凸起_落差_看不见_矮草_机身():
    cfg = Config()
    pts = 场景(box=((2.0, 2.4), (-0.2, 0.2), 0.5), drop=((1.5, 1.9), (1.0, 1.4)),
               hole=((2.5, 3.0), (-1.5, -1.0)))
    grass = np.array([[1.0, -1.0, -H + 0.05]])
    leg = np.array([[0.3, 0.1, -H + 0.2]])                       # 机身里(腿)的点
    occ, known = classify(np.vstack([pts, grass, leg]), H, cfg)
    assert occ[_cell(cfg, 2.2, 0.0)] and known[_cell(cfg, 2.2, 0.0)], "箱子挡"
    assert occ[_cell(cfg, 1.7, 1.2)], "落差挡"
    assert not known[_cell(cfg, 2.7, -1.2)] and not occ[_cell(cfg, 2.7, -1.2)], "打不到 = 未知"
    assert known[_cell(cfg, 1.0, -1.0)] and not occ[_cell(cfg, 1.0, -1.0)], "地面、矮草不挡"
    assert not occ[_cell(cfg, 0.3, 0.1)], "机身自己的点不算"
    assert not known[_cell(cfg, -2.0, 0.0)], "身后没看见"
    far_drop = 场景(drop=((3.5, 3.9), (0.0, 0.4)))
    o2, _ = classify(far_drop, H, cfg)
    assert not o2[_cell(cfg, 3.7, 0.2)], "落差只在近处认"
    assert classify(np.zeros((0, 3)), H, cfg) == (bytes(cfg.size ** 2), bytes(cfg.size ** 2))


def test_净空距离与许可():
    cfg = Config()
    occ, known = classify(场景(box=((2.0, 2.4), (-0.2, 0.2), 0.5)), H, cfg)
    d = clear_distance(occ, known, cfg)
    assert abs(d - (2.0 - 0.465)) <= 0.1
    assert permit(d, cfg) == 300
    occ2, known2 = classify(场景(box=((0.6, 0.8), (0.0, 0.3), 0.5)), H, cfg)
    d2 = clear_distance(occ2, known2, cfg)
    assert d2 < 0.2 and permit(d2, cfg) == 0
    occ3, known3 = classify(场景(box=((1.0, 1.2), (0.6, 0.9), 0.5)), H, cfg)
    assert clear_distance(occ3, known3, cfg) > 3.0, "走廊外面的不管"
    occ4, known4 = classify(场景(hole=((1.0, 1.3), (-0.1, 0.1))), H, cfg)
    assert clear_distance(occ4, known4, cfg) < 0.7, "看不见当挡"
    assert cfg.stop_dist(0.6) == pytest.approx(0.12 + 0.36)


class 假对端:
    def __init__(self):
        self.a, self.b = socket.socketpair()
        self.lines = []

    def read(self):
        self.b.setblocking(False)
        buf = b""
        try:
            while True:
                chunk = self.b.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
        except BlockingIOError:
            pass
        self.lines += [json.loads(x) for x in buf.splitlines() if x]
        return self.lines


def test_感知_自检完了才发_栅格合契约_许可只在够远时发():
    m = Mount.from_frames(AIRY)
    obs, side = 假对端(), 假对端()
    per = Perception(m, cfg=Config(), obs=LineClient(lambda: obs.a, {"t": "hello", "proto": 1}),
                     sidecar=LineClient(lambda: side.a, None))
    per.check = SelfCheck(frames=2)
    sensor = 到雷达系(m, 场景(box=((2.0, 2.4), (-0.2, 0.2), 0.5)))
    assert per.on_front(sensor, 10) is None
    g = per.on_front(sensor, 20)
    assert g is not None and g["check"] == "ok" and not g["rear"]
    got = obs.read()
    assert got[0] == {"t": "hello", "proto": 1}
    grid = Grid(**{k: v for k, v in got[1].items() if k != "t"})
    occ, _ = grid.bits()
    assert occ[_cell(Config(), 2.2, 0.0)]
    cmds = side.read()
    assert cmds and cmds[0]["cmd"] == "clear" and cmds[0]["ms"] == 300 and cmds[0]["dist"] > 1.0
    near = 到雷达系(m, 场景(box=((0.6, 0.8), (0.0, 0.3), 0.5)))
    per.on_front(near, 30)
    assert len(side.read()) == 1, "前面太近:不发许可"


def test_感知_自检不过_栅格照发_不发许可():
    m = Mount.from_frames(AIRY)
    side = 假对端()
    per = Perception(m, sidecar=LineClient(lambda: side.a, None))
    per.check = SelfCheck(frames=1)
    tilted = 场景()
    tilted[:, 2] += tilted[:, 0] * math.tan(math.radians(10))
    g = per.on_front(到雷达系(m, tilted), 1)
    assert g["check"] == "extrinsic_bad" and g["reason"]
    assert side.read() == []


def test_后雷达_新鲜才合进来():
    m = Mount.from_frames(AIRY)
    t = [0.0]
    per = Perception(m, rear=m.mirrored(), clock=lambda: t[0])
    per.check = SelfCheck(frames=1)
    back = 场景(back=True)
    back_only = back[back[:, 0] < -0.5]
    per.on_rear(到雷达系(m.mirrored(), back_only))
    g = per.on_front(到雷达系(m, 场景()), 1)
    known = unpack_bits(g["known"], 80)
    assert g["rear"] and known[_cell(Config(), -2.0, 0.0)]
    t[0] = 1.0
    g2 = per.on_front(到雷达系(m, 场景()), 2)
    assert not g2["rear"] and not unpack_bits(g2["known"], 80)[_cell(Config(), -2.0, 0.0)]


def test_发不出去就断开_过一会儿再连():
    calls = []

    def 连不上():
        calls.append(1)
        raise OSError("没有")
    c = LineClient(连不上, None, reconnect_s=10.0)
    assert not c.send({"a": 1}) and not c.send({"a": 2})
    assert len(calls) == 1, "没到重连间隔不再连"


def test_跟着正在用的图找外参(tmp_path):
    assert active_frames(tmp_path) is None
    (tmp_path / "active.json").write_text(json.dumps({"map_id": "m", "version": "2"}))
    assert active_frames(tmp_path) is None, "图目录里还没有 frames.json"
    d = tmp_path / "m" / "2"
    d.mkdir(parents=True)
    (d / "frames.json").write_text("{}")
    assert active_frames(tmp_path) == ("m:2", d / "frames.json")
    for bad in ({"map_id": "../x", "version": "2"}, {"map_id": "m"}, [], {"map_id": 1,
                                                                          "version": "2"}):
        (tmp_path / "active.json").write_text(json.dumps(bad))
        assert active_frames(tmp_path) is None, bad
    (tmp_path / "active.json").write_text("坏")
    assert active_frames(tmp_path) is None


def test_换图_换外参重新自检():
    m = Mount.from_frames(AIRY)
    per = Perception(m)
    per.check = SelfCheck(frames=1)
    per.on_front(到雷达系(m, 场景()), 1)
    assert per.check.check == "ok"
    per.set_mount(m.mirrored(), None)
    assert per.check.check == "initializing" and per.front.x == -m.x
