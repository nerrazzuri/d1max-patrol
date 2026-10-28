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

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.messages import (
    Event,
    Status,
    TaskState,
    Telemetry,
    parse_fault_event_data,
)
from d1max_contract.storage import WARN_RATIO
from d1max_site.alert_store import AlertDesk
from d1max_site.clockskew import ALARM_S as SKEW_ALARM_S
from d1max_site.clockskew import CLEAR_S as SKEW_CLEAR_S
from d1max_site.clockskew import ClockSkew
from d1max_site.priorities import STANDBY_PREFIX

log = logging.getLogger(__name__)

#: 故障文本里出现这些词就认作跌倒。**这是个假设,真机清单里要量**:厂商故障帧只有自由文本,
#: 没有结构化的「跌倒」标记。不收单独一个 ``fall``:它会命中 ``fallback``,误报一条 P1 的代价
#: 是有人半夜开车出门。
FALLEN_WORDS: tuple[str, ...] = ("跌倒", "摔倒", "倒地", "fallen", "fall down", "falldown",
                                 "tipped over")
#: 上装接在继电器上的几路(W21):``payload_<哪一路>`` 故障 → 同名告警。
PAYLOAD_OUTPUTS = {"strobe": "警灯", "siren": "警笛", "spotlight": "聚光灯", "amp": "功放"}

#: 失败理由里出现这些词才认作「没电中止」。代理引擎那条理由原文是「电量 22% 低于中止线 25%」。
BATTERY_WORDS: tuple[str, ...] = ("电量", "电池", "battery")

#: 站点自己那一行告警的 ``robot``。
SITE = "site"

#: 排程这一轮的去向 → (告警 kind, 人话)(W00c6c)。P1 ``schedule_missed``:本该有狗去巡逻而没去;
#: P2 ``schedule_skipped``:排程自己写了错过就算了,但错过本身要知道;P2 ``schedule_blocked``:狗要人
#: 监护、排程派不了(过渡期,W00c6i —— 人要做的是改配置,不是立刻动身)。``started``、``displaced``、
#: ``busy`` 不在表里。
_SCHEDULE_ALERTS: dict[str, tuple[str, str]] = {
    "no_robot": ("schedule_missed", "到点没有能派的机器人"),
    "ambiguous": ("schedule_missed", "能派的机器人不止一台,排程没写派哪台"),
    "skew": ("schedule_missed", "站点的钟不可信,不起"),
    "dispatch_failed": ("schedule_missed", "派了,机器人没收"),
    "alarm": ("schedule_missed", "错过了(排程要求报警)"),
    "lost": ("schedule_missed", "派出去了,回执超时,机器人一直没接"),
    "skip": ("schedule_skipped", "错过了,按排程跳过这一轮"),
    "supervised": ("schedule_blocked", "机器人要人现场监护,排程派不了"),
    # W14:被事件派遣、手动派单、遥控打断了,不续(W00c2b 决定 D2A,用户 2026-10-02 维持)
    "preempted": ("schedule_interrupted", "跑到一半被更优先的任务打断了,这一轮没巡完、不续"),
}
_SCHEDULE_TITLE = {"schedule_missed": "这一轮没跑", "schedule_skipped": "跳过了一轮",
                   "schedule_blocked": "派不了", "schedule_interrupted": "这一轮没巡完"}

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


#: 定位变差、打滑(W29):先报 P2,这么多秒还没恢复再报 P1。
LOC_P1_S = 30
#: 空闲的狗定位不可信(对账 S6):连着这么多秒报 P2 ``loc_lost_idle``「派不了单」,好了自己解决。两分钟:
#: 定位器自己能好的都比这短(2026-10-13 雷达挡了又恢复约 45 s;MOLA 重启后按最后的位置重定位,σ 降回
#: 可信约 20 s;换先验几十秒);再长,就要等到排程派不出去(P1 漏跑)、出了入侵派不了才知道。
LOC_IDLE_P2_S = 120
#: 狗刚起来(第一次见、换了 boot_id)或者刚换完图:定位器要人到场给初始位置(真定位器重开过、新图上都是
#: 这样),人多半正在弄 —— 从那一刻起宽限到这么多秒才报,不在人设位置的时候吵。
LOC_IDLE_BOOT_S = 300


