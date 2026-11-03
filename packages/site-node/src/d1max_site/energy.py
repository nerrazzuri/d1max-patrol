"""三条电量线(W73 第四期,决策 78 §7.2),站点上所有「够不够电」都从这儿算。

- **停止线** :data:`STOP_PCT`:低于它机器人原地站住(狗上 ``battery_abort_pct``);
- **回撤线** ``max(25%,停止线 + 回补能位耗电 × 1.5)``:结束手上这段去补能;
- **出站线**:按任务算 —— 布防一圈 ``回撤线 + 一圈耗电 × 1.2``;报警出动「去 + 停着看 5 分钟 + 回」
  后还在停止线以上。

耗电按时间算(:data:`DRAIN_PCT_PER_H`,设计 §7.1 的保守默认:厂家满载续航 3.5 h,走路、站着都按
100% / 210 min ≈ 0.48%/min;比狗上返航用的 22.3%/h 保守),真机实测后再分开(设计 §11)。电量按
**两块里低的那块**(厂家硬线按单块判)。
"""

from __future__ import annotations

import math

STOP_PCT = 15.0
RETURN_FLOOR_PCT = 25.0
DRAIN_PCT_PER_H = 100.0 / 3.5
#: 没桩换电一次,各防区没人看多久(保安到场 5 分钟 + 换两块 3 分钟,§7.1 我定的默认)。
SWAP_GAP_MIN = 8
RETURN_FACTOR = 1.5
LAP_FACTOR = 1.2
#: 报警出动:到了停着看这么久。
WATCH_S = 300
#: 有桩时最多充到这么多回来接着值守(不一定等满,§7.3)。
MAX_RESUME_PCT = 95.0
#: 换电:新装上的那块读到这么多才算换好(§7.4)。
SWAP_OK_PCT = 80.0
#: 走直线到点的米数 → 秒:绕路 1.4 倍、0.4 m/s(跟狗上 ``ReturnParams`` 一样)。
_DETOUR, _SPEED = 1.4, 0.4


def drain_pct(seconds: float) -> float:
    return max(0.0, float(seconds)) / 3600.0 * DRAIN_PCT_PER_H


def walk_s(distance_m: float) -> float:
    return max(0.0, float(distance_m)) * _DETOUR / _SPEED


def return_line(back_s: float = 0.0) -> float:
    return max(RETURN_FLOOR_PCT, STOP_PCT + drain_pct(back_s) * RETURN_FACTOR)


def lap_line(lap_s: float, back_s: float = 0.0) -> float:
    """开新的一圈要的电:跑完这一圈 + 回补能位后还在回撤线以上。"""
    return return_line(back_s) + drain_pct(lap_s) * LAP_FACTOR


def alarm_line(go_s: float, back_s: float | None = None, watch_s: float = WATCH_S) -> float:
    """报警出动要的电:去、停着看、回来以后还在停止线以上(比布防放宽一档)。"""
    back = go_s if back_s is None else back_s
    return STOP_PCT + drain_pct(go_s + watch_s + back)


def topup_target(remaining_s: float, lap_s: float) -> float:
    """补能补到多少回来:够撑到布防结束(按一圈的出站线留够),最多 :data:`MAX_RESUME_PCT`。"""
    need = return_line() + drain_pct(remaining_s) * LAP_FACTOR
    return round(min(MAX_RESUME_PCT, max(need, lap_line(lap_s) + 10.0)), 1)


def laps_per_charge(lap_s: float, cycle_s: float, start_pct: float = MAX_RESUME_PCT) -> int:
    """从 ``start_pct`` 出发,补一次能之前能开几圈(每圈按整个周期耗电算,保守)。"""
    line, d = lap_line(lap_s), drain_pct(max(cycle_s, lap_s))
    if start_pct < line or d <= 0:
        return 0
    return int(math.floor((start_pct - line) / d)) + 1
