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
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_agent.assembly import EngineParts, build_engine
from d1max_agent.commands import CommandProcessor
from d1max_agent.events import EventBook
from d1max_agent.idempotency import IdempotencyStore
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
from d1max_contract.errors import ContractError
from d1max_contract.hal import Fault, HalUnsupported, RobotHAL
from d1max_contract.messages import Command, MapPose, Reconcile, fault_event_data
from d1max_contract.policy import policy_for
from d1max_contract.registration import Registration
from d1max_contract.storage import StorageFacts
from d1max_contract.supervision import parse_supervise
from d1max_contract.teleop import TeleopFrame
from d1max_contract.topics import TopicAcl
from d1max_contract.transport import Message, Transport
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

#: 切版本、退版本要的电量(%)。切过去起不来,开机守卫还要再退、再起一次。
RELEASE_MIN_BATTERY_PCT = 30.0

#: 电量低于这条线,断线时按「不安全」处理(停住等待)。
BATTERY_FLOOR_PCT = 15.0

#: 要人监护(W00c6i)时,只有这几种任务受监护租约约束(遥控、叫停、换图、发布照常)。
_AUTONOMOUS_KINDS = frozenset({"goto", "patrol"})


def _dumps(d: dict) -> bytes:
    return json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()


class AgentRuntime:
    def __init__(self, *, transport: Transport, registration: Registration, hal: RobotHAL,
                 store_dir: Path, now_ms: Callable[[], int], loaded_map: tuple[str, str] | None,
                 boot_id: str | None = None, telemetry_period_ms: int = 1000,
                 status_period_ms: int = 30_000, parts: EngineParts | None = None,
                 home: Pose | None = None, runs_root: Path | None = None,
                 monotonic: Callable[[], float] | None = None, video: Any = None,
                 storage_facts: Callable[[], StorageFacts | None] | None = None,
                 maps: Any = None, mapper: Any = None, releases: Any = None,
                 autonomy: str = "autonomous") -> None:
        self.registration = registration
        self.topics = registration.topics
        self.hal = hal
        self.boot_id = boot_id or f"boot-{uuid.uuid4().hex[:10]}"
        self._now = now_ms
        self.loaded_map = loaded_map
        self._telemetry_period = telemetry_period_ms
        #: 空闲时也要周期刷 status 的 last_seen —— 派遣条件要「last_seen 新鲜」(总设计 §3.1)。
        self.status_period_ms = status_period_ms
        self._next_status_ms: int | None = None
        store_dir = Path(store_dir)
        # W00b:goto 跑在 MissionEngine 上。parts 不给就按 loaded_map/home 现装一组。
        if parts is None and loaded_map is not None:
            parts = build_engine(hal, runs_root=runs_root or (store_dir / "runs"), now_ms=now_ms,
                                 monotonic=monotonic or time.monotonic, map_id=loaded_map[0],
                                 home=home)
        self.parts = parts
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
        if autonomy not in ("supervised", "autonomous"):
            raise ValueError(f"autonomy 要是 supervised 或 autonomous,给的是 {autonomy!r}")
        self.autonomy = autonomy
        #: 监护到什么时候(单调钟,秒);``None`` = 没人监护。
        self._supervised_until: float | None = None
        self._supervisor = ""
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
        #: 各用各的后台槽:换图、录包、重建、发布。``_switching``:正在载入、切坐标系。
        self._map_job: asyncio.Task | None = None
        self._rec_job: asyncio.Task | None = None
        self._build_job: asyncio.Task | None = None
        self._release_job: asyncio.Task | None = None
        self._switching = False
        #: 切版本、退版本收下了、马上要重启:什么都不再接(W00c5d 第三部分内部评审:
        #: 这几秒里收下的任务会被重启掐掉)。切不成就放开。
        self._restarting = False
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
        if cmd.kind != "patrol" or self._storage is None:
            return ""
        f = self._storage()
        return "storage_full" if f is not None and f.full() else ""

    # ------------------------------------------------------------ 地图(W00c5d 第二部分)

    async def _load_active_map(self) -> None:
        """起来时(连站点之前):狗上有站点下发过的正在用的那张图,交给适配器载入,就用它(覆盖
        ``--map``)。载不进去就照旧用 ``--map``,并发一条 ``map_load_failed`` 让站点知道。"""
        ref = self.maps.active() if self.maps is not None else None
        loader = getattr(self.hal, "load_map", None)
        if ref is None or loader is None:
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
        if self.maps is not None and getattr(self.hal, "load_map", None) is not None:
            out["map_activate"] = {}
        if self.mapper is not None:
            out["mapping"] = {}
            out["map_build"] = {}
        if self.releases is not None:
            # 站点据此显示每台狗在跑哪一版。
            out["release_install"] = {"current": self.releases.current()}
            out["release_activate"] = {}
            out["release_rollback"] = {}
        if self._storage is not None:
            out["outbox_retry"] = {}
        return out

    async def _map_command(self, cmd: Command) -> str:
        """地图、发布、发件箱命令:收下(空串)或拒绝原因。下载、载入、录包、重建都在后台做,做完发事件。
        **各用各的后台槽**(W00c5d 第二部分内部评审):重建一小时、下载十几分钟都不许挡出警;
        只有换图(载入、切坐标系)那一小段不接自动任务。"""
        from d1max_contract.maps import MapRef, parse_map_build, parse_mapping
        kind = cmd.kind
        if kind not in self._extra_tasks():
            return "unsupported"
        if self._restarting:
            return "restarting"
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
                f = self._storage() if self._storage is not None else None
                if f is not None and f.full():
                    return "storage_full"         # 录包很大:发件箱满了不录
            elif not self.mapper.recording:
                return "not_recording"
            self._rec_job = loop.create_task(self._record(action, name, cmd.task_id))
            return ""
        if self._running(self._build_job) or self._running(self._rec_job) \
                or self.mapper.recording:
            return "busy"
        if not self._tasks_idle():
            return "busy"                         # 重建吃 CPU:开跑时不跟巡检抢(开跑后不挡出警)
        self._build_job = loop.create_task(self._build(bag, map_id, version, cmd.task_id))
        return ""

    async def _record(self, action: str, name: str, task_id: str) -> None:
        """录包的开始、停止(在后台做:起、停 ros2 bag 要十几秒,不能在命令锁里等)。"""
        try:
            if action == "start":
                await self.mapper.start(name)
            else:
                await self.mapper.stop()
            self.events.emit("mapping", {"task_id": task_id, "action": action,
                                         "name": self.mapper.last_bag})
        except Exception as exc:  # noqa: BLE001 - 起不来/停不了:原因发给站点
            log.warning("录包 %s 没成:%s", action, exc)
            self.events.emit("mapping_failed", {"task_id": task_id, "action": action,
                                                "reason": f"{type(exc).__name__}: {exc}"[:200]})

    async def _release_command(self, cmd: Command) -> str:
        """发布命令(W00c5d 第三部分):装在后台做,双槽所在的盘不够不装;切、退要空闲(不跑任务、
        不在换图、录包、重建)、电量不低于 ``RELEASE_MIN_BATTERY_PCT``。收下切、退之后到重启之前
        什么都不再接(``_restarting``)。"""
        from d1max_contract.releases import ReleaseRef, check_release_name
        try:
            if cmd.kind == "release_install":
                ref = ReleaseRef.from_wire(cmd.payload)
            elif cmd.kind == "release_activate":
                name = check_release_name(cmd.payload.get("name"))
        except ContractError as exc:
            return f"payload: {exc}"
        if self._running(self._release_job) or self._running(self._map_job):
            return "busy"
        if cmd.kind == "release_install":
            if not self.releases.disk_ok(ref.size):
                return "storage_full"             # 下载、解开、落槽、建 venv 都在系统盘上
            job = self._release_install(ref, cmd.task_id)
            self._release_job = asyncio.get_running_loop().create_task(job)
            return ""
        if not self._tasks_idle():
            return "busy"                         # 重启那几秒里谁都停不了它:跑着任务不切、不退
        if self._running(self._build_job) or self._running(self._rec_job) or \
                (self.mapper is not None and self.mapper.recording):
            return "busy"                         # 重启会悄悄掐掉录包、重建
        try:
            battery = (await self.hal.battery()).percent
        except Exception:  # noqa: BLE001 - 读不到电量(适配器还没收到第一帧):不切
            return "battery_unknown"
        if battery < RELEASE_MIN_BATTERY_PCT:
            return "low_battery"                  # 切过去起不来还要再退、再起一次
        if not self.releases.disk_ok(0):
            return "storage_full"                 # 在途标记、幂等记录、事件簿都要写得进去
        if cmd.kind == "release_activate":
            if name == self.releases.current():
                return "already_running"
            if not self.releases.ready(name):
                return "not_installed"
            if not self.releases.can_switch_to(name):
                return "no_agent_start"           # 切过去代理起不来(老服务那一代、启动脚本坏了)
            job = self._release_switch("activate", name, cmd.task_id)
        else:
            if not self.releases.can_roll_back():
                return "nothing_to_roll_back"
            job = self._release_switch("rollback", "", cmd.task_id)
        self._restarting = True                   # 跟上面的空闲检查之间没有 await:不会夹进任务
        self._release_job = asyncio.get_running_loop().create_task(job)
        return ""

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
        old = self.maps.active()
        try:
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
                    await self.hal.load_map(ref.map_id, ref.version, dst)
                except Exception as exc:
                    self.maps.discard(ref)
                    raise MapInstallError(f"适配器载不进去: {type(exc).__name__}: {exc}") from exc
                try:
                    self.maps.commit(ref, home=home)
                except Exception as exc:
                    # 适配器已经是新图了,狗报的还是老版本:把原来那张载回去,不留这种两边不一致。
                    if old is not None:
                        try:
                            await self.hal.load_map(old.map_id, old.version, self.maps.dir_of(old))
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

    async def _build(self, bag: str, map_id: str, version: str, task_id: str) -> None:
        base = {"task_id": task_id, "bag": bag, "map_id": map_id, "version": version}
        try:
            await self.mapper.build(bag, map_id, version)
            self.events.emit("map_built", base)
        except Exception as exc:  # noqa: BLE001 - 重建失败:原因发给站点
            log.warning("重建没成(%s → %s:%s):%s", bag, map_id, version, exc)
            self.events.emit("map_build_failed",
                             base | {"reason": f"{type(exc).__name__}: {exc}"[:200]})

    def _switch_map(self, ref) -> None:
        self.loaded_map = (ref.map_id, ref.version)
        self.processor.loaded_map = self.loaded_map
        self.processor.supported = self._supported()
        if self.parts is not None:
            home = self.maps.home_of(ref) if self.maps is not None else None
            self.parts.switch_map(ref.map_id, None if home is None else Pose.from_xy_yaw(*home),
                                  now_ms=self._now())

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
        # 先载正在用的图,再连站点:连上之后进来的命令要按真的地图版本核对。
        await self._load_active_map()
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
        await self._publish_status(force=True)

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
        if self.video is not None:
            async def _video_off() -> None:
                self.video.close()
            await _step("停推流", _video_off)
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
        async with self._cmd_lock:
            if self._reconnect_pending and self.transport.connected:
                # 真 broker 下离线补投的 cmd 可能比我们的重连收尾先到:reconcile 必须是第一条。
                await self._flush_reconnect()
            ack = await self.processor.handle(wire, m.topic)
            await self.transport.publish(self.topics.ack, _dumps(ack.to_wire()), qos=1)
            await self._publish_status()

    async def _halt_now(self, wire: dict, topic: str) -> None:
        """halt 不排队(W00c5c 内部评审):前面的命令在等回执的 PUBACK(上行拥堵时好几秒),halt 不能
        跟着等。主题是自己的 ``cmd``、报文成形、没过期 → 先让 HAL 停;中止任务、回执照旧排队去做。
        过期的不抢先:那是重连补投的旧命令,停一下会打断重连后正常在跑的任务。"""
        if topic != self.topics.cmd:
            return
        try:
            cmd = Command.from_wire(wire)
        except ContractError:
            return
        if cmd.expires_at <= self._now():
            return
        try:
            await self._stop_motion()
        except Exception:
            log.exception("halt 抢先停车失败,排队那一步还会再停一次")
        # 当场请求中止当前任务(W00c6a):只停车的话,活着的引擎把导航的 Cancelled 当「这个点没到」,
        # 0.5 s 后重发这个点,狗又走起来 —— 直到排队的那一步轮到(上行拥堵时好几秒)。中止请求进了
        # 引擎的队列,重发之前就被处理掉。排队那一步照旧:清待办、中止、回执;这里重复的中止无害
        # (任务收尾后不再理;引擎开新的一趟会清空队列)。
        cur = self.processor.current
        if cur is not None and not cur.done:
            try:
                await cur.abort("halt")
            except Exception:
                log.exception("halt 抢先中止任务失败,排队那一步还会再中止一次")

    # ------------------------------------------------------------ 监护(W00c6i)

    def _supervise(self, cmd: Command) -> str:
        """监护心跳:续(收到时刻 + ``ttl_ms``,单调钟)或放。命令本身过期的,处理器已经拒了。"""
        try:
            sup = parse_supervise(cmd.payload)
        except ContractError as exc:
            return f"payload: {exc}"
        if sup.action == "renew":
            if not self._supervised_now():
                log.info("有人现场监护了:%s", sup.operator or cmd.task_id)
            self._supervised_until = self._mono() + sup.ttl_ms / 1000
            self._supervisor = sup.operator
        else:
            if self._supervised_now():
                log.info("监护放了:%s", sup.operator or self._supervisor)
            self._supervised_until = None
        return ""

    def _supervised_now(self) -> bool:
        until = self._supervised_until
        return until is not None and self._mono() < until

    async def _enforce_supervision(self) -> None:
        """``supervised`` 级别下没人监护了:当场中止 goto/巡检(排队的一起清掉),走停车确认。"""
        if self.autonomy != "supervised" or self._supervised_now():
            return
        if await self.processor.abort_kinds(_AUTONOMOUS_KINDS, "supervision_lost"):
            log.warning("监护过期或放了,中止 goto/巡检")

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
        if self.parts is not None:
            await self.parts.step(dt_s)          # 两个桥:导航状态机 + 设备事件
        await self.processor.step(dt_s)
        if self.video is not None:
            self.video.step()
        await self._watch_faults()
        await self._flush_events()
        await self._publish_status()
        await self._maybe_telemetry()

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
            safe = (not h.estop) and h.loc_quality > 0.0 and h.control \
                and b.percent > BATTERY_FLOOR_PCT
            log.info("断线,当前任务 %s 按 continue_if_safe:%s", cur.task_id,
                     "继续" if safe else "停住等待")
            await cur.on_offline(safe)
        elif policy.on_disconnect == "stop_and_wait":
            await cur.on_offline(False)
        # execute_locally:什么都不做

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
        st = compose_status(online=True, boot_id=self.boot_id, ready=compose_ready(h, motion),
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
            storage=storage)
        await self.transport.publish(self.topics.telemetry, _dumps(tele.to_wire()), qos=0)
