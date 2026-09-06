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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from d1max_patrol.backends.base import (
    BatteryEvent,
    DeviceBackend,
    EventEmitter,
    LocStatusEvent,
    MediaError,
    MediaSource,
    NavBackend,
    NavBackendError,
    NavStatusEvent,
)
from d1max_patrol.engine.archive import RunArchive
from d1max_patrol.engine.form import STANDALONE, Form
from d1max_patrol.engine.homing import (
    DEFAULT_RETURN_PARAMS,
    HomePoint,
    ReturnParams,
    estimate_cost_pct,
)
from d1max_patrol.engine.mission import Action, Mission, MissionWaypoint
from d1max_patrol.engine.preflight import PreflightReport, run_preflight
from d1max_patrol.engine.removable import (
    DEFAULT_PROBE,
    RemovableProbe,
    scan_or_unknown,
)
from d1max_patrol.engine.safety import (
    Decision,
    Ruling,
    SafetyContext,
    battery_ruling,
    rule,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus

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
    RETURNING = "RETURNING"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    DONE = "DONE"


#: 跑完了的状态。``wait_done`` 等的就是这两个。
FINAL_STATES = frozenset({RunState.DONE, RunState.ABORTED})


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

    def to_wire(self) -> dict[str, Any]:
        return {"state": self.state.value, "mission": self.mission,
                "waypoint_index": self.waypoint_index,
                "waypoint_name": self.waypoint_name, "total": self.total,
                "started_ms": self.started_ms, "reason": self.reason,
                "results": [r.to_wire() for r in self.results]}


# ------------------------------------------------------------------ 内部信号


@dataclass(frozen=True, slots=True)
class _Command:
    """用户命令也走队列 —— 直接改状态就把并发修改放回来了。"""

    kind: str          # pause / resume / abort
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

        引擎是按进程建的,原点却是按地图选的(见 ``app/server.py``):构造时
        给的那份只是初始值,真正对得上"这一趟跑哪张图"的那份,由调用方在
        这里换进来。**只在真的要开跑的这条路径上换**——挂在"已经在跑"或
        某个 ``_busy_checks`` 上的请求,不许动正在飞的那趟手里的原点。
        """
        if self.running:
            raise EngineBusy(f"已经在跑 {self._snapshot.mission},先停下来再开新的")
        for check in self._busy_checks:
            reason = check()
            if reason:
                raise EngineBusy(reason)
        self._home = home
        archive = RunArchive(self._runs_root, mission)
        archive.write_manifest(self._fingerprint)
        self._live = _Live(mission, archive, started_ms=int(time.time() * 1000))
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
            self._snapshot = RunSnapshot(self._state, "", 0, "", 0, 0, reason=reason)
            self.emit(self._snapshot)
            return
        wps = live.mission.waypoints
        idx = min(live.index, len(wps) - 1)
        self._snapshot = RunSnapshot(
            state=self._state, mission=live.mission.mission,
            waypoint_index=live.index, waypoint_name=wps[idx].name,
            total=len(wps), started_ms=live.started_ms,
            results=tuple(live.results), reason=reason)
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
        except Exception as exc:  # noqa: BLE001 - 兜底,理由见下
            # 让引擎带着异常静静死掉是最糟的结局:页面上还显示 RUNNING,
            # 人在外面等着。任何意外都要落成一个说得清原因的 ABORTED。
            await self._do_abort(f"引擎内部异常: {type(exc).__name__}: {exc}")
        finally:
            await self._finish()

    async def _do_abort(self, reason: str) -> None:
        """中止 = 停止导航 + 记录 + 告警。**不含任何位移**(主规范 §6.4)。"""
        await self._transition(RunState.ABORTING, reason)
        await self._stop_nav_quietly()
        await self._transition(RunState.ABORTED, reason)

    async def _go_home(self, reason: str) -> None:
        await self._transition(RunState.RETURNING, reason)
        try:
            await self._stop_nav_quietly()
            # 返航也是一次 start_nav,一样只在 StandBy 下受理。
            await self._await_nav_standby(self._clock() + NAV_STANDBY_TIMEOUT_S)
            await self._nav.return_home()
            await self._wait_nav_terminal(self._clock() + RETURN_TIMEOUT_S)
        except _AbortRun as exc:
            await self._do_abort(exc.reason)
            return
        except (NavBackendError, _FailWaypoint, _ReturnHome, _RetryWaypoint) as exc:
            await self._do_abort(f"返航失败: {exc}")
            return
        await self._transition(RunState.DONE, reason)

    async def _finish(self) -> None:
        live = self._live
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
        await self._teardown()
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
        status = await self._nav.loc_status()
        deadline = self._clock() + LOCALIZE_TIMEOUT_S
        while status is not LocStatus.CONTINUOUS_LOC:
            item = await self._next(deadline - self._clock())
            if item is None:
                raise _AbortRun(
                    f"等定位收敛超过 {LOCALIZE_TIMEOUT_S:.0f}s,"
                    f"当前 {status.value if status else '未知'}")
            await self._handle(item)
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
                arrived_ms = int(time.time() * 1000)
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
                raise _FailWaypoint(f"导航回了 {item.status.value}")

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
            path = live.archive.save_photo(wp.name, action.camera or "", frame.data)
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
                             return_cost_pct=self._return_cost_pct())

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
        # resume 在没暂停的时候是空操作,不报错 —— 现场手快点两下很常见。

    async def _pause_until_resumed(self) -> None:
        """暂停:真的停下来,继续时重发当前点。

        用 ``stop`` 而不是 ``pause_nav``:厂商的暂停/继续语义没经真机验证,
        而"停 + 重发"这条路是走通过的,任务本来也是全逐点。
        """
        await self._transition(RunState.PAUSED, "人工暂停")
        await self._stop_nav_quietly()
        while True:
            item = await self._next(None)
            if isinstance(item, _Command):
                if item.kind == "abort":
                    raise _AbortRun(item.reason or "人工中止")
                if item.kind == "resume":
                    break
                continue
            if isinstance(item, BatteryEvent) and self._live is not None:
                self._live.battery_pct = item.percent
            # 暂停期间其他事件只记录:停着的时候没有"当前点失败"这回事。
            self._note("paused_event", event=type(item).__name__)
        await self._transition(RunState.RUNNING, "人工继续")
        raise _RetryWaypoint

    async def _stop_nav_quietly(self) -> None:
        """停导航。已经在收尾的路径上,停不下来也只能记一笔。"""
        try:
            await self._nav.stop()
        except NavBackendError as exc:
            self._note("stop_failed", reason=str(exc))
