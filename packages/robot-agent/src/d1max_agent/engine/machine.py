"""任务引擎状态机。主规范 §6.1 的状态图 + §6.3 的单队列约定。

**单队列、单消费者。** 后端事件、用户命令、超时,全都进同一条
``asyncio.Queue``,由驱动协程一个人消费。这意味着:

* 状态不会被并发修改 —— 没有锁,也不需要锁;
* 行为可重放 —— 测试往队列里灌一串事件就能断言迁移,不需要真后端;
* 驱动协程**永远不裸 sleep**。凡是要等的地方(等到点、等 dwell、等继续)
  都是"带截止时间地从队列里拿" —— 否则等的那几秒里事件就没人处理了。

**降级规则不在这里。** 该怎么处置一律问 ``safety.rule`` / ``battery_ruling``,
状态机只负责把裁决翻成动作。规则表是纯函数,能穷举;状态机不是。

**引擎不认识 HTTP。** 它必须能脱离 app 单独用,有一条测试读源码盯着这点。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from d1max_agent.engine.archive import RunArchive
from d1max_agent.engine.form import STANDALONE, Form
from d1max_agent.engine.homing import (
    DEFAULT_RETURN_PARAMS,
    HomePoint,
    ReturnParams,
    estimate_cost_pct,
)
from d1max_agent.engine.mission import Action, Mission, MissionWaypoint
from d1max_agent.engine.preflight import PreflightReport, run_preflight
from d1max_agent.engine.removable import (
    DEFAULT_PROBE,
    RemovableProbe,
    scan_or_unknown,
)
from d1max_agent.engine.safety import (
    Decision,
    Ruling,
    SafetyContext,
    battery_ruling,
    rule,
)
from d1max_patrol.backends.base import (
    BatteryEvent,
    DeviceBackend,
    DevicePoseEvent,
    EventEmitter,
    LocStatusEvent,
    MediaError,
    MediaSource,
    NavBackend,
    NavBackendError,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

#: 导航终态。到了这几个之一,这一段导航就算有结果了。
NAV_TERMINAL = frozenset({NavStatus.SUCCEED, NavStatus.FAILED, NavStatus.CANCELLED})

#: 等定位收敛最多等这么久。LOCALIZING 阶段和定位重置之后都用它。
LOCALIZE_TIMEOUT_S = 30.0

#: 上一段导航到了终态之后,状态机要过一会儿才回落 StandBy,而 start_nav 只在
#: StandBy 下受理(§3.6)。这中间下一个点会被设备当场拒绝 —— 不是竞态,是设备
#: 状态机本身的约束。cli.py 的 walk 和契约测试 test_连续走多个点 撞的是同一堵墙。
NAV_STANDBY_TIMEOUT_S = 10.0

#: 等 StandBy 期间重新问一次状态的间隔。后端状态一变就会推事件,这个轮询只是
#: 为了不把"回落"这件事全押在事件推送上。
_STANDBY_POLL_S = 0.05

#: 返航最多等这么久。
RETURN_TIMEOUT_S = 300.0

#: Python 3.10 的 ``asyncio.TimeoutError`` **还不是**内建 ``TimeoutError``
#: 的别名(3.11 才合并),而开发机和板载都还在 3.10 —— 只写内建那个,
#: ``wait_for`` 的超时就会漏出去,被兜底当成"引擎内部异常"整趟中止。
_TIMEOUT = (TimeoutError, asyncio.TimeoutError)


class RunState(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    LOCALIZING = "LOCALIZING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    #: 引擎**主动让开腿**:接下来这段路人要亲自开。
    #:
    #: 跟 ``PAUSED`` 的区别是「谁在动这条狗」。``PAUSED`` 是人按了暂停,狗停着,
    #: 没有人碰它,``_busy_checks`` 全是空的。``SUSPENDED`` 期间 ``_busy_checks``
    #: 恰恰是满的 —— 遥控正在动 —— 而那正是 §5.10 要放行的东西。
    #:
    #: **不许跟 PAUSED 合并**:「暂停时不许遥控」和「挂起时必须能遥控」是
    #: 相反的两条规矩,落在同一个状态上就一定有一条是错的。
    SUSPENDED = "SUSPENDED"
    RETURNING = "RETURNING"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    DONE = "DONE"


#: 跑完了的状态。``wait_done`` 等的就是这两个。
FINAL_STATES = frozenset({RunState.DONE, RunState.ABORTED})

#: ``suspend()`` 只在"继续之后引擎知道该往哪走"的状态下受理(§5.10)。
#:
#: **这张表现在是空的。** 空表不是废代码:下面那道按 ``before`` 查表的闸照样在,
#: 它是下一个发现"某个状态挂起不安全"的人唯一的挂靠点。删了表和闸,下次就得把
#: 整套机制重新发明一遍,而重新发明的那一版多半又会犯下面这条 ``PAUSED`` 的错。
#:
#: ``PAUSED`` 不在这张表里,**但这不等于暂停里喊挂起就一律放行。**
#: ``_pause_until_resumed`` 那条 while 会接住 ``suspend`` 命令、转手调
#: ``_suspend_until_resumed``,而 ``_RetryWaypoint`` 最后有没有人接,取决于
#: **按暂停之前**站在哪一层。所以那条 while 查这张表时查的是"暂停之前那个状态",
#: 不是 ``self._state`` —— 那一刻 ``self._state`` 已经是 ``PAUSED``,查什么都查不到,
#: 这道闸就成了"先按暂停、再按让开腿"一绕就过的摆设。**这条纪律跟表里有几项无关,
#: 表空着也得照这么查。**
#:
#: ``RETURNING`` 原来在这儿(挂账 56),2026-09-10 移走了:走廊被堵、地上有水,
#: 人在返航路上要把狗牵开是个真需求。移走的前提是那条路真接得住继续 ——
#: 见 ``_ResumeReturnHome``:人接管完是从狗**现在**停的地方重新规划回家,
#: 不是接着跑发起返航时那一次 ``return_home``。
#:
#: ``LOCALIZING`` 原来是表里最后一项,2026-09-11 移走。旧理由是"挂起再继续,
#: ``_RetryWaypoint`` 会从 ``_await_localized`` 漏给 ``_run`` 的兜底,整趟中止";
#: 那个漏子已经在 ``_await_localized`` 里堵上了,旧理由不再成立。**但"旧理由没了"
#: 不等于"就该放行"**,所以按它自己的是非重推了一遍,结论是移走,五条:
#:
#: 1. **拒绝的实际后果是"这条狗谁也动不了"。** ``app/teleop.py`` 那道闸是
#:    ``if engine.running and not engine.yielding: raise TeleopBusy``,而
#:    ``yielding`` 的定义就是 ``self._state is SUSPENDED``。挂起被拒 → 进不了
#:    SUSPENDED → 遥控也被拒。于是等定位的 30s 里人对这条狗**没有任何操作手段**,
#:    只能看着它走到 ``_AbortRun("等定位收敛超过 30s")``。
#: 2. **而"把狗挪到特征多的地方"正是重定位不收敛时的标准处置。** 拒绝挂起等于
#:    拿走唯一那个真能解决问题的动作,留下的只有"等超时然后整趟中止"。
#: 3. **腿不会被抢。** 这段期间引擎一条运动指令都没下:``_await_localized``
#:    只有 ``loc_status()`` 和等队列。``RUNNING`` 下人接管要防的"两边一起动",
#:    在这儿没有对应物。
#: 4. **继续之后不会吃到过期数据。** 回来只重新问一次 ``loc_status()``,拿的是
#:    厂商此刻的判断;引擎自己不缓存位姿、不做推算。人把狗牵到哪儿,收敛就从哪儿
#:    重新开始 —— 这恰恰是想要的。
#: 5. **挂起点允许没有位姿。** ``SuspendPoint.pose`` 的文档串写明可以是 ``None``,
#:    定位没收敛时拿不到位姿本来就在设计之内,不是被这次改动逼出来的例外。
#:
#: 反方的说法是"位姿在飘,人一动可能收敛到错的地方去"。这条不成立:收敛判定是
#: 厂商侧做的,引擎只读 ``LocStatus``;人不动它也可能收到错的地方去,而人不动
#: 就一定收不了 —— 上面第 2 条。
#:
#: 留一条已知的毛刺(不阻塞,记在这儿):定位期间若来了 ``LOC_LOST``,继续时的
#: 那道闸会说"狗不在地图里,找不到下一个点"。话本身不假,但对着一条"本来就还没
#: 定位好"的狗说这句,现场容易读成新故障。真要改得先给这条闸分状态说话。
_SUSPEND_UNSAFE_STATES: dict[RunState, str] = {}


log = logging.getLogger(__name__)


def _now_ms() -> int:
    """墙钟的毫秒数(Unix epoch)。**引擎里唯一一处 ``time.time()``。**

    时间戳要走墙钟不是洁癖:``SuspendPoint.at_ms`` 原来会被老服务 ``app/server.py``(W00c5e 退役)拿去
    跟它自己的 ``now_ms`` 相减判"挂起太久了"(P1 告警 ``suspend_stale``),两边
    得在同一条纪年上,单调钟那个数只在本进程内有意义。同理 ``_Live.started_ms``
    和 ``WaypointResult.arrived_ms`` 都是要写进归档给人看的时刻。

    但**每次都现问一次墙钟就错了** —— 见 ``MissionEngine._stamp_ms``。所以这个
    函数只在每趟开跑时被调一次,而且可以从构造函数注进来(``wall_ms=``),
    测试里就不用去动系统时间。
    """
    return int(time.time() * 1000)


class EngineBusy(RuntimeError):
    """已经有一趟在跑了。"""


@dataclass(frozen=True, slots=True)
class WaypointResult:
    name: str
    ok: bool
    arrived_ms: int
    elapsed_s: float
    photos: tuple[str, ...]
    note: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "arrived_ms": self.arrived_ms,
                "elapsed_s": round(self.elapsed_s, 2), "photos": list(self.photos),
                "note": self.note}


@dataclass(frozen=True, slots=True)
class SuspendPoint:
    """让开腿的那一刻,狗停在哪儿(§5.10)。

    **位姿是「人接管前狗在哪儿」的唯一记录。** 人开着走一段之后,点位序号
    还在,但狗已经不在那儿了 —— 要判断接管结束后该从哪儿接着跑,靠的是这
    一份快照,不是当前位姿。

    ``pose`` 可以是 ``None``:旁路进程没连上、这一档不报位姿都会这样。
    **少一份位姿不该让「人要接管」卡住** —— 挂不起来的后果是人挪不动狗。
    """

    waypoint_index: int
    waypoint_name: str
    pose: Pose | None
    reason: str
    at_ms: int
    #: 进挂起之前引擎在哪个状态。**"人接管完之后该往哪走"只认这一个字段。**
    #:
    #: 从跑点位挂起的,继续时重发当前点;从返航路上挂起的,继续时得重新规划
    #: 回家(任务 13)。这件事必须显式记下来:``reason`` 是人打的一句话,拿
    #: 它去猜状态就是把控制流建在自由文本上 —— 现场随手写一句"返航路上有
    #: 水",引擎就会走上完全不同的一支。
    #:
    #: **不上线**(不进 ``to_wire``):它是引擎决定下一步的内部依据,不是给
    #: 人看的信息。上线意味着同时改协议和手机端的解析,那是另一件事。
    #:
    #: **没有默认值**:给一个"多半是对的"默认值,等于让漏传的调用方悄悄走上
    #: 重发点位那一支,而没有任何测试会红。
    from_state: RunState
    #: **这一次让开腿之前,已经累计挂起了多少毫秒 —— 口径按阶段分,不是
    #: 笼统一句"同一趟返航里"。**
    #:
    #: 累计器从**任务一开始**就在记(不分跑点位阶段还是返航阶段),
    #: ``_go_home`` 进返航时清零一次(见 ``_go_home`` 里那句
    #: ``live.suspend_total_ms = 0``)。所以:
    #:  - 跑点位阶段让开腿,这个数是"这一趟任务到目前为止累计挂起了多久";
    #:  - 返航路上让开腿,这个数才是"这一趟返航里累计挂起了多久"——因为
    #:    进 ``_go_home`` 那一刻已经清过一次。
    #: 两段口径不同,但服务同一个判定:"这条狗累计有多久没在跑"。跑点位
    #: 阶段反复短接管,同样该报下面那条 P1(可见性正是要的,不是缺陷)。
    #:
    #: 存在的理由是"反复短接管"这一幕:狗在返航路上被拉开、放回、又被拉开,
    #: 每一次都短于 ``SUSPEND_STALE_MS``,于是那条 P1 一次都不报,而狗从
    #: 电量到线那一刻起就一直没在往家走 —— 最后耗到没电,全程零告警。
    #: 判定要看的是"这条狗累计有多久没在往家走",不是"这一次挂了多久";
    #: 也不看接管次数 —— 耗电的是时间不是次数,而"次数超 N"要一个只能等真机
    #: 耗电率才定得下来的新阈值。
    #:
    #: 累计量由引擎按可注入的 ``clock`` 算(不是墙钟),在 ``_go_home`` 进入
    #: 时清零 —— 返航这一段一趟一算,不跨趟累加(跨趟靠 ``_Live`` 每趟
    #: 新建来保证,见 ``_go_home`` 里 ``live.suspend_total_ms = 0`` 边上的
    #: 注释)。
    #:
    #: **不上线**,理由同 ``from_state``:判定原来在老服务 ``app/server.py``(W00c5e 退役)
    #: (``_挂起超时了``),它拿到的是这个对象本身,不是 wire 上那份。
    #:
    #: **没有默认值**:0 就是"这一趟还没挂起过",漏传只会让那条 P1 **晚**报
    #: 甚至不报,而这正是这个字段要堵的那个洞 —— 一个静默失效的默认值比多写
    #: 一个参数坏得多。
    prior_suspend_ms: int

    def to_wire(self) -> dict[str, Any]:
        return {"waypoint_index": self.waypoint_index,
                "waypoint_name": self.waypoint_name,
                "pose": self.pose.to_wire() if self.pose else None,
                "reason": self.reason, "at_ms": self.at_ms}


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """引擎当前的全貌。订阅者拿到的就是这个,页面照着画即可。"""

    state: RunState
    mission: str
    waypoint_index: int
    waypoint_name: str
    total: int
    started_ms: int
    results: tuple[WaypointResult, ...] = ()
    reason: str = ""
    suspended_at: SuspendPoint | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"state": self.state.value, "mission": self.mission,
                "waypoint_index": self.waypoint_index,
                "waypoint_name": self.waypoint_name, "total": self.total,
                "started_ms": self.started_ms, "reason": self.reason,
                "results": [r.to_wire() for r in self.results],
                "suspended_at": (self.suspended_at.to_wire()
                                 if self.suspended_at else None)}


# ------------------------------------------------------------------ 内部信号


@dataclass(frozen=True, slots=True)
class _Command:
    """用户命令也走队列 —— 直接改状态就把并发修改放回来了。"""

    kind: str          # pause / suspend / resume / abort
    reason: str = ""


class _AbortRun(Exception):
    """整趟中止。**不含任何位移**(主规范 §6.4)。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ReturnHome(Exception):
    """电量到线,转返航。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _FailWaypoint(Exception):
    """当前这个点没成。按 ``on_waypoint_failed`` 处置。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _RetryWaypoint(Exception):
    """暂停后继续 —— 重发当前点,但**不算**一次重试。"""


