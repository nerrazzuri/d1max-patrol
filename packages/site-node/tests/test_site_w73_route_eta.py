"""W73 第一期:巡逻路线每个点的预计时间(设计 §6.2)。台子同 ``test_site_w73_guard_plan``。"""

# ruff: noqa: F811 —— 夹具「台」从别的测试文件导入,测试参数同名是 pytest 的用法
from __future__ import annotations

import json
import math

import pytest
from test_site_w73_guard_plan import 台  # noqa: F401 —— 夹具

from d1max_site.route_eta import (
    NOMINAL_SPEED_MPS,
    PHOTO_S,
    TURN_RAD_S,
    estimate,
)


def _route(pts, rid="r1"):
    return {"id": rid, "map_id": "shop", "map_version": "1", "waypoints": pts}


def _p(name, x, y, yaw=0.0, actions=None):
    w = {"name": name, "x": x, "y": y, "yaw": yaw}
    if actions:
        w["actions"] = actions
    return w


def test_没跑过_按规划路径长度和速度估_加转向和动作(台):
    r = _route([_p("A", 2.0, 2.0, 0.0), _p("B", 6.0, 2.0, 0.0,
                                           [{"type": "dwell", "seconds": 5},
                                            {"type": "photo", "camera": "front"}])])
    got = estimate(台.db, 台.reach, r)
    a, b = got["waypoints"]
    assert a["planned_s"] == 0.0 and a["arrive_s"] == 0.0, "第一个点:不知道从哪儿出发,只算动作"
    assert b["travel_m"] == pytest.approx(4.0, abs=0.3)
    assert b["planned_s"] == pytest.approx(b["travel_m"] / NOMINAL_SPEED_MPS + 5 + PHOTO_S, abs=0.2)
    assert got["total_s"] == b["arrive_s"] and got["basis"] == "planned"


def test_隔着墙_按绕门的路算_不按直线(台):
    r = _route([_p("东", 8.0, 1.0), _p("西", 12.0, 1.0)])
    b = estimate(台.db, 台.reach, r)["waypoints"][1]
    assert b["travel_m"] > 4.0 + 4.0, "要绕到 y=5 的门再回来"


def test_转身要时间(台):
    straight = _route([_p("A", 2.0, 2.0, 0.0), _p("B", 6.0, 2.0, 0.0)])
    back = _route([_p("A", 2.0, 2.0, 0.0), _p("B", 6.0, 2.0, math.pi)])
    s = estimate(台.db, 台.reach, straight)["waypoints"][1]["planned_s"]
    t = estimate(台.db, 台.reach, back)["waypoints"][1]["planned_s"]
    assert t - s == pytest.approx(math.pi / TURN_RAD_S, abs=0.2)


def test_没接规划_按直线_说明是直线估的(台):
    got = estimate(台.db, None, _route([_p("A", 2.0, 2.0), _p("B", 6.0, 5.0)]))
    assert got["basis"] == "straight"
    assert got["waypoints"][1]["travel_m"] == pytest.approx(5.0)


def _跑过(台, rid, task, stamps, names=("A", "B"), ok=True):
    台.db.query("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                "issued_at, ack_result) VALUES (?,?,?,?,?,?,?,?)",
                (f"c-{task}", task, "A", "patrol", json.dumps({"mission": {"mission": rid}}),
                 "site", stamps[0], "accepted"))
    for i, (n, t) in enumerate(zip(names, stamps, strict=True)):
        台.db.query("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                    "received_at) VALUES ('A', 'b', ?, ?, 'patrol_waypoint', ?, ?, ?)",
                    (hash((task, i)) & 0xFFFFFF, f"e-{task}-{i}",
                     json.dumps({"task_id": task, "name": n, "ok": ok, "index": i}), t, t))


def test_跑过_用最近5次实际用时的中位数(台):
    r = _route([_p("A", 2.0, 2.0), _p("B", 6.0, 2.0)])
    for k, secs in enumerate([100, 30, 31, 32, 33, 34]):           # 最老那次 100 s 不算
        t0 = 1_000_000 * (k + 1)
        _跑过(台, "r1", f"t{k}", [t0, t0 + secs * 1000])
    b = estimate(台.db, 台.reach, r)["waypoints"][1]
    assert (b["actual_s"], b["samples"], b["use_s"]) == (32.0, 5, 32.0)
    assert b["planned_s"] != b["actual_s"]


def test_实际用时_没到的点不算_别的路线不算_挨着的时间戳不算(台):
    r = _route([_p("A", 2.0, 2.0), _p("B", 6.0, 2.0)])
    _跑过(台, "r1", "bad", [1000, 50_000], ok=False)
    _跑过(台, "other", "o", [1000, 20_000])
    _跑过(台, "r1", "batch", [5000, 5100])
    b = estimate(台.db, 台.reach, r)["waypoints"][1]
    assert b["actual_s"] is None and b["samples"] == 0
