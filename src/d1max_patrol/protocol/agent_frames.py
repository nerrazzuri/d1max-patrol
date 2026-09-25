"""运控旁路进程(``motion/patrol_agent``)的线协议 —— 换行分隔的 JSON。

**为什么会有这个协议,而不是让 Python 直接调 SDK:**

现场实测(``docs/真机待验证清单.md`` #46/#47)得出一条硬约束:

    SDK 资源(控制**和**数据读取)是独占的,开机后默认被上装占住。
    目前唯一已验证的进入方式是"抢开机窗口" —— 在上装占住之前连上去。
    一旦释放,上装立刻收回,连只读都再也进不去。

推论有两层:

1. 会话必须**常驻**,而且是**同一条**连接既订阅遥测又下发控制。
2. 会话**不能是 Python 进程的子进程**。巡检程序会重启、会崩、会被 Ctrl+C,
   而这条 SDK 会话一旦断掉就要重启整台 RK3588 才能再抢一次。所以旁路进程
   是个先于巡检程序启动、比它活得久的**守护进程**,Python 只是**接上去**。

于是需要一条进程间的线:C++ 那头握着 SDK,Python 这头随连随断。选 TCP 而不是
Unix socket,是因为 (a) 开发机是 Windows,UDS 在 ``asyncio`` 上不可用;
(b) 将来旁路进程搬到机器上板载运行时,同一份代码不用改就能跨机连。默认只绑
``127.0.0.1``。

**分层**: 本模块只做编解码,不碰 socket,也不 import ``backends``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

#: 协议版本。旁路进程在 ``hello`` 里报自己的版本,对不上就拒绝连接 ——
#: 现场最不需要的就是"连上了但字段对不上"这种半死不活的状态。
#:
#: 3(W00d):加了 ``vel`` —— 带有效期的持续速度(``fwd/lat/yaw`` 比例值 ±0.5、``ttl_ms`` 50..1000),
#: **立刻回执**、有效期内持续发 ``Move``、到期自停,插队执行。2 号旁路进程不认识它。
#:
#: 2:加了 ``halt``,并且 ``halt``/``estop`` 在旁路进程里**插队**执行、作废
#: 正在走和排着队的 ``walk``。1 号旁路进程不认识 ``halt``、急停排在 walk
#: 后面 —— 新巡检程序配它时停车会无声地不灵,所以握手时就拒。
PROTO_VERSION = 3


class MotionStatus(str, Enum):
    """机身运动状态,逐条对应 SDK 0.1.1 ``sdk_type.hpp`` 的 ``MotionStatus``。

    枚举是**全的**(0-10)。``motion/hold02.cpp`` 那张手写映射表只覆盖了 1-6,
    7-10 会打印成 ``?`` —— 那是调试程序的将就,不要照抄进产品代码。
    """

    UNKNOWN = "Unknown"
    STAND_UP = "StandUp"
    LIE_DOWN = "LieDown"
    CRAWL = "Crawl"
    LOCKED = "Locked"
    GENERAL = "General"
    IN_PLACE = "InPlace"
    STAIR = "Stair"
    CLIMB = "Climb"
    SLIM = "Slim"
    GAIT = "Gait"


#: SDK 的 ``MotionStatus`` 是从 0 起的连续枚举,顺序即 sdk_type.hpp 的声明顺序。
MOTION_BY_CODE: dict[int, MotionStatus] = dict(
    enumerate(MotionStatus)
)


class EmergencyStatus(str, Enum):
    """急停状态。

    **这是三值,不是 bool。** SDK 的 ``EmergencyStatus`` 有
    ``UNKNOWN`` / ``RECOVER`` / ``STOP``,把它压成 bool 会让"还不知道"
    和"没有急停"变成同一个值 —— 安全相关的字段不能这么省。
    """

    UNKNOWN = "Unknown"
    RECOVER = "Recover"      # 急停已解除
    STOP = "Stop"            # 急停生效中


EMERGENCY_BY_CODE: dict[int, EmergencyStatus] = dict(enumerate(EmergencyStatus))


class AgentProtocolError(Exception):
    """收到的行不是本协议能理解的东西。"""


# ------------------------------------------------------------------ 下行帧


@dataclass(frozen=True)
class Hello:
    """连上就收到的第一帧。旁路进程自报家门。"""

    proto: int
    #: 旁路进程实际链接的 SDK 版本,现场结论是 "0.1.1"(清单 #45)。
    sdk: str
    #: 此刻是否真的握着控制权。**False 时不要下发任何动作指令** ——
    #: 上装占着的时候指令会被静默吞掉或直接报 denial。
    held: bool
    #: 旁路进程连的机器地址,形如 "192.168.168.168:8082"。只作日志用。
    robot: str = ""


@dataclass(frozen=True)
class Ack:
    """一条命令的回执。``id`` 与请求里的 ``id`` 对应。"""

    id: int
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class StateFrame:
    """机身状态,由 SDK ``OnRobotStateData`` 转发而来。"""

    motion: MotionStatus
    #: 电池 1/2 的电量百分比 (0-100)。两块电池都可能不在位。
    battery1: float
    battery2: float
    estop_software: EmergencyStatus
    estop_hardware: EmergencyStatus
    ts_ms: int = 0

    @property
    def battery(self) -> float:
        """对上层的单一电量读数 —— 取两块里**低的那块**。

        跑巡检时关心的是"还能撑多久",取低的那块才是保守值。
        全都不在位(都报 0)时返回 0.0,让上层自己判定为"读不到"。
        """
        values = [v for v in (self.battery1, self.battery2) if v > 0.0]
        return min(values) if values else 0.0

    @property
    def emergency(self) -> bool:
        """任一路急停生效即为 True。``UNKNOWN`` 不算生效,但也不该当没事。"""
        return EmergencyStatus.STOP in (self.estop_software, self.estop_hardware)


@dataclass(frozen=True)
class OdomFrame:
    """里程,由 SDK ``OnMcData``(``MotionData``)转发而来。

    ``MotionData.position`` 是**世界系、单位米**,``v_body`` 是机体系速度。
    这是闭环行走要的东西(清单 #48 的下一步)。

    注意: 这是**运控自己的里程**,不是导航模块的定位。真理源规则(规范 §3.4)
    没变 —— 导航进展仍以 ``NavBackend`` 为准,这里只作交叉校验与安全兜底。
    """

    x: float
    y: float
    yaw: float
    #: 机体系速度,m/s 与 rad/s。
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0
    ts_ms: int = 0


@dataclass(frozen=True)
class FaultFrame:
    level: int
    code: int
    message: str


@dataclass(frozen=True)
class ControlLostFrame:
    """控制权没了。

    SDK 的 ``ControlLostInfo`` 是个**空结构体** —— 厂商没给原因字段。
    所以 ``reason`` 通常是旁路进程自己凑的一句话,不要指望它有信息量。
    """

    reason: str = ""


Downstream = Hello | Ack | StateFrame | OdomFrame | FaultFrame | ControlLostFrame


# ------------------------------------------------------------------ 上行编码


def encode_command(cmd_id: int, cmd: str, **fields: Any) -> bytes:
    """把一条命令编成一行(**带换行符**)。

    ``id`` 由调用方分配且必须递增 —— 回执靠它对号入座。
    """
    payload: dict[str, Any] = {"id": cmd_id, "cmd": cmd}
    payload.update(fields)
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def decode_command(line: str | bytes) -> tuple[int, str, dict[str, Any]]:
    """解一条上行命令。旁路进程的模拟实现用它,真机那头是 C++ 自己解。

    返回 ``(id, cmd, 其余字段)``。
    """
    obj = _load(line)
    if "cmd" not in obj:
        raise AgentProtocolError(f"命令缺 cmd 字段: {obj!r}")
    cmd_id = obj.pop("id", 0)
    if not isinstance(cmd_id, int):
        raise AgentProtocolError(f"id 必须是整数,收到 {cmd_id!r}")
    cmd = obj.pop("cmd")
    if not isinstance(cmd, str):
        raise AgentProtocolError(f"cmd 必须是字符串,收到 {cmd!r}")
    return cmd_id, cmd, obj


# ------------------------------------------------------------------ 下行解码


def _load(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentProtocolError(f"这一行不是 UTF-8: {line!r}") from exc
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise AgentProtocolError(f"这一行不是 JSON: {line!r}") from exc
    if not isinstance(obj, dict):
        raise AgentProtocolError(f"顶层必须是对象,收到 {type(obj).__name__}")
    return obj


def _num(obj: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentProtocolError(f"{key} 必须是数字,收到 {value!r}")
    return float(value)


def _motion(value: Any) -> MotionStatus:
    """状态既接受枚举名也接受 SDK 的整数码。

    整数码走 ``MOTION_BY_CODE``;未知的码不抛错,降级成 ``UNKNOWN`` ——
    固件升级新增枚举值不该让整个巡检崩掉(和 NavBackend 那边同一条原则)。
    """
    if isinstance(value, bool):
        raise AgentProtocolError(f"motion 不能是 bool: {value!r}")
    if isinstance(value, int):
        return MOTION_BY_CODE.get(value, MotionStatus.UNKNOWN)
    if isinstance(value, str):
        try:
            return MotionStatus(value)
        except ValueError:
            return MotionStatus.UNKNOWN
    raise AgentProtocolError(f"motion 既不是整数码也不是名字: {value!r}")


def _emergency(value: Any) -> EmergencyStatus:
    if isinstance(value, bool):
        raise AgentProtocolError(f"急停字段不能是 bool: {value!r}")
    if isinstance(value, int):
        return EMERGENCY_BY_CODE.get(value, EmergencyStatus.UNKNOWN)
    if isinstance(value, str):
        try:
            return EmergencyStatus(value)
        except ValueError:
            return EmergencyStatus.UNKNOWN
    raise AgentProtocolError(f"急停字段既不是整数码也不是名字: {value!r}")


def decode_frame(line: str | bytes) -> Downstream:
    """解一条下行帧。看不懂的类型抛 ``AgentProtocolError``。"""
    obj = _load(line)
    kind = obj.get("t")
    if kind == "hello":
        proto = obj.get("proto")
        if not isinstance(proto, int) or isinstance(proto, bool):
            raise AgentProtocolError(f"hello.proto 必须是整数,收到 {proto!r}")
        return Hello(
            proto=proto,
            sdk=str(obj.get("sdk", "")),
            held=bool(obj.get("held", False)),
            robot=str(obj.get("robot", "")),
        )
    if kind == "ack":
        ack_id = obj.get("id")
        if not isinstance(ack_id, int) or isinstance(ack_id, bool):
            raise AgentProtocolError(f"ack.id 必须是整数,收到 {ack_id!r}")
        return Ack(id=ack_id, ok=bool(obj.get("ok", False)),
                   error=str(obj.get("error", "")))
    if kind == "state":
        return StateFrame(
            motion=_motion(obj.get("motion", 0)),
            battery1=_num(obj, "battery1"),
            battery2=_num(obj, "battery2"),
            estop_software=_emergency(obj.get("estop_sw", 0)),
            estop_hardware=_emergency(obj.get("estop_hw", 0)),
            ts_ms=int(_num(obj, "ts_ms")),
        )
    if kind == "odom":
        return OdomFrame(
            x=_num(obj, "x"), y=_num(obj, "y"), yaw=_num(obj, "yaw"),
            vx=_num(obj, "vx"), vy=_num(obj, "vy"), vyaw=_num(obj, "vyaw"),
            ts_ms=int(_num(obj, "ts_ms")),
        )
    if kind == "fault":
        return FaultFrame(
            level=int(_num(obj, "level")),
            code=int(_num(obj, "code")),
            message=str(obj.get("message", "")),
        )
    if kind == "control_lost":
        return ControlLostFrame(reason=str(obj.get("reason", "")))
    raise AgentProtocolError(f"不认识的帧类型 t={kind!r}")
