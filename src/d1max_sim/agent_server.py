"""仿真运控旁路进程 —— ``motion/patrol_agent.cpp`` 的可执行契约。

跟 ``nav_server`` 同一条原则: 客户端行为有争议时,以这台仿真器的表现为准,
直到真机把它推翻。凡文档未明写、由本仿真器补齐的行为,一律加待真机验证标记。

**它刻意模拟的几件坏事**(都是现场真出过的,见 ``docs/真机待验证清单.md``):

* ``deny_control=True`` —— 上装占着 SDK 资源,连只读都进不去(#47)。
* ``drop_control_after`` —— 跑着跑着控制权被收走(#40/#46)。
* 站起要花时间,不是瞬间的(#36 实测 ~6s),所以状态是**慢慢**变的。
* ``forward`` 给太小就只是原地蹭,``motion`` 不会进 ``Gait``(#37)。

**跟 C++ 一样的并发语义**(协议 2): 一条连接上的普通命令排队、一次做一条;
``halt`` / ``estop`` **插队** —— 读到就当场做、当场回执,不排在还没走完的
walk 后面。叫停会让正在走的 walk 立刻收手、排着队的 walk 直接作废,两者都
回 ``WALK_CANCELLED``。

用纯 asyncio 的 TCP server,不依赖 websockets —— 旁路进程的线协议是裸
TCP + JSONL,跟导航那条 WebSocket 链路没有关系。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from typing import Any

from d1max_patrol.protocol.agent_frames import (
    PROTO_VERSION,
    AgentProtocolError,
    EmergencyStatus,
    MotionStatus,
    decode_command,
)

log = logging.getLogger(__name__)

#: 站起/趴下要多久才到位。真机实测约 6s(清单 #36),仿真里缩短到能测的量级,
#: 但**保持"不是瞬间"这个性质** —— 上层若假设站起是原子的,就该在这里挂掉。
STAND_SECONDS = 0.3

#: 遥测频率。真机上 ``OnRobotStateData`` 只在变化时来,``OnMcData``(里程)
#: 是 50Hz。仿真里都降到 20Hz,够测事件流,不至于把测试日志淹了。
TELEMETRY_HZ = 20.0

#: 低于这个量的 ``forward`` 就只是原地蹭,不会真的走(清单 #37)。
#: 假设(待真机验证): 真机上 0.11 "几乎不动"、0.3 "明显走",阈值取中间。
WALK_DEADBAND = 0.2

#: 被叫停的 walk 的拒绝原文。跟 ``patrol_agent.cpp`` 的 ``kWalkCancelled`` 一字不差。
WALK_CANCELLED = "行走被停车/急停打断"

#: 插队执行的命令。跟 ``patrol_agent.cpp`` 的 ``IsUrgent`` 对齐。
URGENT_COMMANDS = frozenset({"halt", "estop", "vel"})

#: ``vel``(协议 v3)的边界:同 ``motion/vel_gate.hpp`` 的 ``kMaxFraction`` 与 ``kTtl*``。
VEL_MAX = 0.5
VEL_TTL_MIN_MS = 50
VEL_TTL_MAX_MS = 1000
#: ``vel`` 在仿真里多久积分一次里程(旁路进程那头是 50 ms 发一次 ``Move``)。
_VEL_TICK_S = 0.05

#: walk 在仿真里睡的时候多久看一眼有没有被叫停。
_CANCEL_POLL_S = 0.01

_Job = tuple[int, str, dict[str, Any], int]


class SimAgentServer:
    """一台假的常驻 SDK 会话。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        held: bool = True,
        deny_control: bool = False,
        drop_control_after: float | None = None,
        battery: float = 71.0,
        sdk: str = "0.1.1",
        telemetry_hz: float = TELEMETRY_HZ,
    ) -> None:
        self._host = host
        self._port = port
        #: 旁路进程此刻是否握着控制权。
        self._held = held and not deny_control
        #: True 时连 ``hold`` 都拒绝 —— 模拟上装占住(#47)。
        self._deny_control = deny_control
        self._drop_after = drop_control_after
        self._sdk = sdk
        self._interval = 1.0 / telemetry_hz

        self.motion = MotionStatus.LIE_DOWN
        self.battery1 = battery
        self.battery2 = battery
        self.estop_software = EmergencyStatus.RECOVER
        self.estop_hardware = EmergencyStatus.RECOVER
        #: 世界系里程,单位米/弧度。
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.vyaw = 0.0
        #: 收到过的命令,按顺序。测试拿它断言"上层到底发了什么"。
        self.commands: list[tuple[str, dict[str, Any]]] = []
        #: 走过的总路程,给闭环实验的测试用。
        self.distance = 0.0
        #: 停车代数。``halt`` / ``estop(on)`` 加一,walk 记下开始排队时的代数,
        #: 代数变了就是被叫停了。同 ``patrol_agent.cpp`` 的 ``g_cancel_gen``。
        self._cancel_gen = 0
        #: 被叫停打断(或排队时就作废)的 walk 有几条。测试断言用。
        self.cancelled_walks = 0

        #: ``vel`` 的当前目标(比例值)与到期时刻(monotonic)。
        self._vel = (0.0, 0.0, 0.0)
        self._vel_until = 0.0
        self._vel_task: asyncio.Task[None] | None = None

        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._started_at = 0.0

    # -------------------------------------------------------------- 生命周期

    @property
    def port(self) -> int:
        """实际监听的端口。传 ``port=0`` 时由内核挑,起来之后从这里读。"""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("还没启动")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port)
        self._started_at = time.monotonic()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for writer in list(self._clients):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> SimAgentServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # -------------------------------------------------------------- 客户端

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            self._send(writer, {
                "t": "hello", "proto": PROTO_VERSION, "sdk": self._sdk,
                "held": self._held, "robot": "sim://d1max",
            })
            await writer.drain()
            telemetry = asyncio.create_task(self._telemetry_loop(writer))
            self._tasks.add(telemetry)
            queue: asyncio.Queue[_Job] = asyncio.Queue()
            worker = asyncio.create_task(self._worker(writer, queue))
            self._tasks.add(worker)
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    await self._on_line(writer, line, queue)
            finally:
                # 客户端走了:队里剩下的一律不做,正在走的那一拍收手 —— 跟 C++
                # 的 ``closing`` 一样。
                for task in (worker, telemetry):
                    task.cancel()
                    self._tasks.discard(task)
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _send(self, writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def _broadcast(self, obj: dict[str, Any]) -> None:
        for writer in list(self._clients):
            self._send(writer, obj)

    # -------------------------------------------------------------- 遥测

    async def _telemetry_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            if (self._drop_after is not None and self._held
                    and time.monotonic() - self._started_at >= self._drop_after):
                self._held = False
                self._send(writer, {"t": "control_lost", "reason": "上装收回控制权"})
            self._send(writer, {
                "t": "state",
                "motion": self.motion.value,
                "battery1": self.battery1,
                "battery2": self.battery2,
                "estop_sw": self.estop_software.value,
                "estop_hw": self.estop_hardware.value,
                "ts_ms": int(time.time() * 1000),
            })
            self._send(writer, {
                "t": "odom", "x": self.x, "y": self.y, "yaw": self.yaw,
                "vx": self.vx, "vy": self.vy, "vyaw": self.vyaw,
                "ts_ms": int(time.time() * 1000),
            })
            with contextlib.suppress(ConnectionError, OSError):
                await writer.drain()
            await asyncio.sleep(self._interval)

    # -------------------------------------------------------------- 命令

    async def _on_line(self, writer: asyncio.StreamWriter, line: bytes,
                       queue: asyncio.Queue[_Job]) -> None:
        try:
            cmd_id, cmd, args = decode_command(line)
        except AgentProtocolError as exc:
            # 解不动的行没有 id,回不了对号入座的 ack。真机那头也只能记日志,
            # 所以这里也只记日志 —— 客户端会自己等超时。
            log.warning("旁路仿真收到看不懂的命令行: %s", exc)
            return
        # 收到就记,不等轮到它才记 —— 测试要断言的是"上层发了什么"。
        self.commands.append((cmd, dict(args)))
        if cmd in URGENT_COMMANDS:
            await self._execute(writer, (cmd_id, cmd, dict(args), self._cancel_gen))
            return
        queue.put_nowait((cmd_id, cmd, dict(args), self._cancel_gen))

    async def _worker(self, writer: asyncio.StreamWriter,
                      queue: asyncio.Queue[_Job]) -> None:
        """一次做一条普通命令。跟 C++ 的 ``ConnWorker`` 一样。"""
        while True:
            await self._execute(writer, await queue.get())

    async def _execute(self, writer: asyncio.StreamWriter, job: _Job) -> None:
        cmd_id, cmd, args, gen = job
        try:
            await self._run(cmd, args, gen)
        except _Rejected as exc:
            self._send(writer, {"t": "ack", "id": cmd_id, "ok": False,
                                "error": str(exc)})
        else:
            self._send(writer, {"t": "ack", "id": cmd_id, "ok": True})
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()

    def _need_control(self, what: str) -> None:
        if not self._held:
            # 真机上这条错误的原文是 "Controlled denial of service"(#40/#47)。
            # 照抄它,现场看日志时能一眼对上。
            raise _Rejected(f"Controlled denial of service ({what})")

    async def _run(self, cmd: str, args: dict[str, Any], gen: int) -> None:
        if cmd == "hold":
            if self._deny_control:
                raise _Rejected("Controlled denial of service (上装占用中)")
            self._held = True
            return
        if cmd == "shutdown":
            self._held = False
            self._broadcast({"t": "control_lost", "reason": "旁路进程按要求退出"})
            return
        if cmd == "halt":
            # 停车不要控制权:作废正在走和排着队的 walk,速度归零。
            self._cancel_gen += 1
            self.vx = self.vy = self.vyaw = 0.0
            return
        if cmd == "estop":
            # 急停不需要控制权 —— 它是安全动作,任何时候都得能发出去。
            on = bool(args.get("on", True))
            if on:
                self._cancel_gen += 1
            self.estop_software = (EmergencyStatus.STOP if on
                                   else EmergencyStatus.RECOVER)
            if on:
                self.vx = self.vy = self.vyaw = 0.0
            return
        if cmd == "light":
            which = str(args.get("which", "both"))
            if which not in ("front", "back", "both"):
                raise _Rejected(f"未知的灯位: {which}")
            return
        if cmd == "stand":
            self._need_control("stand")
            await self._transition(MotionStatus.STAND_UP)
            return
        if cmd == "lie":
            self._need_control("lie")
            await self._transition(MotionStatus.LIE_DOWN)
            return
        if cmd == "head":
            self._need_control("head")
            return
        if cmd == "vel":
            self._start_vel(args)
            return
        if cmd == "walk":
            self._need_control("walk")
            if gen != self._cancel_gen:
                self.cancelled_walks += 1
                raise _Rejected(WALK_CANCELLED)   # 排队的时候已经被叫停了
            await self._walk(args, gen)
            return
        raise _Rejected(f"不认识的命令: {cmd}")

    def _vel_unsafe(self) -> bool:
        return (EmergencyStatus.STOP in (self.estop_software, self.estop_hardware)
                or self.motion in (MotionStatus.LIE_DOWN, MotionStatus.UNKNOWN,
                                   MotionStatus.LOCKED))

    def _start_vel(self, args: dict[str, Any]) -> None:
        """带有效期的持续速度:**立刻回执**;有效期内一直走,到期自停;新的覆盖旧的并续期;
        ``halt``/``estop`` 加停车代数,速度线程看见就停。"""
        self._need_control("vel")
        # 同 patrol_agent.cpp 的 DoVel:任一路急停、趴着/锁死/姿态未知都拒。
        if EmergencyStatus.STOP in (self.estop_software, self.estop_hardware):
            raise _Rejected("急停生效中,拒绝动作")
        if self.motion in (MotionStatus.LIE_DOWN, MotionStatus.UNKNOWN, MotionStatus.LOCKED):
            raise _Rejected("趴着/锁死/姿态未知,走不了,先 stand")
        try:
            fwd = float(args.get("fwd", 0.0))
            lat = float(args.get("lat", 0.0))
            yaw = float(args.get("yaw", 0.0))
            ttl = int(args.get("ttl_ms", 0))
        except (TypeError, ValueError) as exc:
            raise _Rejected(f"vel 参数不对: {exc}") from exc
        if not all(math.isfinite(v) and abs(v) <= VEL_MAX for v in (fwd, lat, yaw)):
            raise _Rejected(f"速度分量要在 ±{VEL_MAX} 内")
        if not VEL_TTL_MIN_MS <= ttl <= VEL_TTL_MAX_MS:
            raise _Rejected(f"ttl_ms 要在 [{VEL_TTL_MIN_MS}, {VEL_TTL_MAX_MS}] 内")
        self._vel = (fwd, lat, yaw)
        self._vel_until = time.monotonic() + ttl / 1000.0
        if self._vel_task is None or self._vel_task.done():
            self._vel_task = asyncio.get_running_loop().create_task(
                self._vel_loop(self._cancel_gen))
            self._tasks.add(self._vel_task)
            self._vel_task.add_done_callback(self._tasks.discard)

    async def _vel_loop(self, gen: int) -> None:
        try:
            while self._cancel_gen == gen and time.monotonic() < self._vel_until:
                if self._vel_unsafe():
                    # 同 vel_gate.hpp 的 OnState:急停、趴下、锁死 → 作废目标,恢复了也不复活。
                    self._vel_until = 0.0
                    break
                fwd, lat, yaw = self._vel
                if max(abs(fwd), abs(lat), abs(yaw)) < WALK_DEADBAND:
                    # 量太小只是原地蹭(清单 #37),不动;真机也不报错。
                    self.vx = self.vy = self.vyaw = 0.0
                else:
                    self.motion = MotionStatus.GAIT
                    # 系数同 walk(待真机验证):满量程 1.0 折 1.2 m/s。
                    self.vx, self.vy, self.vyaw = fwd * 1.2, lat * 1.0, yaw * 1.5
                    self.yaw = _wrap(self.yaw + self.vyaw * _VEL_TICK_S)
                    dx = (self.vx * math.cos(self.yaw) - self.vy * math.sin(self.yaw)) \
                        * _VEL_TICK_S
                    dy = (self.vx * math.sin(self.yaw) + self.vy * math.cos(self.yaw)) \
                        * _VEL_TICK_S
                    self.x += dx
                    self.y += dy
                    self.distance += math.hypot(dx, dy)
                await asyncio.sleep(_VEL_TICK_S)
        finally:
            self.vx = self.vy = self.vyaw = 0.0
            if self.motion is MotionStatus.GAIT:
                self.motion = MotionStatus.GENERAL

    async def _transition(self, target: MotionStatus) -> None:
        """站起/趴下是**有过程的**,不是瞬间。见清单 #36。"""
        if self.estop_software is EmergencyStatus.STOP:
            raise _Rejected("软急停生效中,拒绝动作")
        await asyncio.sleep(STAND_SECONDS)
        self.motion = target
        if target is MotionStatus.STAND_UP:
            # 0.1.1 的实测时序是 SetMode(1) 之后落到 GENERAL(清单 #45),
            # 但站起那一刻先经过 STAND_UP。这里把两步都走一遍。
            await asyncio.sleep(STAND_SECONDS)
            self.motion = MotionStatus.GENERAL

    async def _sleep_unless_cancelled(self, duration: float, gen: int) -> float:
        """睡 ``duration`` 秒,中途被叫停就提前醒。返回真正睡了多久。"""
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if self._cancel_gen != gen or elapsed >= duration:
                return min(elapsed, duration)
            await asyncio.sleep(min(_CANCEL_POLL_S, duration - elapsed))

    async def _walk(self, args: dict[str, Any], gen: int) -> None:
        if self.motion in (MotionStatus.LIE_DOWN, MotionStatus.UNKNOWN):
            raise _Rejected("趴着走不了,先 stand")
        if self.estop_software is EmergencyStatus.STOP:
            raise _Rejected("软急停生效中,拒绝动作")
        seconds = float(args.get("seconds", 0.0))
        fwd = float(args.get("fwd", 0.0))
        lat = float(args.get("lat", 0.0))
        yaw_rate = float(args.get("yaw", 0.0))
        if seconds <= 0:
            raise _Rejected(f"时长要为正,收到 {seconds}")

        if (abs(fwd) < WALK_DEADBAND and abs(lat) < WALK_DEADBAND
                and abs(yaw_rate) < WALK_DEADBAND):
            # 量太小就只是原地蹭 —— 这正是现场"站起来不走"的真相(#37)。
            # 不报错: 真机也不报错,它就是不动。测试要的就是这个沉默的失败。
            #
            # 假设(待真机验证): 现场只量过平移(0.11 几乎不动、0.3 明显走),
            # 转向那一路没验过。这里按同一个死区处理 —— 与其把 yaw 排除在外、
            # 让"纯转向永远不动"变成一个静默的坑,不如先按同样的量级建模。
            await self._sleep_unless_cancelled(min(seconds, STAND_SECONDS), gen)
            if self._cancel_gen != gen:
                self.cancelled_walks += 1
                raise _Rejected(WALK_CANCELLED)
            return

        self.motion = MotionStatus.GAIT
        # 假设(待真机验证): fwd 是百分比,满量程按 1.0 折算 1.2 m/s,
        # 于是 fwd=0.5 → 0.6 m/s,和只读接口的 get_speed x=0.6 对得上(#38)。
        self.vx, self.vy, self.vyaw = fwd * 1.2, lat * 1.0, yaw_rate * 1.5
        vx, vy, vyaw = self.vx, self.vy, self.vyaw
        planned = min(seconds, STAND_SECONDS)
        try:
            slept = await self._sleep_unless_cancelled(planned, gen)
        finally:
            # 断连取消也要把速度落回零,不然遥测里一直挂着"在走"。
            self.vx = self.vy = self.vyaw = 0.0
            self.motion = MotionStatus.GENERAL
        cancelled = self._cancel_gen != gen
        # 仿真把整拍的位移压缩在 ``planned`` 里走完;被叫停时只算走了的那一截。
        moved = seconds * (slept / planned) if cancelled else seconds
        self.yaw = _wrap(self.yaw + vyaw * moved)
        dx = (vx * math.cos(self.yaw) - vy * math.sin(self.yaw)) * moved
        dy = (vx * math.sin(self.yaw) + vy * math.cos(self.yaw)) * moved
        self.x += dx
        self.y += dy
        self.distance += math.hypot(dx, dy)
        if cancelled:
            self.cancelled_walks += 1
            raise _Rejected(WALK_CANCELLED)

    # -------------------------------------------------------------- 注入

    def drop_control(self, reason: str = "上装收回控制权") -> None:
        """立刻把控制权拿走并广播 —— 测试用的故障注入。"""
        self._held = False
        self._broadcast({"t": "control_lost", "reason": reason})

    def push_fault(self, level: int, code: int, message: str) -> None:
        self._broadcast({"t": "fault", "level": level, "code": code,
                         "message": message})

    def set_battery(self, percent: float) -> None:
        self.battery1 = self.battery2 = percent

    def push_raw(self, line: str) -> None:
        """原样塞一行下去,不管它是不是合法帧。

        用来模拟"旁路进程升级了、多吐了一种我们还不认识的帧",以及 C++ 那头
        把日志写串到了 stdout。客户端必须能丢掉它继续跑,而不是断链。
        """
        for writer in list(self._clients):
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.write((line + "\n").encode("utf-8"))


class _Rejected(Exception):
    """旁路进程明确拒绝了一条命令。"""


def _wrap(angle: float) -> float:
    """把角度规到 (-pi, pi]。"""
    return math.atan2(math.sin(angle), math.cos(angle))


__all__ = [
    "STAND_SECONDS",
    "URGENT_COMMANDS",
    "WALK_CANCELLED",
    "WALK_DEADBAND",
    "SimAgentServer",
]
