"""W73 第四期:三条电量线(设计 §7.2)。"""

from __future__ import annotations

import pytest

from d1max_site import energy as e


def test_回撤线_下限25_离得远就提前走():
    assert e.return_line(0) == 25.0
    assert e.return_line(3600) == pytest.approx(15 + 100 / 3.5 * 1.5)


def test_布防一圈的出站线_回撤线加一圈耗电乘1点2():
    # 15 分钟一圈 ≈ 33.6%(设计算的约 33)
    assert e.lap_line(900) == pytest.approx(25 + 100 / 3.5 / 4 * 1.2)


def test_报警出动_去加看5分钟加回来_后还在停止线以上():
    assert e.alarm_line(600) == pytest.approx(15 + 100 / 3.5 * (1200 * 1.5 + 300) / 3600)
    assert e.alarm_line(0) < e.lap_line(0), "报警比布防放宽一档"


def test_补到多少_够撑到布防结束_最多95():
    assert e.topup_target(10 * 3600, 900) == 95.0
    assert e.topup_target(0, 900) == pytest.approx(e.lap_line(900) + 10, abs=0.1)


def test_补一次能之前能开几圈():
    n = e.laps_per_charge(900, 900)
    assert n >= 1 and 95 - (n - 1) * e.drain_pct(900) >= e.lap_line(900)
    assert 95 - n * e.drain_pct(900) < e.lap_line(900)
    assert e.laps_per_charge(900, 900, start_pct=20) == 0


def test_外审_狗上报警优先级跟站点事件派遣对得上():
    from d1max_contract.charging import ALARM_FLOOR_PCT, ALARM_PRIORITY
    from d1max_site.priorities import EVENT
    assert ALARM_PRIORITY == EVENT and ALARM_FLOOR_PCT == e.STOP_PCT
