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
)
from d1max_contract.registration import Registration

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
            tasks={"goto": {"max_speed_mps": 1.0}},
            actuators={"light": [], "siren": False, "speaker": False, "spotlight": False},
            sensing={"lidar": False, "depth": False, "thermal": False, "imu_hz": 0,
                     "joint_effort": False, "foot_force": False}).to_wire(),
        "reconcile": Reconcile(boot_id="boot-b1", control_epoch=3, task=task,
                               unacked_from_seq=6, unacked_to_seq=9).to_wire(),
        "telemetry": Telemetry(stamp=1_760_000_012_000, pose=POSE, battery_pct=87.5,
                               task_state=TaskState.RUNNING, loc_quality=1.0,
                               net={"rssi_dbm": -55}).to_wire(),
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
