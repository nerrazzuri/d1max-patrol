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


# ---------------------------------------------------------------- W47 外审 R7、R8、R10(目标的生灭)

class _Handle:
    accepted = True

    def __init__(self):
        self.cancels = 0
        self.done = []

    def cancel_goal_async(self):
        self.cancels += 1

    def get_result_async(self):
        from types import SimpleNamespace as NS
        return NS(add_done_callback=self.done.append)


def _桥(monkeypatch):
    import sys
    from concurrent.futures import Future
    from types import SimpleNamespace as NS

    from d1max_localizer.nav2bridge import Nav2Bridge
    b = Nav2Bridge.__new__(Nav2Bridge)
    b._init_goal_book()
    b.sent = []
    b.send = b.send_threadsafe = b.sent.append
    b.t_ms = 10
    b._stamp = lambda _t: 0
    monkeypatch.setitem(sys.modules, "geometry_msgs.msg", NS(PoseStamped=lambda: NS(
        header=NS(), pose=NS(position=NS(), orientation=NS()))))
    b._m = {"NavigateToPose": NS(Goal=lambda: NS())}
    futs: dict[int, Future] = {}

    def send_goal_async(_goal):
        f = Future()
        futs[len(futs)] = f
        return f
    b.nav = NS(server_is_ready=lambda: True, send_goal_async=send_goal_async)
    return b, futs


def test_外审R8_Nav2还没收下就取消_收下了马上取消_不当成在走的(monkeypatch):
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 41, "x": 1, "y": 2, "yaw": 0})
    b.on_cancel({"goal_id": 41})
    h = _Handle()
    futs[0].set_result(h)
    assert h.cancels == 1 and b.active_gid is None
    assert not any(m.get("state") == "accepted" for m in b.sent)


def test_外审R8_代理断了_还没收下的目标收下了也马上取消(monkeypatch):
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 7, "x": 1, "y": 2, "yaw": 0})
    b.cancel_all()
    h = _Handle()
    futs[0].set_result(h)
    assert h.cancels == 1 and b.active_gid is None


def test_外审R7_速度标上是哪个目标的(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    futs[0].set_result(_Handle())
    b._on_cmd(NS(linear=NS(x=0.3, y=0.0), angular=NS(z=0.1)))
    assert b.sent[-1]["type"] == "cmd" and b.sent[-1]["goal_id"] == 5


def test_外审R10_收下新目标以前规划的路线不标成新目标的(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.t_ms = 1000
    b.on_goal({"goal_id": 9, "x": 2, "y": 4, "yaw": 0})
    futs[0].set_result(_Handle())
    old = NS(header=NS(stamp=NS(sec=0, nanosec=900_000_000)),
             poses=[NS(pose=NS(position=NS(x=x, y=4.0))) for x in (1.0, 3.0, 6.0)])
    b._on_plan(old)
    assert not [m for m in b.sent if m["type"] == "path"], "收下之前规划的:不报"
    new = NS(header=NS(stamp=NS(sec=1, nanosec=100_000_000)),
             poses=[NS(pose=NS(position=NS(x=x, y=4.0))) for x in (5.0, 3.0, 2.0)])
    b._on_plan(new)
    assert [m["goal_id"] for m in b.sent if m["type"] == "path"] == [9]
