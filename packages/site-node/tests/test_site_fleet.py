"""多台狗(W28,决策 46):各管各的区域、**错开充**;排程挑狗、入侵派遣低电排后。

错开充的规矩在 :class:`ChargeDesk` 上,这里用假派遣器(几台狗、电量手设)看它怎么起回充;一台狗走完
整轮的真代理流程在 ``test_site_charging.py``。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_site_charging import 假告警台

from d1max_contract.charging import FLOOR_PCT, TRIP_ABORT_PCT
from d1max_site.charging import ChargeDesk
from d1max_site.db import SiteDB


class 假派遣器:
    def __init__(self, pcts):
        self.clients = {rid: SimpleNamespace(
            status=SimpleNamespace(online=True, task=None),
            telemetry=SimpleNamespace(battery_pct=p),
            capabilities=SimpleNamespace(tasks={"dock": {"mode": "vendor_dock"}}))
            for rid, p in pcts.items()}
        self.sent = []
        self.reject = False

    def autonomy(self, rid):
        return "autonomous"

    def pct(self, rid, p):
        self.clients[rid].telemetry.battery_pct = p

    async def goto(self, rid, target, speed, *, issued_by, priority, task_id, charge):
        self.sent.append((rid, "goto", task_id))
        return {"ack": {"result": "rejected" if self.reject else "accepted"}}

    async def dock(self, rid, tid, req, *, issued_by):
        self.sent.append((rid, "dock", tid))
        return {"ack": {"result": "accepted"}}


@pytest.fixture
def 台(tmp_path):
    ms = [1_800_000_000_000]
    db = SiteDB(tmp_path / "s.db")
    d = 假派遣器({"A": 60.0, "B": 60.0, "C": 60.0})
    desk = ChargeDesk(db, d, now_ms=lambda: ms[0])
    desk.alerts = 假告警台()
    for i, rid in enumerate("ABC"):
        desk.set_charger(rid, map_id="m", map_version="1", x=float(i), y=0.0, yaw=0.0,
                         by="alice")
    return SimpleNamespace(db=db, d=d, desk=desk, ms=ms)


def _去充的(t):
    return [r for r, k, _ in t.d.sent if k == "goto"]


async def test_一台在充_另一台到线先接着守_守到20也去_报P2一次(台):
    t = 台
    t.d.pct("A", 25.0)
    await t.desk.tick()
    assert _去充的(t) == ["A"]
    t.d.pct("B", 29.0)
    await t.desk.tick()
    assert _去充的(t) == ["A"], "A 在充:B 先接着守"
    assert t.desk.view()["held"] == [{"robot_id": "B", "waiting_for": "A"}]
    t.d.pct("B", 20.5)
    await t.desk.tick()
    assert _去充的(t) == ["A"]
    t.d.pct("B", FLOOR_PCT)
    await t.desk.tick()
    assert _去充的(t) == ["A", "B"], "守到 20%:也去"
    [a] = t.desk.alerts.raised
    assert a["kind"] == "charge_overlap" and a["robot"] == "B" and "A 还在充" in a["detail"]
    assert t.desk.view()["held"] == []
    await t.desk.tick()
    assert len(t.desk.alerts.raised) == 1


async def test_在充的那台充完了_守着的那台马上去_不报(台):
    t = 台
    t.d.pct("A", 25.0)
    await t.desk.tick()
    t.d.pct("B", 28.0)
    await t.desk.tick()
    assert t.desk.view()["held"]
    t.desk._set("A", None)                                # A 这一轮完了、充到 90%
    t.d.pct("A", 90.0)
    await t.desk.tick()
    assert _去充的(t) == ["A", "B"] and t.desk.alerts.raised == []


async def test_歇着的不算在充(台):
    t = 台
    t.d.pct("A", 25.0)
    await t.desk.tick()
    t.desk._set("A", "cooldown", until_ms=t.ms[0] + 600_000)    # A 没充成、歇着
    t.d.pct("B", 28.0)
    await t.desk.tick()
    assert _去充的(t) == ["A", "B"]


async def test_被打断待接着充的算在充(台):
    t = 台
    t.d.pct("A", 25.0)
    await t.desk.tick()
    t.desk._set("A", "resume")                            # 入侵抢走了,回来接着充
    t.d.clients["A"].status.task = SimpleNamespace(task_id="incident-1")
    t.d.pct("B", 28.0)
    await t.desk.tick()
    assert _去充的(t) == ["A"]


async def test_几台同时到线_电量低的先去(台):
    t = 台
    t.d.pct("A", 29.0)
    t.d.pct("B", 26.0)
    t.d.pct("C", 28.0)
    await t.desk.tick()
    assert _去充的(t) == ["B"]
    assert {h["robot_id"] for h in t.desk.view()["held"]} == {"A", "C"}


async def test_只有一台登记了桩_到线就去(台):
    t = 台
    t.desk.remove_charger("B")
    t.desk.remove_charger("C")
    t.d.pct("A", 29.0)
    await t.desk.tick()
    assert _去充的(t) == ["A"]


async def test_守到20去充_被拒_30秒后再派_不重复报(台):
    t = 台
    t.d.pct("A", 25.0)
    await t.desk.tick()
    t.d.pct("B", 19.0)
    t.d.reject = True
    await t.desk.tick()
    assert _去充的(t) == ["A", "B"] and len(t.desk.alerts.raised) == 1
    t.desk.alerts.raised.clear()                          # 保安已经处理掉了那一条
    t.ms[0] += 31_000
    t.d.reject = False
    await t.desk.tick()
    assert _去充的(t) == ["A", "B", "B"] and t.desk.alerts.raised == [], "同一回低电只报一次"
    t.desk._set("B", None)                                # B 充完了
    t.d.pct("B", 90.0)
    await t.desk.tick()
    t.d.pct("B", 19.0)                                    # 下一回低电、A 还在充:再报
    await t.desk.tick()
    assert [x["kind"] for x in t.desk.alerts.raised] == ["charge_overlap"]


def test_去桩那一趟的中止线在守的线下面():
    from d1max_contract.charging import LOW_PCT
    assert TRIP_ABORT_PCT < FLOOR_PCT < LOW_PCT



async def test_外审_报过P2_保安处理了_站点重启_同一回低电不再报(台):
    t = 台
    t.d.pct("A", 25.0)
    await t.desk.tick()
    t.d.pct("B", 19.0)
    t.d.reject = True
    await t.desk.tick()
    assert len(t.desk.alerts.raised) == 1
    t.desk = ChargeDesk(t.db, t.d, now_ms=lambda: t.ms[0])    # 站点重启
    t.desk.alerts = 假告警台()                                  # 那一条保安处理掉了
    t.ms[0] += 31_000
    t.d.reject = False
    await t.desk.tick()
    assert _去充的(t) == ["A", "B", "B"] and t.desk.alerts.raised == []
