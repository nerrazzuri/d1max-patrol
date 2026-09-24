"""能力合成、状态与遥测(总设计 §3.1、§3.3)。

**能力是合成出来的**:HAL 能力 × 软件 × 已加载地图。W00 里只有 ``goto``,而且
只有在有地图、HAL 有速度接口时才声明它。有速度接口不等于支持安全导航 —— 那是后面
工单加了避障之后才能声明的 ``patrol``。
"""

from __future__ import annotations

from typing import Any

from d1max_agent import AGENT_VERSION
from d1max_contract.hal import Battery, HalCapabilities, Health, MotionStatus, Odometry
from d1max_contract.messages import (
    Capabilities,
    MapPose,
    Ready,
    Status,
    TaskState,
    TaskSummary,
    Telemetry,
)


def compose_capabilities(*, robot_id: str, hal_caps: HalCapabilities, adapter_id: str,
                         loaded_map: tuple[str, str] | None,
                         agent_version: str = AGENT_VERSION) -> Capabilities:
    tasks: dict[str, dict[str, Any]] = {}
    if loaded_map is not None and hal_caps.max_vx > 0:
        tasks["goto"] = {"max_speed_mps": hal_caps.max_vx}
    return Capabilities(
        robot_id=robot_id, agent=agent_version, adapter=adapter_id, tasks=tasks,
        actuators={"light": [], "siren": bool(hal_caps.actuators.get("siren")),
                   "speaker": bool(hal_caps.actuators.get("speaker")),
                   "spotlight": bool(hal_caps.actuators.get("spotlight"))},
        sensing={"lidar": bool(hal_caps.sensing.get("lidar")),
                 "depth": bool(hal_caps.sensing.get("depth")),
                 "thermal": bool(hal_caps.sensing.get("thermal")),
                 "imu_hz": 0,
                 "joint_effort": bool(hal_caps.sensing.get("joint_effort")),
                 "foot_force": bool(hal_caps.sensing.get("foot_force"))})


def compose_ready(health: Health, motion: MotionStatus) -> Ready:
    return Ready(control=health.control, motion=motion is MotionStatus.READY,
                 estop_clear=not health.estop, loc_ok=health.loc_quality > 0.0)


def compose_status(*, online: bool, boot_id: str, ready: Ready, control_epoch: int,
                   now_ms: int, task: TaskSummary | None) -> Status:
    return Status(online=online, boot_id=boot_id, ready=ready, control_epoch=control_epoch,
                  last_seen=now_ms, task=task)


def offline_status(*, boot_id: str, control_epoch: int) -> Status:
    """LWT 用的那一份:broker 代发,内容在连接时就定了,所以 ready 全 false、last_seen 0。"""
    return Status(online=False, boot_id=boot_id,
                  ready=Ready(control=False, motion=False, estop_clear=False, loc_ok=False),
                  control_epoch=control_epoch, last_seen=0, task=None)


def compose_telemetry(*, now_ms: int, odom: Odometry, battery: Battery, health: Health,
                      loaded_map: tuple[str, str] | None, task_state: TaskState | None,
                      online: bool) -> Telemetry:
    pose = None
    if loaded_map is not None and odom.valid:
        pose = MapPose(map_id=loaded_map[0], map_version=loaded_map[1], frame_id="map",
                       x=odom.x, y=odom.y, yaw=odom.yaw)
    return Telemetry(stamp=now_ms, pose=pose, battery_pct=battery.percent, task_state=task_state,
                     loc_quality=health.loc_quality, net={"online": online})
