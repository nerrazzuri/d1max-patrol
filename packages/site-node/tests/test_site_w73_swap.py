"""W73 第四期:没有充电桩时保安换电(设计 §7.4)—— 站点按两块电池的在位、电量自动判进度。"""

from __future__ import annotations

from types import SimpleNamespace

from d1max_site.battery_swap import SwapDesk


class 告警:
    def __init__(self):
        self.raised: list[dict] = []
        self.resolved: list[tuple[str, str]] = []

    def raise_alert(self, **kw):
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

    def tick(self, secs=1):
        self.now += secs * 1000
        self.desk.tick()

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
