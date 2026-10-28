"""工单 1(2026-10-28 3z.13):``systemctl stop d1max-localizer`` → ``terminate called without an
active exception``、ABRT,systemd 记 failed。台式机复现:旧代码连起连停 20 次 abort 9 次。
真起 rclpy(要装了 ROS 的机器,CI 上跳过)。"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROS = Path("/opt/ros/humble/setup.bash")
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(not ROS.is_file(), reason="没装 ROS")
def test_工单1_收到停止信号_退出码0_不abort(tmp_path):
    env_py = f"{ROOT}/packages/localizer/src:{ROOT}/packages/contract/src"
    for i in range(8):
        cmd = (f"source {ROS} && export PYTHONPATH={env_py}:${{PYTHONPATH:-}} && "
               f"exec /usr/bin/python3 -m d1max_localizer.main --socket {tmp_path}/none-{i} "
               "--log-level ERROR")
        p = subprocess.Popen(["/bin/bash", "-c", cmd], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, env={k: v for k, v in os.environ.items()
                                                         if k != "PYTHONPATH"})
        time.sleep(1.5)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=20)
        assert p.returncode == 0, (i, p.returncode, err.decode(errors="replace")[-300:])
