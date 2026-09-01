"""仿真运控旁路进程 —— ``motion/patrol_agent.cpp`` 的可执行契约。

跟 ``nav_server`` 同一条原则: 客户端行为有争议时,以这台仿真器的表现为准,
直到真机把它推翻。凡文档未明写、由本仿真器补齐的行为,一律加待真机验证标记。

**它刻意模拟的几件坏事**(都是现场真出过的,见 ``docs/真机待验证清单.md``):

* ``deny_control=True`` —— 上装占着 SDK 资源,连只读都进不去(#47)。
* ``drop_control_after`` —— 跑着跑着控制权被收走(#40/#46)。
* 站起要花时间,不是瞬间的(#36 实测 ~6s),所以状态是**慢慢**变的。
* ``forward`` 给太小就只是原地蹭,``motion`` 不会进 ``Gait``(#37)。

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
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    await self._on_line(writer, line)
            finally:
                telemetry.cancel()
                self._tasks.discard(telemetry)
                with contextlib.suppress(asyncio.CancelledError):
                    await telemetry
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

    async def _on_line(self, writer: asyncio.StreamWriter, line: bytes) -> None:
        try:
            cmd_id, cmd, args = decode_command(line)
        except AgentProtocolError as exc:
            # 解不动的行没有 id,回不了对号入座的 ack。真机那头也只能记日志,
            # 所以这里也只记日志 —— 客户端会自己等超时。
            log.warning("旁路仿真收到看不懂的命令行: %s", exc)
            return
        self.commands.append((cmd, dict(args)))
        try:
            await self._run(cmd, args)
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

    async def _run(self, cmd: str, args: dict[str, Any]) -> None:
        if cmd == "hold":
            if self._deny_control:
                raise _Rejected("Controlled denial of service (上装占用中)")
            self._held = True
            return
        if cmd == "shutdown":
            self._held = False
            self._broadcast({"t": "control_lost", "reason": "旁路进程按要求退出"})
            return
        if cmd == "estop":
            # 急停不需要控制权 —— 它是安全动作,任何时候都得能发出去。
            on = bool(args.get("on", True))
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
        if cmd == "walk":
            self._need_control("walk")
            await self._walk(args)
            return
        raise _Rejected(f"不认识的命令: {cmd}")

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

    async def _walk(self, args: dict[str, Any]) -> None:
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
            await asyncio.sleep(min(seconds, STAND_SECONDS))
            return

        self.motion = MotionStatus.GAIT
        # 假设(待真机验证): fwd 是百分比,满量程按 1.0 折算 1.2 m/s,
        # 于是 fwd=0.5 → 0.6 m/s,和只读接口的 get_speed x=0.6 对得上(#38)。
        self.vx, self.vy, self.vyaw = fwd * 1.2, lat * 1.0, yaw_rate * 1.5
        await asyncio.sleep(min(seconds, STAND_SECONDS))
        self.yaw = _wrap(self.yaw + self.vyaw * seconds)
        dx = (self.vx * math.cos(self.yaw) - self.vy * math.sin(self.yaw)) * seconds
        dy = (self.vx * math.sin(self.yaw) + self.vy * math.cos(self.yaw)) * seconds
        self.x += dx
        self.y += dy
        self.distance += math.hypot(dx, dy)
        self.vx = self.vy = self.vyaw = 0.0
        self.motion = MotionStatus.GENERAL

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


__all__ = ["STAND_SECONDS", "WALK_DEADBAND", "SimAgentServer"]
