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


_号 = iter(range(1, 1_000_000))


def _状态(*codes, ids=None, at_ms=10**9):
    """一份动作状态列表;不给目标号就每项一个新号;``at_ms`` 是 Nav2 收下它的时刻(仿真钟)。"""
    from types import SimpleNamespace as NS
    ids = ids or [next(_号) for _ in codes]
    stamp = NS(sec=at_ms // 1000, nanosec=(at_ms % 1000) * 1_000_000)
    return NS(status_list=[NS(status=c, goal_info=NS(goal_id=NS(uuid=i.to_bytes(16, "big")),
                                                     stamp=stamp))
                           for c, i in zip(codes, ids, strict=True)])


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
    b._on_status("follow_path", _状态(2))                    # 这个目标的控制器开跑了
    b._mono = lambda: 1e9                     # 过了收下之后的屏障(复查 N3)
    b._on_cmd(NS(linear=NS(x=0.3, y=0.0), angular=NS(z=0.1)))
    assert b.sent[-1]["type"] == "cmd" and b.sent[-1]["goal_id"] == 5


def test_外审R10_收下新目标以前规划的路线不标成新目标的(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.t_ms = 1000
    b.on_goal({"goal_id": 9, "x": 2, "y": 4, "yaw": 0})
    futs[0].set_result(_Handle())
    b._on_status("compute_path_to_pose", _状态(2))          # 新目标的规划开始了
    old = NS(header=NS(stamp=NS(sec=0, nanosec=900_000_000)),
             poses=[NS(pose=NS(position=NS(x=x, y=4.0))) for x in (1.0, 3.0, 6.0)])
    b._on_plan(old)
    assert not [m for m in b.sent if m["type"] == "path"], "收下之前规划的:不报"
    new = NS(header=NS(stamp=NS(sec=1, nanosec=100_000_000)),
             poses=[NS(pose=NS(position=NS(x=x, y=4.0))) for x in (5.0, 3.0, 2.0)])
    b._on_plan(new)
    assert [m["goal_id"] for m in b.sent if m["type"] == "path"] == [9]


def test_转接节点握手的协议号跟契约同一个():
    """外审修复把契约升到 2 时转接节点自己还写死着 1:真机上 Nav2 会一直连不上
    (navbench 查出来的)。"""
    from d1max_localizer import nav2bridge

    from d1max_contract.navbridge import PROTO
    assert nav2bridge.PROTO == PROTO


# ---------------------------------------------------------------- W47 复查 N3、N4、N6

def test_外审复查N4_收下的回调夹在中间时来了取消_也不丢(monkeypatch):
    import threading
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 41, "x": 1, "y": 2, "yaw": 0})
    h = _Handle()
    real_lock = b._goal_lock
    inside = threading.Event()
    go = threading.Event()

    class 慢锁:
        """收下回调第一次拿锁、放锁之后停一下(原来两段锁之间正好能插进取消)。"""
        n = 0

        def __enter__(self):
            real_lock.acquire()

        def __exit__(self, *a):
            real_lock.release()
            if threading.current_thread().name == "rclpy" and 慢锁.n == 0:
                慢锁.n += 1
                inside.set()
                go.wait(2)
    b._goal_lock = 慢锁()
    th = threading.Thread(target=lambda: futs[0].set_result(h), name="rclpy")
    th.start()
    inside.wait(0.5)
    b.on_cancel({"goal_id": 41})
    go.set()
    th.join(2)
    assert h.cancels == 1 and b.active_gid is None


