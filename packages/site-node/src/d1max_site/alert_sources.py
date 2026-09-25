"""站点从狗报的事实里认出告警(W00c5a;原先住在狗上的老服务里,§5.2 那张表的左半边)。

**狗只报事实,判定在站点**(总设计 §3.3)。事实来自 MQTT:状态(在线、就绪四项、当前任务)、
事件(任务终态、巡检点位、``robot_fault``)、遥测(时间戳),外加站点自己看得见的两件事:
状态过期(掉线)、排程这一拍没办成。

**认事实,不判级。** 哪个 ``kind`` 是几级只在 ``alerts.LEVEL_OF`` 里说一次。

**状态是状态,不是事件。** 同一份状态会反复来(定时重发、重连补发),``raise_alert`` 喂一次
``count`` 就加一 —— 所以每台狗记着「上次是什么」,只在**变了**的那一刻报一次。记忆按狗分开:
两台狗同时急停是两件事。

**认不出来的就不报。** 跌倒按故障文字认(词表原样搬来,**待真机**:按故障码认);形状不对的
``robot_fault`` 记日志、不报。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from d1max_contract.errors import ContractError
from d1max_contract.messages import (
    Event,
    Status,
    TaskState,
    Telemetry,
    parse_fault_event_data,
)
from d1max_contract.schedule import clock_skew
from d1max_site.alert_store import AlertDesk

log = logging.getLogger(__name__)

#: 故障文本里出现这些词就认作跌倒。**这是个假设,真机清单里要量**:厂商故障帧只有自由文本,
#: 没有结构化的「跌倒」标记。不收单独一个 ``fall``:它会命中 ``fallback``,误报一条 P1 的代价
#: 是有人半夜开车出门。
FALLEN_WORDS: tuple[str, ...] = ("跌倒", "摔倒", "倒地", "fallen", "fall down", "falldown",
                                 "tipped over")

#: 失败理由里出现这些词才认作「没电中止」。代理引擎那条理由原文是「电量 22% 低于中止线 25%」。
BATTERY_WORDS: tuple[str, ...] = ("电量", "电池", "battery")

#: 站点自己那一行告警的 ``robot``。
SITE = "site"

#: 站点主循环多久调一次 :meth:`SiteAlertSources.step`(秒)。升级时限是分钟级,5 s 够细。
STEP_S = 5.0


def _hit(text: str, words: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(w in low for w in words)


@dataclass
class _Mem:
    """一台狗的「上次是什么」。"""

    estop: bool = False
    loc_lost_running: bool = False
    fallen: bool = False
    skew: bool = False
    offline: bool = False
    running: bool = False
    started: str = ""


class SiteAlertSources:
    def __init__(self, desk: AlertDesk, *, now_ms: Callable[[], int],
                 is_stale: Callable[[str], bool] | None = None) -> None:
        self.desk = desk
        self._now = now_ms
        #: 这台狗的状态是不是过期了(派遣器按站点的钟算)。``None`` = 不看过期。
        self._is_stale = is_stale
        self._mem: dict[str, _Mem] = {}
        self._site_errors: dict[str, str] = {}

    def attach(self, dispatcher) -> None:
        """挂到派遣器的三条上行回调上。"""
        dispatcher.on_status(self.on_status)
        dispatcher.on_event(self.on_event)
        dispatcher.on_telemetry(self.on_telemetry)
        self._is_stale = self._is_stale or dispatcher.is_stale

    def _m(self, rid: str) -> _Mem:
        return self._mem.setdefault(rid, _Mem())

    # ------------------------------------------------------------ 状态

    def on_status(self, rid: str, s: Status) -> None:
        m = self._m(rid)
        if not s.online:
            # 遗言:ready 全是假,那不是急停、不是丢定位 —— 是掉线。
            self._offline(rid, m)
            return
        m.offline = False
        running = s.task is not None and s.task.state is TaskState.RUNNING
        m.running = running
        estop = not s.ready.estop_clear
        if estop and not m.estop:
            self.desk.raise_alert(kind="estop_pressed", robot=rid, title="急停被按下",
                                  detail="狗报急停没解除")
        m.estop = estop
        # 代理的引擎丢定位就暂停:「跑着任务 + 定位不行」就是原来的「定位丢失后暂停」。
        loc = running and not s.ready.loc_ok
        if loc and not m.loc_lost_running:
            self.desk.raise_alert(kind="loc_lost_paused", robot=rid, title="定位丢失后暂停",
                                  detail=f"任务 {s.task.task_id} 在跑,狗报定位不行")
        m.loc_lost_running = loc
        if running and s.task.task_id != m.started:
            m.started = s.task.task_id
            self.desk.raise_alert(kind="run_start", robot=rid,
                                  title=f"开跑:{s.task.kind} {s.task.task_id}")

    def step(self) -> None:
        """站点主循环每 ``STEP_S`` 秒调一次:看掉线,再让 P1 未确认的升档。"""
        self.tick()
        self.desk.escalate()

    def tick(self) -> None:
        """站点主循环每几秒调一次:状态过期 = 掉线(遗言也许没到)。"""
        if self._is_stale is None:
            return
        for rid, m in list(self._mem.items()):
            if not m.offline and self._is_stale(rid):
                self._offline(rid, m)

    def _offline(self, rid: str, m: _Mem) -> None:
        if m.offline:
            return
        m.offline = True
        if m.running:
            self.desk.raise_alert(kind="robot_offline", robot=rid, title="狗掉线了(跑着任务)",
                                  detail=f"最后在跑 {m.started}")
        else:
            self.desk.raise_alert(kind="robot_offline_idle", robot=rid, title="狗掉线了")
        # 掉线期间的事看不见:回来之后重新从它报的状态认起。
        m.estop = m.loc_lost_running = False

    # ------------------------------------------------------------ 事件

    def on_event(self, rid: str, e: Event) -> None:
        d = e.data if isinstance(e.data, dict) else {}
        tid = d.get("task_id", "")
        if e.kind == "task_done":
            self.desk.raise_alert(kind="run_done", robot=rid, title=f"跑完了:{tid}")
        elif e.kind == "task_failed":
            reason = str(d.get("reason", ""))
            if _hit(reason, BATTERY_WORDS):
                self.desk.raise_alert(kind="battery_abort", robot=rid,
                                      title="电量不足,整趟中止", detail=reason)
            else:
                # 人点的中止是 task_aborted,不报;失败(引擎自己收的尾)一律报,认不出原因也报。
                self.desk.raise_alert(kind="run_abort", robot=rid, title="整趟中止了",
                                      detail=reason)
        elif e.kind == "patrol_waypoint" and d.get("ok") is False:
            self.desk.raise_alert(kind="stuck", robot=rid, title=f"点位 {d.get('name', '?')} 没到",
                                  detail=str(d.get("note", "")))
        elif e.kind == "robot_fault":
            try:
                faults = parse_fault_event_data(d)
            except ContractError as exc:
                log.warning("%s 的 robot_fault 形状不对,不判: %s", rid, exc)
                return
            m = self._m(rid)
            text = "; ".join(f"[{f.code}] {f.text}" for f in faults)
            fallen = _hit(text, FALLEN_WORDS)
            if fallen and not m.fallen:
                self.desk.raise_alert(kind="fallen", robot=rid, title="狗跌倒了", detail=text)
            m.fallen = fallen

    # ------------------------------------------------------------ 遥测

    def on_telemetry(self, rid: str, t: Telemetry) -> None:
        skew = clock_skew(local_ms=t.stamp, reference_ms=self._now(), source="站点")
        m = self._m(rid)
        if skew.alarm and not m.skew:
            self.desk.raise_alert(kind="clock_skew", robot=rid, title="狗的钟不准",
                                  detail=f"跟站点差 {skew.skew_s:.0f} 秒")
        m.skew = skew.alarm

    # ------------------------------------------------------------ 站点自己

    def on_site_error(self, what: str, error: str) -> None:
        """站点自己的一条协程这一拍没办成(``error`` 非空)。只在由好变坏的那一刻报。"""
        before = self._site_errors.get(what, "")
        self._site_errors[what] = error
        if error and not before and what == "schedule":
            self.desk.raise_alert(kind="schedule_died", robot=SITE, title="排程没办成",
                                  detail=f"到点可能没人起跑:{error}")