class _ResumeReturnHome(Exception):
    """返航途中被人接管,现在人还回来了 —— **从狗现在停的地方重新规划回家**。

    自成一个类型,两条都不能复用:

    * ``_RetryWaypoint`` 的意思是"重发当前点位",而返航路上没有当前点位;
    * ``_ReturnHome`` 的意思是"电量到线,转返航",``_go_home`` 的 except 会把
      它当成返航失败整趟中止 —— 恰恰是这个任务要消掉的那个结局。
    """


class _BusyCheckFailed(Exception):
    """``_busy_checks`` 里有一个回调炸了。

    只在 ``_busy_reason`` 内部生生灭灭:炸了不能当成"没人占用"放行,
    异常本身要变成占用理由说给人听。ruff 的 ``BLE001`` 不认"把异常塞进
    返回字符串"这种用法,但认"就地转译成一个具名异常再 raise"——
    这个类存在的唯一理由就是让那次转译过 BLE001,不是业务上真需要
    一个新异常类型。"""


@dataclass
class _Live:
    """一趟运行里会变的东西。放一起,免得散落成十个实例字段。"""

    mission: Mission
    archive: RunArchive
    started_ms: int
    index: int = 0
    results: list[WaypointResult] = field(default_factory=list)
    battery_pct: float = 100.0
    loc_reset_attempts: int = 0
    photos: list[str] = field(default_factory=list)
    blocked_since: float | None = None
    #: 最近一次收到的位姿。挂起时抄一份进 ``SuspendPoint``。
    last_pose: Pose | None = None
    #: 最近一次收到的定位状态。挂起期间人可能把狗开出地图,resume 要靠它
    #: 分岔(人拍的板 2,2026-09-10)。**跟 last_pose 一样是顺手留的底**,
    #: 不是接管这个事件的处置 —— 处置仍然在 _handle 的规则表那边。
    last_loc: LocStatus | None = None
    #: 现在挂着的那一份。``_publish`` 每次都从这儿取 —— 快照是重建出来的,
    #: 不从这儿取的话,挂起期间任何一次 publish 都会把它抹掉。
    suspended: SuspendPoint | None = None
    #: 这一趟返航里已经累计挂起了多久(毫秒)。见
    #: ``SuspendPoint.prior_suspend_ms`` —— 反复短接管那一幕全靠它才报得出来。
    #: ``_go_home`` 进入时清零,``_suspend_until_resumed`` 每次退出时累加。
    #: **用引擎自己的 ``clock`` 算**,不是墙钟:它是可注入的,测试才推得动。
    suspend_total_ms: int = 0


