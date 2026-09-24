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

import logging
from collections.abc import Callable
from pathlib import Path

from d1max_agent.engine.alerts import AlertBook
from d1max_agent.engine.bundle import read_state
from d1max_agent.engine.machine import RunSnapshot, RunState
from d1max_agent.engine.schedule import clock_skew
from d1max_agent.engine.storage import WARN_USED_RATIO
from d1max_patrol.backends.base import FaultEvent, LocStatusEvent
from d1max_patrol.protocol.nav_types import LocStatus

log = logging.getLogger(__name__)

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


def _槽(名: str) -> tuple[str, int] | None:
    """``<bundle_id>-<version>`` 拆成两半。拆不开就回 ``None``。

    形状的权威定义在 ``engine/bundle.py`` 的 ``BundleManifest.slot_name``。
    这里**只拆不校验**:输出只拿去比大小,一个字都不会被拼进路径 —— 把那
    条路径安全正则抄一份过来,只是多一处迟早会跟本尊分岔的定义。
    """
    bid, _, ver = 名.rpartition("-")
    if not bid or not ver.isdigit():
        return None
    return bid, int(ver)


def _落了但没生效(bundles_root: Path) -> tuple[str, ...]:
    """盘上有比 ``current`` 新的槽 —— 包落好了,但链没换(§3.2)。

    **判据为什么是这个。** ``bundle.land()`` 自己写着「落好了但还没生效是一
    个必须能被看到的状态」:落盘和换链是分开的两步,中间断掉的那台狗会一直
    按旧包干活,而下发那一侧看到的是「发过去了」。这正是 §3.4 那条失效模式
    ——「人以为改生效了,其实没有」。

    **不用 ``bundle.divergence()``。** 那个函数比的是「服务器想让它跑哪一
    版」和「它实际在跑哪一版」,而那个"想要"是服务器侧的意图,``AppContext``
    今天根本没有这个字段(联网档才有,见 §3.2)。硬凑一个出来,单机档上这条
    告警就变成了拿假数据算出来的真警报。

    只看盘和链,两样都是本机事实,单机档上一样成立。

    **跨 ``bundle_id`` 故意不认。** ``current`` 指着 ``site-kl-3``,盘上摆着
    一个 ``site-xy-1``,这里不报。理由是**盘上并存好几个 ``bundle_id`` 是正
    常局面,而不是"有一版没生效"的证据**:

    * ``prune_bundles`` 的规矩是"只留 ``current`` 和 ``previous`` 两份",而
      这两条链**没有任何地方要求它们同 ``bundle_id``** —— 换站点之后
      ``previous`` 天然就是另一个 id 的包。
    * ``prune_bundles`` 今天还没有生产调用方(卷 5 只给了函数),所以更早的
      那些槽会一直躺在盘上。
    * ``landed.json`` 里没有任何一处记着"哪个槽是什么时候落的"
      (``BundleState`` 只有 current / previous / proven / rollbacks /
      denied / applying / forced),而 ``version`` 只在同一个 ``bundle_id``
      内部单调 —— **跨 id 根本排不出先后**。

    也就是说,放宽成"不是 current 也不是 previous 就算滞后"会把回滚备份和
    没清干净的旧槽一起报成 P2,而且那条 P2 **没有任何动作能让它消下去**。
    §5.2 判级的口径是"人得做什么";一条做什么都不消的告警,只会教会人无视
    这个 kind —— 连带把真正管用的那条一起废掉。

    代价说清楚:**"落了一个别的 ``bundle_id`` 的包、一直没生效"这一种,这里
    确实报不出来。** 那条路今天由 §3.2 的下发侧和值守屏上的 ``bundle_lag``
    列表兜(``app/watch.py`` 把这个函数的返回值原样摆在屏上,人看得见盘上
    到底有哪几个槽)。要在这里也认出来,得先有一处记着"这个槽是什么时候落
    的" —— 那是 ``landed.json`` 的改动,不是这个函数的。
    """
    root = Path(bundles_root)
    if not root.is_dir():
        return ()
    当前 = _槽(read_state(root).current)
    新的 = []
    for p in sorted(root.iterdir()):
        if p.is_symlink() or not p.is_dir():
            continue
        槽 = _槽(p.name)
        if 槽 is None:
            continue
        # ``current`` 是空的(一份都没生效过)时,盘上任何一份都算滞后 ——
        # 那台狗手上有包却什么也没在跑。
        if 当前 is None or (槽[0] == 当前[0] and 槽[1] > 当前[1]):
            新的.append(p.name)
    return tuple(新的)


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
                 run_state: Callable[[], RunState],
                 disk: Callable[[], tuple[int, int]] | None = None,
                 bundles_root: Path | None = None,
                 time_reference: Callable[[], tuple[int, str] | None]
                 | None = None) -> None:
        self._book = book
        self._robot = robot
        self._clock_ms = clock_ms
        self._run_state = run_state
        #: :meth:`on_tick` 那三条周期事实的取值口。**三个都默认 ``None``,
        #: 意思是"这条源没接上,所以不认这件事"** —— 不是"认了但值是空的"。
        #: 给个默认实现(比如自己去 ``shutil.disk_usage``)会让每一份为了
        #: 别的目的构出来的 ``AlertSources`` 都开始量宿主机的盘。
        #:
        #: ``disk`` 回 ``(已用, 总量)`` 字节,跟 ``server._disk`` 一个形状:
        #: 注进来而不是自己算,是为了让 ``/api/storage`` 和这条告警**读同一
        #: 个函数** —— 两处各写一遍,页面上说 82% 而告警不响的那天没人查得出
        #: 来是哪一处的口径不一样。
        self._disk = disk
        self._bundles_root = bundles_root
        self._time_reference = time_reference
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
        #:
        #: **开局跟 ``_last_state`` 一样问一次引擎,不是硬编码 ``False``。**
        #: 接线的那一刻引擎已经在跑的话,第一份 ``RUNNING`` 快照会在
        #: ``_判定起止`` 的 ``state is 上次`` 那一句早返 —— 而唯一把这个记忆
        #: 置 True 的那一行正好在被跳过的那个分支里。留 ``False`` 的后果是
        #: ``RUNNING -> PAUSED -> RUNNING`` 之后凭空多一条"开跑"
        #: (任务 6 评审)。
        self._last_run_started: bool = run_state() is RunState.RUNNING
        #: 上一份快照的引擎状态。**开局就问一次**,不是留 ``None`` ——
        #: 留 ``None`` 的话第一份快照永远算作"刚迁移过来",于是接线的那一刻
        #: 引擎正跑着就会凭空多一条"开跑"。
        self._last_state: RunState = run_state()
        #: :meth:`on_tick` 那三条的"上次成不成立"。**这三条比前面几条更容易
        #: 写错**:盘水位过了 80% 之后**每一拍都还过着**,而事实只发生了一
        #: 次。按拍报的话,一个盘满会在 15 分钟的聚合窗口里累出几百次
        #: ``count`` —— §5.4 做聚合的全部理由就是别让一个根因把真要紧的那条
        #: 埋掉,而这正好是自己动手埋。
        self._last_disk_80: bool = False
        #: **任务包这一条记的是"哪几个槽",不是"有没有落差"。** 记 ``bool``
        #: 的话:site-kl-4 落下报了一条,运维把 4 生效了、5 又落下来 ——
        #: "有落差"这个布尔值一路都是 ``True``,一条新告警都不报,而簿子上
        #: 那条的 ``detail`` 还写着 site-kl-4。人照着旧槽名去查,查到的是
        #: 已经生效了的那一版。同一个文件里 ``_last_failed`` 用
        #: ``frozenset[str]`` 就是为了"换了一个就得重新报",这里是同一件事。
        self._last_bundle_lag: tuple[str, ...] = ()
        self._last_clock_skew: bool = False
        #: 上一拍那三条各自读得出来读不出来。**读不出来的日志也要去重** ——
        #: 盘拔掉之后这三条每隔 :data:`~d1max_patrol.app.server._WATER_EVERY`
        #: 拍就各记一条带 traceback 的 WARNING,一天几千条,把日志里真有用
        #: 的那些冲掉。跟告警去重同一套办法:只在跳变那一拍记一条。
        self._last_quiet: dict[str, str] = {}

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
            # **第一份快照不算"换了一趟"。** 它只是我们头一回看见,而构造时
            # 已经就着引擎的现状把 ``_last_run_started`` 问过一次了 —— 这里
            # 无条件清成 ``False`` 会把那个初值当场抹掉,于是"接线时引擎正
            # 跑着"这条路上,暂停再继续照样凭空多一条"开跑"(任务 6 评审)。
            首次 = self._last_started_ms is None
            self._last_started_ms = snapshot.started_ms
            self._last_failed = frozenset()
            if not 首次:
                self._last_run_started = False
            # **``_last_loc_lost`` / ``_last_loc_lost_paused`` 不在这个块
            # 里,这是有意的。** 上面这两份记忆是"按趟"的:``results`` 是按
            # 趟清空的,不跟着清,第二趟同一个点位再卡住就被当成"上次那
            # 条"、一声不响。而定位那两份**会自己清**:``on_nav`` 每收到一
            # 条 ``LocStatusEvent`` 就整个重赋 ``_last_loc_lost``,
            # ``_判定定位丢失`` 结尾那一句 ``self._last_loc_lost_paused =
            # 成立`` 也是无条件的 —— 定位一恢复、或者引擎一离开 PAUSED,它
            # 们当拍就回到 ``False``。事实没了记忆就没了,不需要按趟兜底。
            #
            # 反过来说,在这儿多清一遍不是"更保险"而是"更糟":它们记的是
            # 一个**此刻还成不成立**的组合条件,不是一趟里的历史。起飞那一
            # 拍把它清掉,等于把"这一秒定位确实还丢着"这个事实忘了,而下一
            # 拍条件仍然成立 —— 于是同一次丢定位报第二条。
        self._判定卡住(snapshot, now_ms)
        self._判定起止(snapshot, now_ms)
        self._判定定位丢失(snapshot.state, now_ms)

    # ------------------------------------------------------------ 周期事实

    def on_tick(self) -> None:
        """没有事件推给我们的那几件事:盘水位、任务包滞后、钟偏(§5.2 P2)。

        **这三条不是事件,是水位。** 没有任何一条流会推来"盘满了" —— 只能
        自己隔一会儿去看一眼。所以它们挂在租约看门狗那条协程上(见
        ``server._StateHub._lease_watchdog``):那条协程本来就是为了"没人在
        的时候也得有人去问一句"而存在的。

        **不挂在 ``_StateHub._tick`` 上。** 那条协程自己的 docstring 写着
        "这一拍只管显示和事件流",往里塞处置就把两条相反的容忍度合在了一
        起。

        **三条源都没接的时候这个方法什么也不做**,一次调用不会凭空报出告警
        来 —— 现有那一堆只构了 ``book``/``robot``/``clock``/``run_state``
        的 ``AlertSources`` 一个字都不用改。
        """
        now_ms = self._clock_ms()
        self._判定盘水位(now_ms)
        self._判定任务包滞后(now_ms)
        self._判定钟偏(now_ms)

    def _判定盘水位(self, now_ms: int) -> None:
        """已用超过 :data:`~d1max_agent.engine.storage.WARN_USED_RATIO`。

        水位线**用 ``engine/storage.py`` 那一个**,不在这儿再拍一个 0.8:
        起飞门槛和这条告警说的是同一件事,两处各存一份,调了一处另一处不
        跟着调的那天,页面会说"能起飞"而告警在响。
        """
        if self._disk is None:
            return
        try:
            used, total = self._disk()
        except OSError as exc:
            # 盘拔了、挂载点没了。**量不到不等于满了** —— 报一条假的 P2 比
            # 不报更坏(模块开头那句"认不出来的就不报")。
            #
            # **这里也不动 ``_last_disk_80``**,跟 ``_判定钟偏`` 那个
            # ``None`` 守卫同一个道理:"量不到"不等于"退到线下了"。清掉的
            # 话,盘一恢复就会把同一次盘满再报一遍。
            self._静一句("disk", "量不到盘水位,这一拍不判", exc)
            return
        # 这一拍读出来了 —— 把"上次说过的那句"清掉,下次再坏还得说一次。
        self._last_quiet.pop("disk", None)
        ratio = (used / total) if total > 0 else 0.0
        成立 = ratio >= WARN_USED_RATIO
        if 成立 and not self._last_disk_80:
            self._book.raise_alert(
                kind="disk_80", robot=self._robot, title="盘快满了",
                detail=f"已用 {ratio * 100:.0f}%,过了 "
                       f"{WARN_USED_RATIO * 100:.0f}% 的报警线",
                now_ms=now_ms)
        self._last_disk_80 = 成立

    def _判定任务包滞后(self, now_ms: int) -> None:
        """盘上有比 ``current`` 新的包,判据见 :func:`_落了但没生效`。"""
        if self._bundles_root is None:
            return
        try:
            落后 = _落了但没生效(self._bundles_root)
        except (OSError, ValueError) as exc:
            # ``BundleError`` 是 ``ValueError`` 的子类,所以坏掉的
            # ``landed.json`` 也在这一网里。读不出局面同样是"不知道",不报,
            # 也不动记忆(理由同 ``_判定盘水位``)。
            self._静一句("bundle", "读不出任务包的局面,这一拍不判", exc)
            return
        self._last_quiet.pop("bundle", None)
        # **比的是"哪几个槽",不是"有没有"** —— 见 ``_last_bundle_lag`` 的
        # 注释:换了一批槽就是一件新的事,得重新报一条,不然簿子上那条的
        # detail 会一直指着一个已经生效了的旧版本。
        if 落后 and 落后 != self._last_bundle_lag:
            self._book.raise_alert(
                kind="bundle_lag", robot=self._robot,
                title="任务包落好了但没生效",
                detail="盘上有 " + "、".join(落后) + ",current 还指着旧的",
                now_ms=now_ms)
        self._last_bundle_lag = 落后

    def _判定钟偏(self, now_ms: int) -> None:
        """本地钟跟外头的参照差太多(§3.3 第 4 条)。"""
        if self._time_reference is None:
            return
        ref = self._time_reference()
        if ref is None:
            # **``None`` 不是漂移,是"不知道"。** 断网时就没有参照,拿 0 顶
            # 上等于说"本地钟快了五十多年" —— 一条必然会响、而且永远说不清
            # 的 P2(见 ``server._no_time_reference`` 的 docstring)。
            #
            # 这里**也不动那个 ``_last_``**:不知道不等于"回正了"。清掉的
            # 话,一段断网就会让同一次漂移在恢复之后再报一遍。
            return
        参照, 来源 = ref
        skew = clock_skew(local_ms=now_ms, reference_ms=参照, source=来源)
        if skew.alarm and not self._last_clock_skew:
            self._book.raise_alert(
                kind="clock_skew", robot=self._robot, title="本机钟不准",
                detail=f"跟 {skew.source} 差 {skew.skew_s:.0f} 秒",
                now_ms=now_ms)
        self._last_clock_skew = skew.alarm

    def _静一句(self, 口: str, 话: str, exc: Exception) -> None:
        """同一个毛病只在**变了**的那一拍记一条日志。

        这三条是周期看的:盘拔掉之后,每一轮都会走同一条早返,而
        ``exc_info=True`` 带的是整段 traceback。不去重的话,一块坏掉的 SD 卡
        能在一天里刷出几千条一模一样的 WARNING,把日志里真有用的那些冲掉 ——
        跟告警刷屏是同一个毛病,所以用同一套办法治:只在跳变那一拍说一次。
        """
        指纹 = f"{type(exc).__name__}: {exc}"
        if self._last_quiet.get(口) == 指纹:
            return
        self._last_quiet[口] = 指纹
        log.warning("%s(%s)", 话, 指纹, exc_info=True)

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
        elif state is RunState.ABORTED:
            # **中止一律要报,只是报哪一条要分清。** 不许拿一个 kind 冒充另
            # 一个:``battery_abort`` 说的是"没电了,得去把狗抱回来充电",
            # 认错了就是把人往错的方向支 —— 所以关节过温、导航反复失败这些
            # 走 ``run_abort``,标题里不许出现"电量"。
            #
            # 但"认不出原因就干脆不报"更坏:那恰恰是**最该响**的一类 ——
            # 没人在的时候整趟中止,狗就在原地站到天亮,一声不响。两个 kind
            # 都是 P1,人要做的第一件事都是"过去看看",分开只是为了让他知道
            # 该带什么(见 ``engine/alerts.py`` 里 ``run_abort`` 那条注释)。
            没电 = _命中(snapshot.reason, BATTERY_WORDS)
            self._book.raise_alert(
                kind="battery_abort" if 没电 else "run_abort",
                robot=self._robot,
                title="电量不足,整趟中止" if 没电 else "整趟中止了",
                detail=snapshot.reason, now_ms=now_ms)

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
