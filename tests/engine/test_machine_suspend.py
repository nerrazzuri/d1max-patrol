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

from d1max_patrol.backends.base import BatteryEvent, DevicePoseEvent, NavStatusEvent
from d1max_patrol.engine.archive import read_events
from d1max_patrol.engine.machine import (
    RETURN_TIMEOUT_S,
    MissionEngine,
    RunState,
)
from d1max_patrol.engine.mission import Policy
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

from .conftest import _HOME, NEVER, make_mission, until, 原点

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
async def 重定位中的引擎(make_engine, nav):
    """卡在 ``LOCALIZING`` 里等定位收敛、而且再也收敛不回来的引擎。

    起飞检查那一刻定位是好的(preflight 自己也查一遍 ``loc_status``,查到的
    得是收敛的),进了 ``LOCALIZING`` 就掉了 —— 跟 test_machine.py 的
    ``test_定位没收敛就等等不到就中止`` 是同一个办法。三条用例(S1、N1、
    任务 13 的"仍然拒绝")原来各抄了一遍这段,现在合到这儿。
    """
    calls = {"n": 0}

    async def loc_status() -> LocStatus:
        calls["n"] += 1
        return LocStatus.CONTINUOUS_LOC if calls["n"] == 1 else LocStatus.LOC_LOST

    nav.loc_status = loc_status
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await engine.wait_state(RunState.LOCALIZING)
    yield engine
    if engine.running:
        await engine.abort("测试收尾")
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


