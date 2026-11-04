"""W73 第四期:没有充电桩时保安换电(设计 §7.4)—— 站点按两块电池的在位、电量自动判进度。"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from nexwar_site.alert_store import AlertDesk
from nexwar_site.battery_swap import SwapDesk
from nexwar_site.db import SiteDB


class 告警:
    def __init__(self):
        self.raised: list[dict] = []
        self.resolved: list[tuple[str, str]] = []

    def raise_alert(self, **kw):
        # 跟站点真的告警台同一个签名(11-04 现场:多传了 now_ms,真站点上换电告警一条都没发出去)
        inspect.signature(AlertDesk.raise_alert).bind(None, **kw)
        self.raised.append(kw)

    def has_open(self, robot, kind):
        return any(a["robot"] == robot and a["kind"] == kind for a in self.raised) and \
            (robot, kind) not in self.resolved

    def resolve_all(self, robot, kind, *, who):
        self.resolved.append((robot, kind))
        return 1


class _客户端:
    def __init__(self, t):
        self.t = t

    @property
    def telemetry(self):
        t = self.t
        present = [p for p in t.packs or [] if p is not None]
        return SimpleNamespace(battery_pct=min(present) if present else t.pct,
                               battery_packs=t.packs,
                               pose=SimpleNamespace(map_id="m", map_version="1",
                                                    x=t.pose[0], y=t.pose[1]))

    @property
    def status(self):
        return SimpleNamespace(online=True,
                               task=SimpleNamespace(task_id=self.t.task) if self.t.task else None)


class 台:
    def __init__(self):
        self.now = 1_800_000_000_000
        self.pct, self.packs, self.task, self.pose = 40.0, [30.0, 60.0], None, (0.0, 0.0)
        self.need: float | None = None
        self.eta: float | None = None
        self.charger = False
        self.client = _客户端(self)
        self.d = SimpleNamespace(clients={"A": self.client})
        self.alerts = 告警()
        self.desk = SwapDesk(self.d, now_ms=lambda: self.now, need=lambda r: self.need,
                             eta_min=lambda r: self.eta, has_charger=lambda r: self.charger,
                             home=lambda r, m, v: {"x": 0.0, "y": 0.0})
        self.desk.alerts = self.alerts
        self.sent_home: list[int] = []
        self.sent_kw: list[dict] = []
        tt = self

        class _待命点:
            async def return_to(self, rid, *, issued_by, charge=False):
                tt.sent_home.append(tt.now)
                tt.sent_kw.append({"issued_by": issued_by, "charge": charge})
                return {"ack": {"result": "accepted"}}
        self.desk.standby = _待命点()
        # 外审 r5 #1:叫人过来之前要连着听得见够久 —— 测试开始前机器人已经在线一阵了
        self.tick(0)
        self.tick(10)

    def tick(self, secs=1):
        self.now += secs * 1000
        asyncio.run(self.desk.tick())

    def kinds(self):
        return [a["kind"] for a in self.alerts.raised]


def test_快要补能前15分钟_提醒准备两块满电电池_P2_只一次():
    t = 台()
    t.eta = 30
    t.tick()
    assert t.kinds() == []
    t.eta = 14
    t.tick()
    t.tick()
    assert t.kinds() == ["swap_soon"]
    assert "两块满电电池" in t.alerts.raised[0]["detail"]


def test_有桩的不走换电():
    t = 台()
    t.charger, t.need, t.eta = True, 35.0, 5
    t.tick()
    assert t.kinds() == [] and t.desk.view()["robots"] == []


def test_一块一块换_站点按电池自己判进度():
    t = 台()
    t.need = 35.0
    t.task = "standby-x"                                 # 还在回换电位的路上
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "going"
    t.task = None                                        # 到了,空着
    t.tick()
    v = t.desk.view()["robots"][0]
    assert v["step"] == "first" and v["first"] == 1 and v["second"] == 2
    assert "swap_needed" in t.kinds()
    t.packs = [None, 60.0]                               # 1 号拔了
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "first_out"
    assert "正在换电池" in t.desk.refuse("A"), "这一步不打断"
    t.packs = [50.0, 60.0]                               # 插的不是满电的
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "first_low"
    t.packs = [96.0, 60.0]
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "second"
    assert t.desk.refuse("A") == ""
    t.packs = [96.0, None]
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "second_out"
    t.packs = [96.0, 97.0]
    t.need = None
    t.tick()
    assert t.desk.view()["robots"] == [], "换好了:站点自己确认"
    assert ("A", "swap_needed") in t.alerts.resolved
    assert any(e["step"] == "done" for e in t.desk.events)


def test_保安一直不来_10分钟再推_20分钟推给经理():
    t = 台()
    t.need = 35.0
    t.tick()
    assert t.kinds().count("swap_needed") == 1
    t.tick(9 * 60)
    assert t.kinds().count("swap_needed") == 1
    t.tick(60)
    assert t.kinds().count("swap_needed") == 2
    assert "swap_overdue" not in t.kinds()
    t.tick(10 * 60)
    assert "swap_overdue" in t.kinds()
    t.tick(60)
    assert t.kinds().count("swap_overdue") == 1


def test_老机器人不分块报_只说换电池_电量上来算换好():
    t = 台()
    t.packs, t.pct, t.need = None, 30.0, 35.0
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "any"
    t.pct, t.need = 96.0, None
    t.tick()
    assert t.desk.view()["robots"] == []


def test_换了一块就走了_一直提示2号还没换():
    t = 台()
    t.need = 35.0
    t.tick()
    t.packs = [96.0, 60.0]
    t.tick()
    t.tick(30 * 60)
    assert t.desk.view()["robots"][0]["step"] == "second"


def test_外审_拔着一块时站点重启_先让装回拔着的那块_绝不引导拔剩下那块():
    t = 台()
    t.packs = [None, 20.0]                               # 1 号拔着、2 号是唯一的电;站点刚重启
    assert "1 号电池拔着" in t.desk.busy("A"), "值守、排程不派"
    assert "正在换电池" in t.desk.refuse("A"), "报警也不打断"
    t.tick()
    v = t.desk.view()["robots"][0]
    assert v["step"] == "first_out" and v["first"] == 1
    [a] = t.alerts.raised
    assert "装上一块满电的 1 号" in a["detail"] and "2 号先别动" in a["detail"]
    assert "先换 2 号" not in a["detail"]


def test_外审_拔错了_先换的那块还没好_另一块被拔了_让装回去():
    t = 台()
    t.need = 35.0
    t.tick()
    assert t.desk.view()["robots"][0]["first"] == 1
    t.packs = [30.0, None]
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "wrong_out"
    assert "2 号被拔了:先把它装回去" in t.desk._text(t.desk.s["A"])
    assert t.desk.busy("A") and t.desk.refuse("A")


def test_外审_低电时不在换电位_派它回去_没走成一分钟后再派():
    t = 台()
    t.need, t.pose = 35.0, (6.0, 0.0)                    # 停在路线末端
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "going" and len(t.sent_home) == 1
    t.tick(30)
    assert len(t.sent_home) == 1
    t.tick(31)
    assert len(t.sent_home) == 2, "没走成:一分钟后再派"
    t.task = "standby-1"                                 # 在路上了:不重派
    t.tick(120)
    assert len(t.sent_home) == 2
    t.task, t.pose = None, (0.5, 0.0)
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "first" and "swap_needed" in t.kinds()


def test_外审_换到一半走了_插回不满_每一步没进展都会再推和升级():
    t = 台()
    t.need = 35.0
    t.tick()
    t.packs = [96.0, 60.0]                               # 1 号换好,人走了
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "second"
    t.tick(10 * 60)
    again = [a for a in t.alerts.raised if a["kind"] == "swap_needed"][1:]
    assert len(again) == 1 and "现在换 2 号" in again[0]["detail"]
    t.tick(10 * 60)
    [m] = [a for a in t.alerts.raised if a["kind"] == "swap_overdue"]
    assert "现在换 2 号" in m["detail"]
    t.packs = [96.0, None]
    t.tick()
    t.packs = [96.0, 50.0]                               # 插回的不满:重新计时
    t.tick()
    t.tick(10 * 60)
    assert sum(a["kind"] == "swap_needed" for a in t.alerts.raised) == 3


def test_两块都在_没在换电_不拦():
    t = 台()
    assert t.desk.busy("A") == "" and t.desk.refuse("A") == ""


def test_外审r2_回换电位的路上拔了一块_不再派它走_原地引导装回():
    t = 台()
    t.need, t.pose, t.packs = 35.0, (6.0, 0.0), [25.1, 60.0]
    t.tick()
    assert t.desk.view()["robots"][0]["step"] == "going" and len(t.sent_home) == 1
    t.packs = [None, 60.0]                               # 没回到换电位就被拔了一块
    t.tick(61)
    assert len(t.sent_home) == 1, "拔着电池:绝不派它走"
    v = t.desk.view()["robots"][0]
    assert v["step"] == "first_out" and v["first"] == 1
    assert "装上一块满电的 1 号" in t.alerts.raised[-1]["detail"]
    t.tick(120)
    assert len(t.sent_home) == 1


def test_外审r2_回换电位按去补能派_低电也走得了():
    t = 台()
    t.need, t.pose = 35.0, (6.0, 0.0)
    t.tick()
    assert t.sent_kw == [{"issued_by": "site:swap", "charge": True}]


def test_现场1104_接真的告警台_拔着一块_请来装回的告警真发出去(tmp_path):
    t = 台()
    real = AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: t.now)
    t.desk.alerts = real
    t.packs, t.pose = [97.0, None], (6.0, 0.0)          # 空闲、不在换电位时拔了一块(现场 B1)
    t.tick()
    [a] = [x for x in real.open() if x["kind"] == "swap_needed"]
    assert "有电池拔着" in a["title"] and "装上一块满电的 2 号" in a["detail"]
    t.packs = [97.0, 97.0]
    t.tick()
    assert not real.has_open("A", "swap_needed"), "装回去:自动解决"


def test_现场1104_告警没发成_下一拍补发():
    t = 台()
    boom = [True]
    real_raise = t.alerts.raise_alert

    def flaky(**kw):
        if boom[0]:
            boom[0] = False
            raise RuntimeError("告警台一时出错")
        real_raise(**kw)
    t.alerts.raise_alert = flaky
    t.packs = [None, 60.0]
    t.tick()
    assert t.kinds() == []
    t.tick()
    assert t.kinds() == ["swap_needed"], "没告诉成:不能当已经告诉过"
