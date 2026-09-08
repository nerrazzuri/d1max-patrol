"""引擎让开腿:``SUSPENDED``(§5.10)。

**跟 ``PAUSED`` 的区别是「谁在动这条狗」。** ``PAUSED`` 是人按了暂停,狗停着,
没有人碰它。``SUSPENDED`` 是引擎主动让开,因为接下来这段路人要亲自开 ——
挡在门口的箱子挪不开、点位飘到了墙里。这段时间 ``_busy_checks`` 恰恰是满的,
而那正是 §5.10 要放行的东西。

时刻全是注进来的,一个 ``sleep`` 都没有(§8.5 第 2 条)。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from d1max_patrol.backends.base import BatteryEvent, DevicePoseEvent
from d1max_patrol.engine.archive import read_events
from d1max_patrol.engine.machine import MissionEngine, RunState
from d1max_patrol.engine.mission import Policy
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

from .conftest import _HOME, NEVER, make_mission, until

#: ``nav``/``device``/``media``/``clock``/``make_engine`` 这几个夹具,连同
#: ``NavStub``/``DeviceStub``/``MediaStub``/``Clock`` 那套假后端,都长在
#: ``tests/engine/conftest.py`` 里 —— pytest 会自动喂给这个目录下的每个
#: 测试模块,不用在这儿 import。原来这里是跨模块 import
#: ``test_machine.py`` 里的同名对象、外加一个专门压 ruff F401 的
#: ``__all__``,是全仓第一次这么干;搬到 conftest.py 之后不用再跨模块导
#: 一遍,回到仓里其他共享夹具(比如 ``sample_mission``)一直在用的办法。


async def 等一拍(eng: MissionEngine, *, timeout_s: float = 2.0) -> None:
    """等到引擎把队列里的东西吃完。**有截止时间,不是 sleep。**

    这里要证的是「点了继续但状态没变」,而「没变」只能靠等一小会儿再看 ——
    所以等的是**队列空**这个可观察的事实,不是一个拍脑袋的余量。

    它碰了一个私有属性(``eng._queue``),这是明知故犯的。换成固定
    ``sleep(余量)`` 才是真错:余量给小了偶发红,给大了每跑一次都白等。
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not eng._queue.empty():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("引擎没把队列吃完")
        await asyncio.sleep(0)


# --------------------------------------------------------------------- 夹具


@pytest.fixture
async def 跑起来的引擎(make_engine, nav):
    """已经 ``start`` 起来、卡在第一个点导航上的引擎。

    照抄 ``test_machine.py`` 里 ``test_人工暂停会真的把狗停下来`` 那一路的
    建法:``nav.on_goto = NEVER`` 让 ``goto`` 不产生终态事件,引擎停在
    RUNNING 里一直等,这样才有稳定的时机去 suspend / pause,不必跟
    「点位刚好跑完」赛跑。用例自己不用管收尾,这里统一处理。
    """
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    yield engine
    if engine.running:
        await engine.abort("测试收尾")
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


@pytest.fixture
async def 跑起来的引擎_喂过位姿(跑起来的引擎, device):
    """在 ``跑起来的引擎`` 的基础上,先喂进去一份位姿再交给用例。

    位姿走的是设备事件那条路(``DevicePoseEvent``),不是直接怼私有队列 ——
    这样测的才是「引擎真收到过位姿」这件事,不是「测试自己造了一个字段」。
    等的办法跟 ``等一拍`` 一个道理:碰私有属性(``eng._live.last_pose``),
    但只有它才能确认「已经被 ``_handle`` 处理过」,不是在猜一个时间余量。
    """
    eng = 跑起来的引擎
    device.emit(DevicePoseEvent(Pose.from_xy_yaw(3.0, 4.0, 0.0)))
    deadline = asyncio.get_running_loop().time() + 2.0
    while eng._live is None or eng._live.last_pose is None:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("位姿没被引擎收到")
        await asyncio.sleep(0)
    return eng


# --------------------------------------------------------------------- 用例


async def test_挂起进得去_并且状态是SUSPENDED(跑起来的引擎):
    eng = 跑起来的引擎
    await eng.suspend("门口有箱子,人去挪一下")
    await eng.wait_state(RunState.SUSPENDED)
    assert eng.state is RunState.SUSPENDED


async def test_挂起时引擎让位(跑起来的引擎):
    """``yielding`` 是给 ``TeleopBusy`` 读的那一个布尔(Task 6)。

    做成引擎自己的属性,而不是让遥控去比 ``state is RunState.SUSPENDED``:
    「哪些状态算让位」是引擎的词汇,外壳不该重新讲一遍(全局约束:业务层
    不许知道外壳,反过来外壳也不该复刻业务层的状态表)。
    """
    eng = 跑起来的引擎
    assert eng.yielding is False
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    assert eng.yielding is True


