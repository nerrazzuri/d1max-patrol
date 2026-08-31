"""故障场景。每一条都对应一件真机上必然会发生的事。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    BackendReconnected,
    LocStatusEvent,
    NavBackendError,
    NavConnectionError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.protocol.nav_types import (
    ALG_LIDAR_DISCONNECTED,
    ALG_NAV_BLOCKED,
    LocStatus,
    NavStatus,
    Pose,
)

pytestmark = pytest.mark.scenarios


async def _drain_until(queue, predicate, timeout_s: float = 20.0):
    async def loop():
        while True:
            event = await queue.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(loop(), timeout=timeout_s)


def _nav_is(status):
    return lambda e: isinstance(e, NavStatusEvent) and e.status is status


# ------------------------------------------------------- 算法故障码


async def test_路径被挡的故障码不打断正在进行的导航(rig):
    """13330 是"提示"不是"终止"。上层应当记录并继续,由超时兜底。"""
    with rig.backend.subscription() as queue:
        await rig.backend.goto(Pose.from_xy_yaw(2.0, 0.0, 0.0))
        rig.inject(f"alg_error {ALG_NAV_BLOCKED}")

        alg = await _drain_until(queue, lambda e: isinstance(e, AlgErrorEvent))
        assert alg.items[0].code == ALG_NAV_BLOCKED
        await _drain_until(queue, _nav_is(NavStatus.SUCCEED))


async def test_雷达断连故障码也能透传(rig):
    with rig.backend.subscription() as queue:
        rig.inject(f"alg_error {ALG_LIDAR_DISCONNECTED} 3")
        alg = await _drain_until(queue, lambda e: isinstance(e, AlgErrorEvent))
        assert alg.items[0].code == ALG_LIDAR_DISCONNECTED
        assert alg.items[0].severity == 3


async def test_连续多条故障码不丢(rig):
    with rig.backend.subscription() as queue:
        for _ in range(5):
            rig.inject(f"alg_error {ALG_NAV_BLOCKED}")
        received = 0
        while received < 5:
            await _drain_until(queue, lambda e: isinstance(e, AlgErrorEvent))
            received += 1


# ------------------------------------------------------- 走不动


async def test_卡住时导航等待终态超时(rig):
    """stuck: 状态一直是 Active,永远到不了。只能靠超时发现。"""
    rig.inject("stuck on")
    target = Pose.from_xy_yaw(5.0, 0.0, 0.0)
    with rig.backend.subscription() as queue:
        await rig.backend.goto(target)
        with pytest.raises(NavTimeoutError):
            await rig.backend.wait_nav_terminal(1.0, queue=queue)
    assert await rig.backend.nav_status() is NavStatus.ACTIVE
    # 光凭"超时了"证明不了 stuck 真的生效 —— 1.0s 的超时本来就比 5m/0.6(m/s)
    # 的正常耗时(~8.6s)短得多,不卡也会超时。真正的 stuck 后果是"几乎没动":
    # 冻结时 model.step() 直接早退,goal_distance 应原封不动留在 5.0 附近。
    # 零耗时,只是读一个当前值,不额外等待。
    assert rig.sim.model.goal_distance == pytest.approx(5.0, abs=0.01)


async def test_超时后能停下并恢复(rig):
    """超时之后必须能收拾残局,否则下一个点没法开始。"""
    rig.inject("stuck on")
    await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
    # 等"卡住"这件事真的发生 —— 没有事件可等,stuck 就是状态永不改变,
    # 只能靠固定时长来确认"确实一直停在 Active,不是还没来得及走"。
    await asyncio.sleep(0.3)

    with rig.backend.subscription() as queue:
        await rig.backend.stop()
        await _drain_until(queue, _nav_is(NavStatus.CANCELLED))

        rig.inject("stuck off")
        await _drain_until(queue, _nav_is(NavStatus.STANDBY))

        await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
        status = await rig.backend.wait_nav_terminal(20.0, queue=queue)
    assert status is NavStatus.SUCCEED


async def test_减速会让原本够用的超时变得不够用(rig):
    """slow 是制造"能走但太慢"的手段 —— 巡检里最难判断的一类故障。"""
    # "原本够用"不能靠测试名字自证,必须真的用同一个超时、同一段路程跑一遍
    # 且真的走完,才算数。所以这里让机器狗在 a<->b 之间走两趟同样的路:
    # 第一趟(a->b)不注入 slow,用来证明 timeout_s originally 够用;
    # 第二趟再走回 a,然后原路重发 a->b、注入 slow,用完全相同的
    # timeout_s —— 若这次等不到终态,才是"同一个超时,慢了就不够用"的
    # 直接证据,而不是换了一段更长/更短的路程碰运气凑出来的超时。
    target_a = Pose.from_xy_yaw(0.6, 0.0, 0.0)
    target_b = Pose.from_xy_yaw(1.2, 0.0, 0.0)
    timeout_s = 3.0
    with rig.backend.subscription() as queue:
        # 走到 a,作为两趟 a->b 的共同起点。
        await rig.backend.goto(target_a)
        status = await rig.backend.wait_nav_terminal(10.0, queue=queue)
        assert status is NavStatus.SUCCEED
        # 终态之后要驻留 terminal_hold_s 才回落 StandBy,下一次 start_nav
        # 在此之前会被拒绝 —— 等这条事件到齐才能发下一次导航。
        await _drain_until(queue, _nav_is(NavStatus.STANDBY))

        # 第一趟 a->b: 不注入 slow,必须在 timeout_s 内正常走完 ——
        # 这就是"原本够用"的证明,而不是断言里假设出来的。
        await rig.backend.goto(target_b)
        status = await rig.backend.wait_nav_terminal(timeout_s, queue=queue)
        assert status is NavStatus.SUCCEED
        await _drain_until(queue, _nav_is(NavStatus.STANDBY))

        # 走回 a,准备原路重发一次同样的 a->b。
        await rig.backend.goto(target_a)
        status = await rig.backend.wait_nav_terminal(10.0, queue=queue)
        assert status is NavStatus.SUCCEED
        await _drain_until(queue, _nav_is(NavStatus.STANDBY))

        # 第二趟 a->b: 注入 slow,同一段路、同一个 timeout_s,这次等不到。
        rig.inject("slow 20")
        await rig.backend.goto(target_b)
        with pytest.raises(NavTimeoutError):
            await rig.backend.wait_nav_terminal(timeout_s, queue=queue)


# ------------------------------------------------------- 定位


async def test_定位丢失使导航失败并发出定位事件(rig):
    with rig.backend.subscription() as queue:
        await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
        await _drain_until(queue, _nav_is(NavStatus.ACTIVE))

        rig.inject("loc_lost")
        # 定位丢失与导航失败在仿真器里发生于同一个 tick,轮询把二者读出来
        # 的先后顺序不保证(`_poll_once` 固定先查 nav 再查 loc,若同一轮就
        # 双双改变,NavStatusEvent(FAILED) 反而先进队)。因此不能假设两个
        # 事件谁先到,只能确认两个都到齐。
        seen_failed = False
        seen_loc_lost = False
        while not (seen_failed and seen_loc_lost):
            event = await asyncio.wait_for(queue.get(), timeout=20.0)
            if _nav_is(NavStatus.FAILED)(event):
                seen_failed = True
            if isinstance(event, LocStatusEvent) and event.status is LocStatus.LOC_LOST:
                seen_loc_lost = True


async def test_定位恢复后可以重新导航(rig):
    with rig.backend.subscription() as queue:
        rig.inject("loc_lost")
        await _drain_until(queue, lambda e: isinstance(e, LocStatusEvent)
                           and e.status is LocStatus.LOC_LOST)

        with pytest.raises(NavBackendError):
            await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))

        rig.inject("loc_ok")
        await _drain_until(queue, lambda e: isinstance(e, LocStatusEvent)
                           and e.status is LocStatus.CONTINUOUS_LOC)

        await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
        status = await rig.backend.wait_nav_terminal(20.0, queue=queue)
    assert status is NavStatus.SUCCEED


# ------------------------------------------------------- 链路


async def test_导航途中断链重连后能继续下一个点(rig):
    with rig.backend.subscription() as queue:
        await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
        await _drain_until(queue, _nav_is(NavStatus.ACTIVE))

        # 断链窗口放宽到 0.8s(订正 E 预先批准): 0.4s 的窗口比重连退避
        # (0.05→0.2s)短,曾在偶发情况下让重连在设备仍拒连时耗掉几次尝试。
        rig.inject("disconnect 0.8")
        await _drain_until(queue, lambda e: isinstance(e, BackendDisconnected))
        await _drain_until(queue, lambda e: isinstance(e, BackendReconnected))

        # 重连后状态被重新广播一遍(previous 为 None)
        await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                           and e.previous is None)

        await rig.backend.stop()
        # stop() 产生的 CANCELLED/STANDBY 也会落进这条队列。若不显式排空就
        # 把队列交给下面的 wait_nav_terminal,它会把这条陈旧的 CANCELLED
        # 当成"新导航"的终态直接返回 —— 必须先等它翻回 StandBy。
        await _drain_until(queue, _nav_is(NavStatus.CANCELLED))
        await _drain_until(queue, _nav_is(NavStatus.STANDBY))

        await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
        status = await rig.backend.wait_nav_terminal(20.0, queue=queue)
    assert status is NavStatus.SUCCEED


async def test_断链期间等待终态直接抛错(rig):
    with rig.backend.subscription() as q:
        await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
        # 订阅已在 goto 之前建立,事件不会丢;这里睡 0.2s 只是让导航有
        # 时间真正进入 Active——本用例只关心"等待终态期间断链必须抛错",
        # 断链发生时具体是 Initializing 还是 Active 不影响结论,无需精确
        # 同步到某个状态变化事件上。
        await asyncio.sleep(0.2)
        rig.inject("disconnect 0.5")
        with pytest.raises(NavConnectionError, match="断开"):
            await rig.backend.wait_nav_terminal(20.0, queue=q)


# ------------------------------------------------------- 协议降级


async def test_帧号全程归零仍能跑完一趟三点巡检(rig):
    """最坏情况:帧号完全不可用,全靠函数名降级匹配。"""
    rig.inject("frame_count_zero on")
    with rig.backend.subscription() as queue:
        for target in (Pose.from_xy_yaw(1.0, 0.0, 0.0),
                       Pose.from_xy_yaw(1.0, 1.0, 1.5708),
                       Pose.from_xy_yaw(0.0, 0.0, 3.1416)):
            await rig.backend.goto(target)
            status = await rig.backend.wait_nav_terminal(25.0, queue=queue)
            assert status is NavStatus.SUCCEED
            # 终态之后设备要驻留 terminal_hold_s 才回落 StandBy,下一次
            # start_nav 在此之前会被拒绝 —— 必须等这条事件到齐才能发下一个点。
            await _drain_until(queue, _nav_is(NavStatus.STANDBY))

    assert rig.backend.fallback_matches > 0      # 确实降级了
    assert rig.backend.dropped_frames == 0       # 但一帧没丢


async def test_乱序加并发不串台(rig):
    rig.inject("reorder 0.15")
    for _ in range(5):
        nav, loc, speed = await asyncio.gather(
            rig.backend.nav_status(),
            rig.backend.loc_status(),
            rig.backend.get_speed(),
        )
        assert nav is NavStatus.STANDBY
        assert loc is LocStatus.CONTINUOUS_LOC
        assert set(speed) == {"x", "y", "z"}

    # 上面这段哪怕响应按发出顺序原样到达也会通过 —— 帧号匹配本来就该让
    # "谁先回来"不影响结果正确性。要证明乱序注入真的生效,必须证明响应确实
    # 没有按发出顺序到达。`request()` 里 frame_count 在第一个 await 之前
    # 同步分配,同一批任务的调度顺序等价于发出顺序,于是"完成顺序"如果和
    # "发出顺序"不同,就是乱序确实发生了的直接证据。
    order: list[int] = []

    async def _tagged(i: int) -> None:
        if i % 3 == 0:
            await rig.backend.nav_status()
        elif i % 3 == 1:
            await rig.backend.loc_status()
        else:
            await rig.backend.get_speed()
        order.append(i)

    n = 12
    await asyncio.gather(*(_tagged(i) for i in range(n)))
    assert order != list(range(n)), "完成顺序与发出顺序一致,说明乱序注入没有生效"

    # 而且这份乱序是靠帧号正确路由处理掉的,没有退化成函数名回退匹配、
    # 也没有丢帧。
    assert rig.backend.dropped_frames == 0
    assert rig.backend.fallback_matches == 0


async def test_注入复位后一切回到正常(rig):
    rig.inject("slow 10")
    rig.inject("frame_count_zero on")
    rig.inject("reset")
    assert rig.sim.faults.speed_scale == 1.0
    assert rig.sim.faults.frame_count_zero is False

    with rig.backend.subscription() as queue:
        await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
        status = await rig.backend.wait_nav_terminal(20.0, queue=queue)
    assert status is NavStatus.SUCCEED
