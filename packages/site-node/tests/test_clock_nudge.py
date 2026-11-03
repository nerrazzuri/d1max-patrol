"""W70(用户 11-03 现场,选「代理向站点对时」):站点在笔记本上、机器人经隧道接进来、上不了外网,
机器人的钟慢了 28 s,站点拒发所有命令。钟差过线的,站点自己发带站点时刻的对时命令。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from d1max_site.clockskew import NUDGE_EVERY_MS, ClockNudger


class 假派遣:
    def __init__(self):
        self.sent = []
        self.skew = {"A": -28.0, "B": 0.3, "C": -40.0, "D": -35.0}
        self.clients = {
            "A": SimpleNamespace(capabilities=SimpleNamespace(
                tasks={"ops_timesync": {"site_time": True}})),
            "B": SimpleNamespace(capabilities=SimpleNamespace(
                tasks={"ops_timesync": {"site_time": True}})),
            "C": SimpleNamespace(capabilities=SimpleNamespace(tasks={"ops_timesync": {}})),
            "D": SimpleNamespace(capabilities=None)}

    def clock_skew_s(self, rid):
        return self.skew.get(rid)

    async def map_command(self, rid, kind, payload, *, issued_by, ttl_ms=None):
        self.sent.append((rid, kind, payload, issued_by))
        return {"ack": {"result": "accepted"}}


def test_W70_钟差过线_报了能按站点时刻对时的才发_一分钟最多一次():
    t = [1_000_000]
    d = 假派遣()
    n = ClockNudger(d, now_ms=lambda: t[0])
    asyncio.run(n.tick())
    assert [(r, k, p) for r, k, p, _ in d.sent] == [("A", "ops_timesync", {"site_ms": 1_000_000})]
    assert d.sent[0][3] == "site:clock"
    t[0] += NUDGE_EVERY_MS - 1
    asyncio.run(n.tick())
    assert len(d.sent) == 1
    t[0] += 1
    asyncio.run(n.tick())
    assert len(d.sent) == 2


def test_W70_发不出去_不炸_下一拍照样试别的():
    d = 假派遣()
    d.skew["B"] = -50.0

    async def boom(rid, *a, **k):
        if rid == "A":
            raise RuntimeError("断了")
        d.sent.append(rid)
    d.map_command = boom
    asyncio.run(ClockNudger(d, now_ms=lambda: 5).tick())
    assert d.sent == ["B"]
