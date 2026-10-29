"""W46(决策 74):上装主控板过热 → 站点告警。警戒、降负载给业主(P2);危急出 P1(机器人自己回家)。"""

from __future__ import annotations

T0 = 1_793_000_000_000


def _src(tmp_path):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    desk = AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: T0)
    return desk, SiteAlertSources(desk, now_ms=lambda: T0)


def _ev(kind, data):
    from d1max_contract.messages import Event
    return Event(event_id=f"{kind}-{data.get('level')}", kind=kind, stamp=T0, data=data,
                 boot_id="b", seq=1)


def test_过热三级出告警_回到正常不出(tmp_path):
    from d1max_site.alert_audience import audience
    desk, src = _src(tmp_path)
    src.on_event("A", _ev("board_thermal", {"level": "ok", "reason": "SoC 50 °C"}))
    assert desk.open() == []
    src.on_event("A", _ev("board_thermal", {"level": "warn", "reason": "SoC 81 °C",
                                            "fans_stopped": ["stir"]}))
    got = {a["kind"]: a for a in desk.open()}
    assert got["board_hot"]["level"] == "P2" and "SoC 81" in got["board_hot"]["detail"]
    assert "stir" in got["board_hot"]["detail"]
    src.on_event("A", _ev("board_thermal", {"level": "shed", "reason": "SoC 86 °C"}))
    got = {a["kind"]: a for a in desk.open()}
    assert "降负载" in got["board_hot"]["title"]
    src.on_event("A", _ev("board_thermal", {"level": "critical", "reason": "SoC 96 °C"}))
    got = {a["kind"]: a for a in desk.open()}
    assert got["board_overheat"]["level"] == "P1" and "回" in got["board_overheat"]["title"]
    assert audience("board_hot") == "owner" and audience("board_overheat") == "help"
    src.on_event("A", _ev("board_thermal", {"level": "ok", "reason": "SoC 60 °C"}))
    assert {a["kind"] for a in desk.open()} == {"board_hot", "board_overheat"}, "人去看过再关"


def test_热像镜头被挡_出P2给业主(tmp_path):
    from d1max_site.alert_audience import audience
    desk, src = _src(tmp_path)
    src.on_event("A", _ev("thermal_lens_blocked", {"camera": "thermal", "detail": 1.2,
                                                   "baseline": 9.8}))
    got = {a["kind"]: a for a in desk.open()}
    assert got["lens_blocked"]["level"] == "P2" and "擦镜头" in got["lens_blocked"]["detail"]
    assert audience("lens_blocked") == "owner"
