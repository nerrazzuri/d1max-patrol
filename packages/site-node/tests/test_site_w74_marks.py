"""W74 三期:地图上的出口线、易藏点(设计 §六、§七)。存取、校验、越线判断、附近易藏点按去向排。"""

from __future__ import annotations

import math

import pytest

from nexwar_site.db import SiteDB
from nexwar_site.map_marks import MapMarks, MarkError, inside_pose


@pytest.fixture
def 标(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    yield MapMarks(db, now_ms=lambda: 1000)
    db.close()


def test_存取_改_删_各图各版分开(标):
    a = 标.set("m", "1", {"kind": "exit", "name": "东门", "points": [[10, 0], [10, 4]]}, by="gina")
    h = 标.set("m", "1", {"kind": "hide", "name": "楼梯间", "points": [[3, 3]], "yaw": 1.5},
              by="gina")
    assert a["id"] and h["id"] and a["id"] != h["id"]
    got = {m["name"]: m for m in 标.list("m", "1")}
    assert got["东门"]["kind"] == "exit" and got["东门"]["points"] == [[10.0, 0.0], [10.0, 4.0]]
    assert got["楼梯间"]["yaw"] == 1.5
    assert 标.list("m", "2") == []
    标.set("m", "1", {"id": a["id"], "kind": "exit", "name": "东大门",
                     "points": [[10, 0], [10, 5]]}, by="gina")
    assert {m["name"] for m in 标.list("m", "1")} == {"东大门", "楼梯间"}
    assert 标.remove("m", "1", h["id"]) and not 标.remove("m", "1", h["id"])
    assert [m["name"] for m in 标.list("m", "1")] == ["东大门"]


@pytest.mark.parametrize("bad", [
    {"kind": "door", "name": "x", "points": [[0, 0]]},
    {"kind": "exit", "name": "x", "points": [[0, 0]]},
    {"kind": "exit", "name": "x", "points": [[0, 0], [0, 0.2]]},           # 太短
    {"kind": "exit", "name": "x", "points": [[0, 0], [0, 100]]},           # 太长
    {"kind": "hide", "name": "x", "points": [[0, 0], [1, 1]]},
    {"kind": "hide", "name": "", "points": [[0, 0]]},
    {"kind": "hide", "name": "x" * 41, "points": [[0, 0]]},
    {"kind": "hide", "name": "x", "points": [[0, float("nan")]]},
    {"kind": "hide", "name": "x", "points": [[0, 0]], "yaw": "北"},
    {"kind": "hide", "name": "x", "points": [[0, 0]], "id": "不存在"},
    [],
])
def test_不成形的拒(标, bad):
    with pytest.raises(MarkError):
        标.set("m", "1", bad, by="gina")


def test_越过出口线_才算_两头都在一边不算(标):
    标.set("m", "1", {"kind": "exit", "name": "东门", "points": [[10, 0], [10, 4]]}, by="g")
    assert 标.crossed("m", "1", (9, 2), (9.8, 2)) is None
    got = 标.crossed("m", "1", (9, 2), (11, 2.5))
    assert got is not None and got["name"] == "东门"
    assert 标.crossed("m", "1", (9, 5), (11, 5)) is None, "从线头外面绕过去不算"
    assert 标.crossed("m", "2", (9, 2), (11, 2)) is None


def test_出口线内3米_朝出口(标):
    m = 标.set("m", "1", {"kind": "exit", "name": "东门", "points": [[10, 0], [10, 4]]}, by="g")
    x, y, yaw = inside_pose(m, (9, 2), (11, 2))
    assert abs(x - 7.0) < 1e-6 and abs(y - 2.0) < 1e-6, "人从西边来:站线西边 3 m"
    assert abs(yaw - 0.0) < 1e-6, "朝东看出口"
    x, y, yaw = inside_pose(m, (12, 1), (8, 1))
    assert abs(x - 13.0) < 1e-6 and abs(abs(yaw) - math.pi) < 1e-6, "从东边来:站东边、朝西"


def test_附近易藏点_去向那边的先看_30米以外不看(标):
    for n, p in (("北1", [0, 5]), ("北2", [0, 12]), ("南", [0, -3]), ("远", [0, 40])):
        标.set("m", "1", {"kind": "hide", "name": n, "points": [p]}, by="g")
    标.set("m", "1", {"kind": "exit", "name": "门", "points": [[5, 0], [5, 2]]}, by="g")
    assert [h["name"] for h in 标.hide_near("m", "1", 0, 0, 90.0)] == ["北1", "北2", "南"]
    assert [h["name"] for h in 标.hide_near("m", "1", 0, 0, None)] == ["南", "北1", "北2"]
    assert [h["name"] for h in 标.hide_near("m", "1", 0, 0, -90.0)] == ["南", "北1", "北2"]
