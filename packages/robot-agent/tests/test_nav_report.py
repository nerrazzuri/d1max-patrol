"""运营可视化:规划路径上报(抽稀、点数上限、字节上限、限频、只在导航时报)和规划后端的
``nav_replan`` / ``nav_blocked.waited_s`` 事件。"""

from __future__ import annotations

import json
import math

from test_obstacle_avoid import 台子

from d1max_agent.nav_report import (
    MAX_POINTS,
    MIN_GAP_MS,
    REFRESH_MS,
    NavPathReporter,
    decimate,
)
from d1max_contract.messages import Telemetry
from d1max_patrol.protocol.nav_types import NavStatus, Pose


def _line(n, step=0.05):
    return [(i * step, 0.0) for i in range(n)]


def _wiggle(n):
    """每一步都拐一下的折线(抽稀去不掉):点数会超上限。"""
    return [(i * 0.5, 0.5 * (i % 2)) for i in range(n)]


def test_抽稀_直线只剩头尾_头尾一定在():
    got = decimate(_line(2000))
    assert got == [[0.0, 0.0], [round(1999 * 0.05, 2), 0.0]]


def test_点数有上限_头尾都在_字节有上限():
    got = decimate(_wiggle(5000))
    assert len(got) == MAX_POINTS
    assert got[0] == [0.0, 0.0] and got[-1] == [round(4999 * 0.5, 2), 0.5 * (4999 % 2)]
    assert len(json.dumps({"seq": 1, "path": got})) < 1500


def test_坐标取两位小数_非有限数的点丢掉():
    got = decimate([(0.0, 0.0), (math.nan, 1.0), (1.23456, 0.0), (2.0, math.inf)])
    assert got == [[0.0, 0.0], [1.23, 0.0]]
    assert decimate([(0.0, 0.0)]) == []


def test_限频_变了的路径两份之间至少隔2秒_期间只带序号():
    r = NavPathReporter()
    a, b = _wiggle(10), _wiggle(12)
    first = r.wire(a, 0)
    assert first["seq"] == 1 and first["path"]
    assert r.wire(a, 1000) == {"seq": 1}                  # 没变:不重发
    assert r.wire(b, 1000) == {"seq": 1}                  # 变了,但没到 2 秒:先不发
    sent = r.wire(b, MIN_GAP_MS)
    assert sent["seq"] == 2 and sent["path"]
    assert r.wire(b, MIN_GAP_MS + 1000) == {"seq": 2}


def test_没变也定时重发_遥测会丢_站点重启后靠它补上():
    r = NavPathReporter()
    p = _wiggle(10)
    r.wire(p, 0)
    assert "path" not in r.wire(p, REFRESH_MS - 1)
    again = r.wire(p, REFRESH_MS)
    assert again["seq"] == 1 and again["path"]            # 内容没变,序号不涨


def test_没路径就不带_导航一结束站点就清_下一趟马上发():
    r = NavPathReporter()
    assert r.wire(None, 0) is None and r.wire([], 0) is None
    r.wire(_wiggle(10), 0)
    assert r.wire(None, 500) is None                      # 收尾
    nxt = r.wire(_wiggle(10), 600)                        # 下一趟:不受上一趟的限频
    assert nxt is not None and "path" in nxt


def test_遥测契约_nav可选_老的没有这个键():
    from d1max_contract.messages import _stamped
    base = {"stamp": 1, "pose": None, "battery_pct": 50.0, "task_state": None,
            "loc_quality": 1.0, "net": {}}
    t = Telemetry.from_wire(_stamped(dict(base)))
    assert t.nav is None and "nav" not in t.to_wire()
    t2 = Telemetry.from_wire(_stamped(dict(base) | {"nav": {"seq": 3, "path": [[0, 0], [1, 1]]}}))
    assert t2.nav == {"seq": 3, "path": [[0, 0], [1, 1]]} and t2.to_wire()["nav"]["seq"] == 3
    junk = Telemetry.from_wire(_stamped(dict(base) | {"nav": "xx"}))
    assert junk.nav is None


async def test_规划后端_走的时候给路径_收尾清掉(tmp_path):
    t = 台子(tmp_path)
    await t.start(1.5, 4.0)
    assert t.nav.current_path() is None
    await t.nav.goto(Pose.from_xy_yaw(10.0, 4.0))
    pts = t.nav.current_path()
    assert pts is not None and len(pts) >= 2
    assert math.dist(pts[0], (1.5, 4.0)) < 0.5 and math.dist(pts[-1], (10.0, 4.0)) < 0.5
    await t.跑(60.0)
    assert NavStatus.SUCCEED in t.status
    assert t.nav.current_path() is None


async def test_被挡的事件带等了多久_绕障重规划带原因(tmp_path):
    t = 台子(tmp_path, walls=[(5.8, 0.0, 6.2, 5.0)])
    await t.start(1.5, 4.0)
    await t.nav.goto(Pose.from_xy_yaw(10.0, 2.0))
    await t.跑(10.0)
    t.world.dyn["gate"] = (5.2, 5.0, 6.8, 7.95)
    await t.跑(60.0)
    blocked = [d for k, d in t.events if k == "nav_blocked"]
    # 工单 21 之后狗会换方向试几回,每回真走了一段算新的一回被挡
    assert 1 <= len(blocked) <= 3 and blocked[0]["waited_s"] >= 5.0
    assert blocked[0]["reason"] and "x" in blocked[0] and "y" in blocked[0]
    replans = [d for k, d in t.events if k == "nav_replan"]
    assert replans and all(d["why"] in ("off_route", "zones", "near_nogo", "detour")
                           for d in replans)
    assert any(d["why"] == "detour" for d in replans)
