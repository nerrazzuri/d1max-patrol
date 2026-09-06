"""安全与降级规则表。主规范 §6.4 那张表,一行一条。

**这是一个纯函数模块。** 不碰后端、不碰 IO、不碰时间 —— 输入一个事件加一份
上下文,输出一个决策。所以它能被穷举测试,而"出事那一刻该怎么办"这种最难
在现场复现的逻辑,恰恰最需要能穷举。

**ABORT 不含任何位移。** 停止导航 + 记录 + 告警,不自动返航、不自动趴下
(主规范 §6.4)。返航是另一个决策 ``RETURN_HOME``,只在电量策略触发且系统
状态健康时才走。这两个别混。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    ControlLostEvent,
    DeviceEvent,
    Event,
    FaultEvent,
    LocStatusEvent,
)
from d1max_patrol.engine.homing import DEFAULT_RETURN_PARAMS
from d1max_patrol.engine.mission import Policy
from d1max_patrol.protocol.nav_types import (
    ALG_LIDAR_DISCONNECTED,
    ALG_NAV_BLOCKED,
    LocStatus,
)

#: 定位重置试这么多次还不收敛就中止。再试下去只是在原地耗电。
MAX_LOC_RESET = 3

#: 被挡住先等这么久。人从狗前面走过去要几秒,叉车挪开要十几秒 —— 等一下
#: 比直接判这个点失败划算。
BLOCKED_WAIT_S = 20.0

#: WS 断连超过这么久才暂停。短暂抖动不该打断任务。
WS_DOWN_PAUSE_S = 5.0


class Decision(str, Enum):
    CONTINUE = "continue"        # 什么都不做
    PAUSE = "pause"
    ABORT = "abort"
    RETURN_HOME = "return_home"
    FAIL_WAYPOINT = "fail_waypoint"
    WAIT = "wait"                # 记录并等待,超时后升级


@dataclass(frozen=True, slots=True)
class Ruling:
    """一条裁决。``reason`` 会原样进事件流,所以必须是人看得懂的话。"""

    decision: Decision
    reason: str
    escalate_after_s: float = 0.0   # 仅 WAIT 有意义


@dataclass(frozen=True, slots=True)
class SafetyContext:
    """裁决所需的全部外部信息。规则表不去别处打听。"""

    policy: Policy
    battery_pct: float
    blocked_for_s: float = 0.0
    loc_reset_attempts: int = 0
    ws_down_for_s: float = 0.0
    #: "从现在这个位置走回原点大约要掉多少个点的电"(``homing.estimate_cost_pct``)。
    #: **默认 0 表示"还没算出来",不表示"回家不要钱"** —— 见 ``return_line_pct``。
    return_cost_pct: float = 0.0
    #: 回家成本的地板,**必须跟算出发线的那一份是同一个数**。
    #: ``ReturnParams`` 是每台引擎可配的(真机标定要改它),而这里原先直接读
    #: ``DEFAULT_RETURN_PARAMS`` —— 两支各按各的地板算,起飞门槛放行了、
    #: 返航线却在同一刻判 ``RETURN_HOME``,狗一起飞就掉头。默认值只是给
    #: 没配过的调用方兜底,引擎自己一律显式传。
    floor_pct: float = DEFAULT_RETURN_PARAMS.floor_pct


def _loc_lost(ctx: SafetyContext) -> Ruling:
    if ctx.loc_reset_attempts >= MAX_LOC_RESET or ctx.policy.on_loc_lost == "abort":
        return Ruling(Decision.ABORT,
                      f"定位丢失,已重置 {ctx.loc_reset_attempts} 次仍未收敛")
    return Ruling(Decision.PAUSE,
                  f"定位丢失,暂停并尝试重置(第 {ctx.loc_reset_attempts + 1} 次)")


def _alg_error(event: AlgErrorEvent, ctx: SafetyContext) -> Ruling:
    codes = {item.code for item in event.items}
    # 雷达先判:一份推送里可能同时带着"被挡住"和"雷达掉了",而雷达掉了的时候
    # "被挡住"本来就不可信 —— 严重的那条说了算。
    if ALG_LIDAR_DISCONNECTED in codes:
        return Ruling(Decision.ABORT, f"雷达掉线(故障码 {ALG_LIDAR_DISCONNECTED})")
    if ALG_NAV_BLOCKED in codes:
        if ctx.blocked_for_s >= BLOCKED_WAIT_S:
            return Ruling(Decision.FAIL_WAYPOINT,
                          f"被挡住已超过 {BLOCKED_WAIT_S:.0f}s,判当前点失败")
        return Ruling(Decision.WAIT, f"导航被挡住,先等 {BLOCKED_WAIT_S:.0f}s",
                      escalate_after_s=BLOCKED_WAIT_S)
    return Ruling(Decision.CONTINUE,
                  f"故障码 {sorted(codes)} 没有对应处置,只记录")


def _fault(event: FaultEvent) -> Ruling:
    if event.fatal:
        return Ruling(Decision.ABORT, f"SDK 致命故障: {', '.join(event.items) or '(无描述)'}")
    return Ruling(Decision.CONTINUE,
                  f"SDK 非致命故障,只记录: {', '.join(event.items) or '(无描述)'}")


def _control_lost(event: ControlLostEvent, ctx: SafetyContext) -> Ruling:
    reason = f"控制权被拿走: {event.reason or '(未说明原因)'}"
    if ctx.policy.on_control_lost == "abort":
        return Ruling(Decision.ABORT, reason)
    # 未知取值也走这条:暂停是可恢复的,而策略字段在 Policy 解析时就已经卡过
    # 词表,能走到这里的只有 pause。
    return Ruling(Decision.PAUSE, reason)


def _disconnected(ctx: SafetyContext) -> Ruling:
    if ctx.ws_down_for_s >= WS_DOWN_PAUSE_S:
        return Ruling(Decision.PAUSE,
                      f"后端断连已 {ctx.ws_down_for_s:.1f}s,超过 {WS_DOWN_PAUSE_S:.0f}s")
    return Ruling(Decision.CONTINUE,
                  f"后端断连 {ctx.ws_down_for_s:.1f}s,还在容忍范围内")


def rule(event: Event | DeviceEvent, ctx: SafetyContext) -> Ruling:
    """给一个事件一份裁决。没规定的事件一律不干预。"""
    if isinstance(event, LocStatusEvent):
        if event.status is LocStatus.LOC_LOST:
            return _loc_lost(ctx)
        return Ruling(Decision.CONTINUE, f"定位状态 {event.status.value},无需干预")
    if isinstance(event, AlgErrorEvent):
        return _alg_error(event, ctx)
    if isinstance(event, FaultEvent):
        return _fault(event)
    if isinstance(event, ControlLostEvent):
        return _control_lost(event, ctx)
    if isinstance(event, BackendDisconnected):
        return _disconnected(ctx)
    return Ruling(Decision.CONTINUE, f"{type(event).__name__} 无对应规则,只记录")


def _return_cost(ctx: SafetyContext) -> float:
    """这一刻按多远算的回家成本,**下有地板**。

    ``return_cost_pct`` 默认 0 —— 那不是"回家不要钱",是"还没算出来"
    (原点还没换进来,或者这一趟没有点位)。按 0 用的话返航线就等于中止线。
    """
    return max(ctx.return_cost_pct, ctx.floor_pct)


def return_line_pct(ctx: SafetyContext) -> float:
    """这一刻的返航线。

    **返航线是动态的**(spec §1.2):跑到场地最远端时的返航线,必然高于刚出发时。
    算法只有一句 —— **你必须在"还够走回去、而且走到家时手上还剩着中止线
    那份余量"的时候就掉头**::

        返航线 = max(静态下限, 中止线 + 回家的成本)

    ``policy.battery_return_pct`` 因此从"返航线"降格成"返航线的静态下限"。
    字段名和 wire 格式都没动 —— 动了要改协议,而它作为下限仍然有意义:
    它挡的是有人把场地配得极小、把返航线压到没有余量。

    ``return_cost_pct`` 为 0 时(还没算出来)回家成本按 ``floor_pct`` 兜底,
    **返回的返航线严格高于中止线** —— 相等跟更低一样死:默认 policy 下两条
    线都是 25,24.9% 判 ABORT、25.0% 判 CONTINUE,中间没有一格是
    ``RETURN_HOME``,那个分支就是永远走不到的死代码。

    兜底用的是 ``ctx.floor_pct``(引擎那一份,不是模块默认那一份),理由跟它
    本身一样:**就算原点就在脚下,起身、站定、对位也要电。** 回家从来不是
    免费的,0 只可能是"还没算",不可能是"不要钱"。
    """
    dynamic = ctx.policy.battery_abort_pct + _return_cost(ctx)
    return max(ctx.policy.battery_return_pct, dynamic)


def battery_ruling(ctx: SafetyContext) -> Ruling:
    """电量单独一条。

    它不是被某个事件触发的,而是每次遥测更新都要重问一遍 —— 所以给它一个
    单独的入口,而不是塞进 ``rule`` 里等 ``BatteryEvent``。
    """
    if ctx.battery_pct < ctx.policy.battery_abort_pct:
        # 撑着走回去可能半路没电趴在外面,原地停下更好找。
        return Ruling(Decision.ABORT,
                      f"电量 {ctx.battery_pct:.0f}% 低于中止线 "
                      f"{ctx.policy.battery_abort_pct:.0f}%,原地停止")
    line = return_line_pct(ctx)
    if ctx.battery_pct < line:
        # 这句话是这一关唯一给人的解释,**两支各说各的**。静态下限赢的时候
        # 照印"中止线 + 回家",人读到的是"低于返航线 60%(中止线 25% +
        # 回家 3%)"—— 25 加 3 不等于 60,站在狗旁边的人会以为程序算错了。
        cost = _return_cost(ctx)
        if line > ctx.policy.battery_abort_pct + cost:
            why = f"静态下限 {ctx.policy.battery_return_pct:.0f}%"
        else:
            why = f"中止线 {ctx.policy.battery_abort_pct:.0f}% + 回家 {cost:.0f}%"
        return Ruling(Decision.RETURN_HOME,
                      f"电量 {ctx.battery_pct:.0f}% 低于返航线 {line:.0f}%"
                      f"({why}),返航")
    return Ruling(Decision.CONTINUE,
                  f"电量 {ctx.battery_pct:.0f}%,高于返航线 {line:.0f}%")
