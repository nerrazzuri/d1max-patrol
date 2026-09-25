"""七种报文 + 位姿(总设计 §3,W00 字段集见设计 §2)。

这些数据类是契约的**真理源**(决定 3):字段、类型、取值范围都在这里,黄金夹具由
它们生成。每类 ``to_wire()`` 出一个带 ``schema`` 的对象,``from_wire()`` 严格校验 ——
缺字段、类型错、枚举值非法一律 :class:`ContractError`,主版本不同 :class:`SchemaMismatch`。
构造时也验(``__post_init__``):代理自己造出来的报文同样不许违约。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from d1max_contract import SCHEMA
from d1max_contract.errors import ContractError
from d1max_contract.hal import Fault
from d1max_contract.wire import (
    as_bool,
    as_dict,
    as_enum,
    as_float,
    as_int,
    as_opt_dict,
    as_str,
    check_schema,
    need,
)


class AckResult(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"
    PREEMPTED = "preempted"


#: 狗报的事件类型。狗只报事实,判定在站点(总设计 §3.3)。``patrol_waypoint`` 是 W00c2a 的,
#: ``robot_fault`` 是 W00c5a 的(HAL 故障集合变了才发一条;空列表 = 都消了);``video_failed`` 是
#: W00c5b 的(推流进程没到期就退了,``{camera, reason}``)。
EVENT_KINDS = ("task_progress", "task_done", "task_failed", "task_preempted", "task_aborted",
               "patrol_waypoint", "robot_fault", "video_failed")


def fault_event_data(faults: tuple[Fault, ...] | list[Fault]) -> dict[str, Any]:
    """``robot_fault`` 事件的 ``data``。"""
    return {"faults": [{"code": f.code, "fatal": f.fatal, "text": f.text} for f in faults]}


def parse_fault_event_data(data: Any) -> tuple[Fault, ...]:
    """读回 ``robot_fault`` 的 ``data``。形状不对抛 :class:`ContractError`,不猜。"""
    items = data.get("faults") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ContractError("robot_fault: 要有 faults 列表")
    out = []
    for it in items:
        if not (isinstance(it, dict) and isinstance(it.get("code"), str)
                and isinstance(it.get("fatal"), bool) and isinstance(it.get("text"), str)):
            raise ContractError(
                f"robot_fault: 故障项要是 {{code: str, fatal: bool, text: str}}: {it!r}")
        out.append(Fault(code=it["code"], fatal=it["fatal"], text=it["text"]))
    return tuple(out)


def _stamped(d: dict[str, Any]) -> dict[str, Any]:
    return {"schema": SCHEMA, **d}


# ------------------------------------------------------------------ 位姿

@dataclass(frozen=True)
class MapPose:
    """带地图版本的平面位姿。**位姿必带 map_id/map_version/frame_id**(总设计 §3.2):
    与代理已加载地图不一致即拒。"""

    map_id: str
    map_version: str
    frame_id: str
    x: float
    y: float
    yaw: float

    def __post_init__(self) -> None:
        for k in ("map_id", "map_version", "frame_id"):
            if not isinstance(getattr(self, k), str) or not getattr(self, k):
                raise ContractError(f"MapPose: {k} 不许为空")
        for k in ("x", "y", "yaw"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ContractError(f"MapPose: {k} 要是有限数")

    def to_wire(self) -> dict[str, Any]:
        return _stamped({"map_id": self.map_id, "map_version": self.map_version,
                         "frame_id": self.frame_id, "x": self.x, "y": self.y, "yaw": self.yaw})

    @classmethod
    def from_wire(cls, d: Any) -> MapPose:
        d = check_schema(d, "MapPose")
        return cls(map_id=as_str(d, "map_id", "MapPose", nonempty=True),
                   map_version=as_str(d, "map_version", "MapPose", nonempty=True),
                   frame_id=as_str(d, "frame_id", "MapPose", nonempty=True),
                   x=as_float(d, "x", "MapPose"), y=as_float(d, "y", "MapPose"),
                   yaw=as_float(d, "yaw", "MapPose"))


# ------------------------------------------------------------------ 命令与回执

@dataclass(frozen=True)
class Precondition:
    """命令的前置条件。W00 只有一条:``resume``/``abort`` 之类必须指向处于某状态的任务。"""

    expect_task_state: TaskState | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"expect_task_state": self.expect_task_state.value
                if self.expect_task_state else None}

    @classmethod
    def from_wire(cls, d: Any) -> Precondition:
        if not isinstance(d, dict):
            raise ContractError("Command: precondition 要是对象")
        v = need(d, "expect_task_state", "Command.precondition")
        if v is None:
            return cls(None)
        return cls(as_enum(d, "expect_task_state", "Command.precondition", TaskState))


@dataclass(frozen=True)
class Command:
    """站点 → 狗(QoS 1,不 retained)。``command_id`` 每条唯一;``task_id`` 是它作用的任务。"""

    command_id: str
    task_id: str
    kind: str
    issued_at: int
    expires_at: int
    control_epoch: int
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    offline_policy: str = "default"
    precondition: Precondition | None = None

    def __post_init__(self) -> None:
        for k in ("command_id", "task_id", "kind"):
            if not isinstance(getattr(self, k), str) or not getattr(self, k):
                raise ContractError(f"Command: {k} 不许为空")
        for k in ("issued_at", "expires_at", "control_epoch", "priority"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ContractError(f"Command: {k} 要是整数")
        if self.expires_at < self.issued_at:
            raise ContractError("Command: expires_at 不许早于 issued_at")
        if not isinstance(self.payload, dict):
            raise ContractError("Command: payload 要是对象")

    def to_wire(self) -> dict[str, Any]:
        return _stamped({
            "command_id": self.command_id, "task_id": self.task_id, "kind": self.kind,
            "issued_at": self.issued_at, "expires_at": self.expires_at,
            "control_epoch": self.control_epoch, "priority": self.priority,
            "offline_policy": self.offline_policy,
            "precondition": self.precondition.to_wire() if self.precondition else None,
            "payload": dict(self.payload)})

    @classmethod
    def from_wire(cls, d: Any) -> Command:
        d = check_schema(d, "Command")
        # 先验 id 与时刻:不成形的命令也要回得出带 command_id/task_id 的回执,
        # 报错文本里先出现的该是这几个字段。
        command_id = as_str(d, "command_id", "Command", nonempty=True)
        task_id = as_str(d, "task_id", "Command", nonempty=True)
        kind = as_str(d, "kind", "Command", nonempty=True)
        issued_at = as_int(d, "issued_at", "Command")
        expires_at = as_int(d, "expires_at", "Command")
        control_epoch = as_int(d, "control_epoch", "Command")
        priority = as_int(d, "priority", "Command")
        offline_policy = as_str(d, "offline_policy", "Command")
        payload = as_dict(d, "payload", "Command")
        pre = as_opt_dict(d, "precondition", "Command")
        return cls(command_id=command_id, task_id=task_id, kind=kind, issued_at=issued_at,
                   expires_at=expires_at, control_epoch=control_epoch, priority=priority,
                   offline_policy=offline_policy,
                   precondition=Precondition.from_wire(pre) if pre is not None else None,
                   payload=payload)


@dataclass(frozen=True)
class Ack:
    """狗 → 站点的命令回执。``duplicate`` 必带 ``original``(原结果),其他不带;
    ``rejected`` 必带 ``reason``。"""

    command_id: str
    task_id: str
    result: AckResult
    reason: str = ""
    original: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, AckResult):
            raise ContractError("Ack: result 要是 AckResult")
        if self.result is AckResult.DUPLICATE and self.original is None:
            raise ContractError("Ack: duplicate 必须带 original(第一次的结果)")
        if self.result is not AckResult.DUPLICATE and self.original is not None:
            raise ContractError("Ack: 只有 duplicate 才带 original")
        if self.result is AckResult.REJECTED and not self.reason:
            raise ContractError("Ack: rejected 必须给 reason")

    def to_wire(self) -> dict[str, Any]:
        return _stamped({"command_id": self.command_id, "task_id": self.task_id,
                         "result": self.result.value, "reason": self.reason,
                         "original": dict(self.original) if self.original is not None else None})

    @classmethod
    def from_wire(cls, d: Any) -> Ack:
        d = check_schema(d, "Ack")
        return cls(command_id=as_str(d, "command_id", "Ack", nonempty=True),
                   task_id=as_str(d, "task_id", "Ack", nonempty=True),
                   result=as_enum(d, "result", "Ack", AckResult),
                   reason=as_str(d, "reason", "Ack"),
                   original=as_opt_dict(d, "original", "Ack"))


# ------------------------------------------------------------------ 事件

@dataclass(frozen=True)
class Event:
    """狗 → 站点(QoS 1)。站点按 ``(boot_id, seq)`` 去重与排序;``seq`` 从 1 起单调。"""

    event_id: str
    seq: int
    boot_id: str
    stamp: int
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ContractError("Event: event_id 不许为空")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1:
            raise ContractError("Event: seq 要是 ≥1 的整数")
        if not self.boot_id:
            raise ContractError("Event: boot_id 不许为空")
        if not self.kind:
            raise ContractError("Event: kind 不许为空")
        if not isinstance(self.data, dict):
            raise ContractError("Event: data 要是对象")

    def to_wire(self) -> dict[str, Any]:
        return _stamped({"event_id": self.event_id, "seq": self.seq, "boot_id": self.boot_id,
                         "stamp": self.stamp, "kind": self.kind, "data": dict(self.data)})

    @classmethod
    def from_wire(cls, d: Any) -> Event:
        d = check_schema(d, "Event")
        return cls(event_id=as_str(d, "event_id", "Event", nonempty=True),
                   seq=as_int(d, "seq", "Event"),
                   boot_id=as_str(d, "boot_id", "Event", nonempty=True),
                   stamp=as_int(d, "stamp", "Event"),
                   kind=as_str(d, "kind", "Event", nonempty=True),
                   data=as_dict(d, "data", "Event"))


# ------------------------------------------------------------------ 状态与能力

@dataclass(frozen=True)
class Ready:
    """能不能派(总设计 §3.1):控制权在手、运动模式就绪、急停未按、定位可用。"""

    control: bool
    motion: bool
    estop_clear: bool
    loc_ok: bool

    @property
    def ok(self) -> bool:
        return self.control and self.motion and self.estop_clear and self.loc_ok

    def to_wire(self) -> dict[str, Any]:
        return {"control": self.control, "motion": self.motion,
                "estop_clear": self.estop_clear, "loc_ok": self.loc_ok}

    @classmethod
    def from_wire(cls, d: Any) -> Ready:
        if not isinstance(d, dict):
            raise ContractError("Status: ready 要是对象")
        return cls(control=as_bool(d, "control", "Status.ready"),
                   motion=as_bool(d, "motion", "Status.ready"),
                   estop_clear=as_bool(d, "estop_clear", "Status.ready"),
                   loc_ok=as_bool(d, "loc_ok", "Status.ready"))


@dataclass(frozen=True)
class TaskSummary:
    task_id: str
    kind: str
    state: TaskState

    def to_wire(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "kind": self.kind, "state": self.state.value}

    @classmethod
    def from_wire(cls, d: Any) -> TaskSummary:
        if not isinstance(d, dict):
            raise ContractError("task 要是对象")
        return cls(task_id=as_str(d, "task_id", "task", nonempty=True),
                   kind=as_str(d, "kind", "task", nonempty=True),
                   state=as_enum(d, "state", "task", TaskState))


@dataclass(frozen=True)
class Status:
    """可用性(retained;LWT 把它改写为 ``online=false``)。派遣条件见总设计 §3.1。"""

    online: bool
    boot_id: str
    ready: Ready
    control_epoch: int
    last_seen: int
    task: TaskSummary | None

    def to_wire(self) -> dict[str, Any]:
        return _stamped({"online": self.online, "boot_id": self.boot_id,
                         "ready": self.ready.to_wire(), "control_epoch": self.control_epoch,
                         "last_seen": self.last_seen,
                         "task": self.task.to_wire() if self.task else None})

    @classmethod
    def from_wire(cls, d: Any) -> Status:
        d = check_schema(d, "Status")
        task = as_opt_dict(d, "task", "Status")
        return cls(online=as_bool(d, "online", "Status"),
                   boot_id=as_str(d, "boot_id", "Status", nonempty=True),
                   ready=Ready.from_wire(need(d, "ready", "Status")),
                   control_epoch=as_int(d, "control_epoch", "Status"),
                   last_seen=as_int(d, "last_seen", "Status"),
                   task=TaskSummary.from_wire(task) if task is not None else None)


@dataclass(frozen=True)
class Capabilities:
    """代理能力(retained,掉线不清)。由代理**合成**,不是 HAL 能力的照搬(总设计 §3.1)。"""

    robot_id: str
    agent: str
    adapter: str
    tasks: dict[str, dict[str, Any]]
    actuators: dict[str, Any]
    sensing: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return _stamped({"robot_id": self.robot_id, "agent": self.agent, "adapter": self.adapter,
                         "tasks": {k: dict(v) for k, v in self.tasks.items()},
                         "actuators": dict(self.actuators), "sensing": dict(self.sensing)})

    @classmethod
    def from_wire(cls, d: Any) -> Capabilities:
        d = check_schema(d, "Capabilities")
        tasks = as_dict(d, "tasks", "Capabilities")
        for k, v in tasks.items():
            if not isinstance(v, dict):
                raise ContractError(f"Capabilities: tasks.{k} 要是对象")
        return cls(robot_id=as_str(d, "robot_id", "Capabilities", nonempty=True),
                   agent=as_str(d, "agent", "Capabilities", nonempty=True),
                   adapter=as_str(d, "adapter", "Capabilities", nonempty=True),
                   tasks={k: dict(v) for k, v in tasks.items()},
                   actuators=as_dict(d, "actuators", "Capabilities"),
                   sensing=as_dict(d, "sensing", "Capabilities"))


# ------------------------------------------------------------------ 对账与遥测

@dataclass(frozen=True)
class Reconcile:
    """重连后代理发的**第一条**:站点以此为准,不凭历史事件猜(总设计 §3.3)。
    ``unacked_from_seq..unacked_to_seq`` 是可能没送到的事件区间;0..0 表示没有。"""

    boot_id: str
    control_epoch: int
    task: TaskSummary | None
    unacked_from_seq: int
    unacked_to_seq: int

    def __post_init__(self) -> None:
        if self.unacked_to_seq < self.unacked_from_seq:
            raise ContractError("Reconcile: unacked_to_seq 不许小于 unacked_from_seq")

    def to_wire(self) -> dict[str, Any]:
        return _stamped({"boot_id": self.boot_id, "control_epoch": self.control_epoch,
                         "task": self.task.to_wire() if self.task else None,
                         "unacked_from_seq": self.unacked_from_seq,
                         "unacked_to_seq": self.unacked_to_seq})

    @classmethod
    def from_wire(cls, d: Any) -> Reconcile:
        d = check_schema(d, "Reconcile")
        task = as_opt_dict(d, "task", "Reconcile")
        return cls(boot_id=as_str(d, "boot_id", "Reconcile", nonempty=True),
                   control_epoch=as_int(d, "control_epoch", "Reconcile"),
                   task=TaskSummary.from_wire(task) if task is not None else None,
                   unacked_from_seq=as_int(d, "unacked_from_seq", "Reconcile"),
                   unacked_to_seq=as_int(d, "unacked_to_seq", "Reconcile"))


@dataclass(frozen=True)
class Telemetry:
    """1 Hz,QoS 0。W00 最小字段。"""

    stamp: int
    pose: MapPose | None
    battery_pct: float
    task_state: TaskState | None
    loc_quality: float
    net: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return _stamped({"stamp": self.stamp,
                         "pose": self.pose.to_wire() if self.pose else None,
                         "battery_pct": self.battery_pct,
                         "task_state": self.task_state.value if self.task_state else None,
                         "loc_quality": self.loc_quality, "net": dict(self.net)})

    @classmethod
    def from_wire(cls, d: Any) -> Telemetry:
        d = check_schema(d, "Telemetry")
        pose = as_opt_dict(d, "pose", "Telemetry")
        ts = need(d, "task_state", "Telemetry")
        return cls(stamp=as_int(d, "stamp", "Telemetry"),
                   pose=MapPose.from_wire(pose) if pose is not None else None,
                   battery_pct=as_float(d, "battery_pct", "Telemetry"),
                   task_state=as_enum(d, "task_state", "Telemetry", TaskState) if ts is not None
                   else None,
                   loc_quality=as_float(d, "loc_quality", "Telemetry"),
                   net=as_dict(d, "net", "Telemetry"))