async def test_暂停不算让位(跑起来的引擎):
    """**这一条是这两个状态不能合并的证明。**

    ``PAUSED`` 的时候不许遥控,``SUSPENDED`` 的时候必须能遥控 —— 相反的两条
    规矩落在同一个状态上,就一定有一条是错的。
    """
    eng = 跑起来的引擎
    await eng.pause()
    await eng.wait_state(RunState.PAUSED)
    assert eng.yielding is False


async def test_挂起时记下停在哪个点(跑起来的引擎):
    eng = 跑起来的引擎
    await eng.suspend("门口有箱子")
    await eng.wait_state(RunState.SUSPENDED)
    点 = eng.snapshot.suspended_at
    assert 点 is not None
    assert 点.waypoint_name == eng.snapshot.waypoint_name
    assert 点.reason == "门口有箱子"


async def test_挂起时记下位姿(跑起来的引擎_喂过位姿):
    """**位姿是「人接管前狗在哪儿」的唯一记录。**

    人开着走一段之后,原来那个点位序号还在,但狗已经不在那儿了。要判断
    「接管结束后该从哪儿接着跑」,靠的是这一份快照,不是当前位姿。
    """
    eng = 跑起来的引擎_喂过位姿
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    assert eng.snapshot.suspended_at.pose is not None


async def test_没收到过位姿也照样挂得起来(跑起来的引擎):
    """位姿是**可有可无**的。旁路进程没连上、这一档不报位姿 —— 都不该
    让「人要接管」这件事卡住。挂不起来的后果是人挪不动狗,那比少一份
    位姿记录严重得多。
    """
    eng = 跑起来的引擎
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    assert eng.snapshot.suspended_at.pose is None


async def test_还有人握着摇杆就不许继续(跑起来的引擎):
    """**这条是这个任务里唯一真正危险的一条。**

    人点「继续」的时候左手很可能还压在摇杆上 —— 那一瞬间引擎和人同时在
    给腿下指令。``add_busy_check`` 存在的全部理由就是它。
    """
    eng = 跑起来的引擎
    握着 = ["遥控在动,先松手再继续"]
    eng.add_busy_check(lambda: 握着[0])
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    await eng.resume()
    await 等一拍(eng)
    assert eng.state is RunState.SUSPENDED
    assert "松手" in eng.snapshot.reason
    握着[0] = ""
    await eng.resume()
    await eng.wait_state(RunState.RUNNING)


async def test_继续之后挂起快照清掉(跑起来的引擎, nav):
    """留着就等于在界面上永远挂着一条「有人接管过」—— 那条信息第二天
    还在的时候,值守的人分不出是现在正被接管还是上礼拜被接管过。

    **没用 ``wait_state(RUNNING)`` 等这一下。** RUNNING 在 suspend 之前就已经
    进过 ``_seen``(``跑起来的引擎`` 在开跑那一刻就先经过一次 RUNNING)——
    ``wait_state`` 等的是"出现过"而不是"此刻是",对一个已经出现过的状态它
    会立刻返回,根本没让事件循环把 ``resume`` 处理掉。既有测试
    ``test_继续之后重发当前点而且不算一次重试`` 撞的是同一堵墙,用的是等
    "一个具体可观察的事实"这条路:继续之后会重发当前点,也就是会有一次
    新的 ``goto`` 调用,等 ``goto_calls`` 涨到 2 就是等 resume 真被处理完。
    """
    eng = 跑起来的引擎
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    await eng.resume()
    await until(lambda: len(nav.goto_calls) == 2)
    assert eng.state is RunState.RUNNING
    assert eng.snapshot.suspended_at is None


async def test_挂起时也能中止(跑起来的引擎):
    """人接管到一半发现这趟没法跑了,得能直接收。"""
    eng = 跑起来的引擎
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    await eng.abort("现场不具备条件")
    assert await eng.wait_done(timeout_s=5.0) is RunState.ABORTED


async def test_上线的形状(跑起来的引擎):
    eng = 跑起来的引擎
    await eng.suspend("门口有箱子")
    await eng.wait_state(RunState.SUSPENDED)
    线 = eng.snapshot.to_wire()
    assert 线["state"] == "SUSPENDED"
    assert set(线["suspended_at"]) == {
        "waypoint_index", "waypoint_name", "pose", "reason", "at_ms"}