@dataclass
class _Mem:
    """一台狗的「上次是什么」。"""

    estop: bool = False
    loc_lost_running: bool = False
    #: 跑着任务、定位不行从哪一刻起(站点的钟;W29:先报 P2,30 秒还没好再报 P1)。
    loc_since: int | None = None
    loc_p1: bool = False
    #: 空闲的狗定位不可信(对账 S6):最近一份在线状态里定位不行没有、空闲着不行从哪一刻起(站点的钟)、
    #: 这一回报过没有、起来 / 换图的宽限到哪一刻、狗最近一条遥测里说的原因。
    loc_bad: bool = False
    loc_idle_since: int | None = None
    loc_idle_p2: bool = False
    loc_quiet_until: int = 0
    loc_reason: str = ""
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
    #: 最后一次遥测里的地图位姿(``MapPose`` 的报文)与收到的时刻(站点的钟),告警带现场用(W17)。
    pose: dict | None = None
    pose_ms: int = 0
    #: 报过「狗在禁行区里」的 P1、还没自己解决(W41);狗离开禁行区够远从哪一刻起(站点的钟)。
    nogo: bool = False
    nogo_clear_since: int | None = None


#: W41(2026-10-26 现场:禁行区删了、狗也不在里面了,P1 还挂着、也不提示能关):狗报的位置离每一个
#: 在生效的禁行区(禁行、抹掉)都至少这么远、连着这么久,「狗在禁行区里」就自己解决。定位不可信时
#: 遥测没有位姿,不解决。
NOGO_CLEAR_M = 0.5
NOGO_CLEAR_S = 5


