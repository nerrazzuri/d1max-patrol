"""黄金夹具(决定 3):由数据类生成,落在 ``packages/contract/fixtures/*.json``。

Dart(W00c 手机改连站点)与站点用同一份;改了报文,``test_fixtures.py`` 先红,
提示跑 ``python -m d1max_contract.fixtures --write`` 重生成 —— 沿用现有
「Python 夹具先红、Dart 跟着红」机制。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from d1max_contract.geometry import Pose
from d1max_contract.hal import Fault
from d1max_contract.messages import (
    Ack,
    AckResult,
    Capabilities,
    Command,
    Event,
    MapPose,
    Precondition,
    Ready,
    Reconcile,
    Status,
    TaskState,
    TaskSummary,
    Telemetry,
    fault_event_data,
)
from d1max_contract.mission import Action, Mission, MissionWaypoint, Policy
from d1max_contract.registration import Registration
from d1max_contract.storage import StorageFacts
from d1max_contract.teleop import (
    FRAME_TTL_DEFAULT_MS,
    LEASE_TTL_DEFAULT_MS,
    TELEOP_PRIORITY,
    TeleopFrame,
    TeleopLease,
    teleop_grant_payload,
)
from d1max_contract.video import VideoRequest

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

POSE = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=12.5, y=-3.25, yaw=1.5708)


def generate() -> dict[str, dict[str, Any]]:
    task = TaskSummary(task_id="task-0001", kind="goto", state=TaskState.RUNNING)
    ready = Ready(control=True, motion=True, estop_clear=True, loc_ok=True)
    accepted = Ack(command_id="cmd-0001", task_id="task-0001", result=AckResult.ACCEPTED)
    return {
        "map_pose": POSE.to_wire(),
        "command_goto": Command(
            command_id="cmd-0001", task_id="task-0001", kind="goto", issued_at=1_760_000_000_000,
            expires_at=1_760_000_060_000, control_epoch=3, priority=0,
            payload={"target": POSE.to_wire(), "max_speed_mps": 0.8}).to_wire(),
        "command_patrol": Command(
            command_id="cmd-0005", task_id="task-0005", kind="patrol",
            issued_at=1_760_000_100_000, expires_at=1_760_000_160_000, control_epoch=3,
            priority=0, payload={"map_version": "7", "mission": Mission(
                mission="night-loop", map_id="estate-1", policy=Policy(loops=1),
                waypoints=(
                    MissionWaypoint(name="gate", pose=Pose.from_xy_yaw(12.5, -3.25, 1.5708),
                                    actions=(Action(type="photo", camera="front"),)),
                    MissionWaypoint(name="pond", pose=Pose.from_xy_yaw(30.0, 4.0, 0.0),
                                    actions=(Action(type="dwell", seconds=5.0),)),
                )).to_wire()}).to_wire(),
        "event_patrol_waypoint": Event(
            event_id="evt-b1-000012", seq=12, boot_id="boot-b1", stamp=1_760_000_130_000,
            kind="patrol_waypoint",
            data={"task_id": "task-0005", "index": 0, "name": "gate", "ok": True,
                  "note": ""}).to_wire(),
        "event_robot_fault": Event(
            event_id="evt-b1-000013", seq=13, boot_id="boot-b1", stamp=1_760_000_131_000,
            kind="robot_fault",
            data=fault_event_data((Fault(code="7", fatal=True, text="左前腿过流"),))).to_wire(),
        "command_video": Command(
            command_id="cmd-0006", task_id="video-front", kind="video",
            issued_at=1_760_000_200_000, expires_at=1_760_000_210_000, control_epoch=3,
            priority=0, payload=VideoRequest(camera="front", url="srt://10.20.0.1:8890",
                                             passphrase="Q7kP2mX9vL4nR8tW", ttl_ms=10_000
                                             ).to_payload()).to_wire(),
        "event_video_failed": Event(
            event_id="evt-b1-000014", seq=14, boot_id="boot-b1", stamp=1_760_000_205_000,
            kind="video_failed",
            data={"camera": "front", "reason": "推流进程退了: Connection refused"}).to_wire(),
        "teleop_frame": TeleopFrame(lease_epoch=2, seq=41, sent_at=1_760_000_300_000,
                                    ttl_ms=FRAME_TTL_DEFAULT_MS, vx=0.25, wz=-0.3).to_wire(),
        "command_teleop": Command(
            command_id="cmd-0007", task_id="teleop-2", kind="teleop",
            issued_at=1_760_000_299_000, expires_at=1_760_000_329_000, control_epoch=3,
            priority=TELEOP_PRIORITY, payload=teleop_grant_payload(
                lease_epoch=2, operator="gina", lease_ttl_ms=LEASE_TTL_DEFAULT_MS)).to_wire(),
        "command_teleop_lease": Command(
            command_id="cmd-0008", task_id="teleop-2", kind="teleop_lease",
            issued_at=1_760_000_301_000, expires_at=1_760_000_331_000, control_epoch=3,
            priority=TELEOP_PRIORITY,
            payload=TeleopLease(action="renew", lease_epoch=2).to_payload()).to_wire(),
        "command_halt": Command(
            command_id="cmd-0009", task_id="halt-1", kind="halt",
            issued_at=1_760_000_302_000, expires_at=1_760_000_332_000, control_epoch=3,
            priority=TELEOP_PRIORITY, payload={"reason": "operator"}).to_wire(),
        "command_abort": Command(
            command_id="cmd-0002", task_id="task-0001", kind="abort",
            issued_at=1_760_000_010_000, expires_at=1_760_000_070_000, control_epoch=3,
            priority=10, precondition=Precondition(expect_task_state=TaskState.RUNNING),
            payload={"reason": "operator"}).to_wire(),
        "ack_accepted": accepted.to_wire(),
        "ack_rejected": Ack(command_id="cmd-0003", task_id="task-0002",
                            result=AckResult.REJECTED, reason="map_mismatch").to_wire(),
        "ack_expired": Ack(command_id="cmd-0004", task_id="task-0003",
                           result=AckResult.EXPIRED).to_wire(),
        "ack_duplicate": Ack(command_id="cmd-0001", task_id="task-0001",
                             result=AckResult.DUPLICATE, original=accepted.to_wire()).to_wire(),
        "event_progress": Event(event_id="evt-b1-000007", seq=7, boot_id="boot-b1",
                                stamp=1_760_000_012_000, kind="task_progress",
                                data={"task_id": "task-0001", "distance_m": 4.2}).to_wire(),
        "event_aborted": Event(event_id="evt-b1-000009", seq=9, boot_id="boot-b1",
                               stamp=1_760_000_015_000, kind="task_aborted",
                               data={"task_id": "task-0001", "reason": "operator"}).to_wire(),
        "status_online": Status(online=True, boot_id="boot-b1", ready=ready, control_epoch=3,
                                last_seen=1_760_000_012_000, task=task).to_wire(),
        "status_offline_lwt": Status(online=False, boot_id="boot-b1", ready=ready,
                                     control_epoch=3, last_seen=0, task=None).to_wire(),
        "capabilities": Capabilities(
            robot_id="D1MAX-C40011", agent="0.1.0", adapter="sim/0.1.0",
            # ``path``:导航走哪种路(W00c6b);``autonomy``:自主级别(W00c6i)。
            tasks={"goto": {"max_speed_mps": 1.0, "path": "straight", "autonomy": "supervised"},
                   "patrol": {"map_id": "estate-1", "map_version": "7",
                              "autonomy": "supervised"}},
            actuators={"light": [], "siren": False, "speaker": False, "spotlight": False},
            sensing={"lidar": False, "depth": False, "thermal": False, "imu_hz": 0,
                     "joint_effort": False, "foot_force": False}).to_wire(),
        "reconcile": Reconcile(boot_id="boot-b1", control_epoch=3, task=task,
                               unacked_from_seq=6, unacked_to_seq=9).to_wire(),
        "telemetry": Telemetry(stamp=1_760_000_012_000, pose=POSE, battery_pct=87.5,
                               task_state=TaskState.RUNNING, loc_quality=1.0,
                               net={"rssi_dbm": -55},
                               storage=StorageFacts(disk_used_ratio=0.42, outbox_bytes=52_428_800,
                                                    outbox_cap_bytes=21_474_836_480,
                                                    backlog_files=12, backlog_bytes=8_388_608,
                                                    oldest_backlog_s=95)).to_wire(),
        "registration": Registration(site_id="penang-1", robot_id="D1MAX-C40011",
                                     credential_fingerprint="sha256:0123456789abcdef",
                                     issued_at=1_759_000_000_000,
                                     expires_at=1_790_000_000_000).to_wire(),
    }


def render(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write(dest: Path = FIXTURES_DIR) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name, d in generate().items():
        p = dest / f"{name}.json"
        p.write_text(render(d), encoding="utf-8")
        out.append(p)
    return out


if __name__ == "__main__":  # pragma: no cover
    if "--write" in sys.argv:
        for p in write():
            print(p)
    else:
        print(render(generate()))
