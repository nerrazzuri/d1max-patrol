"""Super-LIO 建图前端(工单 21):位姿 → 雷达轨迹、关键帧、出图要的中间文件。"""

from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from nexwar_localizer import superlio as S


def _yaw_q(yaw: float) -> tuple[float, float, float, float]:
    return (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))


def _line(n: int = 100, dt: float = 0.1, v: float = 0.5) -> list[S.Pose]:
    """IMU 沿 x 匀速走,朝向不变。"""
    return [(1000.0 + k * dt, (k * dt * v, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)) for k in range(n)]


def test_随版本带的参数文件_外参是旋转矩阵_雷达在IMU前上方几厘米():
    R, t = S.extrinsic(S.DEFAULT_CONFIG)
    det = np.linalg.det(np.array(R))
    assert abs(det - 1) < 1e-3
    assert np.allclose(np.array(R) @ np.array(R).T, np.eye(3), atol=1e-3)
    assert 0 < np.linalg.norm(t) < 0.2


def test_外参不是12个数_说清楚哪不对(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("lio.extrinsic.lidar_imu: [0, 0, 0, 1, 0, 0]\n")
    with pytest.raises(S.SuperLioError, match="12 个数"):
        S.extrinsic(f)
    f.write_text("lio.extrinsic.lidar_imu: [0,0,0, 2,0,0, 0,1,0, 0,0,1]\n")
    with pytest.raises(S.SuperLioError, match="旋转矩阵"):
        S.extrinsic(f)


def test_IMU位姿乘外参_得雷达位姿():
    """/lio/odom 发的是 IMU 位姿(源码 ROSWrapper::pub_odom 用 state.p/state.R):
    雷达 = T_wi·T_il。"""
    R_il = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    p, R = S.lidar_pose((1.0, 2.0, 0.0), _yaw_q(math.pi / 2), R_il, (0.1, 0.0, 0.05))
    assert np.allclose(p, (1.0, 2.1, 0.05))                     # IMU 转了 90°:外参的 x 变成 y
    assert np.allclose(R, [[-1, 0, 0], [0, -1, 0], [0, 0, 1]])


def test_插值_两头之外和断档里的不给():
    poses = _line(10)
    p, q = S.pose_at(poses, 1000.05)
    assert p[0] == pytest.approx(0.025)
    assert S.pose_at(poses, 999.0) is None
    gap = [poses[0], (poses[0][0] + 1.0, (0.5, 0, 0), (0, 0, 0, 1))]
    assert S.pose_at(gap, poses[0][0] + 0.5) is None


def test_清洗_丢非有限值_离谱值_时间倒退():
    raw = [(2.0, (0, 0, 0), (0, 0, 0, 2.0)), (1.0, (0, 0, 0), (0, 0, 0, 1)),
           (3.0, (math.nan, 0, 0), (0, 0, 0, 1)), (4.0, (1e6, 0, 0), (0, 0, 0, 1)),
           (2.0, (5, 0, 0), (0, 0, 0, 1)), (5.0, (0, 0, 0), (0, 0, 0, 0))]
    got = S.clean(raw)
    assert [r[0] for r in got] == [1.0, 2.0]
    assert got[1][2] == (0.0, 0.0, 0.0, 1.0)                 # 归一化了


def test_关键帧_隔030米取一帧_点按距离裁_轨迹每帧都有(tmp_path):
    poses = _line(100)                                        # 0.5 m/s,9.9 s,4.95 m
    pts = np.array([[5.0, 0, 0]] * 800 + [[0.2, 0, 0]] * 100 + [[100.0, 0, 0]] * 100)
    clouds = [(1000.0 + k * 0.1, None) for k in range(98)]
    n = S.make_keyframes(poses, clouds, tmp_path, [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                         (0, 0, 0), lambda _: pts)
    assert n["scans"] == 98 and n["traj"] == 98
    assert 15 <= n["keyframes"] <= 17                          # 4.85 m / 0.3 m
    lines = (tmp_path / "kf/index.txt").read_text().split("\n")
    first = lines[0].split()
    assert len(first) == 15 and first[-1] == "00000.bin"
    assert np.fromfile(tmp_path / "kf/00000.bin", "<f4").size == 800 * 3   # 0.2 m、100 m 的裁掉
    tum = (tmp_path / "traj.tum").read_text().splitlines()
    t0, x0 = (float(c) for c in tum[0].split()[:2])
    assert x0 == pytest.approx(0.025)                          # 取在扫描中间时刻(头 + 0.05 s)
    vx = float(lines[1].split()[8])
    assert vx == pytest.approx(0.5, abs=0.01)                  # 雷达系的线速度


def test_转弯也取关键帧():
    poses = [(k * 0.1, (0.0, 0.0, 0.0), _yaw_q(math.radians(2 * k))) for k in range(60)]
    clouds = [(k * 0.1, None) for k in range(59)]
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        n = S.make_keyframes(poses, clouds, Path(d), np.eye(3).tolist(), (0, 0, 0),
                             lambda _: np.ones((600, 3)) * 3)
    assert 10 <= n["keyframes"] <= 13                          # 116° / 10°


def test_质量_首尾相距_最大单步():
    q = S.quality(_line(100))
    assert q["path_m"] == pytest.approx(4.95, abs=0.06)
    assert q["start_end_m"] == pytest.approx(4.95)
    assert q["max_step_m"] == pytest.approx(0.05)


class _Run:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.cmds: list[list[str]] = []

    def __call__(self, cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc)


def test_出中间文件_kf2sm_再sm2mm_记下前端和质量(tmp_path):
    run = _Run()
    clouds = [(1000.0 + k * 0.1, None) for k in range(98)]
    info = S.prepare(tmp_path / "w", Path("bag"), _line(100), cfg=S.DEFAULT_CONFIG,
                     root=Path("/opt/x"), prior_pipeline=Path("/p.yaml"), run=run,
                     clouds=clouds, xyz_of=lambda _: np.ones((900, 3)) * 4, source="live")
    assert run.cmds[0] == ["/opt/x/bin/kf2sm", str(tmp_path / "w/kf"),
                           str(tmp_path / "w/map.simplemap")]
    assert run.cmds[1][0] == "sm2mm" and "/p.yaml" in run.cmds[1]
    assert not (tmp_path / "w/kf").exists()                    # 关键帧几百 MB,用完删
    assert (tmp_path / "w/traj.tum").is_file()
    saved = json.loads((tmp_path / "w/mola.json").read_text())
    assert saved["builder"] == "super-lio" and saved["source"] == "live"
    assert saved["quality"] == info["quality"] and saved["keyframes"] >= 15


def test_轨迹突跳_跟丢了不出图(tmp_path):
    poses = _line(100)
    poses[50] = (poses[50][0], (9.0, 0.0, 0.0), poses[50][2])
    run = _Run()
    with pytest.raises(S.SuperLioError, match="突跳"):
        S.prepare(tmp_path, Path("bag"), poses, cfg=S.DEFAULT_CONFIG, prior_pipeline=Path("p"),
                  run=run, clouds=[], xyz_of=lambda _: None)
    assert run.cmds == []


def test_位姿太少_工具失败_都说清楚(tmp_path):
    with pytest.raises(S.SuperLioError, match="只给了 5 个位姿"):
        S.prepare(tmp_path, Path("b"), _line(5), cfg=S.DEFAULT_CONFIG, prior_pipeline=Path("p"),
                  clouds=[], xyz_of=lambda _: None)
    with pytest.raises(S.SuperLioError, match="关键帧转 simplemap失败"):
        S.prepare(tmp_path, Path("b"), _line(100), cfg=S.DEFAULT_CONFIG,
                  prior_pipeline=Path("p"), run=_Run(1),
                  clouds=[(1000.0 + k * 0.1, None) for k in range(98)],
                  xyz_of=lambda _: np.ones((900, 3)) * 4)


def test_边走边建_发的话题全改到建图命名空间下_不跟竞品和定位器串():
    a = S.node_args(Path("/c.yaml"), ns_remap=True)
    assert a[:3] == ["--ros-args", "--params-file", "/c.yaml"]
    remaps = [a[i + 1] for i, x in enumerate(a) if x == "-r"]
    assert "/lio/odom:=/nexwar_mapping/lio/odom" in remaps
    assert len(remaps) == len(S.PUBLISHED)
    assert "lio.ros.lidar_topic:=/front_lidar" in a and "lio.map.save_map:=false" in a
    assert "-r" not in S.node_args(Path("/c.yaml"), ns_remap=False)
    assert S.LIVE_TOPIC == "/nexwar_mapping/lio/odom"


def test_现场参数优先(tmp_path, monkeypatch):
    site = tmp_path / "superlio.yaml"
    monkeypatch.setattr(S, "SITE_CONFIG", site)
    assert S.config_path() == S.DEFAULT_CONFIG
    site.write_text("x")
    assert S.config_path() == site


def test_没装_起不来_说先编(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(S, "ROOT", tmp_path)
    assert S.main(["live"]) == 2
    assert "nexwar_superlio_build" in capsys.readouterr().out


def test_装好了才算装好(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "ROOT", tmp_path)
    assert not S.installed()
    for f in (".ok", "install/setup.bash", "bin/kf2sm"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("")
    assert S.installed()
    assert S.node_exe() == tmp_path / "install/super_lio/lib/super_lio/super_lio_node"


def _fake_front(monkeypatch, tmp_path, *, live: bool):
    from nexwar_localizer import build
    monkeypatch.setattr(S, "ROOT", tmp_path / "root")
    for f in (".ok", "install/setup.bash", "bin/kf2sm"):
        (tmp_path / "root" / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "root" / f).write_text("")
    seen: dict[str, object] = {}

    def read_odom(bag, topic):
        seen.setdefault("read", []).append((Path(bag).name, topic))
        if topic == S.LIVE_TOPIC and not live:
            raise S.SuperLioError("没有")
        return _line(100)

    def run_offline(bag, work, *, cfg):
        seen["offline"] = cfg
        return work / "lio_bag"

    def prepare(work, bag, poses, **kw):
        seen["prepare"] = (kw["source"], kw["prior_pipeline"], len(poses))
        return {"quality": S.quality(poses), "keyframes": 16, "prepare_s": 1.0}

    monkeypatch.setattr(S, "read_odom", read_odom)
    monkeypatch.setattr(S, "run_offline", run_offline)
    monkeypatch.setattr(S, "prepare", prepare)
    monkeypatch.setattr(build, "_plugin", lambda: [])
    return seen


def test_建图命令_录包里有边走边建的位姿就直接用(tmp_path, monkeypatch):
    from nexwar_localizer import build, tools
    seen = _fake_front(monkeypatch, tmp_path, live=True)
    t = tools._superlio_front(tmp_path / "bag1", tmp_path / "w", reuse=True)
    assert seen["read"] == [("bag1", S.LIVE_TOPIC)] and "offline" not in seen
    assert seen["prepare"] == ("live", build.PRIOR_PIPELINE, 100)
    assert set(t) == {"superlio_s", "prepare_s"}


def test_建图命令_老包没有位姿_回放重算_边走边建的没有就报错(tmp_path, monkeypatch):
    from nexwar_localizer import tools
    seen = _fake_front(monkeypatch, tmp_path, live=False)
    tools._superlio_front(tmp_path / "bag1", tmp_path / "w", reuse=False)
    assert seen["read"][-1] == ("lio_bag", "/lio/odom") and seen["prepare"][0] == "replay"
    with pytest.raises(S.SuperLioError):
        tools._superlio_front(tmp_path / "bag1", tmp_path / "w", reuse=True)


def test_现场1104_录包里的位姿是定位模式下的_要能强制回放重算(tmp_path, monkeypatch):
    """11-04 广场:录包时机器人上的定位器在旧图上定位,录进来的 Super-LIO 位姿不是建图轨迹。"""
    from nexwar_localizer import tools
    seen = _fake_front(monkeypatch, tmp_path, live=True)
    tools._superlio_front(tmp_path / "bag1", tmp_path / "w", reuse=False, replay=True)
    assert ("bag1", S.LIVE_TOPIC) not in seen["read"] and "offline" in seen
    assert seen["prepare"][0] == "replay"


def test_建图命令_强制回放和用录下的位姿不能一起开(tmp_path, capsys):
    from nexwar_localizer import tools
    with pytest.raises(SystemExit):
        tools.main(["build", "--bag", str(tmp_path), "--out", str(tmp_path / "o"),
                    "--front", "superlio", "--reuse", "--replay"])
    assert "not allowed with" in capsys.readouterr().err


def test_建图命令_没装Super_LIO_说先编(tmp_path, monkeypatch):
    from nexwar_localizer import tools
    monkeypatch.setattr(S, "ROOT", tmp_path)
    with pytest.raises(S.SuperLioError, match="nexwar_superlio_build"):
        tools._superlio_front(tmp_path / "b", tmp_path / "w", reuse=False)


def test_外参的9个数按列读_跟Super_LIO一样(tmp_path):
    """Super-LIO 用 ``M3 __R(r_data.data())`` 读(Eigen 默认列优先)。按行读差一个转置,前雷达
    朝下的 +X 会被当成朝上,出来的图是镜像的(11-02 商场 v2 包实跑,打包时手性检查拦下)。"""
    f = tmp_path / "x.yaml"
    f.write_text("lio.extrinsic.lidar_imu: [0, 0, 0,  0, 1, 0,  -1, 0, 0,  0, 0, 1]\n")
    R, _ = S.extrinsic(f)
    assert np.allclose(R, [[0, -1, 0], [1, 0, 0], [0, 0, 1]])


def test_随版本带的外参_雷达X朝下_对应IMU的负Y():
    """Airy 装法:雷达 +X 朝下(单帧里地面在 +X 0.5 m);IMU 的 +Y 朝上(Super-LIO 按重力摆正)。"""
    R, _ = S.extrinsic(S.DEFAULT_CONFIG)
    assert R[1][0] < -0.99


def test_位姿断档里走的路_挨得很近的位姿_都不算突跳():
    """台式机录位姿丢帧:1.8 s 没位姿、走了 1.58 m(0.87 m/s)不是跟丢。"""
    poses = _line(100)
    gap = poses[:50] + [(r[0] + 1.7, (r[1][0] + 0.85, 0.0, 0.0), r[2]) for r in poses[50:]]
    q = S.quality(gap)
    assert q["max_jump_m"] == 0 and q["gaps"] == 1
    S.check(q)
    # 两个位姿只隔 5 ms、差 1.5 cm(v2 包里真有):算速度 3 m/s,不是跳
    close = poses[:50] + [(poses[49][0] + 0.005, (poses[49][1][0] + 0.015, 0.0, 0.0),
                           poses[49][2])] + [(r[0] + 0.05, r[1], r[2]) for r in poses[50:]]
    S.check(S.quality(close))
    poses[50] = (poses[50][0], (9.0, 0.0, 0.0), poses[50][2])
    with pytest.raises(S.SuperLioError, match="突跳"):
        S.check(S.quality(poses))


# ---------------------------------------------------------------- 外审 W67-1
def test_外审_停进程组_组长退了组里剩下的也收掉(tmp_path):
    """组长(exec 的 sleep)收到 SIGINT 退了,忽略 SIGINT 的子进程还在:要一起收掉。"""
    import os
    p = subprocess.Popen(["bash", "-c", "(trap '' INT; sleep 300) & exec sleep 300"],
                         start_new_session=True)
    time.sleep(0.5)
    S._stop(p, grace_s=5)
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(p.pid, 0)


def test_外审_回放时节点或录包提前退了_说哪个和日志(tmp_path):
    dead = subprocess.Popen(["true"])
    dead.wait()
    alive = subprocess.Popen(["sleep", "30"])
    try:
        with pytest.raises(S.SuperLioError, match="Super-LIO.*提前退出.*n.log"):
            S._check_alive([("Super-LIO", dead, tmp_path / "n.log"),
                            ("录位姿", alive, tmp_path / "r.log")])
        S._check_alive([("录位姿", alive, tmp_path / "r.log")])
    finally:
        alive.kill()


def test_外审_位姿只盖住开头一段_不出残缺的图(tmp_path):
    clouds = [(1000.0 + k * 0.1, None) for k in range(1000)]          # 100 s 点云
    with pytest.raises(S.SuperLioError, match="位姿只覆盖"):
        S.prepare(tmp_path, Path("b"), _line(100), cfg=S.DEFAULT_CONFIG,  # 位姿只有前 10 s
                  prior_pipeline=Path("p"), run=_Run(), clouds=clouds,
                  xyz_of=lambda _: np.ones((900, 3)) * 4)


def test_外审_竖直方向一帧跳10米也算突跳():
    poses = _line(100)
    t, p, q = poses[50]
    poses[50] = (t, (p[0], p[1], 10.0), q)
    with pytest.raises(S.SuperLioError, match="突跳"):
        S.check(S.quality(poses))


def test_外审_外参旋转部分要正交_不能带缩放(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("lio.extrinsic.lidar_imu: [0,0,0, 2,0,0, 0,0.5,0, 0,0,1]\n")
    with pytest.raises(S.SuperLioError, match="旋转矩阵"):
        S.extrinsic(f)
    f.write_text("lio.extrinsic.lidar_imu: [0,0,nan, 1,0,0, 0,1,0, 0,0,1]\n")
    with pytest.raises(S.SuperLioError):
        S.extrinsic(f)