def test_外审复查N3_新目标刚收下时排着队的旧速度不发(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    futs[0].set_result(_Handle())
    b._on_status("follow_path", _状态(2))
    b._on_cmd(NS(linear=NS(x=0.4, y=0.0), angular=NS(z=0.0)))   # 上一个目标排着队的那条
    assert not [m for m in b.sent if m["type"] == "cmd"]
    b._mono = lambda: 1e9                                        # 过了屏障
    b._on_cmd(NS(linear=NS(x=0.3, y=0.0), angular=NS(z=0.0)))
    assert [m["goal_id"] for m in b.sent if m["type"] == "cmd"] == [5]


def test_外审复查N6_同一拍规划的旧路线_终点对不上新目标就不报(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.t_ms = 2000
    b.on_goal({"goal_id": 9, "x": 2, "y": 4, "yaw": 0})
    futs[0].set_result(_Handle())
    b._on_status("compute_path_to_pose", _状态(2))          # 新目标的规划开始了
    old = NS(header=NS(stamp=NS(sec=2, nanosec=0)),
             poses=[NS(pose=NS(position=NS(x=x, y=4.0))) for x in (5.0, 7.0, 8.0)])
    b._on_plan(old)
    assert not [m for m in b.sent if m["type"] == "path"]
    new = NS(header=NS(stamp=NS(sec=2, nanosec=0)),
             poses=[NS(pose=NS(position=NS(x=x, y=4.0))) for x in (5.0, 3.0, 2.05)])
    b._on_plan(new)
    assert [m["goal_id"] for m in b.sent if m["type"] == "path"] == [9]


def test_外审复查P2_近处的旧路线_终点差0点8米也不认(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.t_ms = 2000
    b.on_goal({"goal_id": 9, "x": 2, "y": 4, "yaw": 0})
    futs[0].set_result(_Handle())
    b._on_status("compute_path_to_pose", _状态(2))          # 新目标的规划开始了
    old = NS(header=NS(stamp=NS(sec=2, nanosec=0)),
             poses=[NS(pose=NS(position=NS(x=x, y=4.0))) for x in (5.0, 3.5, 2.8)])
    b._on_plan(old)
    assert not [m for m in b.sent if m["type"] == "path"]


def test_复查三P1_上一个目标在Nav2里没结束_新目标先排着_结束了才发(monkeypatch):
    """Nav2 的速度不带目标号:让上一个目标在 Nav2 里真结束(有终态结果 = 老控制器停了)再发下一个。"""
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 1, "x": 1, "y": 2, "yaw": 0})
    h1 = _Handle()
    futs[0].set_result(h1)
    b.on_cancel({"goal_id": 1})
    b.on_goal({"goal_id": 2, "x": 3, "y": 2, "yaw": 0})
    assert len(futs) == 1, "上一个还没结束:排着"
    import sys
    from types import SimpleNamespace as NS
    GoalStatus = NS(STATUS_SUCCEEDED=4, STATUS_CANCELED=5)
    monkeypatch.setitem(sys.modules, "action_msgs.msg", NS(GoalStatus=GoalStatus))
    b.t_ms = 500
    h1.done[0](NS(result=lambda: NS(status=GoalStatus.STATUS_CANCELED)))
    assert len(futs) == 2 and b.last_end_ms == 500, "结束了马上发"


def test_复查三P1_排着的目标被取消了就不发_排太久回拒(monkeypatch):
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 1, "x": 1, "y": 2, "yaw": 0})
    futs[0].set_result(_Handle())
    b.on_goal({"goal_id": 2, "x": 3, "y": 2, "yaw": 0})
    b.on_cancel({"goal_id": 2})
    assert b.queued is None and b.sent[-1] == {"type": "goal_state", "goal_id": 2,
                                               "state": "canceled", "reason": ""}
    b.on_goal({"goal_id": 3, "x": 3, "y": 2, "yaw": 0})
    b._mono = lambda: 1e9
    b.expire_queued()
    assert b.queued is None and b.sent[-1]["goal_id"] == 3 and b.sent[-1]["state"] == "rejected"
    assert len(futs) == 1


