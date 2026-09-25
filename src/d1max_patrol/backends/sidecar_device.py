"""接到常驻运控旁路进程上的 ``DeviceBackend`` 实现。

配套的旁路进程是 ``motion/patrol_agent.cpp``,协议见
``d1max_patrol.protocol.agent_frames``。仿真版旁路进程在
``d1max_sim.agent_server``,跑测试不需要真机也不需要编 C++。

**读这个文件之前先读懂这条约束**(``docs/真机待验证清单.md`` #46/#47):

    SDK 资源是独占的,开机后默认被上装占住;唯一已验证的进入方式是抢开机
    窗口;**一旦释放,上装立刻收回,连只读都再也进不去,只能重启 RK3588。**

所以本类**不管理 SDK 会话的生命周期** —— 它只是接到一条已经握住控制权的
会话上。connect/close 指的是"接上旁路进程 / 从旁路进程断开",跟 SDK 会话
死活无关。这个区分是本文件里最要紧的一件事,别把两者混起来。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import deque
from typing import Any

from d1max_patrol.backends.base import (
    BatteryEvent,
    ControlLostEvent,
    DeviceBackend,
    DeviceBackendError,
    DevicePoseEvent,
    FaultEvent,
    Frame,
)
from d1max_patrol.protocol.agent_frames import (
    PROTO_VERSION,
    Ack,
    AgentProtocolError,
    ControlLostFrame,
    FaultFrame,
    Hello,
    MotionStatus,
    OdomFrame,
    StateFrame,
    decode_frame,
    encode_command,
)
from d1max_patrol.protocol.nav_types import Pose

#: 旁路进程默认监听的地址。只绑回环 —— 它握着能让机器走路的控制权,
#: 默认对外可达是不可接受的。要跨机连(旁路进程板载、Python 在笔记本上)
#: 就显式传 host,并且自己保证那段网络是可信的。
DEFAULT_AGENT_HOST = "127.0.0.1"
DEFAULT_AGENT_PORT = 8090

#: 单条命令等回执的默认上限。
#:
#: 旁路进程是**做完才回执**的,所以这个值必须盖得住最慢的那条命令:
#: 一次 ``walk`` 最长 10s,前面还有 0.5s 的 ``Gait`` 起步和 0.3s 的零速收尾。
#: 站起实测 ~6s(清单 #36)。取 15s,留一点余量。
DEFAULT_ACK_TIMEOUT_S = 15.0

#: 停车/急停等回执的上限。**比 ``DEFAULT_ACK_TIMEOUT_S`` 短得多**:这两条在
#: 旁路进程里插队执行,最坏情况是等正在走的那一拍松开 SDK 锁(撞上
#: ``Gait(2000)`` 时约 2s)再加 ``SoftEmergencyStop`` 自己的 2s。人按了急停,
#: 页面上必须在几秒内知道成没成,而不是转 15 秒圈。
URGENT_ACK_TIMEOUT_S = 5.0

#: 一次 ``Move`` 在机器上维持约 1s(清单 #38),所以行走靠连续下发维持。
#: 这两个上限是**安全阀**,不是调参项: 现场站着人。
MAX_WALK_SECONDS = 10.0
MAX_WALK_SPEED = 0.5

#: ``vel``(协议 v3)的有效期范围,同 ``patrol_agent.cpp`` 的 ``kVelTtl*``。
VEL_TTL_MIN_MS = 50
VEL_TTL_MAX_MS = 1000

#: 留多少条最近的故障帧给 HAL 读(``recent_faults``)。
RECENT_FAULTS = 20


class SidecarDeviceBackend(DeviceBackend):
    """通过 TCP + JSONL 驱动常驻 SDK 会话。

    用法::

        backend = SidecarDeviceBackend()
        await backend.connect()          # 接上旁路进程,不碰 SDK 会话
        await backend.acquire_control()  # 只是核对"旁路进程真握着控制权"
        await backend.stand()
        await backend.close()            # 断开 Python 这头,SDK 会话继续活着
    """

    def __init__(
        self,
        host: str = DEFAULT_AGENT_HOST,
        port: int = DEFAULT_AGENT_PORT,
        *,
        ack_timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._ack_timeout_s = ack_timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Ack]] = {}
        self._hello: Hello | None = None
        self._state: StateFrame | None = None
        self._odom: OdomFrame | None = None
        self._held = False
        #: 上一次广播出去的电量,用来抑制"每帧都发一条 BatteryEvent"。
        self._last_battery: float | None = None
        #: 最近一帧里程到达的时刻(``time.monotonic``)。HAL 拿它判里程新不新鲜。
        self._odom_at: float | None = None
        #: 最近的故障帧(有界)。事件流里的 ``FaultEvent`` 丢了 code,HAL 要原样的。
        self._faults: deque[FaultFrame] = deque(maxlen=RECENT_FAULTS)

    # -------------------------------------------------------------- 生命周期

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    @property
    def hello(self) -> Hello | None:
        """旁路进程的自我介绍。没连上时是 None。"""
        return self._hello

    @property
    def last_state(self) -> StateFrame | None:
        """最近一帧机身状态。没收到过是 None。"""
        return self._state

    @property
    def last_odom(self) -> OdomFrame | None:
        """最近一帧里程。没收到过是 None。"""
        return self._odom

    @property
    def last_odom_at(self) -> float | None:
        """最近一帧里程到达的 ``time.monotonic()``。没收到过是 None。"""
        return self._odom_at

    @property
    def recent_faults(self) -> tuple[FaultFrame, ...]:
        """最近的故障帧,旧的在前。"""
        return tuple(self._faults)

    async def connect(self) -> None:
        """接上旁路进程,并校验它的协议版本。

        **这不会建立 SDK 会话** —— 那是旁路进程自己在开机窗口里抢的。
        """
        if self.connected:
            return
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self._host, self._port)
        except OSError as exc:
            raise DeviceBackendError(
                f"连不上运控旁路进程 {self._host}:{self._port}: {exc}。"
                f"它起来了吗? 见 docs/真机联调手册.md 的 S1"
            ) from exc

        try:
            hello = await asyncio.wait_for(self._read_frame(), self._ack_timeout_s)
        except asyncio.TimeoutError as exc:
            await self._teardown()
            raise DeviceBackendError("旁路进程连上了但不吐 hello 帧") from exc
        except (AgentProtocolError, OSError) as exc:
            await self._teardown()
            raise DeviceBackendError(f"旁路进程的 hello 帧读不了: {exc}") from exc

        if not isinstance(hello, Hello):
            await self._teardown()
            raise DeviceBackendError(f"第一帧应该是 hello,收到 {type(hello).__name__}")
        if hello.proto != PROTO_VERSION:
            await self._teardown()
            raise DeviceBackendError(
                f"协议版本对不上: 旁路进程 {hello.proto},本端 {PROTO_VERSION}。"
                f"重编 motion/patrol_agent 再来"
            )
        self._hello = hello
        self._held = hello.held
        self._pump = asyncio.create_task(self._pump_frames())

    async def close(self) -> None:
        """断开 Python 这头。

        **SDK 会话不受影响,旁路进程继续握着控制权。** 这正是我们要的:
        巡检程序可以随便重启,控制权不会在缝隙里被上装收回(清单 #47)。
        """
        pump, self._pump = self._pump, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        self._fail_pending(DeviceBackendError("与旁路进程的连接已关闭"))
        await self._teardown()

    async def _teardown(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        self._held = False
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # -------------------------------------------------------------- 收帧

    async def _read_frame(self) -> Any:
        assert self._reader is not None
        line = await self._reader.readline()
        if not line:
            raise OSError("旁路进程关掉了连接")
        return decode_frame(line)

    async def _pump_frames(self) -> None:
        """后台收帧: 回执对号入座,遥测转成事件广播出去。"""
        try:
            while True:
                try:
                    frame = await self._read_frame()
                except AgentProtocolError:
                    # 看不懂的帧不该拖垮整条链路 —— 固件/旁路进程升级时
                    # 完全可能多出我们还不认识的帧类型。丢掉,继续收。
                    continue
                self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError) as exc:
            self._fail_pending(DeviceBackendError(f"与旁路进程的连接断了: {exc}"))
            self._held = False
            self.emit(ControlLostEvent(reason=f"旁路进程连接断开: {exc}"))

    def _dispatch(self, frame: Any) -> None:
        if isinstance(frame, Ack):
            future = self._pending.pop(frame.id, None)
            if future is not None and not future.done():
                future.set_result(frame)
            return
        if isinstance(frame, StateFrame):
            self._state = frame
            battery = frame.battery
            if battery != self._last_battery:
                self._last_battery = battery
                self.emit(BatteryEvent(percent=battery))
            return
        if isinstance(frame, OdomFrame):
            self._odom = frame
            self._odom_at = time.monotonic()
            self.emit(DevicePoseEvent(
                pose=Pose.from_xy_yaw(frame.x, frame.y, frame.yaw)))
            return
        if isinstance(frame, FaultFrame):
            self._faults.append(frame)
            self.emit(FaultEvent(
                items=(f"[level={frame.level} code={frame.code}] {frame.message}",),
                # 厂商没给"哪些 level 算致命"的定义。取 level>=2 为致命是
                # 假设(待真机验证): 现场只见过 level 打印,没见过分级说明。
                fatal=frame.level >= 2,
            ))
            return
        if isinstance(frame, ControlLostFrame):
            self._held = False
            self.emit(ControlLostEvent(
                reason=frame.reason or "旁路进程报告控制权丢失(SDK 未给原因)"))
            return
        if isinstance(frame, Hello):
            # 重复的 hello 只更新持有状态,不重置连接。
            self._hello = frame
            self._held = frame.held

    def _fail_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    # -------------------------------------------------------------- 发命令

    async def _command(self, cmd: str, *, timeout_s: float | None = None,
                       **fields: Any) -> Ack:
        if not self.connected:
            raise DeviceBackendError(f"没连上旁路进程,发不了 {cmd}")
        assert self._writer is not None
        self._next_id += 1
        cmd_id = self._next_id
        future: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future
        try:
            self._writer.write(encode_command(cmd_id, cmd, **fields))
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            self._pending.pop(cmd_id, None)
            raise DeviceBackendError(f"{cmd} 没发出去: {exc}") from exc

        limit = self._ack_timeout_s if timeout_s is None else min(
            timeout_s, self._ack_timeout_s)
        try:
            ack = await asyncio.wait_for(future, limit)
        except asyncio.TimeoutError as exc:
            self._pending.pop(cmd_id, None)
            raise DeviceBackendError(f"{cmd} 等回执超过 {limit}s") from exc
        if not ack.ok:
            raise DeviceBackendError(f"{cmd} 被旁路进程拒绝: {ack.error}")
        return ack

    async def _require_control(self, action: str) -> None:
        if not self._held:
            raise DeviceBackendError(
                f"旁路进程没握着控制权,不能 {action}。"
                f"多半是被上装占了(清单 #47),要重启 RK3588 重抢开机窗口"
            )

    # -------------------------------------------------------------- 控制权

    async def acquire_control(self) -> None:
        """**核对**旁路进程握着控制权,而不是去申请。

        真正的 ``TakeControl`` 是旁路进程在抢开机窗口时做的。到了这里
        再去申请已经晚了 —— 上装早占住了。所以这里只做一次确认,拿不到
        就直接抛,让上层知道今天走不了运控这条路。
        """
        if not self.connected:
            raise DeviceBackendError("还没连上旁路进程")
        ack = await self._command("hold")
        self._held = True
        del ack

    async def release_control(self) -> None:
        """**故意不释放 SDK 控制权。**

        释放 = 上装立刻收回 = 连只读都进不去,只能重启整台 RK3588
        (清单 #47)。而上层的清理路径(``finally``、异常收尾、程序退出)
        天然会调这个方法 —— 让它真去 ``ReleaseControl``,等于把"巡检程序
        崩了一次"升级成"整台机器要重启"。

        所以这里只清掉本端的"我在开车"标记。真要交还控制权,是运维动作,
        显式调 ``shutdown_agent()``。
        """
        self._held = False

    async def has_control(self) -> bool:
        return self._held and self.connected

    async def shutdown_agent(self) -> None:
        """让旁路进程 ``ReleaseControl`` 并退出。

        **这是不可逆的运维动作,不是清理动作。** 执行完就要重启 RK3588
        才能再抢一次控制权。只在确定今天不再用机器时调。
        """
        with contextlib.suppress(DeviceBackendError):
            await self._command("shutdown")
        self._held = False
        await self.close()

    # -------------------------------------------------------------- 动作

    async def stand(self) -> None:
        """站起。旁路进程内部走 ``SetMode(1) → StandUp``。

        只 ``StandUp`` 不 ``SetMode`` 是站不起来的(清单 #45) —— 那个时序
        封在旁路进程里,这里不重复。
        """
        await self._require_control("站起")
        await self._command("stand")

    async def lie(self) -> None:
        await self._require_control("趴下")
        await self._command("lie")

    async def walk(self, seconds: float, forward: float,
                   lateral: float = 0.0, yaw: float = 0.0) -> None:
        """直驱行走 —— 运控层的开环平移,**不是导航**。

        导航仍然走 ``NavBackend``,真理源规则(规范 §3.4)没变。这个方法是
        给"导航够不着的最后一米"和闭环实验用的。

        量级很要紧(清单 #37/#38): 单位是百分比、受 ``SpeedLevel`` 限速,
        ``forward=0.11`` 几乎不动,``0.3~0.5`` 才真的走; ``0.5`` 约
        0.5-0.6 m/s。上限在这里硬卡住,现场旁边站着人。
        """
        await self._require_control("行走")
        if not 0 < seconds <= MAX_WALK_SECONDS:
            raise DeviceBackendError(
                f"行走时长要在 (0, {MAX_WALK_SECONDS}] 秒内,收到 {seconds}")
        for name, value in (("forward", forward), ("lateral", lateral),
                            ("yaw", yaw)):
            if not math.isfinite(value) or abs(value) > MAX_WALK_SPEED:
                raise DeviceBackendError(
                    f"{name} 要在 ±{MAX_WALK_SPEED} 内,收到 {value}")
        await self._command("walk", seconds=seconds, fwd=forward,
                            lat=lateral, yaw=yaw)

    async def vel(self, forward: float, lateral: float, yaw: float, ttl_ms: int, *,
                  timeout_s: float = URGENT_ACK_TIMEOUT_S) -> None:
        """带有效期的持续速度(协议 v3,W00d)。旁路进程**立刻回执**、插队执行;有效期内
        每 50 ms 发一次 ``Move``,到期自己连发零速停下。新的 ``vel`` 覆盖旧的并续期。

        单位同 ``walk``:比例值,不是 m/s。换算归 HAL(``d1max_adapter_d1max``)。
        """
        await self._require_control("持续速度")
        for name, value in (("forward", forward), ("lateral", lateral), ("yaw", yaw)):
            if not math.isfinite(value) or abs(value) > MAX_WALK_SPEED:
                raise DeviceBackendError(f"{name} 要在 ±{MAX_WALK_SPEED} 内,收到 {value}")
        if not VEL_TTL_MIN_MS <= ttl_ms <= VEL_TTL_MAX_MS:
            raise DeviceBackendError(
                f"ttl_ms 要在 [{VEL_TTL_MIN_MS}, {VEL_TTL_MAX_MS}] 内,收到 {ttl_ms}")
        await self._command("vel", timeout_s=timeout_s, fwd=forward, lat=lateral,
                            yaw=yaw, ttl_ms=int(ttl_ms))

    async def halt(self) -> None:
        """停车。旁路进程**插队**执行:不排在还没走完的 walk 后面。

        不查控制权 —— 本端的 ``_held`` 可能已经被 ``release_control`` 清掉,
        而旁路进程那头也许还在走最后一拍;停不停由旁路进程判断。
        """
        await self._command("halt", timeout_s=URGENT_ACK_TIMEOUT_S)

    async def emergency_stop(self, on: bool = True) -> None:
        """软急停。旁路进程插队执行,并作废正在走和排着队的 walk。

        **软急停不能替代机身上那个硬急停按钮。** 它走的是同一条 SDK 链路,
        链路本身出问题时它也没了。
        """
        await self._command("estop", timeout_s=URGENT_ACK_TIMEOUT_S, on=on)

    async def set_light(self, on: bool) -> None:
        """前后补光灯一起开关。要分开控制用 ``set_front_light`` / ``set_back_light``。"""
        await self._command("light", which="both", on=on)

    async def set_front_light(self, on: bool) -> None:
        await self._command("light", which="front", on=on)

    async def set_back_light(self, on: bool) -> None:
        await self._command("light", which="back", on=on)

    async def set_gimbal(self, pitch: float, yaw: float) -> None:
        """机身头部角度,映射到 SDK 的 ``ControlHead(left_right, up_down)``。

        # 假设(待真机验证): 单位取 rad,且 ``yaw`` → ``left_right``、
        # ``pitch`` → ``up_down``。厂商头文件一个单位都没写,现场也没验过
        # 这个接口。真机上第一件事就是发一个小角度看它往哪转、转多少。
        """
        await self._require_control("转头")
        await self._command("head", yaw=yaw, pitch=pitch)

    async def take_photo(self) -> Frame:
        """**SDK 没有拍照接口。**

        0.1.1 的 ``sdk_client.hpp`` 里跟相机沾边的只有
        ``UpdateCameraBitrate`` —— 调码率的,不出图。取图只有 RTSP 一条路
        (``rtsp://192.168.234.1:8554/{front,back}``),那是 ``MediaSource``
        的活。这里明确抛错,不做"看起来能用"的假实现。
        """
        raise DeviceBackendError(
            "SDK 不提供拍照接口,请用 MediaSource 走 RTSP 抽帧")

    async def battery(self) -> float:
        """当前电量百分比 (0-100),取两块电池里低的那块。

        白灯转黄灯就是低电量告警(清单 #43),厂商文档没写这条;一天能从
        71% 掉到 17%,四足断电会当场瘫倒 —— 所以巡检要盯这个值。
        """
        if self._state is None:
            raise DeviceBackendError("还没收到过状态帧,读不到电量")
        return self._state.battery

    async def emergency(self) -> bool:
        """急停是否按下。软急停与硬急停任一为 STOP 就算按下。

        没收到过状态帧时抛错而不是回 False:那种情况是"不知道",放行等于
        赌运气。
        """
        if self._state is None:
            raise DeviceBackendError("还没收到过状态帧,读不到急停状态")
        return self._state.emergency

    async def motion_status(self) -> MotionStatus:
        """当前运动状态。没收到过状态帧时抛错,不猜。"""
        if self._state is None:
            raise DeviceBackendError("还没收到过状态帧,读不到运动状态")
        return self._state.motion

    async def wait_motion(self, target: MotionStatus, timeout_s: float) -> None:
        """等到机身进入某个运动状态。

        站起实测要 ~6s(清单 #36),别把超时设得比它还短。
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if self._state is not None and self._state.motion is target:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                current = self._state.motion.value if self._state else "(无状态帧)"
                raise DeviceBackendError(
                    f"等 {target.value} 超过 {timeout_s}s,当前是 {current}")
            await asyncio.sleep(min(0.05, remaining))

    def __repr__(self) -> str:  # pragma: no cover - 只为调试好看
        return (f"<SidecarDeviceBackend {self._host}:{self._port} "
                f"connected={self.connected} held={self._held}>")


def parse_agent_endpoint(text: str) -> tuple[str, int]:
    """把 ``host:port`` 解成二元组,给 CLI 用。只给端口号也行。"""
    text = text.strip()
    if not text:
        raise ValueError("旁路进程地址不能为空")
    if ":" not in text:
        return DEFAULT_AGENT_HOST, _port(text)
    host, _, port = text.rpartition(":")
    return (host or DEFAULT_AGENT_HOST), _port(port)


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError as exc:
        raise ValueError(f"端口不是整数: {text!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"端口超范围: {port}")
    return port


__all__ = [
    "DEFAULT_AGENT_HOST",
    "DEFAULT_AGENT_PORT",
    "MAX_WALK_SECONDS",
    "MAX_WALK_SPEED",
    "URGENT_ACK_TIMEOUT_S",
    "SidecarDeviceBackend",
    "parse_agent_endpoint",
]
