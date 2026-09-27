"""建图打包(W09c1):MOLA 建出来的三样 → 地图版本的文件。MOLA 的命令换成假的:``sm2mm``/``mm2ply``
按合成的屋子写二进制点云,``mm-kf-regroup`` 抄文件。雷达两种装法(X 朝上、X 朝下)都要标对「上」。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from d1max_localizer import build as B  # noqa: E402
from d1max_localizer import grid as G  # noqa: E402
from d1max_localizer.frames import Frames, mat_to_quat  # noqa: E402


def _Rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _world(upside_down):
    """水平系(地图平面 + 高度)→ MOLA 系(建图起点那一刻的雷达系)的旋转,以及雷达系里「前、左、上」
    三根轴。正装:雷达 X 朝上、Z 朝前;倒装:X 朝下、Z 朝前。"""
    fwd = np.array([0.0, 0.0, 1.0])
    up = np.array([-1.0, 0.0, 0.0]) if upside_down else np.array([1.0, 0.0, 0.0])
    left = np.cross(up, fwd)
    B_ = np.c_[fwd, left, up]                             # 雷达系里的 前、左、上(当列)
    R0 = _Rz(0.0) @ B_.T                                  # 起点那一刻:雷达 → 水平系(朝 x)
    return R0.T, B_                                       # 水平系 → MOLA 系(= 起点雷达系)


def _scene(tmp, upside_down):
    W2M, B_ = _world(upside_down)
    rng = np.random.default_rng(1)
    pts = []
    for h in np.linspace(0.0, 2.4, 13):                   # 10 × 6 m 的屋子,地面在 z=0
        xs, ys = np.arange(0, 10, 0.02), np.arange(0, 6, 0.02)
        pts += [np.c_[xs, np.zeros_like(xs), np.full_like(xs, h)],
                np.c_[xs, np.full_like(xs, 6.0), np.full_like(xs, h)],
                np.c_[np.zeros_like(ys), ys, np.full_like(ys, h)],
                np.c_[np.full_like(ys, 10.0), ys, np.full_like(ys, h)]]
    fl = rng.uniform([0, 0], [10, 6], (60000, 2))
    pts.append(np.c_[fl, np.zeros(len(fl))])
    P = np.vstack(pts) - np.array([1.0, 3.0, 0.6])        # 以建图起点(雷达离地 0.6 m)为原点
    lines = []
    for i in range(120):                                  # 雷达沿屋子中线走,偶尔转个头
        x, yaw = 0.07 * i, 0.3 * math.sin(i / 15)
        R = W2M @ _Rz(yaw) @ B_.T                         # 雷达系 → 水平系 → MOLA 系
        p = W2M @ np.array([x, 0.0, 0.0])
        q = mat_to_quat(tuple(tuple(r) for r in R))
        lines.append(f"{100 + 0.1 * i:.2f} {p[0]} {p[1]} {p[2]} {q[0]} {q[1]} {q[2]} {q[3]}")
    work = tmp / "work"
    work.mkdir()
    (work / "traj.tum").write_text("\n".join(lines) + "\n")
    (work / "raw_prior.mm").write_bytes(b"prior")
    (work / "map.simplemap").write_bytes(b"sm")
    return work, (P @ W2M.T).astype("<f4")


class 假MOLA:
    def __init__(self, points, fail=""):
        self.points, self.fail, self.calls = points, fail, []

    def __call__(self, cmd, env=None, stdout=None, stderr=None):
        self.calls.append(cmd)
        if self.fail and cmd[0] == self.fail:
            stdout.write("出错了:内存不够\n")
            return SimpleNamespace(returncode=2)
        if cmd[0] == "mm2ply":
            out = cmd[cmd.index("-o") + 1]
            head = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(self.points)}\n"
                    "property float x\nproperty float y\nproperty float z\nend_header\n").encode()
            with open(out + "_raw.ply", "wb") as fh:
                fh.write(head + self.points.tobytes())
        if cmd[0] == "mm-kf-regroup":
            src, dst = cmd[cmd.index("-i") + 1], cmd[cmd.index("-o") + 1]
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read() + b"-regrouped")
        return SimpleNamespace(returncode=0)


@pytest.mark.parametrize("upside_down", [False, True])
def test_打包出版本的文件_上下装都标对_栅格跟定位同一个平面(tmp_path, upside_down):
    work, pts = _scene(tmp_path, upside_down)
    out = tmp_path / "out"
    files = B.package(work, out, run=假MOLA(pts), source="bag:t-1")
    assert sorted(files) == sorted(B.FILES)
    assert all((out / f).is_file() for f in files)
    f = Frames.load(out / "frames.json")
    assert f.sensor_in_base == B.FRONT_LIDAR_IN_BASE
    q = G.level(pts.astype(float)[::50], f)
    sensor_h = f.sensor_height
    floor = np.median(q[np.abs(q[:, 2] - (sensor_h - 0.6)) < 0.05][:, 2])
    assert floor < sensor_h, "地面在雷达下面"
    b = json.loads((out / "build.json").read_text())
    assert ("倒装" in b["frames"]) == upside_down, b["frames"]
    assert b["source"] == "bag:t-1" and b["prior_pack"] == "none"
    assert (out / "prior.mm").read_bytes() == b"prior"
    cov = json.loads((out / "coverage.json").read_text())["path"]
    assert 15 <= len(cov) <= 20, "走了 8.3 m、每 0.5 m 一点"
    txt = (out / "floor.yaml").read_text()
    ox, oy = (float(v) for v in txt.split("origin: [")[1].split("]")[0].split(",")[:2])
    img = (out / "floor.pgm").read_bytes().split(b"\n255\n", 1)[1]
    w = int((out / "floor.pgm").read_bytes().split(b"\n")[1].split()[0])
    h = len(img) // w
    grid = np.frombuffer(img, np.uint8).reshape(h, w)
    g = G.Grid(image=grid, origin=(ox, oy), res=0.05)
    for x, y in cov[::3]:                                 # 走过的路(狗身中心)落在可通行格子上
        assert grid[g.cell_of(x, y)] == G.FREE, (x, y)
    # 不是镜像:狗在世界里往左转头(朝向增大),地图平面上的朝向也要增大 —— 标反了「上」的话正好反号
    from d1max_localizer.replay import read_tum
    yaws = np.unwrap([f.to_map2d(p, q)[2] for _, p, q in read_tum(work / "traj.tum")])
    world = [0.3 * math.sin(i / 15) for i in range(120)]
    assert np.corrcoef(yaws, world)[0, 1] > 0.99


def test_先验要压缩就调合并工具_参数写进_build_json(tmp_path):
    work, pts = _scene(tmp_path, False)
    fake = 假MOLA(pts)
    B.package(work, tmp_path / "out", run=fake, prior_pack=B.PriorPack.parse("regroup:0.3:0.5"))
    [cmd] = [c for c in fake.calls if c[0] == "mm-kf-regroup"]
    assert cmd[cmd.index("--decimate-voxel") + 1] == "0.3"
    assert cmd[cmd.index("--extent-factor") + 1] == "0.5"
    assert json.loads((tmp_path / "out" / "build.json").read_text())["prior_pack"] == \
        "regroup:0.3:0.5"


def test_哪一步不成_说是哪一步_带日志尾巴(tmp_path):
    work, pts = _scene(tmp_path, False)
    with pytest.raises(B.BuildError, match="simplemap 转点云失败.*内存不够"):
        B.package(work, tmp_path / "out", run=假MOLA(pts, fail="sm2mm"))
    (work / "traj.tum").unlink()
    with pytest.raises(B.BuildError, match="没出 traj.tum"):
        B.package(work, tmp_path / "out", run=假MOLA(pts))


def test_先验打包的写法():
    assert B.PriorPack.parse("none") == B.PriorPack()
    assert B.PriorPack.parse("regroup:0.3:0.5").label() == "regroup:0.3:0.5"
    for bad in ("regroup", "regroup:0:1", "regroup:0.3:9", "zip"):
        with pytest.raises(ValueError):
            B.PriorPack.parse(bad)


def test_MOLA_建图的命令(tmp_path):
    fake = 假MOLA(None)
    B.run_mapping(tmp_path / "bag", tmp_path / "w", run=fake)
    [cmd] = fake.calls
    assert cmd[0] == "mola-lidar-odometry-cli"
    assert cmd[cmd.index("--output-simplemap") + 1] == str(tmp_path / "w" / "map.simplemap")
    assert cmd[cmd.index("--output-tum-path") + 1] == str(tmp_path / "w" / "traj.tum")


def test_打包出的文件表就是契约里的那张():
    from d1max_contract.maps import GEOMETRY_FILES
    assert B.FILES == GEOMETRY_FILES


def test_有录包就用逐帧扫描打真射线_读不了退回模拟射线_写进build_json(tmp_path):
    work, pts = _scene(tmp_path, False)
    got = []

    def 读扫描(bag, traj, topic):
        got.append((bag, len(traj), topic))
        _, p, _ = traj[0]
        yield np.array(p), pts[::10]

    B.package(work, tmp_path / "out", run=假MOLA(pts), bag=tmp_path / "bag", read_scans=读扫描)
    assert got == [(tmp_path / "bag", 120, "/front_lidar")]
    assert json.loads((tmp_path / "out" / "build.json").read_text())["grid"]["rays"] == "scans:1"

    def 没有ROS(bag, traj, topic):
        raise ImportError("No module named 'rosbag2_py'")
        yield

    B.package(work, tmp_path / "out2", run=假MOLA(pts), bag=tmp_path / "bag", read_scans=没有ROS)
    rays = json.loads((tmp_path / "out2" / "build.json").read_text())["grid"]["rays"]
    assert rays.startswith("synthetic") and "rosbag2_py" in rays
    B.package(work, tmp_path / "out3", run=假MOLA(pts))
    assert json.loads((tmp_path / "out3" / "build.json").read_text())["grid"]["rays"] == \
        "synthetic:没给录包"


def test_点云消息按字段偏移取xyz_丢掉NaN():
    names = ("x", "y", "z", "intensity", "ring")
    dt = np.dtype({"names": list(names), "formats": ["<f4", "<f4", "<f4", "<f4", "<u2"],
                   "offsets": [0, 4, 8, 16, 20], "itemsize": 32})
    a = np.zeros(3, dt)
    a["x"], a["y"], a["z"] = [1, 2, np.nan], [3, 4, 5], [6, 7, 8]
    msg = SimpleNamespace(fields=[SimpleNamespace(name=n, offset=o)
                                  for n, o in zip(names, (0, 4, 8, 16, 20), strict=True)],
                          point_step=32, data=a.tobytes(), is_bigendian=False)
    assert B.cloud_xyz(msg).tolist() == [[1, 3, 6], [2, 4, 7]]


@pytest.mark.skipif(not Path("/opt/ros/humble/setup.sh").is_file(), reason="要 ROS")
def test_从真的mcap录包读逐帧扫描_按轨迹时刻配位姿(tmp_path):
    """开发机上有 ROS:写一个三帧的 mcap 录包,在 ROS 的系统 Python 里读(狗上也是这么跑的)。"""
    import os
    import subprocess
    import textwrap
    repo = Path(__file__).resolve().parents[3]
    script = textwrap.dedent(f"""
        import numpy as np, rosbag2_py
        from pathlib import Path
        from rclpy.serialization import serialize_message
        from sensor_msgs.msg import PointCloud2, PointField
        from d1max_localizer import build as B
        bag = Path({str(tmp_path / 'bag')!r})
        w = rosbag2_py.SequentialWriter()
        w.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
               rosbag2_py.ConverterOptions("cdr", "cdr"))
        w.create_topic(rosbag2_py.TopicMetadata(name="/front_lidar",
                       type="sensor_msgs/msg/PointCloud2", serialization_format="cdr"))
        for i in range(6):
            m = PointCloud2()
            m.header.stamp.sec, m.header.stamp.nanosec = 100 + i, 0
            m.header.frame_id = "rslidar_head"
            m.fields = [PointField(name=n, offset=4 * k, datatype=7, count=1)
                        for k, n in enumerate("xyz")]
            m.point_step, m.height, m.width = 12, 1, 1
            m.data = np.array([1.0, 0.0, 0.0], "<f4").tobytes()
            w.write("/front_lidar", serialize_message(m), (100 + i) * 10**9)
        del w
        traj = [(100.0 + i, (float(i), 0.0, 0.0), (0.0, 0.0, 0.7071068, 0.7071068))
                for i in range(6)]
        got = list(B.bag_scans(bag, traj, "/front_lidar", every=2))
        print([(o.tolist(), (p.round(3) + 0.0).tolist()) for o, p in got])  # -0.0 → 0.0
    """)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["OURS"] = f"{repo}/packages/localizer/src:{repo}/packages/contract/src"
    r = subprocess.run(["bash", "-c", "set +u; . /opt/ros/humble/setup.bash; "
                        'PYTHONPATH="$PYTHONPATH:$OURS" /usr/bin/python3 -'],
                       input=script, capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    # 每 2 帧取 1 帧;绕 Z 转 90°:雷达系的 x 朝地图的 y
    assert r.stdout.strip() == ("[([0.0, 0.0, 0.0], [[0.0, 1.0, 0.0]]), "
                                "([2.0, 0.0, 0.0], [[2.0, 1.0, 0.0]]), "
                                "([4.0, 0.0, 0.0], [[4.0, 1.0, 0.0]])]"), r.stdout