def test_复查四P2_派出新目标以后_规划动作重新开跑之前来的路线一律不认(monkeypatch):
    """路线的时间戳不是目标代次(Smac 拿到锁才填):老的规划停了(派之前确认过)、新的规划开跑以后
    来的路线才是新目标的。"""
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.t_ms = 1000
    b.on_goal({"goal_id": 9, "x": 2, "y": 4, "yaw": 0})
    futs[0].set_result(_Handle())

    def plan(at_ms, end):
        pose = NS(pose=NS(position=NS(x=end[0], y=end[1])))
        return NS(header=NS(stamp=NS(sec=at_ms // 1000, nanosec=(at_ms % 1000) * 1_000_000)),
                  poses=[pose])
    b._on_plan(plan(1200, (2.0, 4)))                      # 时间戳新、终点也对,可规划还没开跑
    assert not [m for m in b.sent if m["type"] == "path"]
    b._on_status("compute_path_to_pose", _状态(2))
    b._on_plan(plan(1000, (2.0, 4)))                      # 开跑以后:同一拍的也认(不晚一秒起步)
    assert [m for m in b.sent if m["type"] == "path"][-1]["goal_id"] == 9


def test_复查三P4_脱困动作的速度单独一路_标上beh(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    futs[0].set_result(_Handle())
    b._on_status("follow_path", _状态(2))
    b._on_status("backup", _状态(2))
    b._mono = lambda: 1e9
    b._on_beh(NS(linear=NS(x=-0.15, y=0.0), angular=NS(z=0.0)))
    b._on_cmd(NS(linear=NS(x=0.3, y=0.0), angular=NS(z=0.1)))
    assert [m["src"] for m in b.sent if m["type"] == "cmd"] == ["beh", "ctl"]


def test_复查三P1_代理断了重连_旧目标在Nav2里结束之前_新目标排着(monkeypatch):
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 1, "x": 1, "y": 2, "yaw": 0})
    h = _Handle()
    futs[0].set_result(h)
    b.cancel_all()
    assert h.cancels == 1
    b.on_goal({"goal_id": 2, "x": 3, "y": 2, "yaw": 0})
    assert len(futs) == 1 and b.queued["goal_id"] == 2


def _结束(b, h, monkeypatch, status=5):
    import sys
    from types import SimpleNamespace as NS
    monkeypatch.setitem(sys.modules, "action_msgs.msg",
                        NS(GoalStatus=NS(STATUS_SUCCEEDED=4, STATUS_CANCELED=5)))
    h.done[0](NS(result=lambda: NS(status=status)))


def test_复查四P1_导航报结束了_控制器那个动作还在跑_新目标排着_它真停了才发(monkeypatch):
    """Nav2 取消下层动作只等 20 ms,等不到也报结束:看控制器 follow_path 自己的动作状态才算停了。"""
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 1, "x": 1, "y": 2, "yaw": 0})
    h = _Handle()
    futs[0].set_result(h)
    b._on_status("follow_path", _状态(2))                      # 控制器在跑
    b.on_cancel({"goal_id": 1})
    b.on_goal({"goal_id": 2, "x": 3, "y": 2, "yaw": 0})
    _结束(b, h, monkeypatch)                                     # 导航报「已取消」
    assert len(futs) == 1, "控制器还没停:不发"
    b._on_status("follow_path", _状态(3))                      # 正在取消
    assert len(futs) == 1
    b.t_ms = 700
    b._on_status("follow_path", _状态(5))                      # 控制器自己结束了
    assert len(futs) == 2 and b.last_end_ms == 700


def test_复查四P4_脱困动作还在跑_新目标也排着(monkeypatch):
    b, futs = _桥(monkeypatch)
    b._on_status("backup", _状态(2))
    b.on_goal({"goal_id": 1, "x": 1, "y": 2, "yaw": 0})
    assert len(futs) == 0
    b._on_status("backup", _状态(4, 6))
    assert len(futs) == 1


def test_复查四P1_结果查询出错_也要等生产端真停了(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 1, "x": 1, "y": 2, "yaw": 0})
    h = _Handle()
    futs[0].set_result(h)
    b._on_status("navigate_to_pose", _状态(2))
    b.on_goal({"goal_id": 2, "x": 3, "y": 2, "yaw": 0})

    import sys

    def boom():
        raise RuntimeError("结果拿不到")
    monkeypatch.setitem(sys.modules, "action_msgs.msg",
                        NS(GoalStatus=NS(STATUS_SUCCEEDED=4, STATUS_CANCELED=5)))
    h.done[0](NS(result=boom))
    assert len(futs) == 1, "导航动作自己的状态还在跑:不发"
    b._on_status("navigate_to_pose", _状态(6))
    assert len(futs) == 2


def test_复查四Q1_排着的时候又来一个_顶掉旧的_派的时候过期了回拒不发(monkeypatch):
    b, futs = _桥(monkeypatch)
    b._on_status("follow_path", _状态(2))
    b.on_goal({"goal_id": 2, "x": 3, "y": 2, "yaw": 0})
    b.on_goal({"goal_id": 3, "x": 4, "y": 2, "yaw": 0})
    assert b.sent[-1] == {"type": "goal_state", "goal_id": 2, "state": "rejected",
                          "reason": "又来了一个新目标"}
    assert b.queued["goal_id"] == 3
    b._mono = lambda: 1e9                                      # 排太久了
    b._on_status("follow_path", _状态(5))
    assert len(futs) == 0 and b.queued is None
    assert b.sent[-1]["goal_id"] == 3 and b.sent[-1]["state"] == "rejected"


