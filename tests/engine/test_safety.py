"""安全降级规则表:逐行对着主规范 §6.4 那张表。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    BackendReconnected,
    BatteryEvent,
    ControlLostEvent,
    DevicePoseEvent,
    FaultEvent,
    LocStatusEvent,
    NavStatusEvent,
)
from d1max_patrol.engine.mission import Policy
from d1max_patrol.engine.safety import (
    BLOCKED_WAIT_S,
    MAX_LOC_RESET,
    WS_DOWN_PAUSE_S,
    Decision,
    SafetyContext,
    battery_ruling,
    return_line_pct,
    rule,
)
from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import (
    ALG_LIDAR_DISCONNECTED,
    ALG_NAV_BLOCKED,
    LocStatus,
    NavStatus,
    Pose,
)


def _alg(*codes: int) -> AlgErrorEvent:
    return AlgErrorEvent(tuple(AlgErrorItem(c, f"code {c}", 1) for c in codes))


BLOCKED = _alg(ALG_NAV_BLOCKED)
LIDAR_GONE = _alg(ALG_LIDAR_DISCONNECTED)
FATAL = FaultEvent(("电机过温",), fatal=True)

ALL_SAMPLE_EVENTS = (
    LocStatusEvent(LocStatus.LOC_LOST),
    LocStatusEvent(LocStatus.CONTINUOUS_LOC),
    BLOCKED,
    LIDAR_GONE,
    _alg(99999),
    FATAL,
    FaultEvent(("轻微告警",)),
    ControlLostEvent("上装抢走了"),
    BackendDisconnected("ws closed"),
    BackendReconnected(),
    NavStatusEvent(NavStatus.ACTIVE),
    BatteryEvent(50.0),
    DevicePoseEvent(Pose.from_xy_yaw(0.0, 0.0, 0.0)),
)


@pytest.fixture
def ctx() -> SafetyContext:
    return SafetyContext(policy=Policy(), battery_pct=80.0)


# ------------------------------------------------------------------- 定位丢失


def test_定位丢了先暂停(ctx):
    assert rule(LocStatusEvent(LocStatus.LOC_LOST), ctx).decision is Decision.PAUSE


def test_定位重置试满次数就中止(ctx):
    r = rule(LocStatusEvent(LocStatus.LOC_LOST),
             replace(ctx, loc_reset_attempts=MAX_LOC_RESET))
    assert r.decision is Decision.ABORT
    assert "定位" in r.reason


def test_定位策略配成中止就不试重置(ctx):
    c = replace(ctx, policy=replace(ctx.policy, on_loc_lost="abort"))
    assert rule(LocStatusEvent(LocStatus.LOC_LOST), c).decision is Decision.ABORT


def test_定位收敛不触发任何处置(ctx):
    for good in (LocStatus.CONTINUOUS_LOC, LocStatus.INIT_LOCALIZATION):
        assert rule(LocStatusEvent(good), ctx).decision is Decision.CONTINUE


# ------------------------------------------------------------------- 导航故障


def test_导航被挡先等一会儿(ctx):
    r = rule(BLOCKED, ctx)
    assert r.decision is Decision.WAIT
    assert r.escalate_after_s == pytest.approx(BLOCKED_WAIT_S)


def test_导航一直被挡就判这个点失败(ctx):
    r = rule(BLOCKED, replace(ctx, blocked_for_s=BLOCKED_WAIT_S + 1))
    assert r.decision is Decision.FAIL_WAYPOINT


def test_雷达掉线立刻中止(ctx):
    assert rule(LIDAR_GONE, ctx).decision is Decision.ABORT


def test_同时被挡和雷达掉线按严重的那条算(ctx):
    """雷达没了的时候"被挡住"本来就不可信,不能只等 20 秒。"""
    both = _alg(ALG_NAV_BLOCKED, ALG_LIDAR_DISCONNECTED)
    assert rule(both, ctx).decision is Decision.ABORT


def test_不认识的故障码不擅自处置(ctx):
    r = rule(_alg(99999), ctx)
    assert r.decision is Decision.CONTINUE
    assert "99999" in r.reason, "记录下来,别悄悄咽掉"


# ------------------------------------------------------------------- SDK 故障


def test_致命故障立刻中止(ctx):
    assert rule(FATAL, ctx).decision is Decision.ABORT


def test_非致命故障只记录(ctx):
    assert rule(FaultEvent(("轻微告警",)), ctx).decision is Decision.CONTINUE


def test_致命故障的理由里带得出是什么故障(ctx):
    assert "电机过温" in rule(FATAL, ctx).reason


# --------------------------------------------------------------------- 控制权


def test_丢控制权按策略走_默认暂停(ctx):
    assert rule(ControlLostEvent("上装抢走了"), ctx).decision is Decision.PAUSE


def test_丢控制权策略配成中止就中止(ctx):
    c = replace(ctx, policy=replace(ctx.policy, on_control_lost="abort"))
    assert rule(ControlLostEvent("上装抢走了"), c).decision is Decision.ABORT


def test_丢控制权的理由里带得出对方给的原因(ctx):
    assert "上装抢走了" in rule(ControlLostEvent("上装抢走了"), ctx).reason


# ----------------------------------------------------------------------- 电量


def test_电量掉到返航线就返航(ctx):
    c = replace(ctx, policy=replace(ctx.policy,
                                    battery_return_pct=25.0, battery_abort_pct=15.0))
    assert battery_ruling(replace(c, battery_pct=24.0)).decision is Decision.RETURN_HOME


def test_电量掉到中止线就地中止而不是硬撑着回去(ctx):
    """撑着走回去可能半路没电趴在外面 —— 原地停下更好找。"""
    c = replace(ctx, policy=replace(ctx.policy,
                                    battery_return_pct=25.0, battery_abort_pct=15.0))
    assert battery_ruling(replace(c, battery_pct=14.0)).decision is Decision.ABORT


def test_电量够就什么都不做(ctx):
    assert battery_ruling(replace(ctx, battery_pct=80.0)).decision is Decision.CONTINUE


def test_中止线优先于返航线(ctx):
    """两条线都越过了要中止,不能因为先判返航就带着 14% 的电往回走。"""
    c = replace(ctx, policy=replace(ctx.policy,
                                    battery_return_pct=25.0, battery_abort_pct=15.0))
    assert battery_ruling(replace(c, battery_pct=10.0)).decision is Decision.ABORT


def test_返航线是中止线加上回家的成本():
    ctx = SafetyContext(policy=Policy(battery_return_pct=25.0, battery_abort_pct=25.0),
                        battery_pct=80.0, return_cost_pct=8.0)
    assert return_line_pct(ctx) == pytest.approx(33.0)


def test_返航线永远高于中止线否则返航轮不到():
    # 这条是本次改动的全部意义。中止先判,返航线要是不严格高于中止线,
    # RETURN_HOME 这个分支就是死代码 —— 而它是死代码这件事,不会有任何人发现。
    policy = Policy(battery_return_pct=25.0, battery_abort_pct=25.0)
    ctx = SafetyContext(policy=policy, battery_pct=80.0, return_cost_pct=3.0)
    assert return_line_pct(ctx) > policy.battery_abort_pct


def test_静态下限还在_场地很小的时候不许把返航线压太低():
    # 原点就在脚下时返航成本是 3.0,中止 25 + 3 = 28。但要是有人把
    # battery_return_pct 配成 40,那就听 40 的 —— 下限是下限,不是上限。
    ctx = SafetyContext(policy=Policy(battery_return_pct=40.0, battery_abort_pct=25.0),
                        battery_pct=80.0, return_cost_pct=3.0)
    assert return_line_pct(ctx) == pytest.approx(40.0)


def test_跑在场地远端时的返航线高于刚出发时():
    policy = Policy(battery_return_pct=25.0, battery_abort_pct=25.0)
    near = SafetyContext(policy=policy, battery_pct=60.0, return_cost_pct=3.0)
    far = SafetyContext(policy=policy, battery_pct=60.0, return_cost_pct=14.0)
    assert return_line_pct(far) > return_line_pct(near)


def test_同样的电在远端就该掉头_在原点旁边不用():
    policy = Policy(battery_return_pct=25.0, battery_abort_pct=25.0)
    far = SafetyContext(policy=policy, battery_pct=36.0, return_cost_pct=14.0)
    near = SafetyContext(policy=policy, battery_pct=36.0, return_cost_pct=3.0)
    assert battery_ruling(far).decision is Decision.RETURN_HOME
    assert battery_ruling(near).decision is Decision.CONTINUE


def test_返航的理由里要说清楚是按多远算的():
    ctx = SafetyContext(policy=Policy(battery_return_pct=25.0, battery_abort_pct=25.0),
                        battery_pct=30.0, return_cost_pct=8.0)
    reason = battery_ruling(ctx).reason
    assert "33" in reason, "人要能从这句话里看出返航线当时是多少"


def test_返航成本没喂进来时退回静态返航线():
    # return_cost_pct 默认 0 —— 那不是"回家不要钱",是"还没算出来"。
    # 这时必须退回静态那条线,不能算出一条比中止线还低的返航线。
    ctx = SafetyContext(policy=Policy(battery_return_pct=25.0, battery_abort_pct=15.0),
                        battery_pct=24.0)
    assert return_line_pct(ctx) == pytest.approx(25.0)
    assert battery_ruling(ctx).decision is Decision.RETURN_HOME


# ----------------------------------------------------------------------- 断连


def test_断连超过阈值才暂停(ctx):
    assert rule(BackendDisconnected("ws closed"), ctx).decision is Decision.CONTINUE
    assert rule(BackendDisconnected("ws closed"),
                replace(ctx, ws_down_for_s=WS_DOWN_PAUSE_S + 1)).decision is Decision.PAUSE


# ----------------------------------------------------------------------- 通则


def test_中止绝不包含任何位移(ctx):
    """主规范 §6.4:ABORT = 停止导航 + 记录 + 告警。不自动返航、不自动趴下。"""
    for ev in (LIDAR_GONE, FATAL):
        assert rule(ev, ctx).decision is Decision.ABORT
    assert Decision.ABORT is not Decision.RETURN_HOME


def test_没规定的事件一律不干预(ctx):
    for ev in (BackendReconnected(), NavStatusEvent(NavStatus.ACTIVE),
               BatteryEvent(50.0), DevicePoseEvent(Pose.from_xy_yaw(0, 0, 0))):
        assert rule(ev, ctx).decision is Decision.CONTINUE


def test_每条裁决都带得住人看的理由(ctx):
    for ev in ALL_SAMPLE_EVENTS:
        assert rule(ev, ctx).reason.strip(), f"{ev} 的裁决没写理由"


def test_只有等待才给升级时限(ctx):
    for ev in ALL_SAMPLE_EVENTS:
        r = rule(ev, ctx)
        if r.decision is not Decision.WAIT:
            assert r.escalate_after_s == 0.0, f"{ev} 不是 WAIT 却带了升级时限"


def test_规则表不碰后端也不碰io():
    """纯函数是它能被穷举的前提 —— 一旦它去 await 什么,这些测试就全废了。"""
    import inspect
    from pathlib import Path

    from d1max_patrol.engine import safety
    src = Path(inspect.getfile(safety)).read_text(encoding="utf-8")
    for banned in ("async def", "await ", "import asyncio", "open("):
        assert banned not in src, f"safety.py 里不该出现 {banned!r}"
