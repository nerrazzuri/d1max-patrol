#!/usr/bin/env python3
"""按现场姿势，用真正的 SidecarDeviceBackend 客户端对着 SimAgentServer 跑一遍命令流。

验证"发命令的姿势"：连上旁路进程口 → 核对握权 → 读遥测 → 站 → 走 → 趴 →
急停开关 → 本端释放（不真交还）。全程走 patrol_agent 的 TCP/JSONL 线协议，
不需要真机、也不需要编译 patrol_agent —— SimAgentServer 就是它 TCP 口的契约仿真。

跑法（在装了本项目的环境里；若 shell 里 source 过 ROS，先清 PYTHONPATH）：
    python scripts/demo_sidecar_sim.py
    env -u PYTHONPATH ./.venv/bin/python scripts/demo_sidecar_sim.py
"""
from __future__ import annotations

import asyncio
import contextlib

from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
from d1max_patrol.protocol.agent_frames import MotionStatus
from d1max_sim.agent_server import SimAgentServer


async def show(be: SidecarDeviceBackend, tag: str) -> None:
    await asyncio.sleep(0.35)  # 给遥测帧一点时间刷新
    with contextlib.suppress(Exception):
        ms = await be.motion_status()
        batt = await be.battery()
        print(f"    └─ [{tag}] motion={ms.value}  batt={batt:.0f}%")


async def main() -> None:
    sim = SimAgentServer(port=0)
    await sim.start()
    print(f"[sim] SimAgentServer 监听 127.0.0.1:{sim.port}")

    be = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=5.0)
    await be.connect()
    print(f"[1] connect()          connected={be.connected}  hello={be.hello}")

    # 等第一帧状态
    for _ in range(50):
        if be.last_state is not None:
            break
        await asyncio.sleep(0.02)
    await show(be, "初始")

    # 现场姿势：客户端只“核对”旁路进程已握权（真正 TakeControl 是它抢窗口时做的）
    await be.acquire_control()
    print(f"[2] acquire_control()  has_control={await be.has_control()}")

    print("[3] stand()")
    await be.stand()
    with contextlib.suppress(Exception):
        await be.wait_motion(MotionStatus.STAND_UP, timeout_s=5.0)
    await show(be, "站起后")

    print("[4] walk(seconds=1.0, forward=0.4)")
    await be.walk(seconds=1.0, forward=0.4)
    await show(be, "行走后")

    print("[5] lie()")
    await be.lie()
    with contextlib.suppress(Exception):
        await be.wait_motion(MotionStatus.LIE_DOWN, timeout_s=5.0)
    await show(be, "趴下后")

    print("[6] emergency_stop(True) → emergency_stop(False)")
    await be.emergency_stop(True)
    await asyncio.sleep(0.2)
    await be.emergency_stop(False)
    await show(be, "急停演示后")

    # 本端释放（故意不真交还 SDK 控制权，见 sidecar release_control 注释）
    await be.release_control()
    print(f"[7] release_control()  has_control={await be.has_control()} (本端标记，不真交还)")

    await be.close()
    await sim.stop()
    print("[done] 命令流全程跑通，姿势 OK。")


if __name__ == "__main__":
    asyncio.run(main())