@pytest.fixture
async def 返航中的引擎(make_engine, nav, device):
    """已经转进 ``RETURNING``、``return_home`` 已经下发、卡在"等这段返航有
    结果"上的引擎(任务 13)。

    比 ``跑起来的引擎`` 多走一步:先让第一个点的 ``goto`` 发出去
    (``on_goto = NEVER``,狗停在半路),再喂一条低电量事件把它推进返航。

    **``on_return_home = NEVER`` 是关键。** 默认那份会立刻推一条 ``Succeed``
    回来,引擎一拍就跑完 DONE,根本没有「返航途中」这段时间可以去 suspend。
    最后等 ``home_calls`` 涨,是为了确保停在 ``_wait_nav_terminal`` 而不是
    还没走到 ``return_home`` —— 这两处都在 ``RETURNING`` 状态里,只看状态
    分不出来。
    """
    nav.on_goto = NEVER
    nav.on_return_home = NEVER
    engine = make_engine()
    await engine.start(
        make_mission(policy=Policy(battery_abort_pct=15.0, battery_return_pct=25.0)),
        home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(BatteryEvent(percent=20.0))          # 返航线 25,中止线 15
    await engine.wait_state(RunState.RETURNING)
    await until(lambda: nav.home_calls)
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


async def test_定位中不能挂起(重定位中的引擎):
    """S1:``LOCALIZING`` 期间没有一条安全路径能接住 resume 之后的
    ``_RetryWaypoint``(见 ``_SUSPEND_UNSAFE_STATES`` 的注释)—— 它会一路
    逃到 ``_run`` 的兜底,把整趟按"引擎内部异常"中止。所以 suspend 必须在
    这里就诚实拒绝,不是悄悄接受、等 resume 那一刻才炸。
    """
    engine = 重定位中的引擎
    await engine.suspend("门口有箱子")
    拒绝 = await _等到拒绝(engine)
    assert engine.state is RunState.LOCALIZING
    assert 拒绝[-1]["reason"] == "正在重定位,现在不能让开腿"


# ---------------------------------------------------------------- 评审第二轮 N1/N2/PAUSED


async def test_挂起被拒也广播出去(重定位中的引擎):
    """N1:``suspend_refused`` 原来只走 ``_note``,没走 ``_publish`` ——
    落进了 events.jsonl,却没广播给订阅方。旁边 ``resume_refused``
    (``_suspend_until_resumed`` 里)两步都做了。只落档不广播等于没说出口:
    现场的人在手机上点「让开腿」,界面上什么都不动,只会以为按钮坏了,
    反复点。事后要有人翻 events.jsonl 才知道当时是被拒了。

    钉的是**广播**这一步,不是只钉 ``_note`` 落的那条事件——S1/S2 已经
    钉过事件和状态没变,这里改钉 ``eng.snapshot.reason``:它只在
    ``_publish`` 被调用之后才会变,单靠 ``_note`` 动不了它。
    """
    engine = 重定位中的引擎
    await engine.suspend("门口有箱子")
    await _等到拒绝(engine)
    assert engine.snapshot.reason == "正在重定位,现在不能让开腿"


async def test_暂停时喊挂起不再产生拒绝事件(跑起来的引擎):
    """这条原来钉的是"暂停中喊挂起被拒、但拒绝要说得出口"——那时"暂停中
    要不要放行人工接管"还没有产品决策(挂在 Task 14)。

    人拍的板 1(2026-09-09)推翻了那个"待定":暂停已经是人在主导,再拦一道
    是官僚。决策落地之后这条不再拒绝,改钉「不再拒绝」这件事本身:
    ``events.jsonl`` 里不该再多出一条 ``suspend_refused``。转进 SUSPENDED
    的完整覆盖(状态、挂起点位、闸)见前面 ``test_暂停中点让开腿能进挂起``和
    ``test_从暂停进挂起之后遥控的闸放行`` —— 跟这里对照着看,正好是
    LOCALIZING/RETURNING(S1/S2,仍然拒绝、仍然落 ``suspend_refused``)的
    反例。
    """
    eng = 跑起来的引擎
    before = len(read_events(eng.archive.path))
    await eng.pause()
    await eng.wait_state(RunState.PAUSED)
    await eng.suspend("门口有箱子")
    await eng.wait_state(RunState.SUSPENDED)
    新增 = read_events(eng.archive.path)[before:]
    assert not any(e["kind"] == "suspend_refused" for e in 新增)


async def test_暂停中点让开腿能进挂起(跑起来的引擎):
    """人拍的板 1(2026-09-09):暂停已经是人在主导,再拦一道是官僚。"""
    eng = 跑起来的引擎
    await eng.pause()
    await eng.wait_state(RunState.PAUSED)

    await eng.suspend("人要过去挪箱子")
    await eng.wait_state(RunState.SUSPENDED)

    # 挂起点位必须记下来 —— 这是 §5.6 的硬要求,不是顺手
    snap = eng.snapshot
    assert snap.suspended_at is not None
    assert snap.suspended_at.reason == "人要过去挪箱子"


async def test_从暂停进挂起之后遥控的闸放行(跑起来的引擎):
    """闸的文字不动(裁决四):放行的判据仍然是 yielding,不是 paused。"""
    eng = 跑起来的引擎
    await eng.pause()
    await eng.wait_state(RunState.PAUSED)
    assert eng.yielding is False          # 暂停时闸是关的

    await eng.suspend("接管")
    await eng.wait_state(RunState.SUSPENDED)
    assert eng.yielding is True           # 挂起后才开


async def test_让开腿之前先把导航停掉(跑起来的引擎, nav):
    """**这一条正是第 7 卷的靶心。**

    ``app/teleop.py`` 的 ``pulse()`` 写着「任务在跑,先停任务再遥控 ——
    两边一起动是在抢腿」,而手机遥控唯一被放行的时机,就是引擎进了
    ``SUSPENDED`` 之后(``yielding``)。也就是说 ``SUSPENDED`` 一到,人的
    摇杆就开了闸。这时候导航那一路 ``goto`` 要是还挂着,狗还在朝原来那个
    点位走 —— 遥控发的 ``walk`` 和导航发的速度指令同时压在同一台底盘上,
    正是那句话说的抢腿,只不过换成了引擎自己去抢。

    **断的是相对量(``> before``),不是 ``>= 1``。** 起跑到这一刻为止流程里
    已经有过别的 ``stop``,``>= 1`` 是一句恒真的空话(形状 1)。

    **等的是 ``stop_calls`` 涨,不是 ``wait_state(SUSPENDED)``。**
    停导航排在状态翻页**后面**:等状态的话,这句断言会在 ``_stop_nav_quietly``
    还没跑到的那一瞬间就跑,红得跟真漏了一模一样。
    """
    eng = 跑起来的引擎
    before = nav.stop_calls
    await eng.suspend("手机接管:门口有箱子")
    await until(lambda: nav.stop_calls > before)
    assert eng.state is RunState.SUSPENDED


# --------------------------------------------------------- 人拍的板 2(2026-09-10)
#
# 接管完点继续,按狗当下在不在地图里分两条:不在 —— 直接报错,明说「狗不在
# 地图里,找不到下一个点」,不猜、不试着走、不静默中止;在 —— 就接着跑下
# 一个节点,不提示、不二次确认。报错不是中止:引擎留在 SUSPENDED,人还能把
# 狗牵回地图里再点一次继续。


async def test_接管完狗不在地图里点继续会报错(跑起来的引擎, nav):
    """人拍的板 2:不在地图里 —— 直接报错,不猜、不试着走、不静默中止。"""
    eng = 跑起来的引擎
    await eng.suspend("人要接管")
    await eng.wait_state(RunState.SUSPENDED)

    nav.emit_loc(LocStatus.LOC_LOST)          # 人把狗开出了地图
    await until(lambda: eng.loc_lost)

    await eng.resume()
    await until(lambda: "不在地图里" in eng.snapshot.reason)
    # 状态不许离开 SUSPENDED —— 报错不等于中止,人还能补救
    assert eng.state is RunState.SUSPENDED
    assert "找不到下一个点" in eng.snapshot.reason


async def test_接管完狗还在地图里点继续就接着跑(跑起来的引擎, nav):
    """人拍的板 2:在地图里 —— 就接着跑下一个节点,不提示、不二次确认。"""
    eng = 跑起来的引擎
    await eng.suspend("人要接管")
    await eng.wait_state(RunState.SUSPENDED)
    nav.emit_loc(LocStatus.CONTINUOUS_LOC)

    before = len(nav.goto_calls)
    await eng.resume()
    # 不用 wait_state(RUNNING):跑起来的引擎在开跑那一刻就先经过一次
    # RUNNING,那个状态早就进过 _seen,wait_state 对"已经出现过"的状态会
    # 立刻返回,根本没让事件循环把 resume 处理掉(同样的坑见前面
    # test_继续之后挂起快照清掉 的说明)。等一个具体可观察的事实:继续
    # 之后会重发当前点,也就是 goto_calls 会涨。
    await until(lambda: len(nav.goto_calls) > before)
    assert eng.state is RunState.RUNNING
    assert "不在地图里" not in eng.snapshot.reason     # 没有提示串混进来


async def test_报错之后定位回来了还能继续(跑起来的引擎, nav):
    """报错是可补救的:人把狗牵回地图里,再点继续就该走。"""
    eng = 跑起来的引擎
    await eng.suspend("人要接管")
    await eng.wait_state(RunState.SUSPENDED)
    nav.emit_loc(LocStatus.LOC_LOST)
    await until(lambda: eng.loc_lost)
    await eng.resume()
    await until(lambda: "不在地图里" in eng.snapshot.reason)

    nav.emit_loc(LocStatus.CONTINUOUS_LOC)
    await until(lambda: not eng.loc_lost)
    before = len(nav.goto_calls)
    await eng.resume()
    await until(lambda: len(nav.goto_calls) > before)
    assert eng.state is RunState.RUNNING


async def test_挂起理由不被拒绝理由盖掉(跑起来的引擎, nav):
    """人再看快照要想得起来当初为什么停在这儿。"""
    eng = 跑起来的引擎
    await eng.suspend("人要过去挪箱子")
    await eng.wait_state(RunState.SUSPENDED)
    nav.emit_loc(LocStatus.LOC_LOST)
    await until(lambda: eng.loc_lost)
    await eng.resume()
    await until(lambda: "不在地图里" in eng.snapshot.reason)
    assert "人要过去挪箱子" in eng.snapshot.reason


async def test_继续之后规划不出路的话跟不在地图里说的不一样(make_engine, nav):
    """规划失败(被挪到隔断另一侧之类)跟定位丢失不是一回事:定位好好的、
    狗确实在地图里,只是从这个位置规划不出到下一个点的路,``LOC_LOST``
    根本不会亮。两句话不该混在一起 —— 现场的人要靠这句话判断"该去找狗"
    还是"该去看这个点位是不是走不通了"。

    (这条区分是这个任务的实现读法,不是产品拍的板,见 brief 说明。)
    """
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(
        make_mission(policy=Policy(on_waypoint_failed="abort")), home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.suspend("人来开")
    await engine.wait_state(RunState.SUSPENDED)

    nav.on_goto = [NavStatusEvent(NavStatus.FAILED)]
    await engine.resume()
    await engine.wait_done(timeout_s=5.0)

    assert engine.state is RunState.ABORTED
    assert "到不了下一个点" in engine.snapshot.reason
    assert "不在地图里" not in engine.snapshot.reason
    await engine.aclose()


# ------------------------------------------------------- 任务 13:返航途中也能让开腿
#
# 挂账 56。第 7 卷把 RETURNING 放进 _SUSPEND_UNSAFE_STATES「诚实地拒绝」,当时
# 是对的 —— 一条说得出口的拒绝,比一趟悄悄中止的任务好太多。但产品上,返航
# 路上人想把狗牵开是个真需求(走廊被堵、地上有水),不做的代价是现场遇到这
# 一幕只能中止整趟重跑。
#
# 放行的前提是挂账 56 自己写的那条真解:接管完点继续,必须**从狗现在停的地
# 方重新规划回家**,不是接着跑发起返航时那一次 return_home。下面第二条钉的
# 就是这一句 —— 没有它,这个任务等于没做。
#
# LOCALIZING 不在本任务范围内,仍然拒绝(见最后一条)。


async def test_返航途中能让开腿(返航中的引擎):
    """这一条是 ``test_返航中不能挂起`` 的原地翻面:同样的场景、相反的结论。

    翻面的依据是产品决策,不是测试迁就实现 —— 拒绝那一版的理由("继续之后
    ``_RetryWaypoint`` 没人接")在下面第二条把接的人补上之后就不成立了。
    """
    eng = 返航中的引擎
    await eng.suspend("走廊被堵了")
    await eng.wait_state(RunState.SUSPENDED)
    assert eng.state is RunState.SUSPENDED
    assert eng.snapshot.suspended_at is not None


async def test_返航中接管完继续_重新规划回家而不是接着走老路(返航中的引擎, nav):
    """挂账 56 的真解。接着走老路 = 从一个没人知道的位置照原计划走。

    人接管的那几分钟里狗已经被开到别处了,发起返航时那一次 ``return_home``
    是从**当时**那个位置算出来的。继续时若只是接着等它的结果,狗要么原地不
    动等一个永远不来的终态,要么按一条从别处起算的路走 —— 两种都是现场最难
    归因的那类失败。

    **等的是"下发流水又长出东西来",不是 ``wait_state(RETURNING)``。**
    ``wait_state`` 等的是"出现过",而 ``RETURNING`` 在挂起之前就已经进过
    ``_seen``,对它调用会立刻返回,根本没让事件循环把 ``resume`` 处理掉 ——
    那样这句断言会在流水还空着的时候就跑(同样的坑见前面
    ``test_继续之后挂起快照清掉`` 的说明)。

    **"没有中途闪成 RUNNING"这句话是记着广播序列断的,不是拿末态断的。**
    原来这儿写的是 ``assert eng.state is RunState.RETURNING`` 加一句
    ``# 也没有中途闪成 RUNNING`` 的注释 —— 闪一下再翻回来它照样绿,断言比
    注释小了一整圈。订阅一路广播、把 ``state`` 收下来,闪变才真的抓得到。
    收队列的时机是安全的:``_go_home`` 那一圈是先 ``_transition(RETURNING)``
    再 ``return_home()``,所以流水一长出东西,该看的广播早就进队列了。
    """
    eng = 返航中的引擎
    await eng.suspend("走廊被堵了")
    await eng.wait_state(RunState.SUSPENDED)
    nav.清空下发记录()
    nav.emit_loc(LocStatus.CONTINUOUS_LOC)           # 人把狗牵回来了,还在图里

    with eng.subscription() as 广播:
        await eng.resume()
        await until(lambda: nav.下发过的目标点)
        assert nav.下发过的目标点[-1] == 原点         # 重新发了一次,不是续跑
        assert eng.state is RunState.RETURNING
        状态序列 = []
        while not 广播.empty():
            状态序列.append(广播.get_nowait().state)
    # 先断非空,不然上面那句"RUNNING 不在里面"在广播队列本来就是空的时候
    # 也恒真 —— 那就什么都没测到。
    assert RunState.RETURNING in 状态序列, f"广播序列是空的或没有 RETURNING: {状态序列}"
    assert RunState.RUNNING not in 状态序列, f"中途闪过 RUNNING: {状态序列}"


async def test_返航中接管完狗不在地图里_照样报错(返航中的引擎, nav):
    """人拍的板 2 对返航这条路同样成立:不在地图里就直接报错,不猜、不试着
    走、不静默中止。报错不是中止 —— 引擎留在 SUSPENDED,人还能把狗牵回图里
    再点一次继续。
    """
    eng = 返航中的引擎
    await eng.suspend("走廊被堵了")
    await eng.wait_state(RunState.SUSPENDED)
    nav.emit_loc(LocStatus.LOC_LOST)
    await until(lambda: eng.loc_lost)

    await eng.resume()
    await until(lambda: "不在地图里" in eng.snapshot.reason)
    assert eng.state is RunState.SUSPENDED


async def test_返航中接管完还能直接中止(返航中的引擎):
    """挂起是可以收场的,返航这条路也不例外 —— 人接管到一半发现这趟没法收,
    得能直接中止,而不是被卡在一个只能"继续"的状态里。

    钉这一条是因为 ``_go_home`` 现在多了一条 ``_ResumeReturnHome`` 的重来路
    径:重来的那一支要是把 ``_AbortRun`` 一起吞了,人就再也停不下这条狗,而
    上面三条测试全都照样绿。
    """
    eng = 返航中的引擎
    await eng.suspend("走廊被堵了")
    await eng.wait_state(RunState.SUSPENDED)
    await eng.abort("现场不具备条件")
    assert await eng.wait_done(timeout_s=5.0) is RunState.ABORTED


async def test_重定位期间仍然拒绝(重定位中的引擎):
    """``LOCALIZING`` 不在本任务范围内 —— 它是另一个病:``_RetryWaypoint``
    会从 ``_await_localized`` 漏给 ``_run`` 的兜底,被当成引擎内部错误直接
    中止。而且重定位期间人接管本来也没什么意义,狗还不知道自己在哪儿。

    跟前面 S1 那条不重复:S1 钉的是归档里那条 ``suspend_refused`` 事件,
    这里钉的是**移走 RETURNING 之后这张表没有被顺手清空** —— 断的是
    ``snapshot.reason`` 上那句话还在。
    """
    eng = 重定位中的引擎
    await eng.suspend("试试")
    await until(lambda: "正在重定位" in eng.snapshot.reason)
    assert eng.state is RunState.LOCALIZING


# ------------------------------------------------- 任务 13 修复轮 1(必修 1/3/4/6)
#
# 上面那组测试盖住的是"返航中直接让开腿"这一条路。修复轮 1 补的是它旁边那几
# 条侧门:从暂停里进挂起(``from_state`` 记的是 PAUSED 而不是 RETURNING)、
# ``return_home`` 还没发出去的那个停靠点、以及反复短接管累计耗掉的时间。


async def test_返航中暂停再让开腿再继续_也重新规划回家(返航中的引擎, nav):
    """**任务 13 要杀的那一幕,从侧门原样走了回来。**

    整条链每一步今天都成立:狗在 ``RETURNING`` 卡在 ``_wait_nav_terminal``
    里等结果,人按暂停 —— 那两个等待循环都照常 ``_handle`` 事件,一路走到
    ``_pause_until_resumed``,状态变 ``PAUSED``;人在暂停里再点让开腿 ——
    ``PAUSED`` 不在 ``_SUSPEND_UNSAFE_STATES`` 里,挂得起来,而
    ``SuspendPoint.from_state`` 记下的是 ``PAUSED``,不是 ``RETURNING``;人点
    继续 —— 走的是重发点位那一支,抛 ``_RetryWaypoint``,一路漏到 ``_go_home``
    的 except 里,**整趟按「返航失败」中止**。

    人做的事跟 ``test_返航中接管完继续_重新规划回家而不是接着走老路`` 一模一
    样,只是中间多按了一次暂停,结果却是这一趟没了 —— 现场最难归因的那种
    失败。所以这一条断的是同一句话:重新规划回家,而且不许中止。
    """
    eng = 返航中的引擎
    await eng.pause()
    await eng.wait_state(RunState.PAUSED)
    await eng.suspend("地上有水,人把狗牵到一边")
    await eng.wait_state(RunState.SUSPENDED)
    点 = eng.snapshot.suspended_at
    assert 点 is not None
    assert 点.from_state is RunState.PAUSED, "侧门这一幕的前提没摆出来"
    nav.清空下发记录()

    await eng.resume()
    await until(lambda: nav.下发过的目标点)
    assert nav.下发过的目标点[-1] == 原点, "没重新规划回家"
    # 中止是立刻发生的(``_do_abort`` 里没有等待),给半秒的窗口足够看清。
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await eng.wait_done(timeout_s=0.5)
    assert eng.state is RunState.RETURNING, eng.snapshot.reason
    assert "返航失败" not in eng.snapshot.reason, eng.snapshot.reason


async def test_返航还没发出去就让开腿_继续之后照样发得出去(make_engine, nav, device):
    """停靠点是 ``_await_nav_standby`` —— ``return_home`` **还没发出去**。

    ``RETURNING`` 里有两个等待点,只看状态分不出来。旧测试
    ``test_返航中不能挂起`` 站的就是这一个(``nav.nav = ACTIVE``),它的结论
    在任务 13 里被翻了面,但**停靠点不能跟着结论一起丢**:翻面之后这一个点
    零覆盖,而 ``_go_home`` 重来那一圈恰恰要重新走一遍它。

    ``nav.nav = ACTIVE`` 不能提前设:那样第一个点的 ``goto`` 自己都发不出去。
    """
    nav.on_goto = NEVER
    nav.on_return_home = NEVER
    eng = make_engine()
    await eng.start(
        make_mission(policy=Policy(battery_abort_pct=15.0, battery_return_pct=25.0)),
        home=_HOME)
    try:
        await until(lambda: nav.goto_calls)
        nav.nav = NavStatus.ACTIVE                   # 现在才卡住 _await_nav_standby
        device.emit(BatteryEvent(percent=20.0))      # 返航线 25,中止线 15
        await eng.wait_state(RunState.RETURNING)
        await eng.suspend("门口有箱子")
        await eng.wait_state(RunState.SUSPENDED)
        assert nav.home_calls == 0, "停靠点站错了:return_home 已经发出去了"
        assert eng.snapshot.suspended_at is not None

        nav.nav = NavStatus.STANDBY                  # 人挪完了,导航也回落了
        await eng.resume()
        await until(lambda: nav.home_calls)
        assert eng.state is RunState.RETURNING
    finally:
        if eng.running:
            await eng.abort("测试收尾")
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await eng.wait_done(timeout_s=5.0)
        await eng.aclose()


async def test_让开腿盖的点位序号不越界(返航中的引擎):
    """``waypoint_name`` 夹了、``waypoint_index`` 没夹。

    返航段上"当前点位"这个概念本来就不成立,越界的那个序号会顺着
    ``to_wire`` 一路流到手机上,变成一个不存在的点。

    直接改 ``_live.index`` 是明知故犯:今天引擎自己没有哪条路把它推出界,
    这条越界只能造出来。夹取是一行的事,而没有它的话,哪天真有一条路把序号
    推出界(多一层循环、返航段自己记个数),露面的方式是页面上一个错的点位,
    不是一条异常 —— 没有任何测试会红。
    """
    eng = 返航中的引擎
    总数 = len(eng._live.mission.waypoints)
    eng._live.index = 99
    await eng.suspend("走廊被堵了")
    await eng.wait_state(RunState.SUSPENDED)
    点 = eng.snapshot.suspended_at
    assert 点 is not None
    assert 点.waypoint_index == 总数 - 1, 点.waypoint_index
    assert 点.waypoint_name == eng._live.mission.waypoints[-1].name


async def test_同一趟返航里反复短接管_挂起时长会累计(返航中的引擎, nav, clock):
    """**反复短接管会让狗永远回不了家,而且零告警。**

    ``_go_home`` 那个 ``while True`` 没有计数器也没有上限,每圈还把
    ``RETURN_TIMEOUT_S`` 重算;挂起期间电量事件只留一份底不触发中止;看门狗
    只在两次接管之间那几秒的缝里才有机会开火。于是狗在"回家 → 被拉开 →
    回家 → 被拉开"里耗到没电,而每一次接管都短于十分钟,``suspend_stale``
    那条 P1 一次都不报。

    **处置是只补可见性、不补拒绝**(封顶等于在人最需要把狗拉到一边的时候拒
    绝他),而可见性靠的是累计时长而不是次数 —— 耗电的是时间不是次数。这一
    条钉引擎这一半:账要加起来。判定那一半在
    ``app/server.py::_挂起超时了``,守卫是
    ``tests/app/test_suspend_e2e.py::test_反复短接管按累计算不按单次算``。

    时钟是注进来的那份(``clock.offset``),不是 sleep 四分钟。
    """
    eng = 返航中的引擎
    四分钟_s = 4 * 60.0
    记下的: list[int] = []
    for n in range(3):
        await eng.suspend(f"第 {n + 1} 次被拉到一边")
        # 第二圈之后 SUSPENDED 早就进过 ``_seen``,``wait_state`` 会立刻返回,
        # 只能等"此刻是"。
        await until(lambda: eng.state is RunState.SUSPENDED)
        点 = eng.snapshot.suspended_at
        assert 点 is not None
        记下的.append(点.prior_suspend_ms)
        clock.offset += 四分钟_s                     # 人接管了四分钟
        nav.清空下发记录()
        await eng.resume()
        await until(lambda: nav.下发过的目标点)      # 重新规划回家了
        await until(lambda: eng.state is RunState.RETURNING)

    assert 记下的[0] == 0, "第一次让开腿之前这一趟还没挂起过"
    # ``>=`` 而不是 ``==``:累计量是拿 ``clock()`` 的差算的,里面还夹着真
    # monotonic 走掉的那几毫秒。上界拦的是"多加了一圈"这种错。
    assert 4 * 60_000 <= 记下的[1] < 5 * 60_000, 记下的
    assert 8 * 60_000 <= 记下的[2] < 9 * 60_000, 记下的


async def test_新一趟返航开始时挂起累计清零(make_engine, nav, device, clock):
    """账按**这一趟返航**算,不跨趟累加。

    跑点位的时候人正常接管过一会儿,那几分钟不该记在返航的账上 —— 记了的话
    返航路上第一次让开腿就可能立刻挨一条 P1,而人很快就学会无视一个总在误报
    的 P1(``SUSPEND_STALE_MS`` 注释里那段下界论证)。
    """
    nav.on_goto = NEVER
    nav.on_return_home = NEVER
    eng = make_engine()
    await eng.start(
        make_mission(policy=Policy(battery_abort_pct=15.0, battery_return_pct=25.0)),
        home=_HOME)
    try:
        await until(lambda: nav.goto_calls)
        await eng.suspend("跑点位的时候先接管一次")
        await until(lambda: eng.state is RunState.SUSPENDED)
        clock.offset += 4 * 60.0
        await eng.resume()
        await until(lambda: eng.state is RunState.RUNNING)
        assert eng._live.suspend_total_ms >= 4 * 60_000, \
            "这一次接管压根没记账,下面那个 0 是恒真的空话"

        device.emit(BatteryEvent(percent=20.0))      # 返航线 25,中止线 15
        await eng.wait_state(RunState.RETURNING)
        await until(lambda: nav.home_calls)
        await eng.suspend("返航路上又被拉开")
        await until(lambda: eng.state is RunState.SUSPENDED)
        点 = eng.snapshot.suspended_at
        assert 点 is not None
        assert 点.prior_suspend_ms == 0, \
            f"跑点位那一段的接管被算进返航的账里了: {点.prior_suspend_ms}"
    finally:
        if eng.running:
            await eng.abort("测试收尾")
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await eng.wait_done(timeout_s=5.0)
        await eng.aclose()


async def test_跨趟真的不带账(make_engine, nav, device, clock):
    """顺手 1:上一条 (``test_新一趟返航开始时挂起累计清零``) 走的是同一趟内
    「跑点位阶段 → 返航阶段」的清零,``eng.start()`` 只调过一次 —— 没有任何
    一条测试真的跨过 ``start()`` 走一遍。

    这条走真路径:第一趟里攒出一笔非零的 ``suspend_total_ms``、把那一趟
    中止收尾,再用**同一个引擎对象**开第二趟(真调 ``eng.start()``,不直接
    造 ``_Live``),断言第二趟一开始账是 0。

    **为什么不是同义反复:** 光断言"新引擎的 ``suspend_total_ms`` 是 0"
    只是在断言 dataclass 默认值管用(见 ``machine.py:641`` 边上的引用)。
    这条先把第一趟的账攒成非零,再跨 ``start()`` 检查第二趟是不是真的清零
    了 —— 如果哪天 ``_Live`` 被改成跨趟复用、或者 ``start()`` 手滑把上一份
    字段带了过去,这条会红;字段默认值本身没变,不会拿"默认值对"这件事
    继续骗过去。
    """
    nav.on_goto = NEVER
    eng = make_engine()
    try:
        # 第一趟:接管一次,攒出非零账。
        await eng.start(make_mission(), home=_HOME)
        await until(lambda: nav.goto_calls)
        await eng.suspend("第一趟里接管一次")
        await until(lambda: eng.state is RunState.SUSPENDED)
        clock.offset += 4 * 60.0
        await eng.resume()
        await until(lambda: eng.state is RunState.RUNNING)
        assert eng._live.suspend_total_ms >= 4 * 60_000, \
            "第一趟压根没记上账,后面那个 0 就没什么好证明的"
        await eng.abort("第一趟收尾")
        await eng.wait_done(timeout_s=5.0)

        # 第二趟:真的再调一次 start(),不是直接造 _Live。
        nav.清空下发记录()
        await eng.start(make_mission(), home=_HOME)
        await until(lambda: nav.goto_calls)
        assert eng._live.suspend_total_ms == 0, \
            f"第二趟一开始账就不是 0,跨趟带过来了: {eng._live.suspend_total_ms}"
    finally:
        if eng.running:
            await eng.abort("测试收尾")
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await eng.wait_done(timeout_s=5.0)
        await eng.aclose()


async def test_重来那一圈的返航超时预算是新算的(返航中的引擎, nav, clock):
    """人接管的时间比一整个 ``RETURN_TIMEOUT_S`` 还长,重来那一圈得拿一份新
    预算,不是接着用发起返航时那一份。

    没有这一条,"每圈重算超时预算"在整组测试里**无人守**:假后端恒 StandBy、
    假时钟不推进,把 ``deadline`` 提到 ``while`` 外面照样全绿。现场的后果是
    "人接管久了,点继续之后立刻判返航超时",整趟按返航失败中止 —— 恰恰是
    任务 13 要消掉的那个结局,换了个触发条件。

    断言落在"没中止"上而不是"重发了一次":``return_home()`` 在
    ``_wait_nav_terminal`` **之前**,预算就算是旧的,那一次下发也照样发得
    出去 —— 只断流水的话这条测试是恒真的。
    """
    eng = 返航中的引擎
    await eng.suspend("走廊被堵了,人要挪很久")
    await until(lambda: eng.state is RunState.SUSPENDED)
    clock.offset += RETURN_TIMEOUT_S + 60.0
    nav.清空下发记录()

    await eng.resume()
    await until(lambda: nav.下发过的目标点)
    assert nav.下发过的目标点[-1] == 原点
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await eng.wait_done(timeout_s=0.5)
    assert eng.state is RunState.RETURNING, eng.snapshot.reason
    assert "返航失败" not in eng.snapshot.reason, eng.snapshot.reason