def test_复查四Q1_空闲时来的目标_先看有没有排着的_不越过它(monkeypatch):
    """ROS 线程清了账、还没轮到事件循环派排着的 B,这时来了 C:C 顶掉 B,不是 C 先走、B 留着。"""
    b, futs = _桥(monkeypatch)
    b.queued, b.queued_at = {"goal_id": 2, "x": 3, "y": 2, "yaw": 0}, b._mono()
    b.on_goal({"goal_id": 3, "x": 4, "y": 2, "yaw": 0})
    assert len(futs) == 1 and b.queued is None
    assert b.sent[-1]["goal_id"] == 2 and b.sent[-1]["state"] == "rejected"


def test_复查四P2_规划状态只收到完成那一条_也认得出是新规划(monkeypatch):
    """状态话题只留最新一条:规划快的话「在跑」被「完成」盖掉,按列表里出现的新目标号认。"""
    b, futs = _桥(monkeypatch)
    b._on_status("compute_path_to_pose", _状态(4, ids=[1]))       # 老的,早结束了
    b.on_goal({"goal_id": 9, "x": 2, "y": 4, "yaw": 0})
    futs[0].set_result(_Handle())
    b._on_status("compute_path_to_pose", _状态(4, ids=[1]))       # 还是老的那份:不开
    assert "plan" not in b.opened
    b._on_status("compute_path_to_pose", _状态(4, 4, ids=[1, 2]))  # 多了一个新号(已完成)
    assert "plan" in b.opened


def test_复查五_控制器的速度_要派出以后控制器自己出现新目标才转(monkeypatch):
    """Twist 不带目标号:派出新目标以后 follow_path 的状态里出现了新目标号(这个目标的控制器开跑了),
    控制器的速度才转给代理;之前来的(老控制器晚到的)不转。"""
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b._on_status("follow_path", _状态(5, ids=[1]))           # 老目标的,早结束了
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    futs[0].set_result(_Handle())
    b._mono = lambda: 1e9
    b._on_cmd(NS(linear=NS(x=0.4, y=0.0), angular=NS(z=0.0)))
    assert not [m for m in b.sent if m["type"] == "cmd"], "新控制器还没开跑"
    b._on_status("follow_path", _状态(5, 2, ids=[1, 2]))
    b._on_cmd(NS(linear=NS(x=0.3, y=0.0), angular=NS(z=0.0)))
    assert [m["vx"] for m in b.sent if m["type"] == "cmd"] == [0.3]