def _preflight_reason(report: PreflightReport) -> str:
    return "起飞前检查未通过: " + "; ".join(
        f"{c.name}({c.detail})" for c in report.failures)


class MissionEngine(EventEmitter[RunSnapshot]):
    """一趟巡检的驱动。

    ``media`` 是"相机名 -> 取流"的映射(``MediaSource.grab()`` 不带参数,
    一路相机一个源)。缺哪路相机,用到它的点位就明确判失败,不静默跳过。
    """

    def __init__(self, nav: NavBackend, device: DeviceBackend,
                 media: Mapping[str, MediaSource], runs_root: Path,
                 *, clock: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], int] = _now_ms,
                 fingerprint: Mapping[str, Any] | None = None,
                 form: Form = STANDALONE,
                 removable: RemovableProbe = DEFAULT_PROBE,
                 return_params: ReturnParams = DEFAULT_RETURN_PARAMS) -> None:
        super().__init__()
        self._nav = nav
        self._device = device
        self._media = dict(media)
        self._runs_root = Path(runs_root)
        self._clock = clock
        self._wall_ms = wall_ms
        # 每趟开跑时对一次表:墙钟读一次当锚点,之后所有对外时刻都是
        # "锚点 + 单调钟走过的量"。见 ``_stamp_ms``。
        self._epoch_ms = 0
        self._epoch_at = 0.0
        self._fingerprint = dict(fingerprint or {})
        self._form = form
        self._removable = removable
        self._return_params = return_params
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._state = RunState.IDLE
        self._seen: set[RunState] = set()
        self._live: _Live | None = None
        self._task: asyncio.Task[None] | None = None
        self._forwarders: list[asyncio.Task[None]] = []
        self._done = asyncio.Event()
        self._done.set()
        self._snapshot = RunSnapshot(RunState.IDLE, "", 0, "", 0, 0)
        self._busy_checks: list[Callable[[], str]] = []
        # 原点不进构造函数: 引擎按进程建,原点按地图选,每趟开跑时由
        # ``start`` 换进来。构造时给一份就意味着它活不过第一次 start ——
        # 一个悄悄失效的参数比没有这个参数更坑人。
        self._home: HomePoint | None = None

    # ------------------------------------------------------------------ 对外

    @property
    def snapshot(self) -> RunSnapshot:
        return self._snapshot

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def archive(self) -> RunArchive | None:
        return self._live.archive if self._live is not None else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def archive_error(self) -> str:
        """这一趟归档第一次写不进去的原因(W00c6a);空串 = 一直写得进去。代理据此报人。"""
        return self._live.archive.error if self._live is not None else ""

    @property
    def crash(self) -> str:
        """引擎任务带着异常结束了的话,是什么异常;否则空串。**正常路径走不到这儿**(收尾一定
        落成终态),这是给代理看门用的最后一道(W00c6a)。"""
        t = self._task
        if t is None or not t.done() or t.cancelled():
            return ""
        exc = t.exception()
        return f"{type(exc).__name__}: {exc}" if exc is not None else ""

    def now_ms(self) -> int:
        """引擎这一趟的"现在",Unix epoch 毫秒。**判"挂起多久了"该拿这个去减。**

        跟 ``SuspendPoint.at_ms`` 出自同一口钟(见 ``_stamp_ms``):开跑时对一
        次墙钟当锚点,之后按可注入的单调钟推。所以
        ``engine.now_ms() - 点.at_ms`` 是一段**真实走过的时长**,狗上墙钟中途
        被校一下、或者压根没有 NTP,都不影响它。

        **这原来是给老服务 ``app/server.py::_挂起超时了`` 预备的(W00c5e 退役)。** 那边当时拿的是
        ``self._ctx.clock()``(每次现问墙钟),跟 ``at_ms`` 一减就是"活墙钟 −
        锚定时刻",墙钟一跳这条 P1 的判据就跟着跳。那一半不在 engine 的地盘
        里,已写进报告等协调;这个方法先备好,那边改成 ``ctx.engine.now_ms()``
        是一行的事。

        没在跑的时候没有锚点,直接回墙钟 —— 那时候也没有挂起点可减。
        """
        if self._live is None:
            return self._wall_ms()
        return self._stamp_ms()

    @property
    def yielding(self) -> bool:
        """引擎现在让着腿吗(§5.10)。

        **做成引擎自己的属性,不让外壳去比状态**:「哪些状态算让位」是引擎的
        词汇,外壳复刻一遍状态表就等于把这条规矩存了两份,而两份迟早不一样。
        """
        return self._state is RunState.SUSPENDED

    @property
    def loc_lost(self) -> bool:
        """狗现在不在地图里吗(人拍的板 2,2026-09-10)。

        **做成引擎自己的属性,不让外壳去比枚举**:理由跟 ``yielding`` 一样,
        "哪个枚举值算丢"是引擎的词汇,外壳复刻一遍就是把规矩存了两份。

        任务 13(返航途中同一条规矩)也读这个属性 —— 公开只读,不是内部
        细节。
        """
        return (self._live is not None
                and self._live.last_loc is LocStatus.LOC_LOST)

    @property
    def form(self) -> Form:
        """这台狗跑在哪一档。**按进程定,一趟跑不会换。**

        对外只读,是为了让"跑在哪一档"在整个进程里只有这一个出处 ——
        引擎里那道 preflight 才是真拦住这一趟的那道,别处再存一份,
        两份迟早不一样,而不一样的那天没有任何测试会红。
        """
        return self._form

    @property
    def removable(self) -> RemovableProbe:
        """怎么去认外插盘。**对外只读,理由跟 ``form`` 一样。**

        `AppContext.removable` 就是问这里要的 —— 一个按进程走的属性只该有
        一个出处,两份迟早不一样,而不一样的那天没有任何测试会红。
        """
        return self._removable

    @property
    def return_params(self) -> ReturnParams:
        """把"还有多远"换算成"还要多少电"的那四个系数。**按进程定,只读。**

        出发线和飞行中的返航线必须用**同一份**:这四个数是这一卷唯一待真机
        标定的,标定落地那天只换一处的话,两条线就开始用两套系数 —— 而今天
        两边都是默认值,不一样的那天没有任何测试会红。
        """
        return self._return_params

    def add_busy_check(self, check: Callable[[], str]) -> None:
        """登记一个"本体现在被别人占着吗"的检查。返回占用原因,空串表示没占。

        引擎管得住自己不并行开两趟,管不住**别人**在动这条狗 —— 页面上的
        遥控就是这样一个别人。它同时在动,任务的每一步都在跟人抢腿。

        做成回调而不是让引擎认识遥控:遥控是外壳那一层的东西,引擎不许知道
        外壳存在(见全局约束)。回调是纯函数,没有这个方向的依赖。
        """
        self._busy_checks.append(check)

    async def start(self, mission: Mission, *,
                    home: HomePoint | None = None) -> None:
        """开一趟。已经在跑就拒绝 —— 两趟并行会把归档搅在一起。

        引擎是按进程建的,原点却是按地图选的(调用方按地图选):构造时
        给的那份只是初始值,真正对得上"这一趟跑哪张图"的那份,由调用方在
        这里换进来。**只在真的要开跑的这条路径上换**——挂在"已经在跑"或
        某个 ``_busy_checks`` 上的请求,不许动正在飞的那趟手里的原点。
        """
        if self.running:
            raise EngineBusy(f"已经在跑 {self._snapshot.mission},先停下来再开新的")
        blocked = self._busy_reason()
        if blocked:
            raise EngineBusy(blocked)
        self._home = home
        suffix = getattr(self, "run_suffix", None)
        archive = RunArchive(self._runs_root, mission, suffix=suffix() if suffix else "")
        archive.write_manifest(self._fingerprint)
        # 对表:墙钟这一趟只读这一次,后面所有时刻都从这个锚点按单调钟推。
        self._epoch_ms = self._wall_ms()
        self._epoch_at = self._clock()
        self._live = _Live(mission, archive, started_ms=self._epoch_ms)
        self._seen = set()
        while not self._queue.empty():       # 上一趟的残留不许漏进这一趟
            self._queue.get_nowait()
        self._state = RunState.IDLE
        self._done.clear()
        self._publish()
        self._forwarders = [
            asyncio.create_task(self._forward(self._nav)),
            asyncio.create_task(self._forward(self._device)),
        ]
        self._task = asyncio.create_task(self._run())

    async def pause(self) -> None:
        await self._queue.put(_Command("pause"))

    async def suspend(self, reason: str) -> None:
        """让开腿:接下来这段路人亲自开(§5.10)。

        **不是暂停。** 暂停是狗停着没人碰;挂起期间人正在用遥控开它。
        """
        await self._queue.put(_Command("suspend", reason))

    async def resume(self) -> None:
        await self._queue.put(_Command("resume"))

    async def abort(self, reason: str) -> None:
        await self._queue.put(_Command("abort", reason))

    async def wait_done(self, timeout_s: float | None = None) -> RunState:
        await asyncio.wait_for(self._done.wait(), timeout_s)
        return self._state

    async def wait_state(self, state: RunState, timeout_s: float = 10.0) -> None:
        """等到某个状态**出现过**。

        看"出现过"而不是"此刻是":状态机跑得比调用方快,``RUNNING`` 可能在
        这句 await 回来之前就已经翻页了。
        """
        deadline = self._clock() + timeout_s
        with self.subscription() as queue:
            while state not in self._seen:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError(
                        f"等 {state.value} 超过 {timeout_s}s,当前 {self._state.value}")
                with contextlib.suppress(*_TIMEOUT):
                    await asyncio.wait_for(queue.get(), remaining)

    async def aclose(self) -> None:
        """停掉后台任务。引擎自己不关后端 —— 后端比它活得长。"""
        if self.running:
            await self.abort("引擎关闭")
            with contextlib.suppress(*_TIMEOUT, asyncio.CancelledError):
                await self.wait_done(timeout_s=5.0)
        await self._teardown()

    # ------------------------------------------------------------ 状态与落盘

    def _publish(self, reason: str = "") -> None:
        """快照 + state.json + 广播,**三件事捆在一起**。

        任何一条路径只写其中一两件,页面和崩溃恢复就会各说各话。
        """
        live = self._live
        if live is None:
            self._snapshot = RunSnapshot(self._state, "", 0, "", 0, 0, reason=reason,
                                         suspended_at=None)
            self.emit(self._snapshot)
            return
        wps = live.mission.waypoints
        idx = min(live.index, len(wps) - 1)
        self._snapshot = RunSnapshot(
            state=self._state, mission=live.mission.mission,
            waypoint_index=live.index, waypoint_name=wps[idx].name,
            total=len(wps), started_ms=live.started_ms,
            results=tuple(live.results), reason=reason,
            suspended_at=live.suspended)
        live.archive.write_state(self._snapshot.to_wire())
        self.emit(self._snapshot)

    async def _transition(self, to: RunState, reason: str = "") -> None:
        frm = self._state
        self._state = to
        self._seen.add(to)
        if self._live is not None:
            self._live.archive.append_event("state", frm=frm.value, to=to.value,
                                            reason=reason)
        self._publish(reason)

    def _note(self, kind: str, **fields: Any) -> None:
        if self._live is not None:
            self._live.archive.append_event(kind, **fields)

    def _stamp_ms(self) -> int:
        """对外时刻(Unix epoch 毫秒),**锚点 + 单调钟**,不是现问墙钟。

        为什么不直接 ``int(time.time() * 1000)``:``SuspendPoint`` 的两个同胞
        字段原来跑在两条时基上 —— ``at_ms`` 是墙钟,``prior_suspend_ms`` 是
        ``self._clock()`` 这口可注入的单调钟,而它俩是同一个 ``finally`` 成对
        收尾的。老服务(W00c5e 退役)判 ``suspend_stale``(P1 告警)算的是
        ``now_ms - 点.at_ms + 点.prior_suspend_ms`` —— 一个式子里把两条时基加
        在一起。现场没有 NTP,狗上墙钟一跳,这条 P1 的判据就跟着跳:往前跳
        误报一串 P1(2 分钟没人确认就 push、5 分钟出声),往后跳则该报的不报。

        改成锚点 + 单调钟之后:``at_ms`` 仍然是 epoch 毫秒(app 那边拿它跟自己
        的 ``now_ms`` 相减的算法一个字不用动),但**一趟之内它和
        ``prior_suspend_ms`` 走的是同一口钟**,狗上墙钟中途跳多少都不影响两者
        的差。锚点在 ``start`` 里读一次,那一次读的是真墙钟。

        剩下的一半在 app 侧,不在这个文件里:那边的 ``now_ms`` 还是每次现问
        **手机/服务端**的墙钟。两台机器的墙钟差 + 服务端自己跳表,仍然会动这条
        判据。已写进报告,不越界改。
        """
        return self._epoch_ms + int((self._clock() - self._epoch_at) * 1000)

    # ------------------------------------------------------------------ 队列

    async def _forward(self, emitter: NavBackend | DeviceBackend) -> None:
        """把后端事件搬进唯一那条队列。搬运工不做判断。"""
        with emitter.subscription() as queue:
            while True:
                await self._queue.put(await queue.get())

    async def _next(self, timeout_s: float | None) -> Any | None:
        """拿一个输入。到点还没有就回 None —— 超时不是异常,是正常节拍。"""
        if timeout_s is not None and timeout_s <= 0:
            return None
        try:
            return await asyncio.wait_for(self._queue.get(), timeout_s)
        except _TIMEOUT:
            return None

    # --------------------------------------------------------------- 主流程

    async def _run(self) -> None:
        live = self._live
        assert live is not None
        try:
            await self._transition(RunState.PREFLIGHT)
            report = await run_preflight(self._nav, self._device,
                                         live.mission, self._runs_root,
                                         home=self._home, form=self._form,
                                         removable=await scan_or_unknown(
                                             self._removable),
                                         return_params=self._return_params,
                                         robot_sn=self._fingerprint.get(
                                             "robot_sn", ""))
            self._note("preflight", ok=report.ok,
                       checks=[{"name": c.name, "ok": c.ok, "detail": c.detail}
                               for c in report.checks])
            if not report.ok:
                raise _AbortRun(_preflight_reason(report))

            await self._transition(RunState.LOCALIZING)
            await self._await_localized()

            await self._transition(RunState.RUNNING)
            for _ in range(live.mission.policy.loops):
                for i, wp in enumerate(live.mission.waypoints):
                    live.index = i
                    self._publish()
                    live.results.append(await self._do_waypoint(wp))
            await self._transition(RunState.DONE)
        except _ReturnHome as exc:
            await self._go_home(exc.reason)
        except _AbortRun as exc:
            await self._do_abort(exc.reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 让引擎带着异常静静死掉是最糟的结局:页面上还显示 RUNNING,
            # 人在外面等着。任何意外都要落成一个说得清原因的 ABORTED。
            #
            # **这里必须兜住一切,收窄到具体类型就是错的**:兜的是"我们没想到
            # 的那种异常",一旦写得出类型名,它就已经是想到过的了。原来这行挂
            # 着一句 ``noqa: BLE001`` 硬压 —— 而本项目的 ruff 配置写明 ``BLE``
            # 不许用 noqa 绕。改成带 ``exc_info`` 记一条日志:ruff 认这种写法
            # (兜住 + 留证据,不是吞掉),而且它补上了一个真缺口 ——
            # ``_do_abort`` 的 reason 里只有 ``类型: 消息``,traceback 原来
            # 整段丢了,事后没人查得出炸在哪一行。
            log.exception("引擎内部异常,整趟按中止收尾")
            await self._do_abort(f"引擎内部异常: {type(exc).__name__}: {exc}")
        finally:
            await self._finish()

    async def _do_abort(self, reason: str) -> None:
        """中止 = 停止导航 + 记录 + 告警。**不含任何位移**(主规范 §6.4)。

        停导航放在 ``finally``(W00c6a):前面记状态出什么事,导航都要停 —— 以前第一步
        记 ABORTING 就抛(写盘失败),停导航那一句根本没执行到。"""
        try:
            await self._transition(RunState.ABORTING, reason)
        finally:
            await self._stop_nav_quietly()
        await self._transition(RunState.ABORTED, reason)

    async def _go_home(self, reason: str) -> None:
        """回家。**是一个循环,不是一次性的**(任务 13)。

        循环的唯一理由是"返航途中被人接管过":人接管完点继续,狗已经不在发起
        返航时那个位置了,得重新走一遍这个循环 —— 重新停、重新等 StandBy、
        重新 ``return_home``。接着等原来那次的结果是不行的:那一次是从一个
        没人知道的位置算出来的。
        """
        live = self._live
        assert live is not None
        # 累计挂起时长按**这一趟返航**算,不跨趟累加(见
        # ``SuspendPoint.prior_suspend_ms``)。清在 ``while`` **外面**:重来
        # 那一圈走的是 ``continue``,清在里面等于每被接管一次就把账抹平,那条
        # P1 就还是永远不报 —— 也就是这个字段白加了。
        #
        # 「不跨趟」结构上不需要靠这句清零来保证:``_Live`` 全仓只有一个创建点
        # (``start()`` 里的 ``self._live = _Live(...)``),每趟新开都会造一个
        # 新对象,``suspend_total_ms`` 的 dataclass 默认值就是 0。这句清零管的
        # 是**同一个** ``_Live`` 内部「跑点位阶段 → 返航阶段」这一次切换,不是
        # 跨趟。
        live.suspend_total_ms = 0
        await self._transition(RunState.RETURNING, reason)
        while True:
            try:
                await self._stop_nav_quietly()
                # 返航也是一次 start_nav,一样只在 StandBy 下受理。
                await self._await_nav_standby(self._clock() + NAV_STANDBY_TIMEOUT_S)
                # 后端会返航的(规划器上线后,W10),这一圈从狗**当下**的位置重新规划回家 —— 人接管过、
                # 狗被挪开了,重新规划的起点就是新位置(W08 决定 6)。现在代理唯一的导航桥是直线桥,它
                # 不认 ``return_home``(W00c6b),引擎走下面的沿来路回。
                # (以前这里押的是厂商导航 ``start_nav_return_home`` 会不会重新规划;
                # 代理不用厂商导航了。)
                try:
                    await self._nav.return_home()
                except NavRequestError as exc:
                    # **后端不会返航,引擎自己回(W04)。** 自建导航和仿真的
                    # ``return_home`` 是明确拒绝的;以前这个拒绝直接把整趟按
                    # 「返航失败」中止 —— 低电时狗原地趴下,而这正是最不该
                    # 趴下的时候。沿来路倒着走回去:直线回家会穿墙。
                    log.info("后端不支持返航(%s),引擎沿来路回原点", exc)
                    await self._retrace_home()
                    break
                # 超时预算每一圈重算:人接管花了多久,不该记在这一趟返航头上
                # (跟 ``_do_waypoint`` 里"到点超时按这一次尝试算"同一个道理)。
                await self._wait_nav_terminal(self._clock() + RETURN_TIMEOUT_S)
            except (_ResumeReturnHome, _RetryWaypoint):
                # **两个信号在这儿是同一件事:有人 resume 了,重新规划回家。**
                #
                # ``_ResumeReturnHome`` 是直接从 ``RETURNING`` 让开腿那一支
                # (``SuspendPoint.from_state is RETURNING``)。
                #
                # ``_RetryWaypoint`` 是所有绕了一道的 resume。返航段上根本没有
                # "下一个点"可以重试,所以它抵达这儿**只可能**是有人点了继续
                # —— 数得清的路径一共两条,都在 ``_handle`` 那一侧:
                #   1. 返航中按暂停 → ``_pause_until_resumed`` → 点继续;
                #   2. 返航中按暂停、在暂停里再点让开腿 → ``_suspend_until_
                #      resumed`` 记下的 ``from_state`` 是 ``PAUSED`` 而不是
                #      ``RETURNING`` → 点继续走的是重发点位那一支。
                # 第三条候选 ``_recover_localization`` 到不了这儿:
                # ``_handle`` 里那道门槛要求 ``self._state is RUNNING``,而在
                # 这个循环里状态只会是 RETURNING / PAUSED / SUSPENDED。
                #
                # 不接住它的后果不是"少支持一种操作",是**整趟按「返航失败」
                # 中止**:上面那两条路径在任务 13 之前就存在,人在返航路上按了
                # 暂停再继续,这趟任务就没了。
                #
                # 状态在这儿翻回 ``RETURNING``。``_ResumeReturnHome`` 那一支
                # **全程不经过 RUNNING**(闪一下 RUNNING 会让订阅方以为任务又
                # 在跑点位了,而这条狗从头到尾只是在回家路上);绕道 ``_Retry
                # Waypoint`` 那两支会先被上游翻成 RUNNING 再翻回来。
                #
                # **这一闪是安全的,论证如下:** ``_suspend_until_resumed``
                # 的 ``finally`` 里不 await 任何东西,只做
                # ``live.suspended = None`` 和累加 ``suspend_total_ms``;紧
                # 接着 ``await self._transition(RUNNING, "人工接管结束")``,
                # 下一句就是 ``raise``。这个异常一路传到这儿(中间只经过
                # ``_wait_nav_terminal`` / ``_await_nav_standby`` 里
                # ``await self._handle(item)`` 那一层调用返回),**这段窗口
                # 内没有任何 ``await self._next(...)``**——引擎是单任务的,
                # 处理不了任何新事件,``_handle`` 里那两道门槛都穿不透:
                # 定位恢复那一支要求 ``self._state is RUNNING``,
                # ``RETURN_HOME`` 那一支遇上 ``self._state is RETURNING``
                # 直接 ``return``。副作用只落两处:(1) 广播快照上闪一下 RUNNING,手机
                # 上能看到;(2) 审计串里多一行 RUNNING「人工接管结束」,但
                # 下一行紧跟着的 RETURNING「人工接管结束,重新规划返航」把它
                # 纠正过来。
                #
                # ``RUNNING → RETURNING`` 这个**序列**是这一刀新造的(任务
                # 13 之前接的是 ``RUNNING → ABORTED``)。上游那次
                # ``_transition(RUNNING)`` 调用本身确实是既有代码,但接在它
                # 后面走到 RETURNING 而不是 ABORTED,是这一刀改出来的路径。
                await self._transition(RunState.RETURNING, "人工接管结束,重新规划返航")
                continue
            except _AbortRun as exc:
                await self._do_abort(exc.reason)
                return
            except (NavBackendError, _FailWaypoint, _ReturnHome) as exc:
                await self._do_abort(f"返航失败: {exc}")
                return
            break
        await self._transition(RunState.DONE, reason)

    async def _retrace_home(self) -> None:
        """沿来路回原点:已到过的点位倒序各走一段,最后一段到原点。

        每一段都是一次普通的 ``goto`` + 等终态,所以人在半路接管、暂停再继续
        走的仍是 ``_go_home`` 那个循环(``_ResumeReturnHome``/``_RetryWaypoint``
        从这儿一路抛上去),重来时按当时的 ``live.index`` 重算这条回路。
        「已到过」按 ``live.index`` 算:它是正在去的那个点,前面的都到过(或试过);
        没到过任何点就只剩原点这一段。原点没标的话没得回,交给上层按返航失败处理。
        """
        live = self._live
        assert live is not None
        legs = [wp.pose for wp in live.mission.waypoints[:live.index]][::-1]
        if self._home is None:
            raise _FailWaypoint("没有原点,不知道该回哪儿")
        legs.append(self._home.pose)
        for pose in legs:
            await self._stop_nav_quietly()
            await self._await_nav_standby(self._clock() + NAV_STANDBY_TIMEOUT_S)
            await self._nav.goto(pose)
            await self._wait_nav_terminal(self._clock() + RETURN_TIMEOUT_S)

    async def _finish(self) -> None:
        """收尾。**一定走完**(W00c6a):以前这里一抛(写盘失败),``_done.set()`` 走不到,
        引擎任务带着异常死掉、对外快照停在 RUNNING,代理的任务永远等下去。

        兜底路径自己炸了、走到这儿状态还不是终态:先停导航,再把状态落成 ABORTED(内存里
        一定落上;广播、记档尽力而为)。"""
        live = self._live
        try:
            if self._state not in FINAL_STATES:
                await self._stop_nav_quietly()
                self._state = RunState.ABORTED
                self._seen.add(RunState.ABORTED)
                try:
                    self._publish("引擎收尾时出错,按中止收尾")
                except Exception:
                    log.exception("收尾时广播也失败了")
            if live is not None:
                succeeded = sum(1 for r in live.results if r.ok)
                live.archive.finish({
                    "state": self._state.value,
                    "reason": self._snapshot.reason,
                    "succeeded": succeeded,
                    "failed": len(live.results) - succeeded,
                    "total": len(live.mission.waypoints) * live.mission.policy.loops,
                    "results": [r.to_wire() for r in live.results],
                })
                live.archive.close()
        except Exception:
            log.exception("引擎收尾出错(状态已落成 %s)", self._state.value)
        finally:
            try:
                await self._teardown()
            finally:
                self._done.set()

    async def _teardown(self) -> None:
        for task in self._forwarders:
            task.cancel()
        for task in self._forwarders:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._forwarders = []

    # ------------------------------------------------------------------ 定位

    async def _await_localized(self) -> None:
        """等定位收敛。等的期间照常处理事件 —— 包括暂停和让开腿。

        **``_RetryWaypoint`` 在这儿必须有人接。** 它是"人点了继续"的信号:
        ``_pause_until_resumed`` 和 ``_suspend_until_resumed`` 走到末尾都无条件
        抛它。这个函数原来一个 ``except`` 都没有,于是等定位的时候光按一下暂停
        再点继续,异常就一路漏给 ``_run`` 的兜底,整趟按
        ``引擎内部异常: _RetryWaypoint`` 中止 —— 屏幕上是一句没人看得懂的话,
        现场会读成"软件崩了"。而"等定位收敛"是现场执行单里一条明确的等待步骤,
        人盯着一条不动的狗按一下暂停再正常不过。

        接住之后**接着等收敛**:定位期间没有"当前点"可重发,``_RetryWaypoint``
        在这儿的正确含义就是"人回来了,继续等"。做法跟 60 行开外的 ``_go_home``
        (``except (_ResumeReturnHome, _RetryWaypoint)`` → ``continue``)是同一个,
        不是新机制,是把已有的做法补到漏掉的这一处。

        **只接 ``_RetryWaypoint``,不接 ``_ResumeReturnHome``** —— 核过:
        ``_ResumeReturnHome`` 只有 ``_suspend_until_resumed`` 在
        ``point.from_state is RETURNING`` 时才抛,而 ``_suspend_until_resumed``
        只有两个调用点,``from_state`` 拿的都是当下的 ``self._state``:
        ``_handle_command`` 那条在这个循环里看到的是 ``LOCALIZING``,
        ``_pause_until_resumed`` 那条看到的是 ``PAUSED``。两个都不是
        ``RETURNING``,所以那个异常到不了这儿。接一个到不了的异常等于给读代码
        的人立一块错路牌。
        """
        status = await self._nav.loc_status()
        deadline = self._clock() + LOCALIZE_TIMEOUT_S
        while status is not LocStatus.CONTINUOUS_LOC:
            item = await self._next(deadline - self._clock())
            if item is None:
                raise _AbortRun(
                    f"等定位收敛超过 {LOCALIZE_TIMEOUT_S:.0f}s,"
                    f"当前 {status.value if status else '未知'}")
            try:
                await self._handle(item)
            except _RetryWaypoint:
                # 状态翻回 LOCALIZING:上游那两条路都会先 ``_transition(RUNNING)``
                # 再抛,而这条狗根本没在跑点位,还在等收敛。这一闪是安全的,论证
                # 跟 ``_go_home`` 那一支一模一样:中间没有任何 ``self._next(...)``,
                # 引擎处理不了新事件。
                await self._transition(RunState.LOCALIZING, "人工继续,接着等定位收敛")
                # **预算重置。** 不重置的话,人暂停五分钟再继续,回来时
                # ``deadline - self._clock()`` 已经是负的,``_next`` 立刻返回
                # ``None`` → "等定位收敛超过 Ns" —— 那还是一次错误的中止,只是
                # 理由从看不懂换成了看得懂的假话:人没有"定位收敛失败",人只是
                # 按了暂停。纪律照 ``_do_waypoint`` 那一段:超时按**这一次尝试**算。
                deadline = self._clock() + LOCALIZE_TIMEOUT_S
                # 重新问一次:人接管的那几分钟里定位很可能已经收敛了,而那期间的
                # ``LocStatusEvent`` 被暂停/挂起那两条循环吃掉了(它们只记档,不往
                # 上转发)。不重问就会守着一个过时的 ``status`` 再等满一个
                # ``LOCALIZE_TIMEOUT_S``,然后中止一趟其实已经好了的任务。
                status = await self._nav.loc_status()
                continue
            if isinstance(item, LocStatusEvent):
                status = item.status

    # ------------------------------------------------------------------ 点位

    async def _do_waypoint(self, wp: MissionWaypoint) -> WaypointResult:
        live = self._live
        assert live is not None
        policy = live.mission.policy
        # 只有 retry_then_skip 才重试。abort / skip 都是一次定生死。
        attempts = 1 + (policy.waypoint_retry
                        if policy.on_waypoint_failed == "retry_then_skip" else 0)
        started = self._clock()
        note = ""
        tried = 0
        while tried < attempts:
            live.photos = []
            # 到点超时按**这一次尝试**算,不是从进这个点开始算:暂停五分钟
            # 再继续,不该一恢复就立刻判超时。
            attempt_started = self._clock()
            try:
                # 先等上一段导航的余温散掉。等的上限取两者中先到的那个:
                # 既不该超出这一次尝试的预算,也不值得为回落等满两分钟。
                await self._await_nav_standby(min(
                    attempt_started + policy.waypoint_timeout_s,
                    self._clock() + NAV_STANDBY_TIMEOUT_S))
                await self._nav.goto(wp.pose)
                self._note("nav", waypoint=wp.name, attempt=tried + 1)
                await self._wait_nav_terminal(
                    attempt_started + policy.waypoint_timeout_s)
                arrived_ms = self._stamp_ms()
                await self._do_actions(wp)
                return WaypointResult(wp.name, True, arrived_ms,
                                      self._clock() - started, tuple(live.photos))
            except _RetryWaypoint:
                continue                     # 暂停后继续,不算一次重试
            except _FailWaypoint as exc:
                note = exc.reason
                tried += 1
                self._note("waypoint_failed", waypoint=wp.name,
                           attempt=tried, reason=note)
        if policy.on_waypoint_failed == "abort":
            raise _AbortRun(f"点位 {wp.name} 失败: {note}")
        return WaypointResult(wp.name, False, 0, self._clock() - started, (), note)

    async def _await_nav_standby(self, deadline: float) -> None:
        """等导航状态机回落 StandBy。等的期间照常处理事件。

        不这么等,后果是整趟停在第二个点上:第一个点走完是 Succeed,紧接着
        下一个 start_nav 会被设备拒绝,而那是个 ``NavRequestError`` —— 落到
        兜底那一层,整趟按"引擎内部异常"中止。这条路径只有把多个点连起来跑
        才会露面,单点测试全绿也盖不住它。
        """
        status = await self._nav.nav_status()
        while status is not NavStatus.STANDBY:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _FailWaypoint(
                    f"导航没回到 StandBy,停在 "
                    f"{status.value if status else '未知'}")
            item = await self._next(min(remaining, _STANDBY_POLL_S))
            if item is None:
                status = await self._nav.nav_status()
                continue
            await self._handle(item)
            if isinstance(item, NavStatusEvent):
                status = item.status

    async def _wait_nav_terminal(self, deadline: float) -> None:
        """等到这一段导航有结果。等的期间照常处理事件。"""
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                await self._stop_nav_quietly()
                raise _FailWaypoint("到点超时")
            item = await self._next(remaining)
            if item is None:
                continue
            await self._handle(item)
            if isinstance(item, NavStatusEvent) and item.status in NAV_TERMINAL:
                if self._live is not None:
                    self._live.blocked_since = None
                if item.status is NavStatus.SUCCEED:
                    return
                # 跟"不知道狗在哪儿"(LOC_LOST)分开说:这里定位是好的、狗
                # 确实在地图里,只是这一段导航没能走到——可能是规划不出路
                # (被挪到隔断另一侧之类),也可能是别的原因,厂商这一层只给
                # 得出"没到"这个事实,不给原因。跟 LOC_LOST 混在一句话里,
                # 现场的人会分不清"该去找狗"还是"该去看这个点走不通"。
                raise _FailWaypoint(f"到不了下一个点(导航回了 {item.status.value})")

    async def _do_actions(self, wp: MissionWaypoint) -> None:
        for action in wp.actions:
            await self._do_action(wp, action)

    async def _do_action(self, wp: MissionWaypoint, action: Action) -> None:
        live = self._live
        assert live is not None
        if action.type == "dwell":
            deadline = self._clock() + (action.seconds or 0.0)
            while (remaining := deadline - self._clock()) > 0:
                item = await self._next(remaining)
                if item is not None:
                    await self._handle(item)
            return
        if action.type == "photo":
            source = self._media.get(action.camera or "")
            if source is None:
                raise _FailWaypoint(f"没有配 {action.camera!r} 这路相机的取流")
            try:
                frame = await source.grab()
            except MediaError as exc:
                raise _FailWaypoint(f"{action.camera} 取图失败: {exc}") from exc
            try:
                path = live.archive.save_photo(wp.name, action.camera or "", frame.data)
            except OSError as exc:
                # 盘满、只读重挂(W00c6a):这个点按失败走任务包的策略,不整趟中止 ——
                # 已经在跑的那一趟不因为盘满而停(storage.py),巡逻本身照样有价值。
                raise _FailWaypoint(f"照片存不下: {exc}") from exc
            live.photos.append(path.name)
            self._note("photo", waypoint=wp.name, camera=action.camera,
                       file=path.name)
            return
        if action.type == "light":
            await self._device.set_light(bool(action.on))
            return
        if action.type == "head":
            await self._device.set_gimbal(action.pitch or 0.0, action.yaw or 0.0)
            return
        raise _FailWaypoint(f"不认识的动作类型 {action.type!r}")

    # ------------------------------------------------------------ 事件与降级

    def _return_cost_pct(self) -> float:
        """从"当前在哪"估回原点的电量成本。

        **位置用"当前正要去的那个点",不用实时位姿。** NavBackend 没有位姿口子
        (位姿走的是 8091 那座桥),而正要去的那个点比实际位置更远 —— 往
        更费电的方向偏,正是我们要的方向。

        没标原点时返回 0,由 ``return_line_pct`` 退回静态返航线。
        起飞门槛(``preflight``)那一关会先把"没标原点"拦下来,所以跑到这里
        还没有原点,只可能是有人绕过了门槛。

        **系数用 ``self._return_params``,跟出发线那条用的是同一份。**
        写死 ``DEFAULT_RETURN_PARAMS`` 的话,标定落地那天出发线换成了实测值,
        飞行中的返航线还留在默认值上。
        """
        live = self._live
        home = self._home
        if live is None or home is None or not live.mission.waypoints:
            return 0.0
        index = min(live.index, len(live.mission.waypoints) - 1)
        here = live.mission.waypoints[index].pose
        return estimate_cost_pct(home.pose.distance_to(here),
                                 self._return_params)

    def _context(self) -> SafetyContext:
        live = self._live
        assert live is not None
        blocked_for = (0.0 if live.blocked_since is None
                       else self._clock() - live.blocked_since)
        return SafetyContext(policy=live.mission.policy,
                             battery_pct=live.battery_pct,
                             blocked_for_s=blocked_for,
                             loc_reset_attempts=live.loc_reset_attempts,
                             return_cost_pct=self._return_cost_pct(),
                             # 跟起飞门槛同一份系数。少了这一行,两支就会
                             # 按不同的地板算,标定改了 floor_pct 之后狗会
                             # 一起飞就掉头 —— 而没有一条测试会红。
                             floor_pct=self._return_params.floor_pct)

    async def _handle(self, item: Any) -> None:
        """处理一个输入。该怎么办由规则表说了算,状态机自己不写规则。"""
        live = self._live
        assert live is not None
        if isinstance(item, _Command):
            await self._handle_command(item)
            return
        if isinstance(item, BatteryEvent):
            live.battery_pct = item.percent
            self._apply(battery_ruling(self._context()), item)
            return
        if isinstance(item, DevicePoseEvent):
            live.last_pose = item.pose
            # 不 return:位姿照样交给规则表 —— 这里只是顺手留一份底,
            # 不是接管这个事件的处置。
        if isinstance(item, LocStatusEvent):
            live.last_loc = item.status
            # 不 return:事件照常交给规则表 —— 这里只是顺手留一份底。
        ruling = rule(item, self._context())
        if ruling.decision is Decision.WAIT and live.blocked_since is None:
            # 记下"从什么时候开始被挡的"。下一条同样的推送进来时,规则表拿这个
            # 时长决定是继续等还是判这个点失败。
            live.blocked_since = self._clock()
        if (isinstance(item, LocStatusEvent) and item.status is LocStatus.LOC_LOST
                and ruling.decision is Decision.PAUSE
                and self._state is RunState.RUNNING):
            # 只在跑点位的时候做恢复。LOCALIZING 阶段本来就在等收敛,
            # RETURNING 阶段插一脚重置只会把返航打断。
            await self._recover_localization(ruling.reason)
            return
        self._apply(ruling, item)

    def _apply(self, ruling: Ruling, item: Any) -> None:
        """把裁决翻成信号。位移相关的动作一律不在这里做。

        ``WAIT`` 与 ``PAUSE`` 在这里都不产生信号:``WAIT`` 只是记下起点,
        ``PAUSE`` 走 ``_recover_localization`` / ``_handle_command`` 那两条
        有明确恢复动作的路径。
        """
        if ruling.decision is Decision.CONTINUE:
            return
        self._note("ruling", event=type(item).__name__,
                   decision=ruling.decision.value, reason=ruling.reason)
        if ruling.decision is Decision.ABORT:
            raise _AbortRun(ruling.reason)
        if ruling.decision is Decision.RETURN_HOME:
            if self._state is RunState.RETURNING:
                return      # 已经在返航路上,再触发一次只会把返航打断
            raise _ReturnHome(ruling.reason)
        if ruling.decision is Decision.FAIL_WAYPOINT:
            raise _FailWaypoint(ruling.reason)

    async def _recover_localization(self, reason: str) -> None:
        """定位丢了:停下、重置、等收敛,然后重发当前点。"""
        live = self._live
        assert live is not None
        live.loc_reset_attempts += 1
        await self._transition(RunState.PAUSED, reason)
        await self._stop_nav_quietly()
        try:
            await self._nav.reset_localization()
        except NavBackendError as exc:
            raise _AbortRun(f"定位重置被拒: {exc}") from exc
        deadline = self._clock() + LOCALIZE_TIMEOUT_S
        while True:
            item = await self._next(deadline - self._clock())
            if item is None:
                raise _AbortRun(f"定位重置后 {LOCALIZE_TIMEOUT_S:.0f}s 仍未收敛")
            if (isinstance(item, LocStatusEvent)
                    and item.status is LocStatus.CONTINUOUS_LOC):
                break
            await self._handle(item)
        await self._transition(RunState.RUNNING, "定位已恢复")
        raise _RetryWaypoint

    async def _handle_command(self, cmd: _Command) -> None:
        if cmd.kind == "abort":
            raise _AbortRun(cmd.reason or "人工中止")
        if cmd.kind == "pause":
            await self._pause_until_resumed()
        if cmd.kind == "suspend":
            deny = _SUSPEND_UNSAFE_STATES.get(self._state)
            if deny is not None:
                # **诚实拒绝,不是硬撑着支持。** 进表的状态是那些"resume 之后
                # 的 ``_RetryWaypoint`` 没有一条安全的路能接住"的状态(见上面
                # 常量的注释)——真要支持,得先把那条路径走通,那是引擎行为的
                # 扩展,不是这里能顺手做的事。一句说得出口的拒绝,好过一趟
                # 悄悄中止、现场最难归因的那种失败。
                #
                # 表眼下是空的,所以这一支现在走不到;留着它和上面那张表是一
                # 回事 —— 加一项就立刻生效,不用重新发明一遍。
                #
                # ``_note`` 只落归档,不广播。旁边 ``resume_refused``
                # (``_suspend_until_resumed`` 里)两步都做——这里原来只做了
                # 一半:events.jsonl 里有,订阅方却什么都收不到。现场的人在
                # 手机上点「让开腿」,界面上没反应,只会以为按钮坏了,反复点。
                # 补上 ``_publish`` 才算把这句拒绝真的说出口。
                self._note("suspend_refused", state=self._state.value, reason=deny)
                self._publish(deny)
                return
            await self._suspend_until_resumed(cmd.reason)
        # resume 在没暂停的时候是空操作,不报错 —— 现场手快点两下很常见。

    async def _pause_until_resumed(self) -> None:
        """暂停:真的停下来,继续时重发当前点。

        用 ``stop`` 而不是 ``pause_nav``:厂商的暂停/继续语义没经真机验证,
        而"停 + 重发"这条路是走通过的,任务本来也是全逐点。
        """
        # 在翻进 ``PAUSED`` **之前**把暂停前的状态抄下来:翻完了就只剩
        # ``PAUSED``,而下面那道挂起闸要问的恰恰是"按暂停之前在干什么"。
        # 曾经的已知缺口(2026-09-11 修掉):``LOCALIZING`` 期间光按暂停再点
        # 继续,这里末尾那句 ``raise _RetryWaypoint`` 会漏给 ``_run`` 的兜底,
        # 整趟按"引擎内部异常"中止。那是暂停自己的病,拦挂起拦不掉它 ——
        # 所以修在 ``_await_localized``:它现在接住 ``_RetryWaypoint``,重置
        # 超时预算,接着等收敛。
        before = self._state
        await self._transition(RunState.PAUSED, "人工暂停")
        await self._stop_nav_quietly()
        while True:
            item = await self._next(None)
            if isinstance(item, _Command):
                if item.kind == "abort":
                    raise _AbortRun(item.reason or "人工中止")
                if item.kind == "resume":
                    break
                if item.kind == "suspend":
                    # 人拍的板(2026-09-09):暂停中允许人工接管。
                    #
                    # **实现成"多一条通往挂起的路",不是放宽遥控那道闸**
                    # (裁决四)。放宽闸会造出一个引擎不知道的状态:人在
                    # PAUSED 下开着狗,而下面那句 break 是照着"暂停期间没人
                    # 碰狗"写的——它不查 _busy_reason()。人左手压着摇杆点
                    # 继续,引擎和人就同时在给腿下指令。
                    #
                    # 转进 SUSPENDED 之后,"谁在动这条狗"就是显式的:继续
                    # 走的是挂起那一支,那一支查 busy。
                    #
                    # **但这道闸照样要过。** 查的是 ``before`` 不是
                    # ``self._state``:这一刻状态已经是 ``PAUSED``,拿它去查
                    # ``_SUSPEND_UNSAFE_STATES`` 永远查不到,同一个"让开腿"
                    # 的意图在 ``_handle_command`` 那条路上被拒、在这条路上
                    # 却受理 —— 先按暂停就能把闸绕过去,同一个意图两条路两个
                    # 答案。**表现在是空的,这道闸照样要留、照样查 ``before``**:
                    # 下一个往表里加状态的人不必再想一遍这件事,加一项就两条路
                    # 一起生效。
                    deny = _SUSPEND_UNSAFE_STATES.get(before)
                    if deny is None:
                        await self._suspend_until_resumed(item.reason)
                    else:
                        # 落档 + 广播两步都做,跟 ``_handle_command`` 那条路
                        # 一模一样:只落档不广播,现场的人在手机上看不到任何
                        # 反应,只会以为按钮坏了。``state`` 记的是 ``before``
                        # —— 说清楚是"因为暂停之前在重定位"才拒的,记成
                        # ``PAUSED`` 反而让事后翻档的人看不懂。
                        self._note("suspend_refused", state=before.value,
                                   reason=deny)
                        self._publish(deny)
                continue
            if isinstance(item, BatteryEvent) and self._live is not None:
                self._live.battery_pct = item.percent
            # 暂停期间其他事件只记录:停着的时候没有"当前点失败"这回事。
            self._note("paused_event", event=type(item).__name__)
        await self._transition(RunState.RUNNING, "人工继续")
        raise _RetryWaypoint

    async def _suspend_until_resumed(self, reason: str) -> None:
        """挂起:停下来让开腿,人接管完再继续,回来时重发当前点。

        跟 ``_pause_until_resumed`` 长得很像,但**不能合并**:这一支要在
        继续之前查 ``_busy_checks``,而暂停那一支不查也不该查(暂停期间本来
        就没人碰狗)。合了之后两条相反的规矩会落在同一段代码上。

        回来重发当前点,是因为人可能已经把狗开到别处去了 —— 接着往下走等于
        从一个没人知道的位置出发。

        **"重发当前点"只是跑点位那一支的做法。** 从返航路上挂起的,回来要重新
        规划回家(任务 13),这两支靠 ``SuspendPoint.from_state`` 分开 —— 显式
        字段,不是去解析 ``reason`` 里那句人话。
        """
        live = self._live
        assert live is not None
        wps = live.mission.waypoints
        # **序号和名字夹同一个 ``idx``。** 只夹名字不夹序号,越界的那个序号会
        # 顺着 ``to_wire`` 流到快照上,而返航段上"当前点位"这个概念本来就不
        # 成立 —— 手机上会显示一个不存在的点。
        idx = min(live.index, len(wps) - 1)
        started = self._clock()
        # 在翻进 SUSPENDED **之前**抄下来:翻完了就只剩 SUSPENDED,再也问不出
        # 是从哪儿进来的。
        #
        # 抄进一个局部变量 ``point`` 再往下读,**不是另存一份 ``from_state``
        # 局部量**:那样这个字段就成了没人读的装饰,而"人接管完之后该往哪走"
        # 这件事又回到了存两份的老路上。``live.suspended`` 在下面的 ``finally``
        # 里会被清成 ``None``,所以读的是这个局部引用,不是那个字段位置。
        point = SuspendPoint(
            waypoint_index=idx, waypoint_name=wps[idx].name,
            pose=live.last_pose, reason=reason,
            at_ms=self._stamp_ms(), from_state=self._state,
            prior_suspend_ms=live.suspend_total_ms)
        live.suspended = point
        await self._transition(RunState.SUSPENDED, reason)
        await self._stop_nav_quietly()
        try:
            while True:
                item = await self._next(None)
                if isinstance(item, _Command):
                    if item.kind == "abort":
                        raise _AbortRun(item.reason or "人工中止")
                    if item.kind == "resume":
                        blocked = self._busy_reason()
                        if not blocked:
                            if self.loc_lost:
                                # 人拍的板(2026-09-10):不在地图里就直接报错,
                                # 明说、不猜、不试着走、不静默中止。
                                #
                                # **不离开 SUSPENDED**:报错不是中止,人可以把狗
                                # 牵回地图里再点一次。走 abort 的话这一趟就没了,
                                # 而人要的恰恰是把它救回来。
                                deny = "狗不在地图里,找不到下一个点"
                                self._note("resume_refused", reason=deny)
                                base = live.suspended.reason if live.suspended else ""
                                self._publish(f"{base} —— 还不能继续: {deny}"
                                             if base else f"还不能继续: {deny}")
                                continue
                            break
                        # **人点「继续」的时候左手很可能还压在摇杆上。** 那一瞬间
                        # 引擎和人同时在给腿下指令 —— add_busy_check 存在的全部
                        # 理由就是它。说清楚为什么没继续,然后接着等。
                        self._note("resume_refused", reason=blocked)
                        # 拒绝理由和挂起理由是两件事,不许互相覆盖:直接
                        # ``self._publish(blocked 那句)`` 会把 snapshot.reason
                        # 从"为什么挂起"整个换成"为什么没能继续",人再看
                        # 快照就想不起来当初为什么停在这儿。用
                        # ``live.suspended.reason``(挂起时就定了、之后不再
                        # 变)当稳定的底,不用 ``self._snapshot.reason`` ——
                        # 后者如果已经被上一次拒绝改过,再拿来接就会一次比
                        # 一次长。
                        base = live.suspended.reason if live.suspended else ""
                        self._publish(f"{base} —— 还不能继续: {blocked}"
                                     if base else f"还不能继续: {blocked}")
                        continue
                    continue
                if isinstance(item, DevicePoseEvent):
                    # 人开着的时候位姿一直在变 —— 每帧 odom 一条(见
                    # sidecar_device.py),接管期间是持续流,几分钟就能灌上千条。
                    # **不落盘。** 落了就是几分钟接管往归档里写上千行,还在事件
                    # 循环里做上千次阻塞写;最新那份已经够用,存 live.last_pose
                    # 就行。留着最新的 —— 但**不覆盖** ``live.suspended.pose``:
                    # 那一份记的是「人接管前在哪儿」,被现在的位置盖掉就什么都
                    # 不剩了。
                    live.last_pose = item.pose
                    continue
                if isinstance(item, BatteryEvent):
                    live.battery_pct = item.percent
                if isinstance(item, LocStatusEvent):
                    # 这条循环是 SUSPENDED 自己的等待,不经过 _handle —— 跟
                    # 位姿/电量一样顺手留一份底,好让上面 resume 分支的
                    # loc_lost 检查有东西可查(人拍的板 2,2026-09-10)。
                    live.last_loc = item.status
                self._note("suspended_event", event=type(item).__name__)
        finally:
            # ``finally`` 而不是分别在 abort / resume 两条路径上各清一次:
            # 漏了第三条退出路径(比如这个协程被直接 ``cancel()``,不经过
            # 命令队列)就会剩下一份挂起快照,快照上写着"正在被接管"但其实
            # 这一趟已经没了。
            live.suspended = None
            # 这一次挂了多久,记进累计总账(见
            # ``SuspendPoint.prior_suspend_ms``)。放在 ``finally`` 不是跟
            # 上面一句"图省事共用一个块",是这里**只能**放在 ``finally``:
            # 这个函数没有一条正常落地的出口,``break`` 出循环之后走的是
            # ``raise _ResumeReturnHome`` 或 ``raise _RetryWaypoint``
            # (就在这个函数末尾那两句 ``raise``),``abort`` 分支走的是
            # ``raise _AbortRun``——三条出口全是异常,没地方能在 ``try``
            # 主体里正常写"累加"这一句。
            live.suspend_total_ms += int((self._clock() - started) * 1000)
        if point.from_state is RunState.RETURNING:
            # 返航路上接管的那一支:交给 ``_go_home`` 重新规划回家,不在这儿
            # 翻状态 —— 它自己那一圈会把状态翻回 RETURNING。
            raise _ResumeReturnHome
        await self._transition(RunState.RUNNING, "人工接管结束")
        raise _RetryWaypoint

    def _busy_reason(self) -> str:
        """现在有别人在动这条狗吗。返回理由,空串表示没有。

        回调是 ``app/`` 层注册的,引擎管不了它内部会不会炸。**炸了不能当成
        "没人占用"放行**:一个抛错的检查意味着"不知道现在安不安全",而这
        道检查存在的全部意义就是"不确定就不放"。所以异常本身变成占用理由,
        现场的人看得到真实原因,狗也不会在人还握着摇杆时动起来——把它当
        "没查出问题"处理才是真正危险的那个方向。
        """
        try:
            return self._raw_busy_reason()
        except _BusyCheckFailed as exc:
            return str(exc)

    def _raw_busy_reason(self) -> str:
        for check in self._busy_checks:
            try:
                reason = check()
            except Exception as exc:  # 见 _BusyCheckFailed:就地转译再 raise
                name = getattr(check, "__name__", repr(check))
                raise _BusyCheckFailed(
                    f"安全检查出错,不能继续: {name} {type(exc).__name__}: {exc}"
                ) from exc
            if reason:
                return reason
        return ""

    async def _stop_nav_quietly(self) -> None:
        """停导航。已经在收尾的路径上,停不下来也只能记一笔。"""
        try:
            await self._nav.stop()
        except NavBackendError as exc:
            self._note("stop_failed", reason=str(exc))
