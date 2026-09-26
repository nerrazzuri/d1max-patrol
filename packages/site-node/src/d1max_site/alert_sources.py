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
from d1max_contract.storage import WARN_RATIO
from d1max_site.alert_store import AlertDesk
from d1max_site.priorities import STANDBY_PREFIX

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

#: 发件箱最老一条待传的等了这么久(秒)还没传上来,出 ``upload_backlog``;降到下面那个数以下才重新武装。
BACKLOG_ALARM_S = 1800
BACKLOG_CLEAR_S = 600
#: 盘水位回落到这以下,``disk_80`` 才重新武装(迟滞:不在 80% 上下抖出一串告警)。
DISK_CLEAR_RATIO = 0.75

#: 掉线告警的迟滞:回来之后要连着在线这么久(毫秒),再掉线才另起一条。4G 抖一下就一条新 P1,
#: 人很快就不看 P1 了;这段时间里又掉的,算同一次。
OFFLINE_REARM_MS = 60_000


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
    disk80: bool = False
    backlog: bool = False
    offline: bool = False
    running: bool = False
    started: str = ""
    boot_id: str = ""
    #: 由掉线变回在线的时刻(站点的钟);``None`` = 还没掉过线。掉线告警的迟滞用它。
    online_since: int | None = None


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
        """挂到派遣器的三条上行回调上。挂之前先用库里**最后见过**的状态给每台狗的记忆做种:
        站点重启后记忆是空的,不做种的话,重启前已经确认过、还按着的急停会另起一条没确认的 P1,
        停机期间跑着任务掉线的狗会被报成空闲掉线(P2)(W00c5a 内部评审)。"""
        import json

        from d1max_contract.errors import ContractError as _CE
        for row in dispatcher.db.query("SELECT robot_id, status FROM robot_state "
                                       "WHERE status IS NOT NULL"):
            try:
                self.seed(row["robot_id"], Status.from_wire(json.loads(row["status"])))
            except (_CE, ValueError, TypeError):
                log.warning("%s 库里的状态读不懂,不做种", row["robot_id"])
        dispatcher.on_status(self.on_status)
        dispatcher.on_event(self.on_event)
        dispatcher.on_telemetry(self.on_telemetry)
        # 站点自己推的「没回待命点」(W00c6b):以前只进 SSE 事件流,告警簿不收、值守屏看不见。
        dispatcher.feed.listen(self.on_feed)
        self._is_stale = self._is_stale or dispatcher.is_stale

    def _m(self, rid: str) -> _Mem:
        return self._mem.setdefault(rid, _Mem())

    def seed(self, rid: str, s: Status) -> None:
        """用一份**已经知道**的状态给记忆做种,不报任何告警。"""
        m = self._m(rid)
        m.boot_id = s.boot_id
        m.offline = not s.online
        if s.online:
            m.running = s.task is not None and s.task.state is TaskState.RUNNING
            m.estop = not s.ready.estop_clear
            m.loc_lost_running = m.running and not s.ready.loc_ok
            if m.running:
                m.started = s.task.task_id

    # ------------------------------------------------------------ 状态

    def on_status(self, rid: str, s: Status) -> None:
        m = self._m(rid)
        if not s.online:
            # 遗言:ready 全是假,那不是急停、不是丢定位 —— 是掉线。
            self._offline(rid, m)
            return
        if s.boot_id != m.boot_id:
            # 狗重启过:它上一辈子的故障(跌倒)不再算数,起来第一拍它会报全集。
            m.boot_id = s.boot_id
            m.fallen = False
        if m.offline:
            m.online_since = self._now()
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
        """站点主循环每 ``STEP_S`` 秒调一次:看掉线,再让 P1 未确认的升档,再修剪内存。"""
        self.tick()
        self.desk.escalate()
        self.desk.trim()

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
        # 掉线期间的事看不见:回来之后重新从它报的状态认起。跌倒的记忆也清掉(狗可能被扶起、重启)。
        m.estop = m.loc_lost_running = m.fallen = False
        if m.online_since is not None and self._now() - m.online_since < OFFLINE_REARM_MS:
            log.info("%s 回来不到 %d s 又掉线:算同一次,不另起告警", rid, OFFLINE_REARM_MS // 1000)
            return
        if m.running:
            self.desk.raise_alert(kind="robot_offline", robot=rid, title="狗掉线了(跑着任务)",
                                  detail=f"最后在跑 {m.started}")
        else:
            self.desk.raise_alert(kind="robot_offline_idle", robot=rid, title="狗掉线了")

    # ------------------------------------------------------------ 事件

    def on_event(self, rid: str, e: Event) -> None:
        d = e.data if isinstance(e.data, dict) else {}
        tid = d.get("task_id", "")
        if e.kind == "task_done":
            self.desk.raise_alert(kind="run_done", robot=rid, title=f"跑完了:{tid}")
        elif e.kind == "task_failed":
            reason = str(d.get("reason", ""))
            # 站点自己派的回待命点那一趟(回程巡检)失败,狗停在半路;标题别说「整趟中止」
            # (W00c6b 内审)。
            back = str(tid).startswith(STANDBY_PREFIX)
            if _hit(reason, BATTERY_WORDS):
                self.desk.raise_alert(kind="battery_abort", robot=rid,
                                      title="电量不足,回待命点停在半路" if back
                                      else "电量不足,整趟中止", detail=reason)
            else:
                # 人点的中止是 task_aborted,不报;失败(引擎自己收的尾)一律报,认不出原因也报。
                self.desk.raise_alert(kind="run_abort", robot=rid,
                                      title="回待命点没成,停在半路" if back else "整趟中止了",
                                      detail=reason)
        elif e.kind in ("release_install_failed", "release_activate_failed",
                        "release_rollback_failed"):
            what = {"release_install_failed": "装版本", "release_activate_failed": "切版本",
                    "release_rollback_failed": "退版本"}[e.kind]
            self.desk.raise_alert(kind="release_failed", robot=rid,
                                  title=f"{what}没成:{d.get('name', '')}",
                                  detail=str(d.get("reason", ""))[:300])
        elif e.kind == "release_rolled_back" and d.get("no_fallback"):
            # 上一版是老服务那一代,退不回去,开机守卫把狗留在了新版(W00c5e 内部评审)。
            self.desk.raise_alert(kind="release_failed", robot=rid,
                                  title=f"新版 {d.get('from', '?')} 一直没坐实,上一版退不回去,"
                                        "狗留在新版",
                                  detail=f"开机守卫数了 {d.get('attempts', '?')} 次;看狗到站点的"
                                         "网络、证书、站点地址")
        elif e.kind == "release_rolled_back":
            # 新版起不来,狗上的开机守卫退回了上一版(W00c5d 第三部分内部评审)。
            self.desk.raise_alert(kind="release_failed", robot=rid,
                                  title=f"新版 {d.get('from', '?')} 起不来,狗自己退回了 "
                                        f"{d.get('to', '?')}",
                                  detail=f"开机守卫数了 {d.get('attempts', '?')} 次")
        elif e.kind in ("map_activate_failed", "map_build_failed"):
            what = "换图" if e.kind == "map_activate_failed" else "重建"
            which = f"{d.get('map_id', '?')}:{d.get('version', '?')}"
            self.desk.raise_alert(kind="map_failed", robot=rid, title=f"{what}没成:{which}",
                                  detail=str(d.get("reason", ""))[:300])
        elif e.kind == "archive_write_failed":
            # 盘满、只读重挂(W00c6a):那一趟照跑,但照片、记录没存下 —— 证据缺了,多半是盘满或盘坏了。
            self.desk.raise_alert(kind="archive_failed", robot=rid,
                                  title=f"狗上这一趟的记录写不进去:{tid}",
                                  detail=str(d.get("reason", ""))[:300])
        elif e.kind == "patrol_waypoint" and d.get("ok") is False \
                and str(d.get("note", "")).startswith("照片存不下"):
            # 到了、只是照片存不下(W00c6a 内审):不是「没到」;跟「记录写不进去」并成一类,
            # 不每个点刷一条。
            self.desk.raise_alert(kind="archive_failed", robot=rid,
                                  title=f"点位 {d.get('name', '?')} 的照片存不下",
                                  detail=str(d.get("note", "")))
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
        if t.storage is not None:
            self._storage(rid, m, t.storage)

    def _storage(self, rid: str, m: _Mem, f) -> None:
        """狗的发件箱(W00c5d,决策 8):盘到 80%、证据积压半小时,各报一次;回落之后才重新武装。"""
        if f.disk_used_ratio >= WARN_RATIO and not m.disk80:
            m.disk80 = True
            self.desk.raise_alert(
                kind="disk_80", robot=rid, title="狗的发件箱所在的盘快满了",
                detail=f"已用 {f.disk_used_ratio:.0%};到 90% 狗就不接新的巡检(不删没传完的)")
        elif f.disk_used_ratio < DISK_CLEAR_RATIO:
            m.disk80 = False
        oldest = f.oldest_backlog_s or 0
        if oldest >= BACKLOG_ALARM_S and not m.backlog:
            m.backlog = True
            self.desk.raise_alert(
                kind="upload_backlog", robot=rid, title="狗上的证据半小时没传上来",
                detail=f"积压 {f.backlog_files} 个文件、{f.backlog_bytes // 1024} KB,"
                       f"最老的等了 {oldest // 60} 分钟")
        elif oldest < BACKLOG_CLEAR_S:
            m.backlog = False

    # ------------------------------------------------------------ 站点自己

    def on_feed(self, item: dict) -> None:
        """站点推送流里的一条。只管 ``standby_failed``(自动回待命点没派成,狗停在原地)。"""
        if item.get("kind") != "standby_failed":
            return
        self.desk.raise_alert(kind="standby_failed", robot=str(item.get("robot_id", SITE)),
                              title="没回待命点",
                              detail=f"{item.get('after', '?')} 结束后:"
                                     f"{item.get('reason', '')}"[:300])

    def on_site_error(self, what: str, error: str) -> None:
        """站点自己的一条协程这一拍没办成(``error`` 非空)。只在由好变坏的那一刻报。"""
        before = self._site_errors.get(what, "")
        self._site_errors[what] = error
        if error and not before and what == "schedule":
            self.desk.raise_alert(kind="schedule_died", robot=SITE, title="排程没办成",
                                  detail=f"到点可能没人起跑:{error}")
