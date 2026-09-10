"""把告警接到已有的事实源上(§5.2 那张表的左半边)。

这一整份测的是同一件事的两面:**该报的报得出来**,以及**不该报的不许报**。
后者才是这一层真正难的地方 —— 引擎快照每约 0.5 秒重建一次,内容常常一模
一样,而 ``raise_alert`` 不是幂等的。把每一份快照直接喂进去,一次卡住会在
聚合窗口里累成几十次,真要紧的那条就被自己埋了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from d1max_patrol.app.alert_sources import AlertSources
from d1max_patrol.app.server import AppContext, _StateHub
from d1max_patrol.backends.base import (
    BatteryEvent,
    ControlLostEvent,
    FaultEvent,
    LocStatusEvent,
    NavStatusEvent,
)
from d1max_patrol.engine.alerts import LEVEL_OF, AlertBook, Level
from d1max_patrol.engine.machine import RunSnapshot, RunState, WaypointResult
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus
from tests.app.conftest import get_json

ROBOT = "dog-1"


class 假钟:
    """能拨的钟。这一层一律不许读真时间(§8.5 第 2 条)。"""

    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def 前进(self, ms: int) -> None:
        self.now_ms += ms


@dataclass
class 环境:
    """一套装好的告警源。多数测试只要 ``book`` 和 ``src``,所以支持

    ``book, src = 装好(...)`` 这种解包;要拨钟或改引擎状态的那几条再按
    字段取。
    """

    book: AlertBook
    src: AlertSources
    钟: 假钟
    盒: dict = field(default_factory=dict)

    def __iter__(self):
        yield self.book
        yield self.src

    def 引擎进(self, state: RunState) -> None:
        self.盒["state"] = state


def 装好(*, engine_state: RunState = RunState.RUNNING, now_ms: int = 1_000,
       disk=None, bundles_root=None, time_reference=None) -> 环境:
    """后三个参数是 ``on_tick`` 那三条周期事实的取值口。

    **留空就是"这条源没接上"**,那几个判定整个不动 —— 所以既有的几十条测试
    一个字都不用改,也不会因为跑测试的这台机器盘满了就凭空多一条 P2。
    """
    book = AlertBook()
    钟 = 假钟(now_ms)
    盒 = {"state": engine_state}
    src = AlertSources(book, robot=ROBOT, clock_ms=钟,
                       run_state=lambda: 盒["state"], disk=disk,
                       bundles_root=bundles_root,
                       time_reference=time_reference)
    return 环境(book=book, src=src, 钟=钟, 盒=盒)


def 失败点位(name: str, *, note: str = "没到") -> WaypointResult:
    return WaypointResult(name=name, ok=False, arrived_ms=0, elapsed_s=1.0,
                          photos=(), note=note)


def 成功点位(name: str) -> WaypointResult:
    return WaypointResult(name=name, ok=True, arrived_ms=0, elapsed_s=1.0,
                          photos=("a.jpg",))


def 造快照(*, state: RunState = RunState.RUNNING,
         results: list[WaypointResult] | None = None,
         reason: str = "", started_ms: int = 100,
         mission: str = "日常巡检") -> RunSnapshot:
    return RunSnapshot(state=state, mission=mission, waypoint_index=0,
                       waypoint_name="阀门A", total=3, started_ms=started_ms,
                       results=tuple(results or ()), reason=reason)


# ------------------------------------------------------------------ 定位丢失


def test_丢定位但没暂停_不报P1():
    """§5.2 写的是「定位丢失后暂停」,两个条件都要。

    光丢定位是常事(过个转角、单帧 TF 查不到),报了就是狼来了。
    """
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    assert book.open() == ()


def test_丢定位并且暂停了_报P1():
    book, src = 装好(engine_state=RunState.PAUSED)
    src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    assert [a.kind for a in book.open()] == ["loc_lost_paused"]
    assert book.open()[0].level is Level.P1


def test_先丢定位_引擎随后才暂停_照样报得出来():
    """两个事实分别从两条流上来,先后顺序不是我们能定的。

    安全模块是**收到 LocLost 之后**才让引擎暂停的,所以「先丢定位、后
    暂停」才是真机上的常态。只在 ``on_nav`` 那一刻判一次,这条最该报的
    P1 就永远报不出来 —— 而现场看到的是狗停在原地一晚上没人知道。
    """
    env = 装好(engine_state=RunState.RUNNING)
    env.src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    assert env.book.open() == ()

    env.引擎进(RunState.PAUSED)
    env.src.on_run(造快照(state=RunState.PAUSED))
    assert [a.kind for a in env.book.open()] == ["loc_lost_paused"]


def test_定位丢着不动_每拍快照不会各报一条():
    """暂停期间快照照样每拍重建,条件一直成立 —— 但事实只发生了一次。"""
    env = 装好(engine_state=RunState.PAUSED)
    env.src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    for _ in range(5):
        env.src.on_run(造快照(state=RunState.PAUSED))
    assert len(env.book.open()) == 1
    assert env.book.open()[0].count == 1


def test_定位回来了再丢一次_算新的一次():
    """恢复过就把记忆清掉,不然第二次真丢定位反而没声音。"""
    env = 装好(engine_state=RunState.PAUSED)
    env.src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    env.src.on_nav(LocStatusEvent(status=LocStatus.CONTINUOUS_LOC,
                                  previous=LocStatus.LOC_LOST))
    env.钟.前进(1_000)
    env.src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    assert len(env.book.open()) == 1
    assert env.book.open()[0].count == 2


def test_定位恢复又跑起来之后再丢一次_还认得出来():
    """跨趟也得认得出来:恢复 -> 新的一趟 -> 再丢一次,第二条照样报。

    钉的是那两份定位记忆**自己清得干净** —— ``on_nav`` 每条
    ``LocStatusEvent`` 都整个重赋 ``_last_loc_lost``,``_判定定位丢失``
    结尾那句 ``self._last_loc_lost_paused = 成立`` 是无条件的。所以哪怕
    换趟清零那块没管它们,第二趟上的丢失也认得出来。

    **它钉不住"别把这两份记忆加进换趟清零块"。** 真去加,这条照样绿:清是
    在 ``on_run(started_ms=2_000)`` 那一刻发生的,而紧跟着的
    ``on_nav(LOC_LOST)`` 又把 ``_last_loc_lost`` 重新置了回来 —— 清了个
    寂寞。真正守着那个方向的是
    ``test_先丢定位_引擎随后才暂停_照样报得出来``:那一条里
    ``on_nav`` 在前、``on_run`` 在后,记忆一旦被 ``on_run`` 清掉,
    ``loc_lost_paused`` 就报不出来了。

    (任务 6 评审:确认这处不对称是对的,不是漏了;代码不用动。)
    """
    env = 装好(engine_state=RunState.PAUSED)
    env.src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    env.src.on_run(造快照(state=RunState.PAUSED, started_ms=1_000))
    assert [a.kind for a in env.book.open()] == ["loc_lost_paused"]
    assert env.book.open()[0].count == 1

    # 定位回来了,人把这一趟收了,又开了新的一趟。
    env.src.on_nav(LocStatusEvent(status=LocStatus.CONTINUOUS_LOC,
                                  previous=LocStatus.LOC_LOST))
    env.引擎进(RunState.RUNNING)
    env.src.on_run(造快照(state=RunState.RUNNING, started_ms=2_000))

    # 新的一趟上又丢了一次,又暂停了。
    env.钟.前进(1_000)
    env.引擎进(RunState.PAUSED)
    env.src.on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    env.src.on_run(造快照(state=RunState.PAUSED, started_ms=2_000))
    丢定位的 = [a for a in env.book.open() if a.kind == "loc_lost_paused"]
    assert len(丢定位的) == 1
    assert 丢定位的[0].count == 2


def test_导航状态事件不掺和定位判定():
    book, src = 装好(engine_state=RunState.PAUSED)
    src.on_nav(NavStatusEvent(status=NavStatus.PAUSE, previous=None))
    assert book.open() == ()


# ------------------------------------------------------------------ 卡住


def test_同一次卡住不会每拍报一条():
    """引擎快照每 0.5 秒重建一次,认「事实」不能认成认「快照」。"""
    book, src = 装好(engine_state=RunState.RUNNING)
    snap = 造快照(results=[失败点位("阀门A")])
    src.on_run(snap)
    src.on_run(snap)
    src.on_run(snap)
    assert len(book.open()) == 1
    assert book.open()[0].count == 1        # count 也不许涨


def test_又一个点位失败了才再报一条():
    env = 装好(engine_state=RunState.RUNNING)
    env.src.on_run(造快照(results=[失败点位("阀门A")]))
    env.钟.前进(30_000)
    env.src.on_run(造快照(results=[失败点位("阀门A"), 失败点位("表计B")]))
    a = [x for x in env.book.open() if x.kind == "stuck"]
    assert len(a) == 1                      # 聚合成一条(§5.4)
    assert a[0].count == 2
    assert "表计B" in a[0].title


def test_成功的点位不报卡住():
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_run(造快照(results=[成功点位("阀门A"), 成功点位("表计B")]))
    assert [x.kind for x in book.open()] == []


def test_下一趟里同名点位再失败_还认得出来():
    """``results`` 是按趟清空的,记忆也得跟着按趟清 —— 不然第二趟同一个

    点位再卡住就被当成"上次那条",一声不响。
    """
    env = 装好(engine_state=RunState.RUNNING)
    env.src.on_run(造快照(results=[失败点位("阀门A")], started_ms=100))
    env.钟.前进(60_000)
    env.src.on_run(造快照(results=[失败点位("阀门A")], started_ms=999))
    a = [x for x in env.book.open() if x.kind == "stuck"]
    assert a[0].count == 2


# ------------------------------------------------------------------ 一趟的起止


def test_跑完了是P3不是P1():
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_run(造快照(state=RunState.DONE))
    assert book.open()[0].level is Level.P3


def test_开跑记一条P3():
    book, src = 装好(engine_state=RunState.IDLE)
    src.on_run(造快照(state=RunState.RUNNING))
    assert [x.kind for x in book.open()] == ["run_start"]
    assert book.open()[0].level is Level.P3


def test_暂停之后继续_不算又开跑了一趟():
    """``RUNNING -> PAUSED -> RUNNING`` 是同一趟。按状态迁移傻报,一趟

    里人按几次暂停就多出几条"开跑",交接班看到的是一份假的流水。
    """
    env = 装好(engine_state=RunState.IDLE)
    env.src.on_run(造快照(state=RunState.RUNNING))
    env.src.on_run(造快照(state=RunState.PAUSED))
    env.钟.前进(10_000)
    env.src.on_run(造快照(state=RunState.RUNNING))
    assert [x.kind for x in env.book.open()] == ["run_start"]
    assert env.book.open()[0].count == 1


def test_接线的时候引擎已经在跑_暂停再继续不算第二趟():
    """**接线时刻引擎正跑着**,这是任务 6 评审逮到的那个 bug 的现场。

    ``_last_state`` 开局就问了一次引擎(所以第一份 ``RUNNING`` 快照会在
    ``state is 上次`` 那一句早返),而"这一趟的开跑报过没有"那个记忆开局是
    硬编码的 ``False`` —— 唯一把它置 True 的那一行,正好在被早返跳过的那个
    分支里。于是 ``RUNNING -> PAUSED -> RUNNING`` 之后凭空多一条"开跑",交
    接班看到的是一份假的流水。

    **上面那条 ``test_暂停之后继续_不算又开跑了一趟`` 盖不住这个** —— 它从
    ``IDLE`` 起步,第一份 ``RUNNING`` 是一次真迁移,会走进那个分支把记忆置
    上,整条路径绕开了 bug。差别只在 ``engine_state=`` 那一个词。
    """
    env = 装好(engine_state=RunState.RUNNING)
    env.src.on_run(造快照(state=RunState.RUNNING, started_ms=100))
    env.src.on_run(造快照(state=RunState.PAUSED, started_ms=100))
    env.钟.前进(10_000)
    env.src.on_run(造快照(state=RunState.RUNNING, started_ms=100))
    assert [x for x in env.book.open() if x.kind == "run_start"] == []


def test_两趟各报一条开跑():
    env = 装好(engine_state=RunState.IDLE)
    env.src.on_run(造快照(state=RunState.RUNNING, started_ms=100))
    env.src.on_run(造快照(state=RunState.DONE, started_ms=100))
    env.钟.前进(60_000)
    env.src.on_run(造快照(state=RunState.RUNNING, started_ms=999))
    开跑 = [x for x in env.book.open() if x.kind == "run_start"]
    assert len(开跑) == 1 and 开跑[0].count == 2


# ------------------------------------------------------------------ 电量中止


def test_电量中止报battery_abort():
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_run(造快照(state=RunState.ABORTED,
                   reason="电量 22% 低于中止线 25%"))
    a = book.open()
    assert [x.kind for x in a] == ["battery_abort"]
    assert a[0].level is Level.P1


def test_中止但不是因为电量_不冒充battery_abort():
    """``battery_abort`` 说的是"没电了,得去把狗抱回来充电"。别的原因中止

    走的是别的处置,认错了就是把人往错的方向支 —— 所以关节过温这一趟报的是
    ``run_abort``,**一个字的"电量"都不许出现**。

    (这条原来断言的是"什么都不报"。任务 7 之前确实没有"泛泛中止"这个
    kind,而那意味着**没人在的时候整趟中止,狗在原地站到天亮一声不响** ——
    恰恰是最该响的那一类。现在两个 kind 都在 ``LEVEL_OF`` 里,这条测的就变
    成了"分得清",而不是"都不报"。)
    """
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_run(造快照(state=RunState.ABORTED, reason="SDK 致命故障: 关节过温"))
    a = book.open()
    assert [x.kind for x in a] == ["run_abort"]
    assert a[0].level is Level.P1
    assert "电量" not in a[0].title


def test_关节过温中止报run_abort不报battery_abort():
    """认不出原因不等于不报。**中止本身就是要人过去看的那一类。**"""
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_run(造快照(state=RunState.ABORTED, reason="SDK 致命故障: 关节过温"))
    kinds = [x.kind for x in book.open()]
    assert "run_abort" in kinds
    assert "battery_abort" not in kinds


def test_电量中止仍然报battery_abort不报run_abort():
    """反向那一半:加了泛泛中止之后,别把没电那条也吞进去。

    两条各测各的方向 —— 只测一边的话,把判据写反(``if 没电`` 写成
    ``if not 没电``)时仍然有一条是绿的。
    """
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_run(造快照(state=RunState.ABORTED,
                   reason="电量 22% 低于中止线 25%"))
    kinds = [x.kind for x in book.open()]
    assert "battery_abort" in kinds
    assert "run_abort" not in kinds


def test_泛泛中止摆着不动_只报一次():
    """``ABORTED`` 是个**终态**,快照会一直是这一份。"""
    env = 装好(engine_state=RunState.RUNNING)
    snap = 造快照(state=RunState.ABORTED, reason="导航连续三次失败")
    for _ in range(4):
        env.src.on_run(snap)
    a = [x for x in env.book.open() if x.kind == "run_abort"]
    assert len(a) == 1 and a[0].count == 1


def test_中止状态摆着不动_只报一次():
    env = 装好(engine_state=RunState.RUNNING)
    snap = 造快照(state=RunState.ABORTED, reason="电量 20% 低于中止线 25%")
    for _ in range(4):
        env.src.on_run(snap)
    assert env.book.open()[0].count == 1


# ------------------------------------------------------------------ 急停与跌倒


def test_急停按下报P1():
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_device(FaultEvent(items=("[level=2 code=7] 急停已触发",), fatal=True))
    a = book.open()
    assert [x.kind for x in a] == ["estop_pressed"]
    assert a[0].level is Level.P1


def test_英文写法的急停也认得():
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_device(FaultEvent(items=("[level=2 code=7] EStop engaged",), fatal=True))
    assert [x.kind for x in book.open()] == ["estop_pressed"]


def test_同一条急停故障重复推送_只算一次():
    """故障帧是**状态**,厂商会一直重推同一条。"""
    env = 装好(engine_state=RunState.RUNNING)
    ev = FaultEvent(items=("[level=2 code=7] 急停已触发",), fatal=True)
    for _ in range(5):
        env.src.on_device(ev)
    assert env.book.open()[0].count == 1


def test_急停松开再按_算新的一次():
    env = 装好(engine_state=RunState.RUNNING)
    env.src.on_device(FaultEvent(items=("[level=2 code=7] 急停已触发",), fatal=True))
    env.src.on_device(FaultEvent(items=()))
    env.钟.前进(1_000)
    env.src.on_device(FaultEvent(items=("[level=2 code=7] 急停已触发",), fatal=True))
    assert env.book.open()[0].count == 2


def test_跌倒报P1():
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_device(FaultEvent(items=("[level=2 code=9] 机身跌倒",), fatal=True))
    a = book.open()
    assert [x.kind for x in a] == ["fallen"]
    assert a[0].level is Level.P1


def test_认不出来的故障不硬报():
    """故障码表是厂商的,我们只认得出其中几条。认不出的就别猜 —— 报错级

    别的告警比不报更坏:P1 报多了,人就不看 P1 了。
    """
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_device(FaultEvent(items=("[level=1 code=33] 关节 3 温度偏高",)))
    assert book.open() == ()


def test_电量事件和控制权事件不在本卷接():
    """接不上的源就明确不接。``battery_swap`` / ``lease_expired`` 分别是

    别的任务的事,这里悄悄顺手报一条,两处说法就开始打架了。
    """
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_device(BatteryEvent(percent=8.0))
    src.on_device(ControlLostEvent(reason="别人抢走了"))
    assert book.open() == ()


# ------------------------------------------------------------------ 登记表


# -------------------------------------------------------------- 周期事实 P2
#
# 这三条跟上面那些都不一样:**它们不是事件,是水位。** 盘过了 80% 之后每一
# 拍都还过着,而事实只发生了一次 —— 所以下面每一组都有一条"摆着不动"的用
# 例,而且断言的是 ``count``,不是 ``len(book.open())``:去重坏掉的时候,重
# 复那些会被 §5.4 那个 15 分钟的聚合窗口收成同一条,``len()`` 照样是 1。


def 盘(已用比: float):
    """一个假的"量盘"。总量固定 1000,按比例给已用。"""
    return lambda: (int(1000 * 已用比), 1000)


def test_盘过了报警线报disk_80():
    book, src = 装好(disk=盘(0.85))
    src.on_tick()
    a = book.open()
    assert [x.kind for x in a] == ["disk_80"]
    assert a[0].level is Level.P2
    assert "85%" in a[0].detail


def test_盘一直过着水位_只报一条count也不涨():
    """**这一层最容易写错的地方。** 水位是状态,不是事件。

    按拍报的话,一个盘满会在聚合窗口里累出几百次 ``count`` —— §5.4 做聚合
    的全部理由就是别让一个根因把真要紧的那条埋掉,而这正好是自己动手埋。

    断言 ``count`` 而不只是条数:只看 ``len(book.open()) == 1`` 的话,去重
    整个删掉这条测试**照样是绿的**。
    """
    env = 装好(disk=盘(0.93))
    for _ in range(30):
        env.src.on_tick()
    a = env.book.open()
    assert len(a) == 1
    assert a[0].count == 1


def test_盘没过报警线不报():
    book, src = 装好(disk=盘(0.5))
    src.on_tick()
    assert book.open() == ()


def test_盘退到线下再涨上去_算新的一次():
    """记忆要会自己清。不清的话,第二次盘满反而没声音。"""
    盒 = {"比": 0.85}
    env = 装好(disk=lambda: (int(1000 * 盒["比"]), 1000))
    env.src.on_tick()
    盒["比"] = 0.4
    env.src.on_tick()
    盒["比"] = 0.9
    env.钟.前进(60_000)
    env.src.on_tick()
    a = [x for x in env.book.open() if x.kind == "disk_80"]
    assert len(a) == 1 and a[0].count == 2


def test_量不到盘_不报():
    """盘拔了、挂载点没了。**量不到不等于满了。**

    报一条假的 P2 比不报更坏:P2 多了人就不看 P2 了。
    """
    def 炸():
        raise OSError("挂载点没了")

    book, src = 装好(disk=炸)
    src.on_tick()
    assert book.open() == ()


def test_没接盘这条源_on_tick什么也不报():
    """三条源都留空时 ``on_tick`` 是个空操作 —— 既有那几十条测试因此不用改,

    跑测试的这台机器盘满了也不会凭空多出一条 P2 来。
    """
    book, src = 装好()
    src.on_tick()
    assert book.open() == ()


# ---------------------------------------------------------------- 任务包滞后


def 摆包(root, 槽名: list[str], *, current: str = ""):
    """在 ``root`` 里摆几个槽,可选地把 ``current`` 链指到其中一个。

    手工摆而不是走 ``build_bundle``/``apply_bundle``:这一组测的是"盘上是什
    么局面 -> 报不报",不是打包和换链本身(那是 ``tests/engine`` 的活)。
    """
    root.mkdir(parents=True, exist_ok=True)
    for 名 in 槽名:
        (root / 名).mkdir(exist_ok=True)
    if current:
        # 跟 ``tests/engine/test_bundle_pure_data.py`` 一个写法:用
        # ``Path.symlink_to``,而且**不加 skip**。那边写过原因 —— 这台开发
        # 机建得了符号链接,跳过反而会让 symlink 这道闸在这儿从没被真验过。
        (root / "current").symlink_to(root / current, target_is_directory=True)
    return root


def test_盘上有比current新的包_报bundle_lag(tmp_path):
    """§3.4 那条失效模式:**人以为改生效了,其实没有。**

    ``land()`` 只落盘不换链(它自己的 docstring:"落好了但还没生效是一个必
    须能被看到的状态")。中间断掉的那台狗会一直按旧包干活,而下发那一侧看
    到的是"发过去了"。
    """
    root = 摆包(tmp_path / "bundles", ["site-kl-3", "site-kl-4"],
              current="site-kl-3")
    book, src = 装好(bundles_root=root)
    src.on_tick()
    a = book.open()
    assert [x.kind for x in a] == ["bundle_lag"]
    assert a[0].level is Level.P2
    assert "site-kl-4" in a[0].detail


def test_current就是最新的那一版_不报(tmp_path):
    root = 摆包(tmp_path / "bundles", ["site-kl-3", "site-kl-4"],
              current="site-kl-4")
    book, src = 装好(bundles_root=root)
    src.on_tick()
    assert book.open() == ()


def test_有包但一版都没生效过_也算滞后(tmp_path):
    """``current`` 是空的:手上有包,却什么也没在跑。"""
    root = 摆包(tmp_path / "bundles", ["site-kl-1"])
    book, src = 装好(bundles_root=root)
    src.on_tick()
    assert [x.kind for x in book.open()] == ["bundle_lag"]


def test_任务包目录还不存在_不报(tmp_path):
    """第一次开机时 ``bundles/`` 根本还没有 —— 那不是滞后。"""
    book, src = 装好(bundles_root=tmp_path / "还没有")
    src.on_tick()
    assert book.open() == ()


def test_任务包一直滞后_只报一条count也不涨(tmp_path):
    root = 摆包(tmp_path / "bundles", ["site-kl-3", "site-kl-4"],
              current="site-kl-3")
    env = 装好(bundles_root=root)
    for _ in range(30):
        env.src.on_tick()
    a = env.book.open()
    assert len(a) == 1 and a[0].count == 1


def test_旧的那版生效了_又落了更新的一版_得再报一条(tmp_path):
    """**滞后是"落后了哪几个槽",不是一个 bool。**

    现场序列:kl-4 落了 -> 报一条(人看见了、也去生效了)-> kl-4 生效的同
    时 kl-5 又落下来 -> 这是一件**新的**事。用 bool 记"上一拍报没报过"的
    话,这一拍的"落后"还是真,和上一拍一样,于是不报 —— 而簿子里那条老告
    警的 detail 还写着 kl-4,人照着它去生效,发现早就生效了。

    (§5.4 的聚合窗口会把这两条收成同一条 ``bundle_lag``,所以断言看的是
    ``count`` 和 ``detail``,不是条数 —— 见 docs/测试为什么会说谎.md。)
    """
    root = 摆包(tmp_path / "bundles", ["site-kl-3", "site-kl-4"],
              current="site-kl-3")
    env = 装好(bundles_root=root)
    env.src.on_tick()
    a = env.book.open()
    assert [x.kind for x in a] == ["bundle_lag"] and a[0].count == 1
    assert "site-kl-4" in a[0].detail

    # 人把 kl-4 生效了;与此同时 kl-5 落了下来。
    (root / "current").unlink()
    (root / "site-kl-5").mkdir()
    (root / "current").symlink_to(root / "site-kl-4", target_is_directory=True)
    env.src.on_tick()

    a = env.book.open()
    assert [x.kind for x in a] == ["bundle_lag"]
    assert a[0].count == 2, "换了一版落后的槽,却当成同一件事没再报"
    assert "site-kl-5" in a[0].detail, "报是报了,detail 还指着已经生效的那版"


def test_盘上另一个bundle_id的槽_不算滞后(tmp_path):
    """**跨 ``bundle_id`` 是故意不认的**(见 ``_落了但没生效`` 的 docstring)。

    盘上并存好几个 ``bundle_id`` 是正常局面:``prune_bundles`` 只保
    ``current`` / ``previous`` 两份,而这两条链没有同 id 的要求(换站点之后
    ``previous`` 天然就是另一个 id);何况它今天还没有生产调用方,更早的槽
    会一直躺着。``landed.json`` 里也没记过哪个槽什么时候落的,``version``
    只在同一个 id 内单调 —— 跨 id 排不出先后。

    放宽就会把回滚备份和没清干净的旧槽报成 P2,**而且那条 P2 没有任何动作
    能让它消下去**;§5.2 判级看的是"人得做什么",一条做什么都不消的告警只
    会教人无视这个 kind。代价(别的 id 的包一直没生效这里看不出来)写在
    那个函数的 docstring 末尾。
    """
    root = 摆包(tmp_path / "bundles", ["site-kl-3", "site-xy-9", "yard-b-1"],
              current="site-kl-3")
    env = 装好(bundles_root=root)
    env.src.on_tick()
    assert env.book.open() == (), "把别的 bundle_id 的槽也当成滞后了"


def test_同一个bundle_id里更高的版本_照样认(tmp_path):
    """上一条的另一半:**收窄的只是跨 id,同 id 该认的一样得认。**

    两条钉在一起才有意义 —— 单看上一条,一个"永远返回空"的实现也是绿的。
    """
    root = 摆包(tmp_path / "bundles",
              ["site-kl-3", "site-kl-4", "site-xy-9"], current="site-kl-3")
    env = 装好(bundles_root=root)
    env.src.on_tick()
    a = env.book.open()
    assert [x.kind for x in a] == ["bundle_lag"]
    assert "site-kl-4" in a[0].detail
    assert "site-xy-9" not in a[0].detail


def test_盘读不出来_日志只在跳变那一拍记一条(caplog):
    """盘拔掉之后,那条早返每拍都走一遍 —— **日志也得去重**。

    ``on_tick`` 是周期看的,而这条 WARNING 带着 ``exc_info=True``(整段
    traceback)。不去重的话,一块坏掉的 SD 卡一天能刷出几千条一模一样的
    记录,把日志里真有用的那些冲掉 —— 跟告警刷屏是同一个毛病,治法也一样:
    只在跳变那一拍说一次。

    后半段钉的是**别去重过头**:盘回来了再坏一次,是一件新的事,得再记
    一条(靠的是成功那一路上的 ``_last_quiet.pop``)。
    """
    盒 = {"坏": True}

    def 盘():
        if 盒["坏"]:
            raise OSError("盘拔了")
        return (10, 1000)

    env = 装好(disk=盘)
    with caplog.at_level(logging.WARNING):
        for _ in range(30):
            env.src.on_tick()
        assert len(取水位日志(caplog)) == 1, "同一个毛病每拍刷一条"

        盒["坏"] = False                      # 盘回来了
        env.src.on_tick()
        盒["坏"] = True                       # 又坏了 —— 这是新的一件事
        env.src.on_tick()
    assert len(取水位日志(caplog)) == 2, "去重过头了,盘再坏一次没人记"


def 取水位日志(caplog) -> list:
    return [r for r in caplog.records if "量不到盘水位" in r.getMessage()]


# -------------------------------------------------------------------- 钟偏


def test_钟偏过了报警线报clock_skew():
    钟 = 假钟(1_000_000)
    book = AlertBook()
    src = AlertSources(book, robot=ROBOT, clock_ms=钟,
                       run_state=lambda: RunState.IDLE,
                       time_reference=lambda: (1_000_000 - 300_000, "ntp"))
    src.on_tick()
    a = book.open()
    assert [x.kind for x in a] == ["clock_skew"]
    assert a[0].level is Level.P2
    assert "ntp" in a[0].detail


def test_没有时间参照_不报():
    """**``None`` 不是漂移,是"不知道"。**

    断网时 ``time_reference`` 就回 ``None``(见 ``server._no_time_reference``
    的 docstring:"没有参照的时候,漂移是「不知道」,不是 0")。拿 0 顶上算
    出来的是"本地钟快了五十多年" —— 一条必然会响、而且永远说不清的 P2,而
    单机档的狗**大部分时间都没有参照**,那就是每台狗屏幕上常驻一条假告警。

    **钟要给一个真墙上钟量级的数**(这里是 2025 年的某一刻),不能用这一份
    里默认那个 ``1_000``:拿 0 当参照算出来的漂移正好等于"现在几点",而
    ``now_ms=1000`` 算出来才 1 秒,连报警线都够不着 —— 那样把 ``None`` 判掉
    的那一句整个删了,这条测试照样绿(实测过)。
    """
    book, src = 装好(now_ms=1_757_000_000_000, time_reference=lambda: None)
    for _ in range(5):
        src.on_tick()
    assert book.open() == ()


def test_钟偏在容忍之内不报():
    钟 = 假钟(1_000_000)
    book = AlertBook()
    src = AlertSources(book, robot=ROBOT, clock_ms=钟,
                       run_state=lambda: RunState.IDLE,
                       time_reference=lambda: (1_000_000 - 5_000, "ntp"))
    src.on_tick()
    assert book.open() == ()


def test_钟一直偏着_只报一条count也不涨():
    钟 = 假钟(1_000_000)
    book = AlertBook()
    src = AlertSources(book, robot=ROBOT, clock_ms=钟,
                       run_state=lambda: RunState.IDLE,
                       time_reference=lambda: (钟() - 300_000, "ntp"))
    for _ in range(30):
        src.on_tick()
        钟.前进(1_000)
    a = book.open()
    assert len(a) == 1 and a[0].count == 1


def test_三条源各报各的_互不影响(tmp_path):
    """一拍之内三件事同时成立,三条都要在,而且各只有一条。"""
    root = 摆包(tmp_path / "bundles", ["site-kl-1"])
    钟 = 假钟(1_000_000)
    book = AlertBook()
    src = AlertSources(book, robot=ROBOT, clock_ms=钟,
                       run_state=lambda: RunState.IDLE, disk=盘(0.95),
                       bundles_root=root,
                       time_reference=lambda: (1_000_000 - 300_000, "ntp"))
    for _ in range(5):
        src.on_tick()
    assert sorted(x.kind for x in book.open()) == [
        "bundle_lag", "clock_skew", "disk_80"]
    assert all(x.count == 1 for x in book.open())


def test_本卷用到的kind都在LEVEL_OF里登记过():
    """没登记的 kind ``raise_alert`` 会抛裸 ``KeyError`` —— 那是设计的闸,

    这条测试是那道闸在本任务这一侧的对照。
    """
    for kind in ("estop_pressed", "fallen", "loc_lost_paused", "battery_abort",
                 "run_abort", "stuck", "run_start", "run_done",
                 "lease_expired", "disk_80", "bundle_lag", "clock_skew"):
        assert kind in LEVEL_OF


# ------------------------------------------------------------------ 接线


def test_告警簿挂在AppContext上(ctx: AppContext):
    """任务 7、8、9 都从 ``ctx.alerts`` 上取,它得是**一份**,不是各造各的。"""
    assert isinstance(ctx.alerts, AlertBook)


def test_服务上的告警簿就是ctx那一份(server, ctx: AppContext):
    assert server.alerts is ctx.alerts


def test_接线_导航事件一路走到告警簿(ctx: AppContext):
    """``_StateHub`` 收到的事实要真的落进告警簿,不是只在单测里落。"""
    from d1max_patrol.app.auth import Guard
    from d1max_patrol.app.control import ControlDesk

    hub = _StateHub(ctx, ControlDesk(Guard(None)))
    hub._on_nav(LocStatusEvent(status=LocStatus.LOC_LOST, previous=None))
    hub._on_run(造快照(state=RunState.PAUSED))
    assert [a.kind for a in ctx.alerts.open()] == ["loc_lost_paused"]


def test_接线_设备故障一路走到告警簿(ctx: AppContext):
    from d1max_patrol.app.auth import Guard
    from d1max_patrol.app.control import ControlDesk

    hub = _StateHub(ctx, ControlDesk(Guard(None)))
    hub._on_device(FaultEvent(items=("[level=2 code=7] 急停已触发",), fatal=True))
    assert [a.kind for a in ctx.alerts.open()] == ["estop_pressed"]


def test_接线_告警用的是这只狗的sn(ctx: AppContext):
    """聚合键里带 ``robot``,这就是"P1 不跨狗合并"的实现 —— 填错了,两只

    狗的告警会合成一条。
    """
    from d1max_patrol.app.auth import Guard
    from d1max_patrol.app.control import ControlDesk

    hub = _StateHub(ctx, ControlDesk(Guard(None)))
    hub._on_device(FaultEvent(items=("[level=2 code=9] 机身跌倒",), fatal=True))
    assert ctx.alerts.open()[0].robot == ctx.identity.sn


def test_接线_状态接口没被这一任务改坏(server):
    """挂了一行认事实的调用之后,``/api/state`` 那份快照该长什么样还长什么样。"""
    snap = get_json(server, "/api/state")
    assert snap["run"]["state"] == RunState.IDLE.value
