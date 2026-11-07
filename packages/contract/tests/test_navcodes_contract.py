"""导航 V1 第 5 节:失败原因码和过程事件分开(docs/导航V1-设计.md)。"""
from __future__ import annotations

import pytest

from nexwar_contract import navcodes

V1_CODES = {"route_too_narrow", "goal_too_close_to_wall", "goal_occupied", "goal_unreachable",
            "dynamic_obstacle_blocked", "robot_trapped", "controller_stalled",
            "localization_unreliable", "sensor_lost", "nav2_plan_failed",
            # 开工时补的两个:急停 / 没控制权 / 故障 / 头尾不明;Nav2 没起来或掉了(处理方法都不一样)
            "robot_not_ready", "nav_unavailable"}


def test_失败码就是设计里那些():
    assert set(navcodes.FAIL_CODES) == V1_CODES


def test_每个码都有中英文说明和操作员该做什么():
    for code, info in navcodes.FAIL_CODES.items():
        assert info.zh and info.en and info.action_zh and info.action_en, code


def test_守卫紧急停是过程事件_不是失败码():
    assert "guard_emergency_stop" in navcodes.EVENTS
    assert "guard_emergency_stop" not in navcodes.FAIL_CODES


@pytest.mark.parametrize("raw, want", [
    ("goal_occupied", "goal_occupied"),
    ("GOAL_OCCUPIED", None),
    ("", None),
    (None, None),
    (3, None),
    ("guard_emergency_stop", None),
])
def test_收到的码只认表里的(raw, want):
    assert navcodes.fail_code(raw) == want
