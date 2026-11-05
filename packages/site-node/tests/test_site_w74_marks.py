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


def test_外审r1_12_走到线上停住或折返_不算越线_过线够远才算(标):
    标.set("m", "1", {"kind": "exit", "name": "门", "points": [[-1, 0], [1, 0]]}, by="g")
    assert 标.crossed("m", "1", (0, 1), (0, 0)) is None, "走到线上"
    assert 标.crossed("m", "1", (0, 0), (0, 1)) is None, "从线上回来"
    assert 标.crossed("m", "1", (0, 1), (0, -0.1)) is None, "过线一点点(定位误差以内)"
    assert 标.crossed("m", "1", (0, 1), (0, -0.5)) is not None


def test_外审r2_7_每条线记最后确定在哪一边_从线的延长线附近绕过来照样算越线(标):
    标.set("m", "1", {"kind": "exit", "name": "门", "points": [[-1, 0], [1, 0]]}, by="g")
    last: dict = {}
    seq = [(1.4, 0.8), (1.4, 0.2), (-1.0, -0.4)]       # 中间那点离线段远、离直线近
    hits = [标.track("m", "1", last, p) for p in seq]
    assert hits[:2] == [None, None]
    assert hits[2] is not None and hits[2][0]["name"] == "门", hits
    assert hits[2][1] == (1.4, 0.8), "越线那一段从最后一个确定在哪一边的点算起"
    last = {}
    assert [标.track("m", "1", last, p) for p in [(1.5, 1.0), (1.5, -1.0)]] == [None, None], \
        "从线段端点外面绕过去:不算"
    last = {}
    assert [标.track("m", "1", last, p) for p in [(0, 1), (0, 0.1), (0, 0), (0, 1)]] \
        == [None] * 4, "走到线上停住、折返"
    assert 标.track("m", "1", last, (0, -0.1)) is None, "过线一点点"
    assert 标.track("m", "1", last, (0, -0.5)) is not None


def test_外审r3_3_误差带里的点也算轨迹_按实际走的折线判越线(标):
    标.set("m", "1", {"kind": "exit", "name": "门", "points": [[-1, 0], [1, 0]]}, by="g")
    last: dict = {}
    hits = [标.track("m", "1", last, p) for p in [(1.4, 0.8), (0.6, 0.2), (0.6, -0.2),
                                                  (1.4, -0.8)]]
    assert hits[:3] == [None] * 3 and hits[3] is not None, "在 (0.6, 0) 穿过了门:算越线"
    last = {}
    assert [标.track("m", "1", last, p) for p in [(0, 0.8), (1.5, 0.2), (1.5, -0.2),
                                                  (0, -0.8)]] == [None] * 4, \
        "从门的端点外面绕过去(两头连起来的直线虽然穿过门):不算"


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
