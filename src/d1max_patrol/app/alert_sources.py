"""把已有的事实源翻成告警(§5.2 那张表的左半边)。

**认事实,不判级。** 哪个 ``kind`` 是几级,只在 ``engine/alerts.py`` 的
``LEVEL_OF`` 里说一次;这个模块只回答"什么算发生了"。分层上也只能这样:
``engine/`` 不许 import ``app/``,而"从哪儿认出急停"必须看得见 app 这一
侧的事件流 —— 所以判级留在 engine,认事实放这儿,中间只隔一个 ``kind``
字符串。塞进 ``_StateHub._on_nav`` 的 ``elif`` 链里也不行:那条链是"把事
件抄进快照字段",跟"这算不算 P1"是两件会各自变长的事。

**快照是状态,不是事件 —— 这是这一层唯一真正难的地方。**
``_StateHub`` 的引擎快照每约 0.5 秒重建一次,内容常常一模一样;而
``AlertBook.raise_alert`` **不是幂等的**,喂一次 ``count`` 就加一。所以这
个类必须自己记住"上一次看到的是什么"(下面那几个 ``_last_*`` 字段),只在
**变了**的那一刻报一次。直接把每一份快照喂进去,一次卡住会在 15 分钟的
聚合窗口里累成几十次 —— 而 §5.4 做聚合的全部理由,就是别让一个根因把真
要紧的那条埋掉。

**时间注进来**(§8.5 第 2 条):``clock_ms`` 由调用方给,这里不读钟。

**认不出来的就不报。** 厂商的故障码表我们只认得出其中几条,认不出的一律
不猜。报错级别比不报更坏:P1 报多了,人就不看 P1 了。
"""

from __future__ import annotations

from collections.abc import Callable

from d1max_patrol.backends.base import FaultEvent, LocStatusEvent
from d1max_patrol.engine.alerts import AlertBook
from d1max_patrol.engine.machine import RunSnapshot, RunState
from d1max_patrol.protocol.nav_types import LocStatus

#: 故障文本里出现这些词就认作急停。
#:
#: **这是个假设,真机清单里要量。** 厂商的 ``FaultFrame`` 只给
#: ``level``/``code``/``message`` 三个字段,``message`` 是自由文本,没有
#: 结构化的"这条是急停"标记(见 ``protocol/agent_frames.py``)。真机上把
#: 急停按下,把那条 message 原文抄回来,再把这张表改成按 ``code`` 认 ——
#: 按文本认迟早会被一次固件改版的措辞换掉。
ESTOP_WORDS: tuple[str, ...] = ("急停", "estop", "e-stop", "emergency stop")

#: 故障文本里出现这些词就认作跌倒。理由同 :data:`ESTOP_WORDS`。
#:
#: 不收单独一个 ``fall``:它会命中 ``fallback`` 这种词,而误报一条 P1 的
#: 代价是有人半夜开车出门。
FALLEN_WORDS: tuple[str, ...] = ("跌倒", "摔倒", "倒地", "fallen",
                                 "fall down", "falldown", "tipped over")

#: 中止原因里出现这些词才认作"没电中止"。``engine/safety.py`` 那条中止
#: 理由的原文是"电量 22% 低于中止线 25%"。
BATTERY_WORDS: tuple[str, ...] = ("电量", "电池", "battery")


def _命中(文本: str, 词表: tuple[str, ...]) -> bool:
    低 = 文本.lower()
    return any(词 in 低 for 词 in 词表)