async def test_没挂起时上线是null(跑起来的引擎):
    assert 跑起来的引擎.snapshot.to_wire()["suspended_at"] is None


# ------------------------------------------------------------- 评审 M1/M2/S1/S2


async def test_挂起期间位姿事件不逐条落盘(跑起来的引擎, device):
    """M1:人接管期间位姿是持续流(``sidecar_device.py`` 每帧 odom 一条,
    不节流),几分钟的接管就能灌上千条。落了就是几分钟接管往归档里写上千行,
    还在事件循环里做上千次阻塞写 —— 这里灌 N 条,断言落盘的事件数不随 N 涨。

    **N2(复审反向探测抓到的盲区):只钉这半句不够。** 把过滤改成"挂起期间
    什么都不记",这条测试原样能过——那样电量告警之类真正该留的事件会跟着
    位姿一起从归档里消失,事后复盘正缺这一段。这里在灌位姿流的同时穿插几条
    ``BatteryEvent``,断言它们一条不少地落进 events.jsonl:该挡的只是
    ``DevicePoseEvent`` 这一种(黑名单),不是"挂起期间全部不记"(白名单
    挡过了头)。
    """
    eng = 跑起来的引擎
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    before = len(read_events(eng.archive.path))
    N = 200
    电量事件数 = 5
    for i in range(N):
        device.emit(DevicePoseEvent(Pose.from_xy_yaw(float(i), 0.0, 0.0)))
        if i % (N // 电量事件数) == 0:
            device.emit(BatteryEvent(percent=60.0))
    deadline = asyncio.get_running_loop().time() + 2.0
    while (eng._live is None or eng._live.last_pose is None
           or eng._live.last_pose.position.x != float(N - 1)):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("位姿事件没被引擎收完")
        await asyncio.sleep(0)
    # 队列是单消费者、严格 FIFO:最后一条位姿(i = N - 1)已经被处理完,
    # 说明它之前排队的每一条电量事件也早处理完了,不用再单独等一拍。
    新增 = read_events(eng.archive.path)[before:]
    落盘的电量事件 = [e for e in 新增 if e.get("event") == "BatteryEvent"]
    assert len(落盘的电量事件) == 电量事件数, "电量事件不该被位姿的过滤一起挡掉"
    assert len(新增) == 电量事件数, "位姿事件不该逐条落盘,落了 N 条就该跟着 N 涨"
    await eng.abort("测试收尾")
    await eng.wait_done(timeout_s=5.0)


async def test_busy_check炸了当成busy不放行(跑起来的引擎):
    """M2:busy check 回调是 ``app/`` 层注册的,炸了不能当成"没人占用"放行——

    一个抛错的检查意味着"不知道现在安不安全",而这道防线的全部意义就是
    "不确定就不放"。修的方向不许反:炸了要挡住继续,不是放行继续。
    """
    eng = 跑起来的引擎

    def 租约簿还没接好() -> str:
        raise RuntimeError("租约簿还没初始化")

    eng.add_busy_check(租约簿还没接好)
    await eng.suspend("人来开")
    await eng.wait_state(RunState.SUSPENDED)
    await eng.resume()
    await 等一拍(eng)
    assert eng.state is RunState.SUSPENDED, "回调炸了不许被当成没人占用"
    assert "安全检查出错" in eng.snapshot.reason
    assert "RuntimeError" in eng.snapshot.reason
    assert "租约簿还没接好" in eng.snapshot.reason


async def _等到拒绝(engine: MissionEngine, timeout_s: float = 2.0) -> list[dict]:
    """等到归档里出现 ``suspend_refused``。

    不用 ``等一拍``(等队列空)在这两条用例里不够:``_await_localized`` /
    ``_await_nav_standby`` 调用 ``_next`` 时传的是一个具体的剩余秒数,不是
    ``None``,``asyncio.wait_for`` 在这种情况下会另建一个 Task 才能拿到那条
    命令,比"队列空"这个信号多绕一圈。等的应该是拒绝这件事本身发生了没有,
    不是一个只在某条调用路径下才成立的旁证。
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        found = [e for e in read_events(engine.archive.path)
                 if e["kind"] == "suspend_refused"]
        if found:
            return found
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("没等到 suspend_refused")
        await asyncio.sleep(0)


async def test_定位中不能挂起(make_engine, nav):
    """S1:``LOCALIZING`` 期间没有一条安全路径能接住 resume 之后的
    ``_RetryWaypoint``(见 ``_SUSPEND_UNSAFE_STATES`` 的注释)—— 它会一路
    逃到 ``_run`` 的兜底,把整趟按"引擎内部异常"中止。所以 suspend 必须在
    这里就诚实拒绝,不是悄悄接受、等 resume 那一刻才炸。
    """
    # 起飞检查那一刻定位是好的(preflight 自己也查一遍 loc_status,查到的
    # 得是收敛的),进了 LOCALIZING 就掉了,而且再没收敛回来 —— 跟
    # test_machine.py 的 test_定位没收敛就等等不到就中止 是同一个办法。
    calls = {"n": 0}

    async def loc_status() -> LocStatus:
        calls["n"] += 1
        return LocStatus.CONTINUOUS_LOC if calls["n"] == 1 else LocStatus.LOC_LOST

    nav.loc_status = loc_status
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await engine.wait_state(RunState.LOCALIZING)
    await engine.suspend("门口有箱子")
    拒绝 = await _等到拒绝(engine)
    assert engine.state is RunState.LOCALIZING
    assert 拒绝[-1]["reason"] == "正在重定位,现在不能让开腿"
    await engine.abort("测试收尾")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_返航中不能挂起(make_engine, nav, device):
    """S2:``RETURNING`` 期间挂起,继续之后状态先闪回 RUNNING,
    ``_RetryWaypoint`` 会被 ``_go_home`` 的 except 接住按"返航失败"中止
    (见 ``_SUSPEND_UNSAFE_STATES`` 的注释)。同样是诚实拒绝,不是等
    resume 那一刻才炸。
    """
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(
        make_mission(policy=Policy(battery_abort_pct=15.0, battery_return_pct=25.0)),
        home=_HOME)
    await until(lambda: nav.goto_calls)
    nav.nav = NavStatus.ACTIVE           # 现在才卡住 _await_nav_standby,
                                          # 不能提前设 —— 那样第一个点的
                                          # goto 自己都发不出去
    device.emit(BatteryEvent(percent=20.0))          # 返航线 25,中止线 15
    await engine.wait_state(RunState.RETURNING)
    await engine.suspend("门口有箱子")
    拒绝 = await _等到拒绝(engine)
    assert engine.state is RunState.RETURNING
    assert 拒绝[-1]["reason"] == "正在返航,现在不能让开腿"
    await engine.abort("测试收尾")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


# ---------------------------------------------------------------- 评审第二轮 N1/N2/PAUSED


async def test_挂起被拒也广播出去(make_engine, nav):
    """N1:``suspend_refused`` 原来只走 ``_note``,没走 ``_publish`` ——
    落进了 events.jsonl,却没广播给订阅方。旁边 ``resume_refused``
    (``_suspend_until_resumed`` 里)两步都做了。只落档不广播等于没说出口:
    现场的人在手机上点「让开腿」,界面上什么都不动,只会以为按钮坏了,
    反复点。事后要有人翻 events.jsonl 才知道当时是被拒了。

    钉的是**广播**这一步,不是只钉 ``_note`` 落的那条事件——S1/S2 已经
    钉过事件和状态没变,这里改钉 ``eng.snapshot.reason``:它只在
    ``_publish`` 被调用之后才会变,单靠 ``_note`` 动不了它。
    """
    calls = {"n": 0}

    async def loc_status() -> LocStatus:
        calls["n"] += 1
        return LocStatus.CONTINUOUS_LOC if calls["n"] == 1 else LocStatus.LOC_LOST

    nav.loc_status = loc_status
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await engine.wait_state(RunState.LOCALIZING)
    await engine.suspend("门口有箱子")
    await _等到拒绝(engine)
    assert engine.snapshot.reason == "正在重定位,现在不能让开腿"
    await engine.abort("测试收尾")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_暂停时喊挂起也说清楚为什么不行(跑起来的引擎):
    """暂停中调 ``suspend()`` 原来是静默吞掉:``_pause_until_resumed`` 自己
    那条读命令的 while 只认 abort/resume,``suspend`` 落进兜底的
    ``continue``,连事件都不落——比 N1 还彻底,连"落档但不广播"都算不上。
    人点了「让开腿」界面上什么反应都没有,只会以为按钮坏了。

    **这里只要求「说得出口」,不改受理与否的判断。** 暂停中该不该放行人工
    接管是产品决策,不是这条测试要证的事(挂在 Task 14)。
    """
    eng = 跑起来的引擎
    await eng.pause()
    await eng.wait_state(RunState.PAUSED)
    await eng.suspend("门口有箱子")
    拒绝 = await _等到拒绝(eng)
    assert eng.state is RunState.PAUSED
    assert 拒绝[-1]["reason"] == "正在暂停,现在不能让开腿"
    assert eng.snapshot.reason == "正在暂停,现在不能让开腿"
