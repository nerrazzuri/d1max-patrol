"""把告警接到已有的事实源上(§5.2 那张表的左半边)。

这一整份测的是同一件事的两面:**该报的报得出来**,以及**不该报的不许报**。
后者才是这一层真正难的地方 —— 引擎快照每约 0.5 秒重建一次,内容常常一模
一样,而 ``raise_alert`` 不是幂等的。把每一份快照直接喂进去,一次卡住会在
聚合窗口里累成几十次,真要紧的那条就被自己埋了。
"""

from __future__ import annotations

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


def 装好(*, engine_state: RunState = RunState.RUNNING, now_ms: int = 1_000) -> 环境:
    book = AlertBook()
    钟 = 假钟(now_ms)
    盒 = {"state": engine_state}
    src = AlertSources(book, robot=ROBOT, clock_ms=钟,
                       run_state=lambda: 盒["state"])
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

    走的是别的处置,认错了就是把人往错的方向支。本卷没有"泛泛中止"这个
    kind —— 宁可不报,也不许拿一个 kind 冒充另一个。
    """
    book, src = 装好(engine_state=RunState.RUNNING)
    src.on_run(造快照(state=RunState.ABORTED, reason="SDK 致命故障: 关节过温"))
    assert [x.kind for x in book.open()] == []


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


def test_本卷用到的kind都在LEVEL_OF里登记过():
    """没登记的 kind ``raise_alert`` 会抛裸 ``KeyError`` —— 那是设计的闸,

    这条测试是那道闸在本任务这一侧的对照。
    """
    for kind in ("estop_pressed", "fallen", "loc_lost_paused", "battery_abort",
                 "stuck", "run_start", "run_done"):
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
