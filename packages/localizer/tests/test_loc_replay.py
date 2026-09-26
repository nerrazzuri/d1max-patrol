"""录包回放出指标(W09b 决定 4):读 MOLA 的输出、按到达时刻喂定位核心与代理、算指标。合成数据,
不跑 MOLA。"""

from __future__ import annotations

import json
import math

from d1max_localizer import replay, tools
from d1max_localizer.frames import Frames, mat_to_quat

FLAT = Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
              sensor_height=0.0)


def _q(yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return mat_to_quat(((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0)))


def _frames(n=300, *, jump_at=(), low_q=(), v=0.5):
    """10 Hz、沿 x 走;``jump_at`` 里的帧往 y 偏 3 m(跳);``low_q`` 里的帧质量低。"""
    out = []
    for i in range(n):
        y = 3.0 if i in jump_at else 0.0
        out.append(replay.Frame(stamp=100.0 + 0.1 * i, p=(v * 0.1 * i, y, 0.0), q=_q(0.0),
                                quality=0.3 if i in low_q else 0.97, proc_s=0.04))
    return out


def test_读_MOLA_的输出_位姿按时间戳配上质量与耗时(tmp_path):
    (tmp_path / "traj.tum").write_text("100.0 1 2 0 0 0 0 1\n100.1 1.1 2 0 0 0 0 1\n")
    (tmp_path / "traces.csv").write_text(
        '"icp_quality","time_onLidar","timestamp",\n0.9,0.041,100.0,\n')
    fs = replay.read_run(tmp_path)
    assert [(f.stamp, f.p[0], f.quality, f.proc_s) for f in fs] == [(100.0, 1.0, 0.9, 0.041),
                                                                     (100.1, 1.1, 0.0, 0.0)]


def test_好好跟住的一段_代理大多时间信_误差为零():
    fr = _frames()
    msgs = replay.simulate(fr, FLAT, (0.0, 0.0, 0.0))
    view = replay.agent_view(msgs)
    ref = [(f.stamp, f.p, f.q) for f in fr]
    m = replay.metrics(fr, msgs, view, FLAT, reference=ref)
    assert m["frames"] == 300 and m["rate_hz"] == 10.0 and m["jumps"] == 0
    assert m["proc_ms"]["p50"] == 40.0
    assert m["agent_trusted_share"] > 0.95
    assert m["error_m"]["all"]["max"] == 0.0 and m["error_m"]["trusted"]["n"] > 280
    assert m["localizer_states"].get("tracking", 0) > 0.95


def test_来回跳的一段_定位器报丢_代理不信_时段与原因列出来():
    fr = _frames(jump_at={100, 102, 104})
    msgs = replay.simulate(fr, FLAT, (0.0, 0.0, 0.0))
    view = replay.agent_view(msgs)
    m = replay.metrics(fr, msgs, view, FLAT)
    assert m["jumps"] >= 3
    assert m["agent_trusted_share"] < 0.9
    assert any("来回跳" in i["why"] for i in m["agent_untrusted"]), m["agent_untrusted"]
    assert any(9.5 <= i["from_s"] <= 10.5 for i in m["agent_untrusted"])
    md = replay.report_md("合成", m)
    assert "代理判可信的时间占比" in md and "来回跳" in md


def test_参考轨迹在别的系里_按时间对齐之后再算误差():
    fr = _frames()
    th, tx, ty = 0.7, 5.0, -3.0
    c, s = math.cos(th), math.sin(th)
    ref = []
    for f in fr:                                           # 参考 = 同一条路,换了个系
        x, y = f.p[0], f.p[1]
        ref.append((f.stamp, (c * x - s * y + tx, s * x + c * y + ty, 0.0), _q(th)))
    msgs = replay.simulate(fr, FLAT, (0.0, 0.0, 0.0))
    view = replay.agent_view(msgs)
    raw = replay.metrics(fr, msgs, view, FLAT, reference=ref)
    assert raw["error_m"]["all"]["p50"] > 1.0, "不对齐:差得远"
    m = replay.metrics(fr, msgs, view, FLAT, reference=ref, reference_same_frame=False)
    assert m["error_m"]["aligned"] and m["error_m"]["all"]["max"] < 1e-6


def test_平面刚体对齐求得回变换():
    a = [(0.0, 0.0), (1.0, 0.0), (1.0, 2.0), (-3.0, 1.0)]
    th, tx, ty = -1.1, 2.0, 0.5
    c, s = math.cos(th), math.sin(th)
    b = [(c * x - s * y + tx, s * x + c * y + ty) for x, y in a]
    got = replay.align2d(a, b)
    assert all(math.isclose(u, v, abs_tol=1e-9) for u, v in zip(got, (th, tx, ty), strict=True))


def test_MOLA_命令行的初值由平面位姿反算(tmp_path):
    f = FLAT.with_sensor_in_base(0.4, 0.0)
    f.save(tmp_path / "frames.json")
    env, cmd = replay.mola_env_and_cmd(tmp_path / "bag", tmp_path, (1.0, 2.0, 0.5),
                                       tmp_path / "out", only_first_n=100)
    assert env["MOLA_MAPPING_ENABLED"] == "false"
    assert env["MOLA_LOAD_MM"] == str(tmp_path / "prior.mm")
    assert float(env["MOLA_INITIAL_X"]) == round(1.0 + 0.4 * math.cos(0.5), 4)
    assert float(env["MOLA_INITIAL_Y"]) == round(2.0 + 0.4 * math.sin(0.5), 4)
    assert math.isclose(float(env["MOLA_INITIAL_YAW"]), math.degrees(0.5), abs_tol=1e-3)
    assert "--only-first-n" in cmd and cmd[cmd.index("--only-first-n") + 1] == "100"
    assert cmd[cmd.index("--input-rosbag2") + 1] == str(tmp_path / "bag")


def test_初值偏了_多久对上(tmp_path):
    base = {round((100.0 + 0.1 * i) * 1000): (0.05 * i, 0.0, 0.0) for i in range(100)}
    run = []
    for i in range(100):                                   # 开头偏 2 m,第 30 帧起对上
        off = 2.0 if i < 30 else 0.0
        run.append(replay.Frame(stamp=100.0 + 0.1 * i, p=(0.05 * i + off, 0.0, 0.0), q=_q(0.0),
                                quality=0.9, proc_s=0.0))
    r = tools.sweep_row(base, run, FLAT)
    assert r["converged"] and r["first_s"] == 3.0 and r["final_m"] == 0.0 and r["max_m"] == 2.0
    never = [replay.Frame(stamp=f.stamp, p=(f.p[0] + 2.0, 0.0, 0.0), q=f.q, quality=0.9,
                          proc_s=0.0) for f in run]
    r = tools.sweep_row(base, never, FLAT)
    assert not r["converged"] and r["final_m"] == 2.0 and r["first_s"] is None


def test_标定子命令写出_frames_json(tmp_path):
    lines = []
    for i in range(50):                                    # 雷达系就是水平系、朝 x 走
        lines.append(f"{100 + 0.1 * i} {0.1 * i} 0 0.6 0 0 0 1")
    (tmp_path / "traj.tum").write_text("\n".join(lines) + "\n")
    assert tools.main(["calibrate", str(tmp_path / "traj.tum"), "--out",
                       str(tmp_path / "frames.json"), "--sensor-up", "0,0,1",
                       "--forward-hint", "1,0,0", "--sensor-in-base", "0.4,0"]) == 0
    d = json.loads((tmp_path / "frames.json").read_text())
    assert d["sensor_in_base"] == [0.4, 0.0] and round(d["sensor_height"], 6) == 0.6
    assert [round(c, 6) for c in d["sensor_forward"]] == [1.0, 0.0, 0.0]
