"""W00d 决定四 A 的两件真机工具:在开发机上用仿真旁路跑一遍,真机上**本次不执行**。

- ``tools/w00d_probe.py``:不动机器,只读(连 ``hold`` 都不发)。
- ``tools/w00d_motion_check.py``:不带 ``--i-am-present`` 连都不连;带了才动,每步都停稳,
  结果与建议的换算系数写进日志目录。"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.protocol.agent_frames import MotionStatus as SdkMotion
from d1max_sim.agent_server import SimAgentServer

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def 仿真旁路():
    loop = LoopBridge()
    loop.start()
    sim = SimAgentServer(port=0)
    loop.call(sim.start)
    yield sim
    loop.call(sim.stop)
    loop.stop()


def _空端口() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_探针只读_报版本控制权姿态电量里程(仿真旁路, tmp_path):
    probe = _load("w00d_probe")
    rc = probe.main(["--sidecar", f"127.0.0.1:{仿真旁路.port}", "--watch-s", "0.5",
                     "--log-dir", str(tmp_path)])
    [log] = tmp_path.glob("w00d-probe-*.json")
    got = json.loads(log.read_text(encoding="utf-8"))
    assert rc == 0 and got["ok"], got
    assert got["hello"]["proto"] == got["proto_want"] == 3 and got["held"] is True
    assert got["sdk_motion"] == "LieDown" and got["hal_motion"] == "lying"
    assert got["battery"] == 71.0 and got["estop"] is False and got["odom"]["valid"]
    assert got["odom_hz"] > 5
    assert got["estop_raw"] == {"software": "Recover", "hardware": "Recover"}
    assert got["odom_speed_max"] == {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}
    assert [c for c, _ in 仿真旁路.commands] == [], "探针一条命令都不发"


def test_探针连不上就如实报_退1(tmp_path):
    probe = _load("w00d_probe")
    rc = probe.main(["--sidecar", f"127.0.0.1:{_空端口()}", "--log-dir", str(tmp_path)])
    [log] = tmp_path.glob("w00d-probe-*.json")
    got = json.loads(log.read_text(encoding="utf-8"))
    assert rc == 1 and got["ok"] is False and got["error"]


def test_运动检查不带在场声明_连都不连(仿真旁路, tmp_path, capsys):
    mc = _load("w00d_motion_check")
    rc = mc.main(["--sidecar", f"127.0.0.1:{仿真旁路.port}", "--log-dir", str(tmp_path)])
    assert rc == 2 and "--i-am-present" in capsys.readouterr().err
    assert 仿真旁路.commands == [] and list(tmp_path.iterdir()) == []


def test_运动检查的速度与时长有硬上限(tmp_path):
    mc = _load("w00d_motion_check")
    for bad in (["--fwd-fraction", "0.4"], ["--fwd-fraction", "-0.3"], ["--turn-fraction", "-0.5"],
                ["--turn-fraction", "nan"], ["--fwd-s", "10"], ["--deadband-fraction", "0.4"]):
        with pytest.raises(SystemExit):
            mc.main(["--i-am-present", "--log-dir", str(tmp_path), *bad])


def test_运动检查_趴着就不动(仿真旁路, tmp_path):
    mc = _load("w00d_motion_check")
    rc = mc.main(["--i-am-present", "--sidecar", f"127.0.0.1:{仿真旁路.port}",
                  "--log-dir", str(tmp_path)])
    got = json.loads(next(tmp_path.glob("w00d-motion-*.json")).read_text(encoding="utf-8"))
    assert rc == 1 and "先站起" in got["error"]
    assert "vel" not in [c for c, _ in 仿真旁路.commands]


def test_运动检查_默认比例_五步走完_给出换算建议(仿真旁路, tmp_path):
    """仿真旁路里比例 1.0 折 1.2 m/s、1.5 rad/s,低于比例 0.2 不动。工具按比例值发命令,**默认参数**
    下不经过任何未实测的系数,建议值就该接近仿真的真值;死区那一步不动。"""
    仿真旁路.motion = SdkMotion.GENERAL
    mc = _load("w00d_motion_check")
    rc = mc.main(["--i-am-present", "--sidecar", f"127.0.0.1:{仿真旁路.port}",
                  "--turn-s", "1", "--fwd-s", "1", "--log-dir", str(tmp_path)])
    got = json.loads(next(tmp_path.glob("w00d-motion-*.json")).read_text(encoding="utf-8"))
    assert rc == 0 and got["ok"], got
    s = got["steps"]
    assert s["turn"]["direction_ok"] is True
    assert s["stop"]["stop_to_still_s"] is not None and s["ttl"]["send_to_still_s"] >= 0.5
    assert s["deadband"]["moved_m"] < 0.01
    assert abs(got["suggest"]["mps_per_unit"] - 1.2) < 0.3, got["suggest"]
    assert abs(got["suggest"]["radps_per_unit"] - 1.5) < 0.4, got["suggest"]
    assert got["suggest"]["invert_yaw"] is False
    vels = [a for c, a in 仿真旁路.commands if c == "vel"]
    assert max(abs(a["fwd"]) for a in vels) <= 0.3 + 1e-9, "发出去的比例不超过要求的比例"
    assert "shutdown" not in [c for c, _ in 仿真旁路.commands]
    assert 仿真旁路.commands[-1][0] == "halt", "最后一条是停车"
