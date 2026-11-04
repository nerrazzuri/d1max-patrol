"""W73 第四期:三条电量线(设计 §7.2)。"""

from __future__ import annotations

import pytest

from nexwar_site import energy as e


def test_回撤线_下限25_离得远就提前走():
    assert e.return_line(0) == 25.0
    assert e.return_line(3600) == pytest.approx(15 + 22.3 * 1.5)


def test_布防一圈的出站线_回撤线加一圈耗电乘1点2():
    # 15 分钟一圈 ≈ 33.6%(设计算的约 33)
    assert e.lap_line(900) == pytest.approx(25 + 22.3 / 4 * 1.2)


def test_报警出动_去加看5分钟加回来_后还在停止线以上():
    assert e.alarm_line(600) == pytest.approx(15 + 22.3 * (1200 * 1.5 + 300) / 3600)
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
    from nexwar_contract.charging import ALARM_FLOOR_PCT, ALARM_PRIORITY
    from nexwar_site.priorities import EVENT
    assert ALARM_PRIORITY == EVENT and ALARM_FLOOR_PCT == e.STOP_PCT


def test_现场1104_耗电默认跟狗上返航一样_比实测多留余量():
    from nexwar_site import energy
    measured = 0.30 * 60                                 # C40221 站着、走路都约 0.30%/min
    assert energy.DRAIN_PCT_PER_H == 22.3 and energy.DRAIN_PCT_PER_H >= measured * 1.2


@pytest.mark.parametrize(("go_m", "back_m"), [(1.0, 1.0), (0.0, 0.0), (10.0, 30.0), (60.0, 5.0),
                                              (200.0, 200.0)])
def test_外审r2_8_报警出动线_不低于狗上的出发线(go_m, back_m):
    """站点放行的,狗上也接:狗上 = 停止线 + max(3%,全程耗电) × 1.5,全程 = 去 + 回原点。"""
    from nexwar_agent.engine.homing import DEFAULT_RETURN_PARAMS, estimate_cost_pct
    from nexwar_agent.engine.preflight import ESTIMATE_SLACK
    from nexwar_contract.charging import ALARM_FLOOR_PCT
    robot = ALARM_FLOOR_PCT + estimate_cost_pct(go_m + back_m) * ESTIMATE_SLACK
    site = e.alarm_line(e.walk_s(go_m), e.walk_s(back_m), watch_s=0)
    assert site == pytest.approx(robot, abs=1e-6)
    assert e.alarm_line(e.walk_s(go_m), e.walk_s(back_m)) > robot, "还要留停着看的那 5 分钟"
    assert e.COST_FLOOR_PCT == DEFAULT_RETURN_PARAMS.floor_pct
