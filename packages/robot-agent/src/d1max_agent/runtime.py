"""把代理核装起来(设计 §5)。**步进式**:``step(dt)`` 由外面驱动 —— 生产上是一个
``asyncio.sleep`` 循环,测试里是注入的钟,一毫秒都不睡。

起来:LWT(status offline,retained)→ 连接 → 订自己的 cmd → 发 capabilities(retained)→
发 status(retained)。
断线:标记离线;下一拍按断线策略表处理当前任务(``continue_if_safe`` 看 health)。
重连:**第一条**是 reconcile(当前任务、代次、未确认事件区间),再补发未确认事件,再 status。
每拍:处理任务 → 把新事件发出去(连着才发,发了就算确认)→ 状态变了发 status →
到点发 telemetry。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from d1max_agent.assembly import EngineParts, build_engine
from d1max_agent.bridge_localizer import BridgeLocalizer
from d1max_agent.commands import READ_KINDS, CommandProcessor
from d1max_agent.engine.homing import HomePoint
from d1max_agent.engine.machine import RunState
from d1max_agent.events import EventBook
from d1max_agent.homes import HomeBook
from d1max_agent.idempotency import IdempotencyStore
from d1max_agent.localization import OdomAnchor
from d1max_agent.locbridge import LocBridgeServer
from d1max_agent.mapping_trail import MappingTrail
from d1max_agent.release_precheck import MIN_BATTERY_PCT as _PRECHECK_MIN_BATTERY_PCT
from d1max_agent.resources import ResourceLedger
from d1max_agent.status import (
    compose_capabilities,
    compose_ready,
    compose_status,
    compose_telemetry,
    offline_status,
)
from d1max_agent.tasks.base import Task
from d1max_agent.tasks.engine_goto import EngineGotoTask
from d1max_agent.transport import GuardedTransport
from d1max_agent.zonebook import ZoneBook
from d1max_contract.errors import ContractError
from d1max_contract.hal import Fault, HalUnsupported, RobotHAL
from d1max_contract.maps import PRIOR_FILES
from d1max_contract.messages import Command, MapPose, Reconcile, fault_event_data
from d1max_contract.policy import policy_for
from d1max_contract.registration import Registration
from d1max_contract.storage import StorageFacts
from d1max_contract.supervision import parse_supervise
from d1max_contract.teleop import TeleopFrame
from d1max_contract.topics import TopicAcl
from d1max_contract.transport import Message, Transport
from d1max_contract.zones import ZoneSet, tightens
from d1max_patrol.protocol.nav_types import Pose

log = logging.getLogger(__name__)

#: 盘况随遥测多久带一次(毫秒)。
STORAGE_EVERY_MS = 10_000
#: 换图:下载好之后最多等多久让狗空下来(秒)。
SWITCH_WAIT_S = 600.0
#: 起来时载入正在用的那张图最多等多久(秒);过了照旧用 ``--map``。
LOAD_MAP_TIMEOUT_S = 120.0


def _parse_home(v: Any) -> tuple[float, float, float] | None:
    """``map_activate`` 里站点给的原点(这台狗在这张图上的待命点):``{x, y, yaw}``。没给为 None。"""
    import math
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ContractError("map_activate: home 要是对象")
    out = []
    for k in ("x", "y", "yaw"):
        x = v.get(k)
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ContractError(f"map_activate: home.{k} 要是有限数")
        out.append(float(x))
    return (out[0], out[1], out[2])

#: 切版本、退版本要的电量(%)。切过去起不来,开机守卫还要再退、再起一次。跟升级前检查同一个数。
RELEASE_MIN_BATTERY_PCT = _PRECHECK_MIN_BATTERY_PCT

#: 升级前检查每个外部来源(W09 定位器、W11 感知)最多等多久(W00c6d 内审:握着命令锁)。
PRECHECK_SOURCE_TIMEOUT_S = 2.0

#: 标原点时给的名字(站点拿它登记待命点,规矩同站点的待命点名)。
_HOME_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")

#: 在当前位置标原点(W00c6f):定位偏差要不大于这个(米)。刚设过位置是 0.2 m,约走出 1.3 m 以内。
HOME_MAX_SIGMA_M = 0.5

#: RTK 解跟此刻的位姿比,最多隔这么久(W09e 内审小:旧的解会差出 v·Δt)。
RTK_FIX_MAX_AGE_MS = 300

#: 一条回执编码之后最多这么大(字节)。站点 broker 单包上限 262144(``max_packet_size``,超了狗被断开),
#: 留出主题与包头。带数据的回执各自有上限(日志尾巴、轨迹),这里是出口的兜底(外审 Qwen 第六节 2)。
ACK_MAX_BYTES = 240 * 1024

#: 电量低于这条线,断线时按「不安全」处理(停住等待)。
BATTERY_FLOOR_PCT = 15.0

#: 要人监护(W00c6i)时,只有这几种任务受监护租约约束(遥控、叫停、换图、发布照常)。
_AUTONOMOUS_KINDS = frozenset({"goto", "patrol"})
#: 放了、过期的监护会话留多久(秒):挡同一会话里迟到的旧心跳(站点的命令有效期 30 s,再多留一点)。
_SESSION_MEMORY_S = 60.0


def _dumps(d: dict) -> bytes:
    return json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()


def _ack_bytes(ack: Any) -> bytes:
    """回执编码成报文;超过 ``ACK_MAX_BYTES`` 就不带数据(连重投里原结果的数据),原因里写多大。"""
    w = ack.to_wire()
    raw = _dumps(w)
    if len(raw) <= ACK_MAX_BYTES:
        return raw
    log.warning("回执 %s 编码后 %d 字节,超过 %d:不带数据", ack.command_id, len(raw), ACK_MAX_BYTES)
    w.pop("data", None)
    if isinstance(w.get("original"), dict):
        w["original"].pop("data", None)
    w["reason"] = f"data_too_large: {len(raw)} 字节(上限 {ACK_MAX_BYTES})"
    return _dumps(w)


class AgentRuntime:
    def __init__(self, *, transport: Transport, registration: Registration, hal: RobotHAL,
                 store_dir: Path, now_ms: Callable[[], int], loaded_map: tuple[str, str] | None,
                 boot_id: str | None = None, telemetry_period_ms: int = 1000,
                 status_period_ms: int = 30_000, parts: EngineParts | None = None,
                 home: Pose | None = None, runs_root: Path | None = None,
                 monotonic: Callable[[], float] | None = None, video: Any = None,
                 storage_facts: Callable[[], StorageFacts | None] | None = None,
                 maps: Any = None, mapper: Any = None, releases: Any = None,
                 autonomy: str | None = None, odom_identity: bool | None = None,
                 localizer: str = "anchor", loc_socket: Path | None = None,
                 rtk: Any = None, nav: str = "straight",
                 robot_radius_m: float | None = None, obstacles: str = "none",
                 obs_socket: Path | None = None) -> None:
        self.registration = registration
        #: RTK 来源(W09e,``d1max_agent.rtk``:自己的串口驱动或厂家的);没配是 None。
        self.rtk = rtk
        self._rtk_logged: int | None = None
        from d1max_agent.rtk_check import RtkCheck
        #: RTK 核对定位器(W09e 决定 8):正在用的图有 geo.json、配了定位器的狗才核。
        self._rtk_check = RtkCheck()
        self._rtk_reloc: asyncio.Task | None = None
        self.topics = registration.topics
        self.hal = hal
        self.boot_id = boot_id or f"boot-{uuid.uuid4().hex[:10]}"
        self._now = now_ms
        self.loaded_map = loaded_map
        #: 正在用的图校验不过的原因(W09g);空串 = 没问题。换图成功清掉。
        self.map_problem = ""
        self._telemetry_period = telemetry_period_ms
        #: 空闲时也要周期刷 status 的 last_seen —— 派遣条件要「last_seen 新鲜」(总设计 §3.1)。
        self.status_period_ms = status_period_ms
        self._next_status_ms: int | None = None
        store_dir = Path(store_dir)
        # W00b:goto 跑在 MissionEngine 上。parts 不给就按 loaded_map/home 现装一组。
        if parts is None and loaded_map is not None:
            parts = build_engine(hal, runs_root=runs_root or (store_dir / "runs"), now_ms=now_ms,
                                 monotonic=monotonic or time.monotonic, map_id=loaded_map[0],
                                 home=home, nav_kind=nav, robot_radius_m=robot_radius_m)
        self.parts = parts
        #: 区域(W10):每个几何版本一份,落盘;``_zones`` 是正在用的那一份,``_zones_pending`` 是等狗空闲
        #: 再换的放宽。
        self.zonebook = ZoneBook(store_dir / "zones.json")
        self._zones: ZoneSet | None = None
        self._zones_pending: ZoneSet | None = None
        self._grid_job: asyncio.Task | None = None
        if parts is not None and hasattr(parts.nav, "on_event"):
            parts.nav.on_event = self._nav_event
        #: 局部避障(W11):感知节点经本机障碍桥(``obs.sock``)给局部栅格;规划后端的守卫用它。
        if obstacles not in ("none", "bridge"):
            raise ValueError(f"obstacles 要是 none 或 bridge,给的是 {obstacles!r}")
        self._obs_srv: Any = None
        self.obs_view: Any = None
        self._obs_state_told = ""
        self._head_told: tuple[str, bool] | tuple[()] = ()
        if obstacles == "bridge":
            if parts is None or not hasattr(parts.nav, "guard"):
                raise ValueError("--obstacles bridge 要配 --nav planned(守卫在规划后端里)")
            from d1max_agent.obs_server import ObsBridgeServer
            from d1max_agent.obstacles import ObstacleGuard, ObstacleView
            self.obs_view = ObstacleView(monotonic=monotonic or time.monotonic)
            parts.nav.obstacles = self.obs_view
            parts.nav.guard = ObstacleGuard()
            self._obs_srv = ObsBridgeServer(obs_socket or store_dir / "obs.sock", self.obs_view,
                                            monotonic=monotonic or time.monotonic)
        #: 每张图上的原点(W00c6f 内审):标的、站点下发时带的都记在这儿,重启先看它(``--home`` 垫底)。
        self.homes = HomeBook(store_dir / "homes.json")
        if parts is not None and loaded_map is not None:
            kept = self.homes.get(*loaded_map)
            if kept is not None:
                parts.home = HomePoint(map_id=loaded_map[0], pose=Pose.from_xy_yaw(*kept),
                                       marked_at_ms=now_ms(), note="W00c6f:狗上记着的原点")
        # W00c6e:地图位姿来自里程锚定。仿真的里程就是真实位置(按原样);真狗开机要人给一次位置。
        if odom_identity is None:
            odom_identity = self.adapter_id.split("/")[0] == "sim"
        #: 本机定位桥(W09a):配了定位器(``localizer="bridge"``)才有。
        self._locsrv: LocBridgeServer | None = None
        if localizer not in ("anchor", "bridge"):
            raise ValueError(f"localizer 要是 anchor 或 bridge,给的是 {localizer!r}")
        if parts is not None and localizer == "bridge":
            # 地图位姿来自本机桥上的定位器(W09b 的 ROS 节点;仿真是仿真定位器)。
            mono = monotonic or time.monotonic
            src = BridgeLocalizer(monotonic=mono)
            src.on_map(loaded_map)
            self._locsrv = LocBridgeServer(loc_socket or store_dir / "loc.sock", src,
                                           monotonic=mono)
            src.link = self._locsrv
            src.on_corrected = self._loc_corrected
            parts.nav.use_anchor(src)
        elif parts is not None:
            anchor = OdomAnchor(identity=odom_identity)
            anchor.on_map(loaded_map)
            parts.nav.use_anchor(anchor)
        self.events = EventBook(store_dir / "events.jsonl", boot_id=self.boot_id, now_ms=now_ms)
        self.idem = IdempotencyStore(store_dir / "idempotency.jsonl")
        self.processor = CommandProcessor(
            registration=registration, now_ms=now_ms, idem=self.idem, events=self.events,
            ledger=ResourceLedger(), supported_tasks=self._supported(),
            loaded_map=loaded_map, state_path=store_dir / "state.json",
            task_factory=self._make_task)
        #: 按需推流(W00c5b)。``video_failed`` 进事件簿:断线时留在狗上、重连补投。
        self.video = video
        #: halt(W00c5c)当场停车;W00c6a 起先撤导航桥的目标再停 HAL,不依赖引擎。
        self.processor.halt_hook = self._stop_motion
        #: 自主级别(W00c6i):``supervised`` 下 goto/巡检只在有人现场监护时才收,监护过期当场中止。
        if autonomy is None:
            # 没给就按适配器定(W00c6i 内审:默认偏收紧):只有仿真可自主,别的一律要人监护。
            autonomy = "autonomous" if self.adapter_id.split("/")[0] == "sim" else "supervised"
        if autonomy not in ("supervised", "autonomous"):
            raise ValueError(f"autonomy 要是 supervised 或 autonomous,给的是 {autonomy!r}")
        self.autonomy = autonomy
        #: 监护会话(W00c6i 内审:多人各自续、各自放):会话号 → (谁, 见过的最大序号, 监护到什么时候,
        #: 这条记录留到什么时候)。时刻都是单调钟秒。放了的会话留一阵(挡同一会话迟到的旧心跳)。
        self._sessions: dict[str, tuple[str, int, float, float]] = {}
        #: 这一次「没人监护」已经停过车了(不每拍都停一次)。
        self._lapse_stopped = False
        self.processor.supervise_hook = self._supervise
        log.info("自主级别: %s%s", autonomy,
                 "(goto/巡检只在有人现场监护时才收)" if autonomy == "supervised" else "")
        #: 遥控的收帧时刻、帧有效期、租约走单调钟(W00c5c 内部评审):墙钟会被 NTP 往回拨。
        self._mono = monotonic or time.monotonic
        #: 发件箱的盘况(W00c5d):随遥测每 ``STORAGE_EVERY_MS`` 带一次;满了不接巡检。
        self._storage = storage_facts
        self._next_storage_ms = 0
        self.processor.admit_hook = self._admit
        #: 地图(W00c5d 第二部分):正在用的那一张(``MapKeeper``)、录包与重建(``MappingService``)。
        self.maps = maps
        self.mapper = mapper
        #: 发布(W00c5d 第三部分):``ReleaseOps``。
        self.releases = releases
        #: 升级前检查的外部来源(W00c6d,W08 追加):W09 定位器、W11 感知与外参自检接上之后各挂一个
        #: ``(名字, async () -> SourceCheck)``;现在是空的,清单里那三项写「还没部署」。每个来源最多等
        #: ``PRECHECK_SOURCE_TIMEOUT_S``,炸了、卡住都按不健康(名字按登记的,不信来源自己报的)。
        self.precheck_sources: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        if self._locsrv is not None:
            self.precheck_sources.append(("定位器", self._loc_health))
        #: 各用各的后台槽:换图、录包、重建、发布。``_switching``:正在载入、切坐标系。
        self._map_job: asyncio.Task | None = None
        self._rec_job: asyncio.Task | None = None
        self._build_job: asyncio.Task | None = None
        self._release_job: asyncio.Task | None = None
        self._switching = False
        #: 切版本、退版本收下了、马上要重启:什么都不再接(W00c5d 第三部分内部评审:
        #: 这几秒里收下的任务会被重启掐掉)。切不成就放开。
        self._restarting = False
        #: 录包时的轨迹(W00c6h);``_trail_on``:上一拍在不在录(录包开始那一拍清空)。
        self.trail = MappingTrail()
        self._trail_on = False
        #: 录包那个后台槽正在做的是开还是停(W00c6h 内审:开录要十几秒,这段时间报 ``starting``)。
        self._rec_action = ""
        #: 发件箱隔离的文件重新排上(站点改了规矩之后,管理员让它再传一次);主程序接上。
        self._outbox_retry: Callable[[], int] | None = None
        self.processor.map_hook = self._map_command
        #: 丢掉的遥控帧计数(不合契约的)。
        self.teleop_malformed = 0
        if video is not None:
            video.emit = self.events.emit
            self.processor.video = video
        self.transport = GuardedTransport(transport, TopicAcl(self.topics),
                                          after_connect=self._flush_reconnect)
        self.online = False
        self._reconnect_pending = False
        self._went_offline = False
        self._last_status_wire: dict | None = None
        self._next_telemetry_ms: int | None = None
        self._started = False
        #: HAL 碰过没有(connect 调过就算,哪怕它抛了)。close() 据此决定要不要收 HAL。
        self._hal_touched = False
        #: 上一次报出去的 HAL 故障集合(W00c5a):变了才发一条 ``robot_fault``。**起来第一拍总发一次
        #: 全集**(``None``):站点记着的可能是重启前的故障(比如「跌倒」),不发的话它永远清不掉,
        #: 下一次跌倒就认不出来(W00c5a 内部评审阻断)。
        self._last_faults: tuple[Fault, ...] | None = None
        self._faults_error = ""
        self._closed = False
        #: 命令按到达顺序串行处理:QoS 1 的重复可能几乎同时到,处理里一旦有 await,两条都会
        #: 先通过幂等查询。
        self._cmd_lock = asyncio.Lock()

    # ------------------------------------------------------------ 合成

    def _supported(self) -> set[str]:
        caps = self.hal.hal_capabilities()
        kinds = ({"goto", "patrol"} if (self.loaded_map is not None and caps.max_vx > 0)
                 else set())
        if self._planned and not self.parts.nav.plan_ok:
            kinds -= {"goto", "patrol"}            # W10:规划后端没有规划栅格,不宣告能自主
        if caps.max_vx > 0:
            kinds.add("teleop")                    # W00c5c:遥控不要地图
        return kinds

    def _make_task(self, cmd: Command) -> Task:
        if cmd.kind == "teleop":
            from d1max_agent.tasks.teleop import TeleopTask
            from d1max_contract.teleop import parse_teleop_grant
            epoch, operator, ttl = parse_teleop_grant(cmd.payload)
            return TeleopTask(task_id=cmd.task_id, lease_epoch=epoch, operator=operator,
                              lease_ttl_ms=ttl, hal=self.hal, now_ms=self._mono_ms,
                              video_live=self._video_live, events=self.events,
                              priority=cmd.priority)
        assert self.parts is not None, "有 loaded_map 就一定装了引擎"
        if cmd.kind == "patrol":
            from d1max_agent.tasks.patrol import PatrolTask
            from d1max_contract.mission import parse_mission
            return PatrolTask(task_id=cmd.task_id, mission=parse_mission(cmd.payload["mission"]),
                              parts=self.parts, events=self.events, now_ms=self._now,
                              priority=cmd.priority)
        target = MapPose.from_wire(cmd.payload["target"])
        return EngineGotoTask(task_id=cmd.task_id, target=target,
                              max_speed_mps=cmd.payload.get("max_speed_mps"), parts=self.parts,
                              events=self.events, now_ms=self._now, priority=cmd.priority)

    def _admit(self, cmd: Command) -> str:
        """发件箱满了(盘到停止水位或发件箱到上限)不接巡检 —— 绝不删没传完的来腾地方。
        事件派遣的 ``goto``、遥控不拍照、不产生文件,照接。正在换图时不接自动任务(遥控照接)。"""
        if self._restarting:
            return "restarting"
        if cmd.kind in _AUTONOMOUS_KINDS and self.autonomy == "supervised" \
                and not self._supervised_now():
            return "unsupervised"                 # W00c6i:没人现场监护,真狗不自己动
        if cmd.kind in ("goto", "patrol") and self._map_busy():
            return "map_switching"
        if cmd.kind in _AUTONOMOUS_KINDS and self.parts is not None and self._head_key()[1]:
            return "head_not_forward"             # W11a、W09i:头尾不知道,或狗尾为前但后面看不清
        if cmd.kind != "patrol" or self._storage is None:
            return ""
        f = self._storage()
        return "storage_full" if f is not None and f.full() else ""

    # ------------------------------------------------------------ 地图(W00c5d 第二部分)

    async def _load_active_map(self) -> None:
        """起来时(连站点之前):狗上有站点下发过的正在用的那张图,**先完整校验**(W09g,在线程里算
        哈希),过了交给适配器载入,就用它(覆盖 ``--map``)。校验不过见 :meth:`_map_integrity_failed`
        (不退回 ``--map``);载不进去就照旧用 ``--map``,并发一条 ``map_load_failed`` 让站点知道。"""
        if self.maps is None:
            return
        from d1max_agent.maps import MapIntegrityError
        try:
            ref = await asyncio.to_thread(self.maps.verify_active)
        except Exception as exc:  # noqa: BLE001 —— 什么错都按「坏了」收,不让代理起不来(内审阻断 1)
            self._map_integrity_failed(exc if isinstance(exc, (MapIntegrityError, OSError))
                                       else MapIntegrityError(f"{type(exc).__name__}: {exc}"))
            return
        if ref is None:
            return
        if self._locsrv is not None:
            missing = [n for n in PRIOR_FILES if n not in {f.name for f in ref.files}]
            if missing:                   # 定位器要读的先验不在清单里:没校验过(内审小 1)
                self._map_integrity_failed(MapIntegrityError(
                    f"正在用的图 {ref.map_id}:{ref.version} 没有定位先验({', '.join(missing)}),"
                    "配了定位器的狗用不了"))
                return
        if self._locsrv is not None:                  # 配了定位器:换图不经 HAL(W09c 决定 6)
            self._switch_map(ref)
            return
        loader = getattr(self.hal, "load_map", None)
        if loader is None:
            return
        try:
            await asyncio.wait_for(loader(ref.map_id, ref.version, self.maps.dir_of(ref)),
                                   LOAD_MAP_TIMEOUT_S)
        except Exception as exc:
            log.exception("起来时载入正在用的图 %s:%s 失败,照旧用 --map", ref.map_id, ref.version)
            self.events.emit("map_load_failed", {"map_id": ref.map_id, "version": ref.version,
                                                 "reason": f"{type(exc).__name__}: {exc}"[:200]})
            return
        self._switch_map(ref)

    def _map_integrity_failed(self, exc: BaseException) -> None:
        """正在用的图校验不过(W09g):不载 HAL 地图、不给定位器先验,**也不退回 ``--map``** —— 站点以为
        狗在它下发的那张图上。没有图:不报 goto、巡检、设位置、标原点,锚定与定位器作废。代理照常起来
        连站点:叫停、遥控、日志、换图(站点再发一次同一版就是修)都能用。"""
        decl = self.maps.active() if self.maps is not None else None
        self.map_problem = str(exc)[:200] or type(exc).__name__
        log.error("正在用的图校验不过,不载、不宣告能自主:%s", self.map_problem)
        self.loaded_map = None
        self.processor.loaded_map = None
        self.processor.supported = self._supported()
        self._rtk_check.on_map(None)
        if self.parts is not None:
            self.parts.nav.anchor.on_map(None)
        data: dict[str, Any] = {"reason": self.map_problem}
        if decl is not None:
            data |= {"map_id": decl.map_id, "version": decl.version}
        self.events.emit("map_integrity_failed", data)

    def _map_busy(self) -> bool:
        """正在**换**图(载入、切坐标系)那一小段:这时候不接自动任务。下载、重建都不算。"""
        return self._switching

    @staticmethod
    def _running(job: asyncio.Task | None) -> bool:
        return job is not None and not job.done()

    def _tasks_idle(self) -> bool:
        cur = self.processor.current
        return (cur is None or cur.done) and not self.processor.pending

    def _extra_tasks(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if self.maps is not None and (self._locsrv is not None
                                      or getattr(self.hal, "load_map", None) is not None):
            # 正在用的图校验不过的原因(W09g 内审再议):告警被人手动解决之后,单狗视图里还看得到
            out["map_activate"] = {"problem": self.map_problem} if self.map_problem else {}
        if self.mapper is not None:
            out["mapping"] = {"live": True}     # W09c2:能边走边建(站点据此放行带版本的开录)
            out["mapping_trail"] = {}           # W00c6h:录包时的轨迹(手机画哪儿走过了)
            if callable(getattr(self.mapper, "preview", None)):
                out["mapping_preview"] = {}     # W09f:边走边建时的预览(手机看正在长的图)
            out["map_build"] = {}
        if self.releases is not None:
            # 站点据此显示每台狗在跑哪一版。
            out["release_install"] = {"current": self.releases.current()}
            out["release_activate"] = {}
            out["release_rollback"] = {}
            out["release_precheck"] = {}        # W00c6d:升级前检查,清单在回执里
        if self.mapper is not None and getattr(self.mapper, "log_dir", None) is not None:
            out["proc_log"] = {}                # W00c6g:录包、重建子进程的日志(列表、尾巴)
        if self.parts is not None and self.loaded_map is not None:
            # W00c6e:设位置(里程锚定)。真狗要人给位置;仿真按原样,也收(测试挪坐标用)。
            out["relocalize"] = {"needs_pose": not self.parts.nav.anchor.identity}
            # W00c6f:在当前位置标原点(定位不好就拒);W13a:也能只标待命点(不动原点)
            out["mark_home"] = {"standby": True}
            # W11a、W09i:头尾方向、这会儿能不能自己走(站点据此派不派会自己走的任务)
            head, blocked = self._head_key()
            out["head"] = {"direction": head, "autonomy": not blocked}
            # W10:区域修订号、守不守(直线桥不守禁行区)、能不能规划。站点按它补发、拒派。
            nav = self.parts.nav
            z: dict[str, Any] = {"rev": self._zones.revision if self._zones else 0,
                                 "enforced": self._planned}
            if self._zones_pending is not None:
                z["pending_rev"] = self._zones_pending.revision
            if self._planned:
                z["plan_ok"] = bool(nav.plan_ok)
                if nav.plan_problem:
                    z["problem"] = nav.plan_problem
            out["zones_set"] = z
        if self.obs_view is not None:
            # W11:避障能不能用(``ok`` 才派会自己走的任务);外参自检没过带原因
            o: dict[str, Any] = {"state": self.obs_view.state(), "rear": self.obs_view.rear}
            if self.obs_view.reason:
                o["reason"] = self.obs_view.reason[:200]
            out["obstacles"] = o
        if self._storage is not None:
            out["outbox_retry"] = {}
        if self.rtk is not None:
            out["rtk"] = {"source": self.rtk.kind}  # W09e:站点、手机据此显示 RTK
        return out

    async def _map_command(self, cmd: Command) -> str | tuple[str, dict[str, Any]]:
        """地图、发布、发件箱命令:收下(空串)或拒绝原因。下载、载入、录包、重建都在后台做,做完发事件。
        **各用各的后台槽**(W00c5d 第二部分内部评审):重建一小时、下载十几分钟都不许挡出警;
        只有换图(载入、切坐标系)那一小段不接自动任务。"""
        from d1max_contract.maps import MapRef, mapping_target, parse_map_build, parse_mapping
        kind = cmd.kind
        if kind not in self._extra_tasks():
            return "unsupported"
        if self._restarting:
            return "restarting"
        if kind == "relocalize":
            return await self._relocalize(cmd)
        if kind == "mark_home":
            return await self._mark_home(cmd)
        if kind == "zones_set":
            return await self._zones_set(cmd)
        if kind == "proc_log":
            return await asyncio.to_thread(self._proc_log, cmd.payload)
        if kind == "mapping_trail":
            n = cmd.payload.get("since", 0)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                return "payload: since 要是不小于 0 的整数"
            starting = self._running(self._rec_job) and self._rec_action == "start"
            return "", self.trail.since(n) | {"recording": bool(self.mapper.recording),
                                              "starting": starting}
        if kind == "mapping_preview":
            n = cmd.payload.get("since", 0)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                return "payload: since 要是不小于 0 的整数"
            try:
                alive_fn = getattr(self.mapper, "preview_alive", None)
                alive = alive_fn() if callable(alive_fn) else True
                got = await asyncio.to_thread(self.mapper.preview, n)
            except Exception as exc:  # noqa: BLE001 —— 回 read_failed,别让这条命令没回执
                return f"read_failed: {type(exc).__name__}: {exc}"[:200]
            if got.get("live") and got.get("recording"):
                got["preview_running"] = alive
            starting = self._running(self._rec_job) and self._rec_action == "start"
            return "", got | {"starting": starting}
        if kind.startswith("release_"):
            return await self._release_command(cmd)
        if kind == "outbox_retry":
            # 放隔离要拿上传线程的锁(它可能正卡在一次上传里):不在事件循环里等。
            n = await asyncio.to_thread(self._outbox_retry) if self._outbox_retry is not None \
                else 0
            self.events.emit("outbox_retry", {"task_id": cmd.task_id, "released": n})
            return ""
        try:
            if kind == "map_activate":
                ref = MapRef.from_wire(cmd.payload)
                home = _parse_home(cmd.payload.get("home"))
            elif kind == "mapping":
                action, name = parse_mapping(cmd.payload)
                target = mapping_target(cmd.payload)
            else:
                bag, map_id, version = parse_map_build(cmd.payload)
        except ContractError as exc:
            return f"payload: {exc}"
        loop = asyncio.get_running_loop()
        if kind == "map_activate":
            if self._running(self._map_job) or self._running(self._release_job):
                return "busy"
            self._map_job = loop.create_task(self._activate(ref, home, cmd.task_id))
            return ""
        if kind == "mapping":
            if self._running(self._rec_job):
                return "busy"
            if action == "start":
                if self.mapper.recording:
                    return "busy"
                if target is not None and self._running(self._build_job):
                    return "busy"                 # 还在重建 / 打包上一趟:不同时在线建
                f = self._storage() if self._storage is not None else None
                if f is not None and f.full():
                    return "storage_full"         # 录包很大:发件箱满了不录
            elif not self.mapper.recording:
                return "not_recording"
            self._rec_job = loop.create_task(self._record(action, name, cmd.task_id, target))
            return ""
        if self._running(self._build_job) or self._running(self._rec_job) \
                or self.mapper.recording:
            return "busy"
        if not self._tasks_idle():
            return "busy"                         # 重建吃 CPU:开跑时不跟巡检抢(开跑后不挡出警)
        self._build_job = loop.create_task(self._build(bag, map_id, version, cmd.task_id))
        return ""

    async def _record(self, action: str, name: str, task_id: str,
                      target: tuple[str, str] | None = None) -> None:
        """录包的开始、停止(在后台做:起、停 ros2 bag 要十几秒,不能在命令锁里等)。"""
        self._rec_action = action
        try:
            if action == "start":
                if target is None:
                    await self.mapper.start(name)
                else:
                    await self.mapper.start(name, target=target, task_id=task_id)
                # 录上了:当场清空、换一趟(W00c6h 内审:不靠每拍看边沿 —— 两拍之间又停又开会漏)。
                self.trail.reset()
                self._trail_on = True
            else:
                await self.mapper.stop()
            self.events.emit("mapping", {"task_id": task_id, "action": action,
                                         "name": self.mapper.last_bag})
        except Exception as exc:  # noqa: BLE001 - 起不来/停不了:原因发给站点
            log.warning("录包 %s 没成:%s", action, exc)
            reason = f"{type(exc).__name__}: {exc}"[:200]
            self.events.emit("mapping_failed", {"task_id": task_id, "action": action,
                                                "reason": reason})
            if target is not None:
                # 这一版没建成:站点据此放开这个版本号(W09c2 内审阻断 2)
                self.events.emit("map_build_failed", {"task_id": task_id, "bag": "",
                                                      "map_id": target[0], "version": target[1],
                                                      "reason": reason})
        if action == "stop" and getattr(self.mapper, "pending", None) is not None \
                and not self._running(self._build_job):
            # 边走边建的(W09c2):打包放到重建那个后台槽里(吃 CPU,跟重建一样不挡出警)。停录报了错
            # 也照样收尾(服务层照样记了待打包;不收的话它一直挂着、之后都开不了 —— 外审复查阻断)
            self._build_job = asyncio.get_running_loop().create_task(
                self._finish_live(task_id))

    async def _release_command(self, cmd: Command) -> str | tuple[str, dict[str, Any]]:
        """发布命令(W00c5d 第三部分):装在后台做,双槽所在的盘不够不装;切、退要空闲(不跑任务、
        不在换图、录包、重建)、电量不低于 ``RELEASE_MIN_BATTERY_PCT``。收下切、退之后到重启之前
        什么都不再接(``_restarting``)。

        W00c6d:切版本按升级前检查的清单判(``release_precheck``),被拒时回执里带整份清单;
        ``release_precheck`` 只算清单、什么都不做。"""
        from d1max_agent.release_precheck import first_block, report
        from d1max_contract.releases import ReleaseRef, check_release_name
        try:
            if cmd.kind == "release_install":
                ref = ReleaseRef.from_wire(cmd.payload)
            elif cmd.kind in ("release_activate", "release_precheck"):
                name = check_release_name(cmd.payload.get("name"))
                schema = cmd.payload.get("mission_schema")
                if schema is not None and (isinstance(schema, bool) or not isinstance(schema, int)
                                           or schema < 1):
                    raise ContractError("mission_schema 要是 ≥1 的整数")
        except ContractError as exc:
            return f"payload: {exc}"
        if cmd.kind in ("release_activate", "release_precheck"):
            items, needs = await self._release_precheck(name, schema)
            if cmd.kind == "release_precheck":
                return "", report(name, items, needs)
            reason = first_block(items)
            if reason:
                return reason, report(name, items, needs)
            self._restarting = True               # 清单里的空闲是最后取的,取完到这儿没有让出
            self._release_job = asyncio.get_running_loop().create_task(
                self._release_switch("activate", name, cmd.task_id))
            return ""
        if self._running(self._release_job) or self._running(self._map_job):
            return "busy"
        if cmd.kind == "release_install":
            if not self.releases.disk_ok(ref.size):
                return "storage_full"             # 下载、解开、落槽、建 venv 都在系统盘上
            job = self._release_install(ref, cmd.task_id)
            self._release_job = asyncio.get_running_loop().create_task(job)
            return ""
        if self._busy_reason():
            return "busy"                         # 重启那几秒谁都停不了它;也会悄悄掐掉录包、重建
        try:
            battery = (await self.hal.battery()).percent
        except Exception:  # noqa: BLE001 - 读不到电量(适配器还没收到第一帧):不切
            return "battery_unknown"
        if battery < RELEASE_MIN_BATTERY_PCT:
            return "low_battery"                  # 切过去起不来还要再退、再起一次
        if not self.releases.disk_ok(0):
            return "storage_full"                 # 在途标记、幂等记录、事件簿都要写得进去
        if not self.releases.can_roll_back():
            return "nothing_to_roll_back"
        if self._busy_reason():
            return "busy"                         # 上面读电量让出过一次:收下之前再看一眼
        self._restarting = True
        self._release_job = asyncio.get_running_loop().create_task(
            self._release_switch("rollback", "", cmd.task_id))
        return ""

    async def _relocalize(self, cmd: Command) -> str:
        """设位置(W00c6e):人给一个地图位姿(或者「狗在原点」),同时记下此刻的里程,锁定锚定。**狗要停着**
        (丢定位暂停时狗停着,可以给;正在走的时候不行)。坐标按狗当前加载的那张图;命令里带了图号就核。"""
        p = cmd.payload
        m = self.loaded_map
        if self.parts is None or m is None:
            return "no_map"
        if "map_id" in p or "map_version" in p:
            if (p.get("map_id"), p.get("map_version")) != m:
                return "map_mismatch"
        if self._running(self._map_job) or self._switching:
            return "busy: 在换图"                 # 给的是这张图上的位置,换完就作废(W09a 内审应修 2)
        if "at_home" in p:
            if p.get("at_home") is not True:
                return "payload: at_home 只能是 true"
            home = self.parts.home
            if home is None:
                return "no_home"
            pose = (home.pose.position.x, home.pose.position.y, home.pose.yaw)
            source = "home"
        else:
            vals = []
            for k in ("x", "y", "yaw"):
                v = p.get(k)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    return f"payload: {k} 要是有限数"
                vals.append(float(v))
            pose = (vals[0], vals[1], vals[2])
            source = "manual"
        cur = self.processor.current
        if cur is not None and not cur.done and cur.kind in ("goto", "patrol") \
                and self.parts.engine.state not in (RunState.PAUSED, RunState.SUSPENDED):
            # 任务跑着、狗只是一时停着(驻留、两段之间)不收:当场改了下一段怎么走(W00c6e 内审阻断 2)。
            # 丢定位暂停、人工暂停/接管时收。
            return "busy"
        o = await self.hal.odometry()
        if not o.valid:
            return "odom_invalid"                 # 先说里程读不到:这时 stopped() 也答不上来
        if not await self.hal.stopped():
            return "moving"
        loc = self.parts.nav.anchor
        if isinstance(loc, BridgeLocalizer):
            # 配了定位器(W09a):请它按人给的位置(初值)重定位;稳下来的时候修正量经 on_corrected
            # 交给引擎。
            why = await loc.relocalize(m, pose)
            if why:
                return why
            # 人给的位置:RTK 核对之前的结论作废、一阵子不按 RTK 自动请(W09e 内审应修 4)
            self._rtk_check.human_override(self._mono())
            loc.rtk_disagree = ""
        else:
            delta = loc.anchor(m, pose, (o.x, o.y, o.yaw))
            # 引擎记的出发点与来路按修正量挪过去(修正量给不出就来路作废);丢定位的次数从头算。
            self.parts.engine.relocalized(delta)
        self.events.emit("relocalized", {"task_id": cmd.task_id, "source": source,
                                         "map_id": m[0], "map_version": m[1],
                                         "x": round(pose[0], 3), "y": round(pose[1], 3),
                                         "yaw": round(pose[2], 4)})
        return ""

    async def _mark_home(self, cmd: Command) -> str | tuple[str, dict[str, Any]]:
        """在当前位置标原点(W00c6f):用**此刻锚定后的地图位姿**当这张图上的原点。狗要停着、不在忙;定位
        要好 —— 锚过、里程新鲜、偏差不大于 ``HOME_MAX_SIGMA_M``。收下:**先落盘**(``homes.json``,
        重启照用;落不了盘就拒)再生效(返航目标、起飞检查),位置放在回执里给站点登记(站点是权威)。

        ``target: "standby"``(W13a,决策 16:原点与待命点拆开):同样的检查、同样回位置,但**不动原点**
        —— 站点拿它登记一个待命点。能力里 ``mark_home.standby`` 报了站点才发(老代理不认这个字段,
        会当成标原点)。"""
        m = self.loaded_map
        if self.parts is None or m is None:
            return "no_map"
        name = cmd.payload.get("name", "home")
        if not isinstance(name, str) or not _HOME_NAME.fullmatch(name):
            return "payload: name 只许字母、数字、. _ -(1–64 字)"
        target = cmd.payload.get("target", "home")
        if target not in ("home", "standby"):
            return "payload: target 只许 home 或 standby"
        busy = self._home_busy()
        if busy:
            return f"busy: {busy}"
        if not await self.hal.stopped():
            return "moving"
        h = await self.hal.health()
        o = await self.hal.odometry()
        anchor = self.parts.nav.anchor
        why = anchor.why_not(h.loc_quality > 0.0 and o.valid)
        if why:
            return f"loc_poor: {why}"
        if anchor.sigma_xy > HOME_MAX_SIGMA_M:
            return (f"loc_poor: 位置偏差可能到 {anchor.sigma_xy:.1f} m(标原点要不大于 "
                    f"{HOME_MAX_SIGMA_M:g} m),先在这儿设一次位置再标")
        est = anchor.estimate((o.x, o.y, o.yaw))
        if est is None:
            return "loc_poor: 报不出地图位姿"
        if (est.map_id, est.map_version) != m or self.loaded_map != m:
            return "busy: 刚换了图"
        data = {"map_id": m[0], "map_version": m[1], "x": round(est.x, 3),
                "y": round(est.y, 3), "yaw": round(est.yaw, 4), "sigma_m": round(est.sigma_xy_m, 2),
                "target": target}
        if target == "standby":
            return "", data                       # 待命点归站点管:狗上什么都不改
        try:
            self.homes.put(m[0], m[1], (est.x, est.y, est.yaw), now_ms=self._now())
        except OSError as exc:
            log.warning("标原点落不了盘:%s", exc)
            return f"persist_failed: {exc}"[:200]
        self.parts.home = HomePoint(map_id=m[0], pose=Pose.from_xy_yaw(est.x, est.y, est.yaw),
                                    marked_at_ms=self._now(),
                                    note=f"W00c6f:在当前位置标的({name})")
        self.events.emit("home_marked", {"task_id": cmd.task_id, "name": name, **data})
        return "", data

    def _proc_log(self, payload: dict[str, Any]) -> str | tuple[str, dict[str, Any]]:
        """建图进程日志(W00c6g,在线程里跑):``{}`` 列表,``{name, bytes?}`` 那一个的尾巴。"""
        from d1max_agent import proc_logs
        d = Path(self.mapper.log_dir)
        try:
            if "name" not in payload:
                return "", proc_logs.list_logs(d)
            return "", proc_logs.tail(d, payload)
        except proc_logs.LogError as exc:
            return str(exc)
        except OSError as exc:
            return f"read_failed: {exc}"[:200]

    def _loc_corrected(self, delta: tuple[float, float, float] | None, human: bool) -> None:
        """定位器跳过之后稳下来、重定位完(W09a):同人工重设(W00c6e),来路、出发点按修正量挪;给不出
        就来路作废。只有人给的位置才把丢定位的次数从头算(内审应修 4)。"""
        if self.parts is not None:
            self.parts.engine.relocalized(delta, reset_attempts=human)

    async def _loc_health(self) -> Any:
        return self.parts.nav.anchor.health()

    def _home_busy(self) -> str:
        """标原点时在忙什么(W00c6f 内审应修 3);空串 = 可以标。遥控不算忙(「开过去再标」)。"""
        if self._running(self._release_job):
            return "在装或在切版本"
        if self._running(self._map_job) or self._switching:
            return "在换图"
        cur = self.processor.current
        if cur is not None and not cur.done and cur.kind in ("goto", "patrol"):
            return f"在跑任务 {cur.task_id}"
        if any(t.kind in ("goto", "patrol") for t in self.processor.pending):
            return "有任务排着队"
        return ""

    def _busy_reason(self) -> str:
        """在忙什么(升级前检查的「空闲」那一项);空串 = 空闲。"""
        if self._running(self._release_job):
            return "在装或在切版本"
        if self._running(self._map_job):
            return "在换图"
        if not self._tasks_idle():
            cur = self.processor.current
            what = cur.task_id if cur is not None and not cur.done else "排着队的任务"
            return f"在跑任务 {what}"
        if self._running(self._build_job):
            return "在重建地图"
        if self._running(self._rec_job) or (self.mapper is not None and self.mapper.recording):
            return "在录包"
        return ""

    async def _release_precheck(self, name: str, mission_schema: int | None):
        """升级前检查(W00c6d):取好事实,交给纯函数算清单 → ``(清单, 槽里那一版要的任务包 schema)``。

        核槽要读整棵槽(放线程里)、来源各等一会儿 —— 这一切都握着命令锁(监护心跳、续租、中止都在
        后面排)。
        **狗在忙就不核、不问**:切版本反正会因为忙被拒(内审应修 2)。"""
        from d1max_agent.release_precheck import PrecheckInputs, SourceCheck, precheck
        rel = self.releases
        busy_before = self._busy_reason()
        try:
            battery: float | None = (await self.hal.battery()).percent
        except Exception:  # noqa: BLE001 - 读不到电量(适配器还没收到第一帧):清单里写读不到
            battery = None
        installed = rel.ready(name)
        needs = rel.requires_mission_schema(name) if installed else None
        package_error = ""
        sources: list[SourceCheck] = []
        if installed and not busy_before:
            package_error = await asyncio.to_thread(rel.check_package, name)
        if not busy_before:
            for label, src in self.precheck_sources:
                try:
                    got = await asyncio.wait_for(src(), PRECHECK_SOURCE_TIMEOUT_S)
                    sources.append(SourceCheck(label, bool(got.ok), str(got.detail)))
                except asyncio.TimeoutError:
                    sources.append(SourceCheck(label, False,
                                               f"超过 {PRECHECK_SOURCE_TIMEOUT_S:g} s 没回"))
                except Exception as exc:  # noqa: BLE001 - 来源自己炸了:这一项按不健康
                    log.warning("升级前检查的来源 %s 炸了: %s", label, exc)
                    sources.append(SourceCheck(label, False,
                                               f"查不了: {type(exc).__name__}: {exc}"))
        # 空闲放在最后取:上面有 await,取完空闲到调用方收下之间不再让出(不会夹进任务)。
        items = precheck(PrecheckInputs(
            name=name, current=rel.current(), installed=installed, package_error=package_error,
            can_switch=rel.can_switch_to(name), disk_ok=rel.disk_ok(0), battery_pct=battery,
            busy=self._busy_reason(), sources=tuple(sources), mission_schema=mission_schema,
            requires_mission_schema=needs, skipped=bool(busy_before)))
        return items, needs

    async def _release_install(self, ref, task_id: str) -> None:
        base = {"task_id": task_id, "name": ref.name}
        try:
            await asyncio.to_thread(self.releases.install, ref)
            self.events.emit("release_installed", base)
        except Exception as exc:  # noqa: BLE001 - 装不上:原因发给站点
            log.warning("装版本没成(%s):%s", ref.name, exc)
            self.events.emit("release_install_failed",
                             base | {"reason": f"{type(exc).__name__}: {exc}"[:200]})

    async def _release_switch(self, what: str, name: str, task_id: str) -> None:
        base = {"task_id": task_id, "name": name}
        try:
            if what == "activate":
                await asyncio.to_thread(self.releases.activate, name)
                self.events.emit("release_activating", base)
            else:
                back = await asyncio.to_thread(self.releases.rollback)
                self.events.emit("release_rolling_back", base | {"name": back})
        except Exception as exc:  # noqa: BLE001 - 切不过去:原因发给站点,照旧跑这一版
            self._restarting = False
            log.warning("%s 没成:%s", what, exc)
            self.events.emit(f"release_{what}_failed",
                             base | {"reason": f"{type(exc).__name__}: {exc}"[:200]})

    async def _activate(self, ref, home, task_id: str) -> None:
        """下载(不挡任务)→ 等空闲(最多 ``SWITCH_WAIT_S``)→ 载入、切坐标系(这一小段不接自动任务)
        → 记成正在用的。提交失败就把原来那张载回去;发不出能力不算换图失败。"""
        from d1max_agent.maps import MapInstallError
        base = {"task_id": task_id, "map_id": ref.map_id, "version": ref.version}
        # 原来那张校验不过(W09g):提交失败时不把它载回去
        old = None if self.map_problem else self.maps.active()
        try:
            if self._locsrv is not None:
                # 配了定位器:这一版要带定位先验(站点上 slam_toolbox 时期的老版本没有;激活了定位器
                # 拒先验,狗之后一直定不了位 —— 内审应修 3)。下载之前就拒。
                missing = [n for n in PRIOR_FILES if n not in {f.name for f in ref.files}]
                if missing:
                    raise MapInstallError(f"这一版没有定位先验({', '.join(missing)}),"
                                          "配了定位器的狗用不了:换一版狗上建的图")
            dst = await asyncio.to_thread(self.maps.install, ref)
            waited = 0.0
            while not self._tasks_idle():
                if waited >= SWITCH_WAIT_S:
                    self.maps.discard(ref)
                    raise MapInstallError(f"等了 {SWITCH_WAIT_S:g} s 狗一直有任务:没换")
                await asyncio.sleep(1.0)
                waited += 1.0
            self._switching = True
            try:
                try:
                    await self._hal_load(ref.map_id, ref.version, dst)
                except Exception as exc:
                    self.maps.discard(ref)
                    raise MapInstallError(f"适配器载不进去: {type(exc).__name__}: {exc}") from exc
                try:
                    # 原点先记(站点是权威:带了就按它,没带就删掉标过的、按图里的 home.json)。
                    self.homes.put(ref.map_id, ref.version, home, now_ms=self._now())
                    self.maps.commit(ref, home=home)
                except Exception as exc:
                    # 适配器已经是新图了,狗报的还是老版本:把原来那张载回去,不留这种两边不一致。
                    if old is not None:
                        try:
                            await self._hal_load(old.map_id, old.version, self.maps.dir_of(old))
                        except Exception:
                            log.exception("提交失败后载回原来的图也失败了")
                    raise MapInstallError(f"记不下正在用的图: {exc}") from exc
                self._switch_map(ref)
            finally:
                self._switching = False
            self.events.emit("map_activated", base)
            log.info("换图了:%s:%s", ref.map_id, ref.version)
        except MapInstallError as exc:
            log.warning("换图没成(%s:%s):%s", ref.map_id, ref.version, exc)
            self.events.emit("map_activate_failed", base | {"reason": str(exc)[:200]})
            return
        except Exception as exc:
            log.exception("换图炸了")
            self.events.emit("map_activate_failed",
                             base | {"reason": f"{type(exc).__name__}: {exc}"[:200]})
            return
        try:
            await self._publish_caps()
        except Exception:
            log.exception("换图之后发能力没成(下次重连会再发)")

    async def _hal_load(self, map_id: str, version: str, d: Path) -> None:
        """适配器载入一张图。配了定位器的狗不经 HAL(W09c 决定 6、W08 决定 5):换图归代理的导航后端,
        先验经本机桥交给定位器(:meth:`_switch_map`)。"""
        if self._locsrv is None:
            await self.hal.load_map(map_id, version, d)

    async def _finish_live(self, task_id: str) -> None:
        """边走边建的这一版收尾(W09c2):打包在线建好的,不成退回从录包建;发 ``map_built``(带是哪种
        建法、退回的原因)或 ``map_build_failed``。"""
        bag, map_id, version = self.mapper.pending
        # 带开录那条命令的号(站点按它认是哪一次,外审阻断 3);没有(老的恢复标记)才用停录的
        task = getattr(self.mapper, "pending_task", "") or task_id
        base = {"task_id": task, "bag": bag, "map_id": map_id, "version": version}
        try:
            _, mode = await self.mapper.finish()
            rays = getattr(self.mapper, "last_rays", "")
            extra = {"mode": mode, **({"grid_rays": rays} if rays else {})}
            if self.mapper.last_fallback:
                extra["fallback"] = self.mapper.last_fallback
            self.events.emit("map_built", base | extra)
        except Exception as exc:  # noqa: BLE001 - 建不成:原因发给站点
            log.warning("边走边建的 %s:%s 没建成:%s", map_id, version, exc)
            self.events.emit("map_build_failed",
                             base | {"reason": f"{type(exc).__name__}: {exc}"[:200]})

    async def _build(self, bag: str, map_id: str, version: str, task_id: str) -> None:
        base = {"task_id": task_id, "bag": bag, "map_id": map_id, "version": version}
        try:
            await self.mapper.build(bag, map_id, version)
            rays = getattr(self.mapper, "last_rays", "")
            if rays.startswith("synthetic"):
                log.warning("建图的栅格用的是模拟射线(跟着狗走的人清不掉):%s", rays)
            self.events.emit("map_built", base | ({"grid_rays": rays} if rays else {}))
        except Exception as exc:  # noqa: BLE001 - 重建失败:原因发给站点
            log.warning("重建没成(%s → %s:%s):%s", bag, map_id, version, exc)
            self.events.emit("map_build_failed",
                             base | {"reason": f"{type(exc).__name__}: {exc}"[:200]})

    def _switch_map(self, ref) -> None:
        self.map_problem = ""
        self._rtk_check.on_map(self._load_geo(ref))
        self.loaded_map = (ref.map_id, ref.version)
        self.processor.loaded_map = self.loaded_map
        self.processor.supported = self._supported()
        if self.parts is not None:
            home = self.homes.get(ref.map_id, ref.version)
            if home is None and self.maps is not None:
                home = self.maps.home_of(ref)
            self.parts.switch_map(ref.map_id, None if home is None else Pose.from_xy_yaw(*home),
                                  now_ms=self._now())
            # 换了图:锚定作废(W00c6e);定位器换先验(W09a,带上这张图在狗上的目录)。
            self.parts.nav.anchor.on_map(
                self.loaded_map, str(self.maps.dir_of(ref)) if self.maps is not None else "")
        # 区域跟着几何版本走(W10):换图就换成这一版记着的那份,等放宽的作废
        self._zones = self.zonebook.get(ref.map_id, ref.version)
        self._zones_pending = None
        if self._planned:
            nav = self.parts.nav
            nav.load_grid(None)
            nav.plan_problem = "规划栅格载入中"
            d = self.maps.dir_of(ref) if self.maps is not None else None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._grid_job = loop.create_task(self._load_grid(d))

    @property
    def _planned(self) -> bool:
        return self.parts is not None and hasattr(self.parts.nav, "load_grid")

    async def _load_grid(self, d: Path | None) -> None:
        """换图后载规划栅格与区域(W10,在线程里读图);完了重发能力(``plan_ok`` 变了)。
        区域取读完图**那一刻**的(内审阻断 1:读图期间站点发来的新区域已经换上了,不许拿换图
        那一刻的旧值盖回去)。"""
        nav = self.parts.nav
        try:
            await asyncio.to_thread(nav.load_grid, d)
            await nav.set_zones(self._zones)
        except Exception as exc:
            log.exception("载规划栅格出错")
            nav.load_grid(None)
            nav.plan_problem = f"载规划栅格出错:{type(exc).__name__}: {exc}"[:200]
        self.processor.supported = self._supported()
        if self.transport.connected:
            await self._publish_caps()

    def _nav_event(self, kind: str, data: dict) -> None:
        m = self.loaded_map or ("", "")
        self.events.emit(kind, dict(data) | {"map_id": m[0], "map_version": m[1]})

    async def _zones_set(self, cmd: Command) -> str | tuple[str, dict[str, Any]]:
        """站点下发区域(W10):整份换。收紧的(或狗空闲)立刻换上;放宽的等狗空闲再换。
        同一修订重发 = 收下不动;旧修订拒(``stale``);几何版本不是正在用的拒(``map_mismatch``)。"""
        try:
            zs = ZoneSet.from_wire(cmd.payload.get("zones"))
        except ContractError as exc:
            return f"payload: {exc}"
        if self.loaded_map != (zs.map_id, zs.map_version):
            return "map_mismatch"
        cur = self._zones or ZoneSet(zs.map_id, zs.map_version, 0, ())
        newest = self._zones_pending or cur
        if zs.revision < newest.revision or (zs.revision == newest.revision and zs != newest):
            return "stale"
        if zs == newest:
            return "", {"revision": zs.revision, "deferred": self._zones_pending is not None}
        try:
            await asyncio.to_thread(self.zonebook.put, zs)
        except OSError as exc:
            return f"store_failed: {exc}"[:200]
        if tightens(cur, zs) or self._tasks_idle():
            await self._apply_zones(zs)
            return "", {"revision": zs.revision, "deferred": False}
        log.info("区域修订 %d 是放宽:等狗空闲再换", zs.revision)
        self._zones_pending = zs
        if self.transport.connected:
            await self._publish_caps()                # 站点看得到「等着换」的那一版
        return "", {"revision": zs.revision, "deferred": True}

    async def _apply_zones(self, zs: ZoneSet) -> None:
        self._zones = zs
        self._zones_pending = None
        if self._planned:
            await self.parts.nav.set_zones(zs)
        if self.transport.connected:
            await self._publish_caps()

    async def _publish_caps(self) -> None:
        caps = compose_capabilities(
            robot_id=self.registration.robot_id, hal_caps=self.hal.hal_capabilities(),
            adapter_id=self.adapter_id, loaded_map=self.loaded_map,
            extra_tasks=self._extra_tasks(),
            nav_path=getattr(self.parts.nav, "PATH_KIND", "straight") if self.parts else "straight",
            autonomy=self.autonomy)
        await self.transport.publish(self.topics.capabilities, _dumps(caps.to_wire()),
                                     qos=1, retain=True)

    def _video_live(self) -> bool:
        """遥控「没画面不许动」在狗这头的那一道:本机推流器至少一路在推。站点那头还有一道。"""
        v = self.video
        return v is not None and bool(v.running())

    @property
    def adapter_id(self) -> str:
        return getattr(self.hal, "adapter_id", type(self.hal).__name__)

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """起不来就回滚:``close()`` 把已经拿到的控制权放掉、链路关掉,再把异常抛给调用方。"""
        try:
            await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self) -> None:
        # HAL 的生死归代理(总设计 §2.2「谁连 SDK」):先连上、拿到厂商控制权,再对站点亮相。
        self._closed = False
        self._hal_touched = True
        await self.hal.connect()
        await self.hal.acquire_control()
        if self.parts is not None:
            # 头尾方向(W11a)先读一次:不然导航桥走第一拍之前进来的 goto 会被当成「不知道」拒掉
            try:
                self.parts.nav.head = getattr(await self.hal.health(), "head", "unknown")
            except Exception:
                log.exception("起来时读不到头尾方向")
            self._head_told = self._head_key()
        if self.parts is not None:
            # 两个桥各自记着「连上了」(老 HTTP 面的绿灯读它);HAL.connect 是幂等的。
            await self.parts.device.connect()
            await self.parts.nav.connect()
        self.transport.set_will(
            self.topics.status,
            _dumps(offline_status(boot_id=self.boot_id,
                                  control_epoch=self.processor.control_epoch).to_wire()),
            qos=1, retain=True)
        self.transport.on_connection(self._on_connection)
        self._started = True
        # **先登记 cmd 的 handler,再连接。** 持久会话在 CONNACK 后立刻补投离线命令;handler
        # 要等连上才登记的话,进程重启期间站点派的 abort 会在无人接收时被投掉、永久丢失。
        await self.transport.subscribe(self.topics.cmd, self._on_cmd, qos=1)
        # 遥控帧(W00c5c):专用主题、QoS 0 —— 断线期间的帧不补投(决策 7:不许重放)。
        await self.transport.subscribe(self.topics.teleop, self._on_teleop, qos=0)
        if self.rtk is not None:
            # 基站改正数据(W09e):QoS 0,过时的没用
            await self.transport.subscribe(self.topics.rtcm, self._on_rtcm, qos=0)
            self.rtk.start()
        # 先载正在用的图,再连站点:连上之后进来的命令要按真的地图版本核对。
        await self._load_active_map()
        if self._zones is None and self.loaded_map is not None:
            self._zones = self.zonebook.get(*self.loaded_map)
            if self._planned and self._grid_job is None:
                await self.parts.nav.set_zones(self._zones)
        if self._locsrv is not None:
            # 定位器可以先连上来,不等站点;但要在校验完正在用的图之后(W09g 内审应修 1:原来先开桥,
            # 校验几百 MB 的那几秒里定位器连上来,拿到的是命令行那张图的先验)
            await self._locsrv.start()
        if self._obs_srv is not None:
            await self._obs_srv.start()          # 感知节点(W11)也可以先连上来
        await self.transport.connect()          # 首次连接:after_connect 会走 _flush_reconnect
        await self._publish_caps()
        if self.releases is not None:
            # 起来、连上站点了:在途的那一次升级算成(W00c5d 第三部分)。起不来的那种,
            # 开机守卫数够次数已经退回上一版了。
            try:
                done = self.releases.commit_if_pending()
            except Exception:
                log.exception("提交升级失败")
                done = None
            if done:
                self.events.emit("release_committed", {"name": done})
                await self._publish_caps()
            try:
                note = self.releases.take_guard_note()
            except Exception:
                log.exception("读开机守卫的条子失败")
                note = None
            if note is not None:
                # 新版起不来,开机守卫退回了上一版(W00c5d 第三部分内部评审):告诉站点。
                self.events.emit("release_rolled_back", {k: note.get(k) for k in
                                                         ("from", "to", "attempts", "at_ms",
                                                          "no_fallback")})
        self._resume_live()
        await self._publish_status(force=True)

    def _resume_live(self) -> None:
        """上次没收完的边走边建(代理重启了,W09c2 内审应修 1):接着收尾;收不了的报「没建成」,站点
        放开那个版本号。"""
        if self.mapper is None:
            return
        for bag, map_id, version, task in getattr(self.mapper, "lost", []):
            self.events.emit("map_build_failed", {"task_id": task, "bag": bag, "map_id": map_id,
                                                  "version": version,
                                                  "reason": "代理重启了,这一趟没收完"})
        if getattr(self.mapper, "pending", None) is not None:
            self._build_job = asyncio.get_running_loop().create_task(self._finish_live(""))

    async def close(self) -> None:
        """收尾,顺序:引擎(停当前这趟)→ HAL 停 → 放控制权(能放的才放)→ 关桥与 HAL 链路
        → 关 transport。
        每一步单独兜异常:哪一步炸了只记日志,后面的照做 —— 控制权不能因为引擎关不干净就
        留在一个要退出的进程手里。可重入:没 start 过、start 到一半、调两次都安全。"""
        if self._closed:
            return
        self._closed = True

        async def _step(what: str, fn) -> None:
            try:
                await fn()
            except Exception:
                log.exception("收尾:%s 失败,后面的照做", what)

        if self.parts is not None:
            await _step("关引擎", self.parts.engine.aclose)
        if self._planned:
            async def _planner_off() -> None:
                if self._grid_job is not None:
                    self._grid_job.cancel()
                self.parts.nav.close_planner()
            await _step("关规划子进程", _planner_off)
        if self.rtk is not None:
            async def _rtk_off() -> None:
                await asyncio.to_thread(self.rtk.close)
            await _step("停 RTK", _rtk_off)
        if self._locsrv is not None:
            await _step("关本机定位桥", self._locsrv.close)
        if self._obs_srv is not None:
            await _step("关本机障碍桥", self._obs_srv.close)
        if self.video is not None:
            async def _video_off() -> None:
                self.video.close()
            await _step("停推流", _video_off)
        if self.mapper is not None and hasattr(self.mapper, "shutdown"):
            # 正在录就好好停下:录包写完索引、在线建图 SIGINT 存盘(W09c2),重启后接着打包
            await _step("停录包", self.mapper.shutdown)
        if self._hal_touched:
            await _step("HAL 停", self.hal.stop)
            if self.hal.hal_capabilities().control_releasable:
                await _step("放控制权", self.hal.release_control)
            else:
                # W00d:真狗的 SDK 控制权放了就得重启整机,常驻旁路进程一直握着。
                log.info("这台机器的控制权不可释放,收尾不放")
            if self.parts is not None:
                await _step("关导航桥", self.parts.nav.close)
                await _step("关设备桥(连带 HAL 链路)", self.parts.device.close)
            await _step("关 HAL 链路", self.hal.close)      # 幂等;设备桥那步炸了也要关到
            self._hal_touched = False
            log.info("HAL 已收尾:停、放控制权、关链路")
        if self._started and self.transport.connected:
            # LWT 只在异常断线时由 broker 代发;正常退出自己发一条,站点立刻知道。
            async def _offline() -> None:
                await self.transport.publish(self.topics.status, _dumps(offline_status(
                    boot_id=self.boot_id, control_epoch=self.processor.control_epoch
                ).to_wire()), qos=1, retain=True)
            await _step("发 offline status", _offline)
        await _step("关 transport", self.transport.close)
        self._started = False

    def _on_connection(self, up: bool) -> None:
        self.online = up
        if up:
            self._reconnect_pending = True
        else:
            self._went_offline = True
            log.warning("与站点断线;当前任务按断线策略表处理")

    async def _flush_reconnect(self) -> None:
        """重连后的第一批:reconcile → 未确认事件 → status。首次连接也走一遍(reconcile 里
        任务为空、区间 0..0,站点由此知道这是干净起步)。"""
        self.online = True
        self._reconnect_pending = False
        # 断线与重连发生在同一拍之间(真机上网络抖动是常态):既然已经连上,断线策略就不
        # 该再执行 —— 不然已在线的任务被停住且没人再解。
        self._went_offline = False
        lo, hi = self.events.unacked_range()
        rc = Reconcile(boot_id=self.boot_id, control_epoch=self.processor.control_epoch,
                       task=self.processor.task_summary(), unacked_from_seq=lo, unacked_to_seq=hi)
        await self.transport.publish(self.topics.reconcile, _dumps(rc.to_wire()), qos=1)
        await self._flush_events()
        if self._started:
            if self.processor.current is not None:
                await self.processor.current.on_online()
            await self._publish_status(force=True)

    # ------------------------------------------------------------ 命令

    async def _on_rtcm(self, m: Message) -> None:
        """站点转来的基站改正数据:交给 RTK 来源(自己的写进模组;厂家的不用)。"""
        try:
            self.rtk.feed(bytes(m.payload))
        except Exception:                             # 一批写不进去就丢了(过时的改正没用)
            log.warning("改正数据交给 RTK 没成", exc_info=True)

    def _load_geo(self, ref: Any) -> Any:
        """这张图的地理配准(``geo.json``,可选);没有、坏了是 None(坏了记一笔)。"""
        if self.maps is None:
            return None
        from d1max_contract.geo import GEO_FILE, GeoRef
        p = Path(self.maps.dir_of(ref)) / GEO_FILE
        try:
            return GeoRef.from_wire(json.loads(p.read_text("utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("这张图的 geo.json 读不了,不拿 RTK 核对定位器:%s", exc)
            return None

    async def _check_rtk(self) -> None:
        """RTK 核对定位器(W09e 决定 8):只核配了定位器的狗;不对就标不可信、请它在 RTK 的位置附近
        重定位。"""
        if self.rtk is None or self.parts is None or self.loaded_map is None:
            return
        anchor = self.parts.nav.anchor
        if not hasattr(anchor, "rtk_disagree"):
            return
        try:
            v = await self._rtk_verdict(anchor)
        except Exception:                             # 核对出错不许挡住这一拍后面的处理与发状态
            log.exception("RTK 核对出错,这一拍跳过")
            return
        for ev in v[:-1]:                             # 最后一个是这一拍的结论,前面的是事件
            self.events.emit(ev.event, (ev.data or {}) | {"map_id": self.loaded_map[0],
                                                           "map_version": self.loaded_map[1]})
        v = v[-1] if v else None
        if v is None:
            return
        if v.reason != anchor.rtk_disagree:
            log.warning("RTK 核对:%s", v.reason or "对回来了")
        anchor.rtk_disagree = v.reason
        if v.reloc is not None and not self._running(self._rtk_reloc):
            from d1max_agent.rtk_check import RELOC_SIGMA_M
            m, pose = self.loaded_map, v.reloc

            async def _reloc() -> None:
                why = await anchor.relocalize(m, pose, RELOC_SIGMA_M, human=False)
                if why:
                    log.warning("按 RTK 请定位器重定位没成:%s", why)
            self._rtk_reloc = asyncio.get_running_loop().create_task(_reloc())

    async def _rtk_verdict(self, anchor: Any) -> list[Any]:
        """这一拍的结论(最后一个)与要发的事件(带 ``event`` 的那些)。解太旧(跟此刻的位姿比会差出
        v·Δt)、定位器正在重定位 / 等稳定时不比(W09e 内审小)。"""
        from d1max_agent.rtk_check import Verdict
        out: list[Any] = []
        base = getattr(self.rtk, "base_ecef", lambda: None)()
        moved = self._rtk_check.on_base(base)
        if moved is not None:
            out.append(moved)
        fix = self.rtk.latest()
        if fix is not None and self._now() - int(fix.get("stamp_ms", 0)) > RTK_FIX_MAX_AGE_MS:
            fix = None
        est = None
        if not getattr(anchor, "busy", False):
            o = await self.hal.odometry()
            e = anchor.estimate((o.x, o.y, o.yaw)) if o.valid else None
            est = None if e is None else (e.x, e.y, e.yaw)
        v = self._rtk_check.step(fix, est, self._mono())
        if v.event:
            out.append(v)
        out.append(Verdict(v.reason, v.reloc, v.gap_m))
        return out

    def _log_rtk(self) -> None:
        """录包时把 RTK 解按收到的时刻记进包目录 ``rtk.jsonl``(W09e 决定 5:建图脚本拿来配经纬度)。
        只记浮点、固定解;同一条不重记。"""
        if self.rtk is None or self.mapper is None or not self.mapper.recording:
            return
        fix = self.rtk.latest()
        if fix is None or fix.get("stale") or fix.get("fix") not in ("fixed", "float") \
                or fix.get("stamp_ms") == self._rtk_logged:
            return
        self._rtk_logged = fix.get("stamp_ms")
        bag = Path(self.mapper.bags_root) / str(self.mapper.last_bag)
        rec = {"t": fix["stamp_ms"] / 1000.0} | {k: fix.get(k) for k in (
            "fix", "lat", "lon", "alt", "std_h_m", "sats")}
        base = getattr(self.rtk, "base_ecef", lambda: None)()
        if base is not None:
            rec["base_ecef"] = [round(v, 4) for v in base]   # 建图时的基站坐标(内审应修 3)
        try:
            with open(bag / "rtk.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            log.warning("记 RTK 解没成:%s", exc)

    async def _on_teleop(self, m: Message) -> None:
        """一帧遥控。不合契约的丢掉、计数;合契约的交给当前那一趟遥控去判(代次、序号、在途)。"""
        try:
            frame = TeleopFrame.from_wire(json.loads(m.payload))
        except (ValueError, UnicodeDecodeError, ContractError):
            self.teleop_malformed += 1
            return
        self.processor.on_teleop_frame(frame, rx_ms=self._mono_ms())

    def _mono_ms(self) -> int:
        return int(self._mono() * 1000)

    async def _on_cmd(self, m: Message) -> None:
        try:
            wire = json.loads(m.payload)
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("cmd 报文不是 JSON,丢弃: %s", exc)
            return
        if isinstance(wire, dict) and wire.get("kind") == "halt":
            await self._halt_now(wire, m.topic)
        cid = wire.get("command_id") if isinstance(wire, dict) else None
        read = isinstance(wire, dict) and wire.get("kind") in READ_KINDS
        async with self._cmd_lock:
            try:
                if self._reconnect_pending and self.transport.connected:
                    # 真 broker 下离线补投的 cmd 可能比我们的重连收尾先到:reconcile 必须是第一条。
                    await self._flush_reconnect()
                ack = await self.processor.handle(wire, m.topic)
            finally:
                # 这条叫停排队那一路处理完了(收下、过期、重复都算):撤掉抢先那一路立的栅栏。
                if isinstance(cid, str):
                    self.processor.unfence(cid)
            if not read:
                await self.transport.publish(self.topics.ack, _ack_bytes(ack), qos=1)
                await self._publish_status()
        if read:
            # 只读查询(日志尾巴、录包轨迹)的回执可能上百 KB,发送要等 broker 收完整包 ——
            # 弱网上好几秒。
            # 放在锁外发(W00c6g 内审应修 1):不然这几秒里遥控续租、监护心跳都排在锁后面,租约会过期。
            # 它不改狗的状态,跟别的回执换个先后无害。
            await self.transport.publish(self.topics.ack, _ack_bytes(ack), qos=1)

    async def _halt_now(self, wire: dict, topic: str) -> None:
        """halt 不排队(W00c5c 内部评审):前面的命令在等回执的 PUBACK(上行拥堵时好几秒),halt 不能
        跟着等。主题是自己的 ``cmd``、报文成形 → 先让 HAL 停;中止任务、回执照旧排队去做。
        **不判过期**(W09d 外审):排队那一路已经不判叫停过期(``NEVER_EXPIRE``);这里还按狗的墙钟判
        的话,狗的钟快过有效期时站点刚发的叫停不抢先、只能排队。重投的旧叫停最多让狗停一下(下面只停车、
        不中止、不立栅栏),方向是安全的。"""
        if topic != self.topics.cmd:
            return
        try:
            cmd = Command.from_wire(wire)
        except ContractError:
            return
        p = self.processor
        # 重投的(回执会是 duplicate)、旧代次的(回执会是 stale_epoch)只停车,不中止任务、不立栅栏
        # (W00c6a 内审 S2)—— 停车本身无条件,那是偏安全的方向。
        fresh = p.idem.lookup(cmd.command_id) is None and cmd.control_epoch >= p.control_epoch
        if fresh:
            # **先立栅栏、清排队、中止当前的,再停车**(W00c6a 内审 B1、B2):只停车的话,活着的引擎
            # 把导航的 Cancelled 当「这个点没到」重发这个点(真 HAL 停车要等回执);只中止当前的话,
            # 排队里已收下的、排在锁前面的,照样起跑。栅栏等排队那一路处理完这条叫停才撤。
            p.fence(cmd.command_id)
            try:
                await p.abort_for_halt()
            except Exception:
                log.exception("halt 抢先中止任务失败,排队那一步还会再中止一次")
        try:
            await self._stop_motion()
        except Exception:
            log.exception("halt 抢先停车失败,排队那一步还会再停一次")

    # ------------------------------------------------------------ 监护(W00c6i)

    def _supervise(self, cmd: Command) -> str:
        """监护心跳:续(收到时刻 + ``ttl_ms``,单调钟)或放。**按会话**记:多人各自续、各自放;同一会话
        里序号不比见过的大的一律不认(断线补投的、手机到站点这一段迟到的旧心跳)。"""
        try:
            sup = parse_supervise(cmd.payload)
        except ContractError as exc:
            return f"payload: {exc}"
        now = self._mono()
        self._sessions = {k: v for k, v in self._sessions.items() if v[3] > now}
        prev = self._sessions.get(sup.session)
        if prev is not None and sup.seq <= prev[1]:
            return "stale_seq"
        # 放了的、过期的会话留 ``_SESSION_MEMORY_S``,挡同一会话里迟到的旧心跳
        # (站点的命令有效期 30 s)。
        if sup.action == "renew":
            if not self._supervised_now():
                log.info("有人现场监护了:%s", sup.operator or sup.session)
            until = now + sup.ttl_ms / 1000
            self._sessions[sup.session] = (sup.operator, sup.seq, until, until + _SESSION_MEMORY_S)
        else:
            log.info("监护放了:%s", sup.operator or sup.session)
            self._sessions[sup.session] = (sup.operator, sup.seq, 0.0, now + _SESSION_MEMORY_S)
        return ""

    def _supervised_now(self) -> bool:
        now = self._mono()
        return any(v[2] > now for v in self._sessions.values())

    async def _enforce_supervision(self) -> None:
        """``supervised`` 级别下没人监护了:当场中止 goto/巡检(排队的一起清掉),**再停车**
        (W00c6i 内审:顺序同叫停 —— 中止请求先进引擎队列;停车不依赖引擎,引擎卡住也停得住)。"""
        if self.autonomy != "supervised" or self._supervised_now():
            self._lapse_stopped = False
            return
        cur = self.processor.current
        moving = cur is not None and not cur.done and cur.kind in _AUTONOMOUS_KINDS
        if await self.processor.abort_kinds(_AUTONOMOUS_KINDS, "supervision_lost"):
            log.warning("监护过期或放了,中止 goto/巡检")
        if moving and not self._lapse_stopped:
            self._lapse_stopped = True
            try:
                await self._stop_motion()
            except Exception:
                log.exception("监护过期时停车失败(任务照样中止)")

    def _head_key(self) -> tuple[str, bool]:
        """(头尾方向, 不许自己走)。"""
        nav = self.parts.nav if self.parts is not None else None
        head = getattr(nav, "head", "unknown")
        block = getattr(nav, "head_block", None)
        return head, bool(block()) if callable(block) else head != "head"

    async def _enforce_head(self) -> None:
        """头尾方向(W11a、W09i):变了发事件、重发能力,**当场中止 goto/巡检、
        停车**(有人拿遥控器调了头:
        人在干预,行进方向也变了);能不能自己走变了也重发能力,变成不能就同样中止。不能自己走:头尾
        不知道,或者狗尾为前但后雷达没标定、没配避障。遥控照常。"""
        head, blocked = key = self._head_key()
        if key == self._head_told:
            return
        prev, self._head_told = self._head_told, key
        changed = bool(prev) and prev[0] != head
        if changed:                               # 起来后第一次读到不算「变了」
            self.events.emit("head_changed", {"head": head, "previous": prev[0]})
        if self.transport.connected:
            await self._publish_caps()
        if not blocked and not changed:
            return
        cur = self.processor.current
        moving = cur is not None and not cur.done and cur.kind in _AUTONOMOUS_KINDS
        if await self.processor.abort_kinds(_AUTONOMOUS_KINDS, "head_not_forward"):
            log.warning("头尾方向是 %s(%s):中止 goto/巡检", head,
                        "调了头" if changed else "不能自己走")
        if moving:
            try:
                await self._stop_motion()
            except Exception:
                log.exception("头尾调过来时停车失败(任务照样中止)")

    async def _stop_motion(self) -> None:
        """叫停(W00c6a):**先撤导航桥的目标**(桥进 Cancelled、从这一拍起不再发速度),再停 HAL。

        以前只停 HAL、指望引擎去停导航:引擎死了或卡住时,导航桥还在 ACTIVE,下一拍又发速度
        (仿真实测:叫停后 5 秒又走了 4 m,回执还是「收下」)。引擎活着时它会收到 Cancelled 和随后
        排进来的中止命令;重发下一个点之前要先等导航回待命(终态驻留),中止在那之前就处理掉了。
        停车本身失败照抛(站点回 502)。"""
        parts = self.parts
        nav_exc: Exception | None = None
        if parts is not None:
            try:
                await parts.nav.stop()
            except Exception as exc:  # noqa: BLE001 - 桥停不了也要接着停 HAL,最后再报
                nav_exc = exc
                log.warning("叫停时导航桥停不了(接着停 HAL): %s", exc)
        await self.hal.stop()
        if nav_exc is not None:
            raise nav_exc

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        if self._reconnect_pending and self.transport.connected:
            await self._flush_reconnect()
        if self._went_offline:
            self._went_offline = False
            await self._apply_offline_policy()
        await self._enforce_supervision()
        if self._locsrv is not None:
            await self._locsrv.tick()            # 本机定位桥:心跳、定位器没声就当它断了
        if self._obs_srv is not None:
            self._obs_srv.tick()                 # 本机障碍桥:感知节点没声就当它断了
            st = self.obs_view.state()
            # 只在「能用 / 不能用 / 原因」变了时重发(ok 与 stale 之间来回翻不发,内审小)
            st = "ok" if st == "stale" else st
            if st != self._obs_state_told:
                self._obs_state_told = st
                if self.transport.connected:
                    await self._publish_caps()   # 避障能不能用变了:站点、手机要知道
        if self.parts is not None:
            await self.parts.step(dt_s)          # 两个桥:导航状态机 + 设备事件
            await self._enforce_head()
        await self._feed_trail()
        try:
            self._log_rtk()
        except Exception:                             # 记不下 RTK 解不许挡住这一拍
            log.exception("记 RTK 解出错")
        await self._check_rtk()
        await self.processor.step(dt_s)
        if self._zones_pending is not None and self._tasks_idle():
            await self._apply_zones(self._zones_pending)
        if self.video is not None:
            self.video.step()
        await self._watch_faults()
        await self._flush_events()
        await self._publish_status()
        await self._maybe_telemetry()

    async def _feed_trail(self) -> None:
        """录包时的轨迹(W00c6h):录包开始那一拍清空,录着的每拍喂一帧里程(读不到、不新鲜不喂)。"""
        rec = self.mapper is not None and bool(self.mapper.recording)
        if rec and not self._trail_on:
            self.trail.reset()
        self._trail_on = rec
        if not rec:
            return
        try:
            o = await self.hal.odometry()
        except Exception:  # noqa: BLE001 - 这一拍读不到:不记,下一拍再说
            return
        if o.valid:
            self.trail.feed(o.x, o.y, o.yaw)

    async def _watch_faults(self) -> None:
        """HAL 故障集合变了就发一条 ``robot_fault``(W00c5a)。狗只报事实:哪条算跌倒、算不算
        P1,是站点的判定。故障帧是**状态**不是事件,厂商会一直重推同一组 —— 所以只在变的那一拍发。
        事件走事件簿:断线期间留在狗上,重连补投、站点确认即清。"""
        try:
            now = tuple(await self.hal.faults())
        except HalUnsupported:
            return
        except Exception as exc:
            # 同一个毛病只在变的那一拍记一条(带栈):每拍一条会把日志刷满。
            why = f"{type(exc).__name__}: {exc}"
            if why != self._faults_error:
                self._faults_error = why
                log.exception("读 HAL 故障失败,这一拍不判")
            return
        self._faults_error = ""
        if now != self._last_faults:
            self._last_faults = now
            self.events.emit("robot_fault", fault_event_data(now))

    async def _apply_offline_policy(self) -> None:
        cur = self.processor.current
        if cur is None:
            return
        policy = policy_for(cur.kind)
        if policy.on_disconnect == "continue_if_safe":
            h = await self.hal.health()
            b = await self.hal.battery()
            safe = (not h.estop) and bool(await self._loc_ok(h)) and h.control \
                and b.percent > BATTERY_FLOOR_PCT
            log.info("断线,当前任务 %s 按 continue_if_safe:%s", cur.task_id,
                     "继续" if safe else "停住等待")
            await cur.on_offline(safe)
        elif policy.on_disconnect == "stop_and_wait":
            await cur.on_offline(False)
        # execute_locally:什么都不做

    async def _loc_ok(self, h) -> bool | None:
        """定位可不可信(W00c6e):看锚定;没有引擎零件(没加载地图)就是 ``None``(按运控的里程新鲜)。"""
        if self.parts is None:
            return None
        o = await self.hal.odometry()
        return self.parts.nav.anchor.ok(h.loc_quality > 0.0 and o.valid)

    async def _flush_events(self) -> None:
        if not self.transport.connected:
            return
        for ev in self.events.pending():
            await self.transport.publish(self.topics.event, _dumps(ev.to_wire()), qos=1)
            self.events.mark_acked(ev.seq)

    async def _publish_status(self, *, force: bool = False) -> None:
        if not self.transport.connected:
            return
        h = await self.hal.health()
        motion = await self.hal.motion_status()
        st = compose_status(online=True, boot_id=self.boot_id,
                            ready=compose_ready(h, motion, loc_ok=await self._loc_ok(h)),
                            control_epoch=self.processor.control_epoch, now_ms=self._now(),
                            task=self.processor.task_summary())
        wire = st.to_wire()
        key = {k: v for k, v in wire.items() if k != "last_seen"}
        now = self._now()
        due = self._next_status_ms is not None and now >= self._next_status_ms
        if not force and not due and key == self._last_status_wire:
            return
        self._last_status_wire = key
        self._next_status_ms = now + self.status_period_ms
        await self.transport.publish(self.topics.status, _dumps(wire), qos=1, retain=True)

    async def _maybe_telemetry(self) -> None:
        now = self._now()
        if self._next_telemetry_ms is None:
            self._next_telemetry_ms = now + self._telemetry_period
            return
        if now < self._next_telemetry_ms or not self.transport.connected:
            return
        self._next_telemetry_ms = now + self._telemetry_period
        cur = self.processor.current
        storage = None
        if self._storage is not None and now >= self._next_storage_ms:
            storage = self._storage()
            if storage is not None:
                self._next_storage_ms = now + STORAGE_EVERY_MS
        tele = compose_telemetry(
            now_ms=now, odom=await self.hal.odometry(), battery=await self.hal.battery(),
            health=await self.hal.health(), loaded_map=self.loaded_map,
            task_state=cur.state if cur is not None else None, online=self.online,
            storage=storage, anchor=self.parts.nav.anchor if self.parts is not None else None)
        if self.rtk is not None:
            tele = dataclasses.replace(tele, rtk=self.rtk.latest())
        await self.transport.publish(self.topics.telemetry, _dumps(tele.to_wire()), qos=0)