class SiteAlertSources:
    def __init__(self, desk: AlertDesk, *, now_ms: Callable[[], int],
                 is_stale: Callable[[str], bool] | None = None,
                 skew_of: Callable[[str], float | None] | None = None) -> None:
        #: 天气(W29,``weather.WeatherDesk``):雷暴中出动的入侵告警写明。站点主程序接上。
        self.weather: Any = None
        self.desk = desk
        self._now = now_ms
        #: 这台狗的状态是不是过期了(派遣器按站点的钟算)。``None`` = 不看过期。
        self._is_stale = is_stale
        self._mem: dict[str, _Mem] = {}
        #: 狗的钟差从哪来(W09d):主程序接派遣器的(站点只有一份);没接就自己按遥测算。
        self._skew = ClockSkew(now_ms=now_ms)
        self._skew_of = skew_of
        self._site_errors: dict[str, str] = {}
        #: 排程告警合并进来的排程 id(按告警的键),拼标题用(W00c6c 内审)。
        self._sched_entries: dict[str, list[str]] = {}
        #: 入侵告警合并进来的防区(按告警的键),拼标题用(W16)。
        self._incident_zones: dict[str, list[str]] = {}
        #: 每条狗的告警都带上狗最后在哪、在跑哪一趟(W17)。
        #: W44 第四期:地图位姿 → 在哪(站点主程序接上:命名区域,没命名用防区名)。
        self.where_of: Callable[[dict], str | None] | None = None
        desk.context_for = self.here
        #: 每台狗最近一次报的上装故障(W21 复查,``payload_*`` 的集合)。告警按它**每拍对账**:
        #: 报、解决失败了下一拍再来;站点起来时从事件库读回每台狗最近一条 ``robot_fault``。
        self._payload: dict[str, frozenset[str]] = {}
        self._payload_loaded = False
        #: 某一版图上在生效的禁行区、限速区(``NavZones.effective``,W41)。主程序接上;``None`` =
        #: 「狗在禁行区里」不自己解决。
        self.nav_zones: Callable[[str, str], Any] | None = None

    def here(self, robot: str) -> dict:
        """这台狗的现场(W17):最后一次报的地图位姿(带站点收到的时刻)、正在跑的那一趟。站点那一行、
        没见过的狗:空的。"""
        m = self._mem.get(robot)
        if m is None:
            return {}
        out: dict = {}
        if m.pose is not None:
            out["pose"] = dict(m.pose) | {"at_ms": m.pose_ms}
            if self.where_of is not None:
                # W44 第四期:在哪(命名的区域,没命名用防区名),报警、报表上写「在车库」
                try:
                    w = self.where_of(m.pose)
                except Exception:                    # 现场拿不到不许挡住告警
                    log.exception("%s 在哪算不出来", robot)
                    w = None
                if w:
                    out["where"] = w
        if m.running and m.started:
            out["task_id"] = m.started
        return out

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
            if m.loc_lost_running:
                # W29 外审 3:站点重启前就在丢定位 —— 从现在起重新计 30 秒(之前的时长不知道,
                # 宁可晚一点报 P1,不能不报)
                m.loc_since, m.loc_p1 = self._now(), False
            # 对账 S6:空闲着丢定位的同样从现在起计;库里挂着的那条,好了照样解决
            m.loc_bad = not s.ready.loc_ok
            if m.loc_bad and not m.running:
                m.loc_idle_since, m.loc_idle_p2 = self._now(), False
            if m.running:
                m.started = s.task.task_id
        # 外审 R8:库里还挂着禁行区 P1(站点重启前报的):照样盯着它离开、够久自动解决(计时从现在的
        # 新位置重新数)
        try:
            m.nogo = bool(self.desk.has_open(rid, "nogo"))
        except Exception:
            log.exception("%s 查有没有挂着的禁行区 P1 没成", rid)
        m.nogo_clear_since = None

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
            m.loc_quiet_until = self._now() + LOC_IDLE_BOOT_S * 1000   # 对账 S6:刚起来,宽限
            m.nogo_clear_since = None                  # 复查 N2:重启过,离开禁行区从头数
        if m.offline:
            m.online_since = self._now()
            m.nogo_clear_since = None                  # 复查 N2:刚回来,从新的可信位置数
        m.offline = False
        running = s.task is not None and s.task.state is TaskState.RUNNING
        m.running = running
        estop = not s.ready.estop_clear
        if estop and not m.estop:
            self.desk.raise_alert(kind="estop_pressed", robot=rid, title="急停被按下",
                                  detail="机器人报急停没解除")
        m.estop = estop
        # 代理的引擎丢定位就暂停(W29,决策 41):「跑着任务 + 定位不行」= 定位变差或打滑(跟里程对不上),
        # 狗原地停、等恢复。先报 P2;**30 秒还没好**再报 P1「定位丢失后暂停」叫人(``tick`` 里看)。
        loc = running and not s.ready.loc_ok
        if loc and not m.loc_lost_running:
            m.loc_since, m.loc_p1 = self._now(), False
            self.desk.raise_alert(kind="loc_degraded", robot=rid,
                                  title="定位变差或打滑:原地停下等恢复",
                                  detail=f"任务 {s.task.task_id} 在跑,机器人报定位不行;"
                                         f"{LOC_P1_S} 秒内恢复就接着走")
        if not loc:
            m.loc_since = None
        elif m.loc_since is None:
            m.loc_since, m.loc_p1 = self._now(), False   # 丢着定位、没在计(W29 外审 3)
        m.loc_lost_running = loc
        self._loc_idle(rid, m, idle_bad=not running and not s.ready.loc_ok, ok=s.ready.loc_ok)
        if running and s.task.task_id != m.started:
            m.started = s.task.task_id
            self.desk.raise_alert(kind="run_start", robot=rid,
                                  title=f"开跑:{s.task.kind} {s.task.task_id}")

    def _loc_idle(self, rid: str, m: _Mem, *, idle_bad: bool, ok: bool) -> None:
        """空闲的狗定位不可信(对账 S6):开表 / 停表;定位好了把挂着的那条解决(解决不成下一份状态
        再来)。报在 ``tick`` 里(到点才报)。"""
        if idle_bad and m.loc_idle_since is None:
            m.loc_idle_since, m.loc_idle_p2 = self._now(), False
        elif not idle_bad:
            m.loc_idle_since = None
        if ok and m.loc_bad:
            try:
                self.desk.resolve_all(rid, "loc_lost_idle", who="site:loc_ok")
            except Exception:
                log.exception("%s 定位好了,空闲丢定位那条解决不了,下一份状态再来", rid)
                return
            m.loc_idle_p2 = False
        if not ok:
            m.nogo_clear_since = None                  # 外审 R5:不可信的那段不算「连着离开」
        m.loc_bad = not ok

    def _loc_idle_due(self, rid: str, m: _Mem) -> None:
        """到点(连着 :data:`LOC_IDLE_P2_S`,起来、换图后不早于宽限)还不好:报 P2,这一回只报一次;
        簿子里还挂着(站点重启前报过)不重复报;写不进去下一拍再报。"""
        if m.offline or m.loc_idle_since is None or m.loc_idle_p2:
            return
        now = self._now()
        if now < max(m.loc_idle_since + LOC_IDLE_P2_S * 1000, m.loc_quiet_until):
            return
        try:
            if not self.desk.has_open(rid, "loc_lost_idle"):
                detail = (f"已经 {(now - m.loc_idle_since) // 60_000} 分钟多;"
                          f"机器人说:{m.loc_reason or '没说为什么'}。"
                          "到场设位置(手机单机器人页「设位置」),"
                          "或者查定位器;好了这条自己解决")
                self.desk.raise_alert(kind="loc_lost_idle", robot=rid,
                                      title="空闲的机器人定位不可信:派不了单", detail=detail[:300])
        except Exception:
            log.exception("%s 空闲丢定位 P2 报不出去,下一拍再报", rid)
            return
        m.loc_idle_p2 = True

    def step(self) -> None:
        """站点主循环每 ``STEP_S`` 秒调一次:看掉线,对账上装告警,再让 P1 未确认的升档,再修剪内存。"""
        self.tick()
        self.reconcile_payload()
        self.desk.escalate()
        self.desk.trim()

    def tick(self) -> None:
        """站点主循环每几秒调一次:状态过期 = 掉线(遗言也许没到)。"""
        if self._is_stale is None:
            return
        for rid, m in list(self._mem.items()):
            if not m.offline and self._is_stale(rid):
                self._offline(rid, m)
            elif m.loc_since is not None and not m.loc_p1 \
                    and self._now() - m.loc_since >= LOC_P1_S * 1000:
                try:                                      # 30 秒还没恢复:叫人(W29)
                    self.desk.raise_alert(kind="loc_lost_paused", robot=rid,
                                          title="定位丢失后暂停,30 秒没恢复:要人到场",
                                          detail="机器人原地停着,不靠不准的定位自己走;到场重新给位置,"
                                                 "或者遥控挪到看得清的地方")
                except Exception:                         # 写不进去:不标报过,下一拍再报(外审 3)
                    log.exception("%s 定位丢失 P1 报不出去,下一拍再报", rid)
                    continue
                m.loc_p1 = True
            self._loc_idle_due(rid, m)

    def _offline(self, rid: str, m: _Mem) -> None:
        if m.offline:
            return
        m.offline = True
        # 掉线期间的事看不见:回来之后重新从它报的状态认起。跌倒的记忆也清掉(狗可能被扶起、重启)。
        m.estop = m.loc_lost_running = m.fallen = False
        m.loc_since = m.loc_idle_since = None              # 掉线不计(loc_bad 留着:回来好了照样解决)
        m.nogo_clear_since = None                          # 外审 R5:掉线那段不算「连着离开」
        if m.online_since is not None and self._now() - m.online_since < OFFLINE_REARM_MS:
            log.info("%s 回来不到 %d s 又掉线:算同一次,不另起告警", rid, OFFLINE_REARM_MS // 1000)
            return
        if m.running:
            self.desk.raise_alert(kind="robot_offline", robot=rid, title="机器人掉线了(跑着任务)",
                                  detail=f"最后在跑 {m.started}")
        else:
            self.desk.raise_alert(kind="robot_offline_idle", robot=rid, title="机器人掉线了")

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
                                        "机器人留在新版",
                                  detail=f"开机守卫数了 {d.get('attempts', '?')} 次;"
                                         "看机器人到站点的"
                                         "网络、证书、站点地址")
        elif e.kind == "release_rolled_back":
            # 新版起不来,狗上的开机守卫退回了上一版(W00c5d 第三部分内部评审)。
            self.desk.raise_alert(kind="release_failed", robot=rid,
                                  title=f"新版 {d.get('from', '?')} 起不来,机器人自己退回了 "
                                        f"{d.get('to', '?')}",
                                  detail=f"开机守卫数了 {d.get('attempts', '?')} 次")
        elif e.kind in ("rtk_disagree", "rtk_gave_up", "rtk_base_moved"):
            # W09e:RTK 核对定位器
            gap = d.get("gap_m")
            title = {"rtk_disagree": f"RTK 说定位差 {gap} m:定位器不可信,在按 RTK 重定位",
                     "rtk_gave_up": f"按 RTK 请了 {d.get('relocs', '?')} 次还对不上:交给人看"
                                    "(RTK 假固定,还是定位器错了)",
                     "rtk_base_moved": f"基站坐标跟建图时差 {d.get('moved_m')} m:不再拿 RTK 核对"
                                       ",基站要用固定坐标、或者重新建图配准"}[e.kind]
            self.desk.raise_alert(kind="rtk", robot=rid, title=title,
                                  detail=f"图 {d.get('map_id', '?')}:{d.get('map_version', '?')}")
        elif e.kind == "inside_nogo":
            # W10:狗在禁行区里 —— 原地停了,不自己往外走
            m = self._m(rid)
            m.nogo, m.nogo_clear_since = True, None
            zone = d.get("label") or d.get("zone", "?")
            self.desk.raise_alert(kind="nogo", robot=rid,
                                  title=f"机器人在禁行区「{zone}」里,原地停着等人",
                                  detail=f"位置 ({d.get('x', '?')}, {d.get('y', '?')}),图 "
                                         f"{d.get('map_id', '?')}:{d.get('map_version', '?')};"
                                         "人到场把它挪出来(遥控),或者核一下区域、定位;"
                                         "机器人离开禁行区、或者区域删了生效以后,这条自己解决")
        elif e.kind == "map_integrity_failed":
            # W09g:狗起来时正在用的图校验不过 —— 没载、不宣告能自主(不退回命令行的图)。
            which = f"{d.get('map_id', '?')}:{d.get('version', '?')}"
            self.desk.raise_alert(kind="map_integrity", robot=rid,
                                  title=f"机器人上正在用的图坏了:{which},不能自主",
                                  detail=f"{str(d.get('reason', ''))[:240]};"
                                         "重新下发这一版(同一版也会重新下载、逐个核对)")
        elif e.kind == "map_activated":
            # 对账 S6:换了图,定位器要人在新图上给初始位置 —— 空闲丢定位宽限一阵
            m = self._m(rid)
            m.loc_quiet_until = max(m.loc_quiet_until, self._now() + LOC_IDLE_BOOT_S * 1000)
        elif e.kind in ("map_activate_failed", "map_build_failed"):
            what = "换图" if e.kind == "map_activate_failed" else "重建"
            which = f"{d.get('map_id', '?')}:{d.get('version', '?')}"
            self.desk.raise_alert(kind="map_failed", robot=rid, title=f"{what}没成:{which}",
                                  detail=str(d.get("reason", ""))[:300])
        elif e.kind == "agent_args_skipped":
            # W32(决策 47):/etc/d1max/env 的 D1MAX_AGENT_ARGS 里有这一版不认识的参数(多半是
            # 退回了老版、参数是新版加的):代理跳过照起,但那几项设置没生效
            args = "、".join(str(a) for a in d.get("args", [])[:10])
            self.desk.raise_alert(kind="agent_args_skipped", robot=rid,
                                  title="机器人上的代理有几个启动参数这一版不认识,跳过了",
                                  detail=f"代理 {d.get('version', '?')} 跳过:{args}"[:300]
                                         + ";多半是退回了老版本,这几项设置没生效")
        elif e.kind == "force_bump":
            # W26(决策 52):狗被撞、被推、被踢了一下(没倒);任务照跑。翻倒、抱起来按能力对账(force.py)
            g = d.get("shock_g", "?")
            self.desk.raise_alert(kind="force_bump", robot=rid, title=f"机器人被撞了一下({g} g)",
                                  detail="受力异常但没倒,任务照跑;位置看告警现场,录像看这个时刻前后"
                                         "(门槛没在真机器人上标定前误报会多)")
        elif e.kind == "force_config_bad":
            self.desk.raise_alert(kind="force_config_bad", robot=rid,
                                  title="机器人上的受力门槛文件读不懂,用的是缺省门槛",
                                  detail=f"/etc/d1max/force.json:{str(d.get('reason', ''))[:250]}")
        elif e.kind == "recording_failed":
            # W18:这一路录像断了(相机不通、盘写不进),狗每 10 s 重试;录回来报 recording_ok
            self.desk.raise_alert(kind="recording_failed", robot=rid,
                                  title=f"{d.get('camera', '?')} 录像断了",
                                  detail=str(d.get("reason", ""))[:300])
        elif e.kind == "recording_dropped":
            why = "机器人的盘紧" if d.get("reason") == "disk" else "攒满配额"
            # W18:断网太久,狗上攒的录像超了配额(或盘紧),删了最旧的几段 —— 那几段站点上没有
            self.desk.raise_alert(kind="recording_dropped", robot=rid,
                                  title=f"机器人上的录像没传到站点,删了 {d.get('count', '?')} 段",
                                  detail=f"从 {d.get('oldest', '?')} 起;"
                                         f"{why}:WiFi 断了太久,或者站点收不进")
        elif e.kind == "archive_write_failed":
            # 盘满、只读重挂(W00c6a):那一趟照跑,但照片、记录没存下 —— 证据缺了,多半是盘满或盘坏了。
            self.desk.raise_alert(kind="archive_failed", robot=rid,
                                  title=f"机器人上这一趟的记录写不进去:{tid}",
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
                self.desk.raise_alert(kind="fallen", robot=rid, title="机器人跌倒了", detail=text)
            m.fallen = fallen
            # 先记下全集(事件已经落库,重投会被去重跳过:告警这一下没成,只能靠每拍对账补)
            self._payload[rid] = frozenset(f.code for f in faults if f.code.startswith("payload_"))
            try:
                self._payload_faults(rid, self._payload[rid])
            except Exception:
                log.exception("%s 的上装告警这一下没办成,下一拍对账再来", rid)

    def _load_payload(self) -> None:
        """站点起来:每台狗最近一条 ``robot_fault``(按站点收到的时刻)里的上装故障。"""
        db = getattr(self.desk, "db", None)
        if db is None:
            self._payload_loaded = True
            return
        rows = db.query(
            "SELECT e.robot_id, e.data FROM events e WHERE e.kind='robot_fault' AND e.rowid = ("
            "SELECT x.rowid FROM events x WHERE x.kind='robot_fault' AND x.robot_id=e.robot_id "
            "ORDER BY x.received_at DESC, x.rowid DESC LIMIT 1)")
        for r in rows:
            if r["robot_id"] in self._payload:
                continue                    # 起来之后已经收到更新的了
            try:
                faults = parse_fault_event_data(json.loads(r["data"]))
            except (ContractError, ValueError):
                continue
            self._payload[r["robot_id"]] = frozenset(
                f.code for f in faults if f.code.startswith("payload_"))
        self._payload_loaded = True

    def reconcile_payload(self) -> None:
        """按每台狗最近的上装故障全集对账告警(W21 复查)。一台没成不挡别的台,下一拍再来。"""
        if not self._payload_loaded:
            try:
                self._load_payload()
            except Exception:
                log.exception("读不回各机器人最近的上装故障,下一拍再读")
                return
        for rid, codes in list(self._payload.items()):
            try:
                self._payload_faults(rid, codes)
            except Exception:
                log.exception("%s 的上装告警对账没成,下一拍再来", rid)

    def _payload_faults(self, rid: str, present: frozenset[str]) -> None:
        """上装关不上、状态不明(W21 复查,狗报 ``payload_<哪一路>``)。
        **告警簿说了算**(同 W19 摄像头):
        这一路在故障里、簿子里还没挂着就报;不在了就解决 —— 重复上报不重复报,站点重启后也对得上。
        ``robot_fault`` 是整个集合(变了才发、代理起来先发一次全集),所以没出现就是好了。"""
        for out, name in PAYLOAD_OUTPUTS.items():
            kind = f"payload_{out}"
            if kind in present:
                if not self.desk.has_open(rid, kind):
                    self.desk.raise_alert(
                        kind=kind, robot=rid, title=f"上装{name}关不上或状态不明",
                        detail="继电器板没确认(串口、板子供电?);机器人上在接着关。去现场看是不是还开着"
                        if out != "siren" else
                        "继电器板没确认,或者喇叭警笛放不出来(声卡、警笛声文件?);去现场看")
            else:
                self.desk.resolve_all(rid, kind, who="site:payload_ok")

    # ------------------------------------------------------------ 遥测

    def on_telemetry(self, rid: str, t: Telemetry) -> None:
        # W09d:按最近一段遥测估(在途不拉偏),2 s 报、1 s 以内重新武装
        if self._skew_of is None:
            self._skew.note(rid, t.stamp, self._now())
            skew = self._skew.skew_s(rid)
        else:
            skew = self._skew_of(rid)
        skew = skew or 0.0
        m = self._m(rid)
        if t.pose is not None:
            m.pose, m.pose_ms = t.pose.to_wire(), self._now()
            if m.nogo:
                self._nogo_check(rid, m, t.pose)
        else:
            m.nogo_clear_since = None                  # 外审 R5:位置断了,从下一个可信位置重新数
        if isinstance(t.loc, dict):
            m.loc_reason = str(t.loc.get("reason") or "")[:200]   # 对账 S6:空闲丢定位的告警带上
        if abs(skew) > SKEW_ALARM_S and not m.skew:
            m.skew = True
            self.desk.raise_alert(kind="clock_skew", robot=rid, title="机器人的钟不准",
                                  detail=f"跟站点差 {skew:+.1f} 秒(机器人该向站点主机对时,见 W09d)")
        elif abs(skew) <= SKEW_CLEAR_S:
            m.skew = False
        if t.storage is not None:
            self._storage(rid, m, t.storage)

    def _nogo_check(self, rid: str, m: _Mem, pose: Any) -> None:
        """「狗在禁行区里」的 P1:狗离开在生效的禁行区够远、连着够久就自己解决(W41)。定位不可信
        (状态里 ``loc_ok`` 假)时报的位置不作数,重新计 —— 推算的位置可能离真位置很远。"""
        if self.nav_zones is None:
            return
        if m.loc_bad or m.offline:
            m.nogo_clear_since = None                  # 复查 N2:掉线期间(迟到的遥测)不计
            return
        from d1max_contract.zones import distance_to_polygon, point_in_polygon
        try:
            zs = self.nav_zones(pose.map_id, pose.map_version)
            near = min((0.0 if point_in_polygon(pose.x, pose.y, z.polygon)
                        else distance_to_polygon(pose.x, pose.y, z.polygon)
                        for z in zs.nogo()), default=float("inf"))
        except Exception:
            log.exception("%s 查禁行区没成", rid)
            m.nogo_clear_since = None                  # 外审 R5:核不了连续性,重新数
            return
        now = self._now()
        if near < NOGO_CLEAR_M:
            m.nogo_clear_since = None
            return
        if m.nogo_clear_since is None:
            m.nogo_clear_since = now
        if now - m.nogo_clear_since < NOGO_CLEAR_S * 1000:
            return
        try:
            self.desk.resolve_all(rid, "nogo", who="site:left_nogo")
        except Exception:
            log.exception("%s 出了禁行区,P1 解决不了,下一拍再来", rid)
            return
        m.nogo, m.nogo_clear_since = False, None

    def _storage(self, rid: str, m: _Mem, f) -> None:
        """狗的发件箱(W00c5d,决策 8):盘到 80%、证据积压半小时,各报一次;回落之后才重新武装。"""
        if f.disk_used_ratio >= WARN_RATIO and not m.disk80:
            m.disk80 = True
            self.desk.raise_alert(
                kind="disk_80", robot=rid, title="机器人的发件箱所在的盘快满了",
                detail=f"已用 {f.disk_used_ratio:.0%};到 90% 机器人就不接新的巡检(不删没传完的)")
        elif f.disk_used_ratio < DISK_CLEAR_RATIO:
            m.disk80 = False
        oldest = f.oldest_backlog_s or 0
        if oldest >= BACKLOG_ALARM_S and not m.backlog:
            m.backlog = True
            self.desk.raise_alert(
                kind="upload_backlog", robot=rid, title="机器人上的证据半小时没传上来",
                detail=f"积压 {f.backlog_files} 个文件、{f.backlog_bytes // 1024} KB,"
                       f"最老的等了 {oldest // 60} 分钟")
        elif oldest < BACKLOG_CLEAR_S:
            m.backlog = False

    # ------------------------------------------------------------ 站点自己

    def on_schedule_outcome(self, entry_id: str, robot_id: str | None, outcome: str,
                            note: str) -> None:
        """排程这一轮没按时跑(W00c6c)。执行器一轮只说一次,这里按去向定告警。

        同一只狗(或站点)同一个 kind 的告警会合成一条(告警簿的规矩):标题把合进来的**每一条排程**
        都列上(内审:以前标题只剩最后那条,前面没跑的那条从告警里消失);详情是最近那条的原因。"""
        hit = _SCHEDULE_ALERTS.get(outcome)
        if hit is None:
            return
        kind, what = hit
        robot = robot_id or SITE
        prev = self.desk.absorbing(robot, kind)
        ids = list(self._sched_entries.get(prev.key, ())) if prev is not None else []
        if entry_id not in ids:
            ids.append(entry_id)
        names = "、".join(ids[:5]) + (f" 等 {len(ids)} 条" if len(ids) > 5 else "")
        a = self.desk.raise_alert(kind=kind, robot=robot,
                                  title=f"排程 {names} {_SCHEDULE_TITLE[kind]}:{what}",
                                  detail=f"{entry_id}:{note}"[:300])
        self._sched_entries[a.key] = ids
        while len(self._sched_entries) > 200:            # 只为合并标题记着,老的放掉
            self._sched_entries.pop(next(iter(self._sched_entries)))

    def on_incident(self, row: dict) -> None:
        """一条入侵有了去向(W16):派出去了、并进已出动的,说「有入侵、谁去了」(记在那只狗名下);没狗、
        防区没映射、派失败,说「有入侵、没狗去」(记在站点名下)。都是 P1:要人动身。同一只狗(或站点)
        同一个 kind 合成一条,标题列上合进来的每个防区(同排程)。"""
        zone = str(row.get("zone") or "?")
        outcome = row.get("outcome")
        if outcome in ("dispatched", "merged") and row.get("robot_id"):
            kind, robot = "intrusion", str(row["robot_id"])
            what = f"{robot} 已出动去 {row.get('intercept') or '拦截点'}"
            if self.weather is not None and self.weather.storm():
                what += "(雷暴中出动)"                     # W29:雷暴照派,写明
        elif outcome in ("no_robot", "unmapped", "dispatch_failed", "merged"):
            kind, robot = "intrusion_unanswered", SITE
            what = {"no_robot": "没有能派的机器人", "unmapped": "这个防区没设机器人去的拦截点",
                    "dispatch_failed": "派了,机器人没收"}.get(str(outcome), "没机器人去")
        else:
            return
        prev = self.desk.absorbing(robot, kind)
        zones = list(self._incident_zones.get(prev.key, ())) if prev is not None else []
        if zone not in zones:
            zones.append(zone)
        names = "、".join(zones[:5]) + (f" 等 {len(zones)} 个防区" if len(zones) > 5 else "")
        title = (f"防区 {names} 有入侵:{what}" if kind == "intrusion"
                 else f"防区 {names} 有入侵,没机器人去:{what}")
        # 给保安看的说明(演示站点实测记下):谁报的、怎么回事,不写事件源编号、事件类型这些技术用语
        src = str(row.get("source") or "?")
        who = f"摄像头 {src[5:]}" if src.startswith("cctv-") else src
        detail = f"{who} 报的;{what}"
        ctx: dict = {"zone": zone}
        if row.get("intercept_pose"):
            ctx["intercept"] = row["intercept_pose"]
        if kind == "intrusion" and row.get("task_id"):
            ctx["task_id"] = row["task_id"]                # 到了拍的照片在这一趟的记录里
        a = self.desk.raise_alert(kind=kind, robot=robot, title=title[:200], detail=detail[:300],
                                  context=ctx)
        self._incident_zones[a.key] = zones
        while len(self._incident_zones) > 200:
            self._incident_zones.pop(next(iter(self._incident_zones)))

    def on_incident_throttled(self, source: str) -> None:
        """事件源一分钟超了上限被限流(W16):摄像头抽风,或者密钥漏了被人刷。"""
        self.desk.raise_alert(kind="incident_flood", robot=SITE,
                              title=f"事件源 {source} 一分钟内报得太多,超出的不收",
                              detail="看看是摄像头误报成串,还是密钥漏了(d1max-site source-rotate)")

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
