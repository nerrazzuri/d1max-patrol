"""告警分给谁(W44,设计 §7):保安只看有人闯入、起火或过热、机器人需要帮忙、机器人掉线;

技术类给业主。"""

from __future__ import annotations

import pytest

from d1max_site.alert_audience import AUDIENCE_OF, GUARD_AUDIENCES, audience
from d1max_site.alerts import LEVEL_OF, AlertBook, Level


@pytest.mark.parametrize("kind, want", [
    ("intrusion", "intrusion"), ("intrusion_unanswered", "intrusion"),
    ("intrusion_person", "intrusion"), ("dog_sees_person", "intrusion"),
    ("force_lifted", "intrusion"), ("authorized_person", "intrusion"),
    ("stuck", "help"), ("nav_blocked", "help"), ("fallen", "help"), ("force_flipped", "help"),
    ("deter_cornered", "help"), ("battery_abort", "help"), ("run_abort", "help"),
    ("charge_failed", "help"), ("dock_stuck", "help"), ("loc_lost_paused", "help"),
    ("loc_lost_idle", "help"), ("estop_pressed", "help"), ("nogo", "help"),
    ("payload_siren", "help"), ("payload_strobe", "help"), ("payload_spotlight", "help"),
    ("schedule_missed", "help"), ("battery_swap", "help"),
    ("robot_offline", "offline"), ("robot_offline_idle", "offline"), ("cctv_offline", "offline"),
    ("cert_expiring", "owner"), ("evidence_plain", "owner"), ("site_disk_low", "owner"),
    ("loc_degraded", "owner"), ("watchdog_died", "owner"), ("payload_amp", "owner"),
    ("没见过的种类", "owner"),
])
def test_告警分给谁(kind, want):
    assert audience(kind) == want


def test_每个已知种类都分了():
    """LEVEL_OF 里每个 kind 都在表里明写,不靠兜底(新加 kind 时要想清楚给谁)。"""
    missing = sorted(set(LEVEL_OF) - set(AUDIENCE_OF))
    assert not missing, f"这几种没说给谁: {missing}"
    assert set(AUDIENCE_OF.values()) <= GUARD_AUDIENCES | {"owner"}


def test_保安的四类():
    assert GUARD_AUDIENCES == frozenset({"intrusion", "fire", "help", "offline"})


def test_回包带audience():
    book = AlertBook()
    a = book.raise_alert(level=Level.P1, kind="stuck", robot="A", title="卡住了", now_ms=1000)
    assert a.to_wire()["audience"] == "help"
    b = book.raise_alert(level=Level.P2, kind="cert_expiring", robot="site", title="证书",
                         now_ms=1000)
    assert b.to_wire()["audience"] == "owner"