class AlertSources:
    """把事件流和引擎快照翻成告警。

    三个入口对着 ``_StateHub`` 已有的三条汇流:``on_nav`` / ``on_device``
    收的是后端事件,``on_run`` 收的是引擎快照。每个入口各自维护自己那点
    "上次是什么"的记忆(``_last_*``),互不相干 —— 三件事的"变了"判据不
    一样,合到一处记只会让哪一条该重置变得说不清。

    ``run_state`` 是**当前引擎状态**的取值口。``loc_lost_paused`` 要"丢定
    位"和"引擎暂停了"两个条件同时成立,而这两个事实从两条不同的流上来:
    安全模块是收到 ``LocLost`` **之后**才让引擎暂停的,所以真机上"先丢定
    位、后暂停"才是常态。只在 ``on_nav`` 那一刻判一次,这条最该报的 P1 就
    永远报不出来,现场看到的是狗停在原地一晚上没人知道。所以两个入口都
    要复判一次这个组合条件。
    """

    def __init__(self, book: AlertBook, *, robot: str,
                 clock_ms: Callable[[], int],
                 run_state: Callable[[], RunState]) -> None:
        self._book = book
        self._robot = robot
        self._clock_ms = clock_ms
        self._run_state = run_state
        #: 上一次看到的定位是不是丢了。定位恢复过就清掉,不然第二次真丢
        #: 定位反而没声音。
        self._last_loc_lost: bool = False
        #: 上一次"丢定位且引擎暂停"这个组合条件成不成立。暂停期间快照照样
        #: 每拍重建、条件一直成立,但事实只发生了一次。
        self._last_loc_lost_paused: bool = False
        #: 上一条故障事件里认出来的急停/跌倒。故障帧是**状态**不是事件,
        #: 厂商会一直重推同一条(``_StateHub._faults`` 也是整组替换的)。
        self._last_estop: bool = False
        self._last_fallen: bool = False
        #: 上一份快照的 ``started_ms``。它一变就是新的一趟,按趟记的那几份
        #: 记忆(失败点位、开跑报过没有)要跟着清 —— ``results`` 是按趟清空
        #: 的,记忆不清,第二趟同一个点位再卡住就被当成"上次那条",一声不响。
        self._last_started_ms: int | None = None
        #: 这一趟里已经报过 ``stuck`` 的点位名。
        self._last_failed: frozenset[str] = frozenset()
        #: 这一趟的开跑报过没有。光看状态迁移不够:
        #: ``RUNNING -> PAUSED -> RUNNING`` 是同一趟,人按几次暂停就会多出
        #: 几条"开跑",交接班看到的是一份假的流水。
        self._last_run_started: bool = False
        #: 上一份快照的引擎状态。**开局就问一次**,不是留 ``None`` ——
        #: 留 ``None`` 的话第一份快照永远算作"刚迁移过来",于是接线的那一刻
        #: 引擎正跑着就会凭空多一条"开跑"。
        self._last_state: RunState = run_state()

    # ------------------------------------------------------------ 导航

    def on_nav(self, event: object) -> None:
        """导航侧的事实。本卷只认定位丢失这一条(§5.2)。"""
        if isinstance(event, LocStatusEvent):
            self._last_loc_lost = event.status is LocStatus.LOC_LOST
            self._判定定位丢失(self._run_state())

    # ------------------------------------------------------------ 设备

    def on_device(self, event: object) -> None:
        """本体侧的事实:急停与跌倒(§5.2)。

        ``BatteryEvent`` / ``ControlLostEvent`` 这一卷**不接**。低电换电是
        ``battery_swap``、控制权是 ``lease_expired``,分别是别的任务的事;
        这里顺手报一条,两处说法就开始打架,而不一样的那天正好是该响的那次
        没响。
        """
        if not isinstance(event, FaultEvent):
            return
        文本 = " ".join(event.items)
        estop = _命中(文本, ESTOP_WORDS)
        fallen = _命中(文本, FALLEN_WORDS)
        now_ms = self._clock_ms()
        if estop and not self._last_estop:
            self._book.raise_alert(kind="estop_pressed", robot=self._robot,
                                   title="急停被按下", detail=文本, now_ms=now_ms)
        if fallen and not self._last_fallen:
            self._book.raise_alert(kind="fallen", robot=self._robot,
                                   title="狗跌倒了", detail=文本, now_ms=now_ms)
        self._last_estop = estop
        self._last_fallen = fallen

    # ------------------------------------------------------------ 引擎快照

    def on_run(self, snapshot: RunSnapshot) -> None:
        """引擎快照。**每拍都会来一份,内容常常一模一样** —— 所有判定都得

        先过一遍"跟上次比变了没有",见模块开头。
        """
        now_ms = self._clock_ms()
        if snapshot.started_ms != self._last_started_ms:
            self._last_started_ms = snapshot.started_ms
            self._last_failed = frozenset()
            self._last_run_started = False
        self._判定卡住(snapshot, now_ms)
        self._判定起止(snapshot, now_ms)
        self._判定定位丢失(snapshot.state, now_ms)

    # ------------------------------------------------------------ 内部判定

    def _判定卡住(self, snapshot: RunSnapshot, now_ms: int) -> None:
        """``results`` 里**新增**一条 ``ok=False`` 才是一次"卡住"。

        整份 ``results`` 每拍都在,按整份报就等于按快照报。
        """
        failed = frozenset(r.name for r in snapshot.results if not r.ok)
        for name in sorted(failed - self._last_failed):
            self._book.raise_alert(
                kind="stuck", robot=self._robot,
                title=f"点位 {name} 没到", detail=snapshot.reason, now_ms=now_ms)
        self._last_failed = failed

    def _判定起止(self, snapshot: RunSnapshot, now_ms: int) -> None:
        state = snapshot.state
        上次, self._last_state = self._last_state, state
        if state is 上次:
            return
        if state is RunState.RUNNING:
            if not self._last_run_started:
                self._last_run_started = True
                self._book.raise_alert(
                    kind="run_start", robot=self._robot,
                    title=f"开跑:{snapshot.mission}", now_ms=now_ms)
            return
        if state is RunState.DONE:
            self._book.raise_alert(
                kind="run_done", robot=self._robot,
                title=f"跑完了:{snapshot.mission}", now_ms=now_ms)
        elif state is RunState.ABORTED and _命中(snapshot.reason, BATTERY_WORDS):
            # 别的原因中止走的是别的处置。本卷没有"泛泛中止"这个 kind ——
            # 宁可不报,也不许拿一个 kind 冒充另一个:``battery_abort`` 说的
            # 是"没电了,得去把狗抱回来充电",认错了就是把人往错的方向支。
            self._book.raise_alert(
                kind="battery_abort", robot=self._robot,
                title="电量不足,整趟中止", detail=snapshot.reason, now_ms=now_ms)

    def _判定定位丢失(self, state: RunState, now_ms: int | None = None) -> None:
        """§5.2 的原文是「定位丢失后暂停」,**两个条件都要**。

        光丢定位是常事(过个转角、单帧 TF 查不到),报了就是狼来了 —— 而
        狼来了的代价是下一次真丢定位没人看。
        """
        成立 = self._last_loc_lost and state is RunState.PAUSED
        if 成立 and not self._last_loc_lost_paused:
            self._book.raise_alert(
                kind="loc_lost_paused", robot=self._robot,
                title="定位丢失后暂停",
                detail="定位报 LocLost,引擎已进 PAUSED",
                now_ms=self._clock_ms() if now_ms is None else now_ms)
        self._last_loc_lost_paused = 成立


__all__ = ["ESTOP_WORDS", "FALLEN_WORDS", "BATTERY_WORDS", "AlertSources"]
