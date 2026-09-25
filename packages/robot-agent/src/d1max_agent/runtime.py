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
import uuid
from collections.abc import Callable
from pathlib import Path

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
from d1max_contract.hal import RobotHAL
from d1max_contract.messages import Command, MapPose, Reconcile
from d1max_contract.policy import policy_for
from d1max_contract.registration import Registration
from d1max_contract.topics import TopicAcl
from d1max_contract.transport import Message, Transport
from d1max_patrol.protocol.nav_types import Pose

log = logging.getLogger(__name__)

#: 电量低于这条线,断线时按「不安全」处理(停住等待)。
BATTERY_FLOOR_PCT = 15.0


def _dumps(d: dict) -> bytes:
    return json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()


class AgentRuntime:
    def __init__(self, *, transport: Transport, registration: Registration, hal: RobotHAL,
                 store_dir: Path, now_ms: Callable[[], int], loaded_map: tuple[str, str] | None,
                 boot_id: str | None = None, telemetry_period_ms: int = 1000,
                 status_period_ms: int = 30_000, parts: EngineParts | None = None,
                 home: Pose | None = None, runs_root: Path | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
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
            import time
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
        self._closed = False
        #: 命令按到达顺序串行处理:QoS 1 的重复可能几乎同时到,处理里一旦有 await,两条都会
        #: 先通过幂等查询。
        self._cmd_lock = asyncio.Lock()

    # ------------------------------------------------------------ 合成

    def _supported(self) -> set[str]:
        caps = self.hal.hal_capabilities()
        return ({"goto", "patrol"} if (self.loaded_map is not None and caps.max_vx > 0)
                else set())

    def _make_task(self, cmd: Command) -> Task:
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
        await self.transport.connect()          # 首次连接:after_connect 会走 _flush_reconnect
        await self.transport.publish(self.topics.capabilities, _dumps(compose_capabilities(
            robot_id=self.registration.robot_id, hal_caps=self.hal.hal_capabilities(),
            adapter_id=self.adapter_id, loaded_map=self.loaded_map).to_wire()),
            qos=1, retain=True)
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

    async def _on_cmd(self, m: Message) -> None:
        try:
            wire = json.loads(m.payload)
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("cmd 报文不是 JSON,丢弃: %s", exc)
            return
        async with self._cmd_lock:
            if self._reconnect_pending and self.transport.connected:
                # 真 broker 下离线补投的 cmd 可能比我们的重连收尾先到:reconcile 必须是第一条。
                await self._flush_reconnect()
            ack = await self.processor.handle(wire, m.topic)
            await self.transport.publish(self.topics.ack, _dumps(ack.to_wire()), qos=1)
            await self._publish_status()

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        if self._reconnect_pending and self.transport.connected:
            await self._flush_reconnect()
        if self._went_offline:
            self._went_offline = False
            await self._apply_offline_policy()
        if self.parts is not None:
            await self.parts.step(dt_s)          # 两个桥:导航状态机 + 设备事件
        await self.processor.step(dt_s)
        await self._flush_events()
        await self._publish_status()
        await self._maybe_telemetry()

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
        tele = compose_telemetry(
            now_ms=now, odom=await self.hal.odometry(), battery=await self.hal.battery(),
            health=await self.hal.health(), loaded_map=self.loaded_map,
            task_state=cur.state if cur is not None else None, online=self.online)
        await self.transport.publish(self.topics.telemetry, _dumps(tele.to_wire()), qos=0)