def test_复查五_脱困的速度_要这个目标的脱困动作开跑过才转(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    futs[0].set_result(_Handle())
    b._mono = lambda: 1e9
    beh = NS(linear=NS(x=-0.15, y=0.0), angular=NS(z=0.0))
    b._on_beh(beh)                                            # 老目标的 BackUp 晚到的
    assert not [m for m in b.sent if m["type"] == "cmd"]
    b._on_status("backup", _状态(2, ids=[7]))
    b._on_beh(beh)
    assert len([m for m in b.sent if m["type"] == "cmd"]) == 1
    b._on_status("backup", _状态(4, ids=[7]))                # 状态先处理到「做完了」
    b._on_beh(beh)                                            # 后面排着的几条后退照样转
    assert len([m for m in b.sent if m["type"] == "cmd"]) == 2, \
        "状态和速度两路在两个线程里处理:要求「还在跑」会丢合法的后退(终点贴墙 4/5 → 0/3)"


def test_复查五_刚起来_动作状态没同步完之前不派目标_也不报就绪(monkeypatch):
    b, futs = _桥(monkeypatch)
    now = [100.0]
    b._mono = lambda: now[0]
    b.status_sync_until = 102.0
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    assert len(futs) == 0
    now[0] = 102.5
    b._pump()
    assert len(futs) == 1


def test_复查五_某个动作服务没了_它的旧状态清掉_不一直挡着(monkeypatch):
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    gids = {"/follow_path/_action/status": [b"A"]}
    b.node = NS(get_publishers_info_by_topic=lambda t: [NS(endpoint_gid=g)
                                                         for g in gids.get(t, [b"X"])])
    b.reap_gone_producers()
    b._on_status("follow_path", _状态(2))
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    assert len(futs) == 0
    gids["/follow_path/_action/status"] = []
    b.reap_gone_producers()
    assert b.producing["follow_path"] is False and len(futs) == 1


def test_复查五_assisted_teleop也算会出速度的动作():
    from d1max_localizer.nav2bridge import PRODUCERS
    assert "assisted_teleop" in PRODUCERS


def test_复查六_派出以前就收下的目标号_晚到也不算这个目标的(monkeypatch):
    """转接节点刚起来,Nav2 里还有上一次的 BackUp 在跑:它的状态在派出之后才第一次收到,
    号是新见到的,可收下时刻早于派出 —— 不开门。"""
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b.t_ms = 5000
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    futs[0].set_result(_Handle())
    b._mono = lambda: 1e9
    beh = NS(linear=NS(x=-0.15, y=0.0), angular=NS(z=0.0))
    b._on_status("backup", _状态(2, at_ms=1000))             # 老的,早就收下了
    b._on_beh(beh)
    assert not [m for m in b.sent if m["type"] == "cmd"]
    b._on_status("follow_path", _状态(2, at_ms=4700))        # 派出前 0.3 s 收下的:也不算
    assert "ctl" not in b.opened
    b._on_status("backup", _状态(2, at_ms=4900))             # 余量 0.2 s 以内:算(钟差一拍)
    b._on_beh(beh)
    assert len([m for m in b.sent if m["type"] == "cmd"]) == 1


def test_复查六_定时那一拍_同步期过了自己派排着的目标(monkeypatch):
    b, futs = _桥(monkeypatch)
    now = [100.0]
    b._mono = lambda: now[0]
    b.status_sync_until = 102.0
    b.check_ready = lambda: None
    b.node = __import__("types").SimpleNamespace(get_publishers_info_by_topic=lambda t: [])
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    assert len(futs) == 0
    now[0] = 102.5
    b.tick_once()
    assert len(futs) == 1


def test_复查七_动作服务换了实例_新实例报的在跑不清_没发布者了才清(monkeypatch):
    """状态跟发布者身份没绑在一起:发布者换了,「在跑」可能是新实例报的,不能按身份变化清掉
    (复查第七轮 Important)。只有一个发布者都没有了(谁也出不了东西)才清。"""
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    gids = {"/follow_path/_action/status": [b"A"]}
    b.node = NS(get_publishers_info_by_topic=lambda t: [NS(endpoint_gid=g)
                                                         for g in gids.get(t, [b"X"])])
    b.reap_gone_producers()
    gids["/follow_path/_action/status"] = [b"B"]              # 换成 B,B 已经报了在跑
    b._on_status("follow_path", _状态(2))
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    b.reap_gone_producers()
    assert b.producing["follow_path"] is True and len(futs) == 0
    gids["/follow_path/_action/status"] = []
    b.reap_gone_producers()
    assert b.producing["follow_path"] is False and len(futs) == 1




def test_复查七_连上代理先全取消_都回了话才放行第一个目标_服务没起来就之后补发(monkeypatch):
    """上一次留下的导航目标转接节点不认识:连上代理先全取消(空的目标号 = 全部)。取消回话之前不派,
    免得新目标先被收下、又被这个「全部取消」一起取消(复查第七轮)。"""
    from types import SimpleNamespace as NS
    b, futs = _桥(monkeypatch)
    b._cancel_req = lambda: "全部"
    done = {"v": False}
    sent = []
    ready = {"thru": False}
    b.cancel_clients = [
        NS(service_is_ready=lambda: True,
           call_async=lambda r: (sent.append(("nav", r)), NS(done=lambda: done["v"]))[1]),
        NS(service_is_ready=lambda: ready["thru"],
           call_async=lambda r: (sent.append(("thru", r)), NS(done=lambda: True))[1])]
    b.cancel_foreign()
    assert sent == [("nav", "全部")]
    b.on_goal({"goal_id": 5, "x": 1, "y": 2, "yaw": 0})
    assert len(futs) == 0, "取消还没回话:不派"
    b.advance_cleanup()
    assert len(futs) == 0
    done["v"] = True                                           # 第一个回话了
    ready["thru"] = True                                       # 第二个服务这才起来
    b.advance_cleanup()                                        # 补发第二个,它当场就回了
    assert sent == [("nav", "全部"), ("thru", "全部")]
    assert not b.cleanup_pending and len(futs) == 1
