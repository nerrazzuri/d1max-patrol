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
    """``lc``:回环工具(``mola-sm-lc-cli``)的假样 —— ``closures`` 个回环,关键帧每个往 ``shift`` 方向
    挪「序号 × 每帧的量」(像是把慢慢积累的漂移拉回来);``lc_fail`` 让它退出码 1。"""

    def __init__(self, points, fail="", *, closures=3, shift=(0.0, 0.0, 0.0), lc_fail=False,
                 lc_kfs_drop=0, kf_garbage=False):
        self.points, self.fail, self.calls, self.envs, self.kws = points, fail, [], [], []
        self.closures, self.shift, self.lc_fail, self.lc_kfs_drop = (closures, shift, lc_fail,
                                                                     lc_kfs_drop)
        self.kf_garbage = kf_garbage

    def __call__(self, cmd, env=None, stdout=None, stderr=None, **kw):
        self.calls.append(cmd)
        self.envs.append(env)
        self.kws.append(kw)
        name = Path(cmd[0]).name
        if cmd[0] == "dpkg-query":
            return SimpleNamespace(returncode=0, stdout="1.2.3")
        if self.fail and name == self.fail:
            stdout.write("出错了:内存不够\n")
            return SimpleNamespace(returncode=2)
        if name == "mola-sm-lc-cli":
            if self.lc_fail:
                stdout.write("Exception: 回环的 ICP 炸了\n")
                return SimpleNamespace(returncode=1)
            src, dst = Path(cmd[cmd.index("-i") + 1]), Path(cmd[cmd.index("-o") + 1])
            dst.write_bytes(src.read_bytes() + b"-lc")
            stdout.write(f"[INFO |SimplemapLoopClosure] Overall number of accepted loop-closures: "
                         f"{self.closures}\n")
        if name == "sm-cli":
            sm, out = Path(cmd[2]), Path(cmd[cmd.index("--output") + 1])
            closed = sm.read_bytes().endswith(b"-lc")
            lines = (sm.parent.parent if closed else sm.parent).joinpath("traj.tum")
            rows = [ln.split() for ln in lines.read_text().splitlines()][::10]
            if closed and self.lc_kfs_drop:
                rows = rows[:-self.lc_kfs_drop]
            text = []
            for k, r in enumerate(rows):
                t, p = float(r[0]) - 0.05, [float(v) for v in r[1:4]]   # 关键帧的时刻是报文头
                if closed:
                    p = [v + k * s for v, s in zip(p, self.shift, strict=True)]
                text.append(" ".join(f"{v:.6f}" for v in (t, *p)) + " " + " ".join(r[4:]))
            if closed and self.kf_garbage:
                text = ["nan? 1 2 3 4 5 6 7", "坏 行 不 是 数 字 啊 !"]
            out.write_text("\n".join(text) + "\n")
        if name == "sm2mm" and "-p" in cmd:
            src, dst = Path(cmd[cmd.index("-i") + 1]), Path(cmd[cmd.index("-o") + 1])
            dst.write_bytes(b"prior-from:" + src.read_bytes())
        if name == "mm2ply":
            out = cmd[cmd.index("-o") + 1]
            head = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(self.points)}\n"
                    "property float x\nproperty float y\nproperty float z\nend_header\n").encode()
            with open(out + "_raw.ply", "wb") as fh:
                fh.write(head + self.points.tobytes())
        if name == "mm-kf-regroup":
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
    import os
    assert os.stat(out / "prior.mm").st_ino == os.stat(work / "raw_prior.mm").st_ino, \
        "不压缩就硬链接(内审小 10:530 MB 再拷一份)"
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


def test_MOLA_建图的命令_版本与参数记下来进_build_json(tmp_path):
    """内审小 11:设计决定 2 要 build.json 里有 MOLA 版本与参数(给回放复现)。"""
    fake = 假MOLA(None)
    B.run_mapping(tmp_path / "bag", tmp_path / "w", run=fake)
    cmd = [c for c in fake.calls if c[0] == "mola-lidar-odometry-cli"][0]
    mola = json.loads((tmp_path / "w" / "mola.json").read_text())
    assert mola["version"] == "1.2.3" and mola["pipeline"].endswith("lidar3d-default.yaml")
    assert mola["env"]["MOLA_LIDAR_TOPIC"] == "/front_lidar"
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

    def 一帧没配上(bag, traj, topic):
        return iter(())

    B.package(work, tmp_path / "out4", run=假MOLA(pts), bag=tmp_path / "bag",
              read_scans=一帧没配上)
    assert json.loads((tmp_path / "out4" / "build.json").read_text())["grid"]["rays"] == \
        "synthetic:录包里没有配得上轨迹的扫描"
    got.clear()
    B.package(work, tmp_path / "out5", run=假MOLA(pts), bag=tmp_path / "bag", read_scans=读扫描,
              lidar_topic="/rear_lidar")
    assert got == [], "逐帧扫描只认前雷达系的话题(MOLA 跟的是它的坐标系)"
    assert json.loads((tmp_path / "out5" / "build.json").read_text())["grid"]["rays"].startswith(
        "synthetic:话题 /rear_lidar")
    B.package(work, tmp_path / "out6", run=假MOLA(pts), bag=tmp_path / "bag", read_scans=读扫描,
              lidar_topic="/d1max/merged_lidar")
    assert got, "前后雷达合并的话题(W09i)也在前雷达系里:照样打真射线"


def test_栅格拿到狗身中心走过的路(tmp_path, monkeypatch):
    work, pts = _scene(tmp_path, False)
    seen = []
    real = G.render

    def 看(*a, **k):
        seen.append(k.get("body_path"))
        return real(*a, **k)
    monkeypatch.setattr(G, "render", 看)

    def 读扫描(bag, traj, topic):
        _, p, _ = traj[0]
        yield np.array(p), pts[::10]
    B.package(work, tmp_path / "out", run=假MOLA(pts), bag=tmp_path / "bag", read_scans=读扫描)
    B.package(work, tmp_path / "out2", run=假MOLA(pts))
    assert len(seen) == 2 and all(b is not None and len(b) == 120 for b in seen)


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
        for i in range(7):                            # 最后一帧的时刻轨迹上没有:不用
            m = PointCloud2()
            m.header.stamp.sec, m.header.stamp.nanosec = (100 + i if i < 6 else 300), 0
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


def test_栅格自己出错不退回_照样算打包失败(tmp_path, monkeypatch):
    """内审应修 4:原来 ``except Exception`` 把栅格的错(内存不够、越界)也当成「读不了扫描」
    悄悄退回。"""
    work, pts = _scene(tmp_path, False)

    def 炸(*a, **k):
        if k.get("scans") is not None:
            list(k["scans"])
            raise MemoryError("栅格太大")
        raise AssertionError("不该退回模拟射线")
    monkeypatch.setattr(G, "render", 炸)

    def 读扫描(bag, traj, topic):
        _, p, _ = traj[0]
        yield np.array(p), pts[::10]
    with pytest.raises(MemoryError):
        B.package(work, tmp_path / "out", run=假MOLA(pts), bag=tmp_path / "bag", read_scans=读扫描)


def test_打包把_MOLA_的版本与参数抄进_build_json(tmp_path):
    work, pts = _scene(tmp_path, False)
    (work / "mola.json").write_text(json.dumps({"version": "1.2.3", "pipeline": "x.yaml"}))
    B.package(work, tmp_path / "out", run=假MOLA(pts))
    assert json.loads((tmp_path / "out" / "build.json").read_text())["mola"]["version"] == "1.2.3"


def test_栅格太大_打包失败说轨迹可能发散了(tmp_path, monkeypatch):
    work, pts = _scene(tmp_path, False)
    monkeypatch.setattr(G, "MAX_CELLS", 100)
    with pytest.raises(B.BuildError, match="发散"):
        B.package(work, tmp_path / "out", run=假MOLA(pts))


def test_录包里有RTK解_配经纬度写geo_json_没有就不写_build_json记原因(tmp_path):
    """W09e 决定 6。合成场景的轨迹未必走够 10 m:写没写跟 build.json、文件表一致;写了朝向要对。"""
    import math

    from d1max_localizer.frames import Frames
    from d1max_localizer.replay import read_tum

    from d1max_contract.geo import GEO_FILE, GeoRef, enu_to_llh
    work, pts = _scene(tmp_path, False)
    bag = tmp_path / "bag"
    bag.mkdir()
    files = B.package(work, tmp_path / "out", run=假MOLA(pts), bag=bag,
                      read_scans=lambda *a: iter(()))
    info = json.loads((tmp_path / "out" / "build.json").read_text())["geo"]
    assert info == {"written": False, "why": "录包里没有 RTK 解"} and GEO_FILE not in files
    frames = Frames.load(tmp_path / "out" / "frames.json")
    th = math.radians(25.0)
    rows = []
    for t, p, q in read_tum(work / "traj.tum"):
        x, y, _ = frames.to_map2d(p, q)
        e, n = math.cos(th) * x - math.sin(th) * y, math.sin(th) * x + math.cos(th) * y
        lat, lon = enu_to_llh(e, n, 5.41, 100.32)
        rows.append({"t": t, "fix": "fixed", "lat": lat, "lon": lon, "alt": 3.0})
    (bag / "rtk.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    files = B.package(work, tmp_path / "out2", run=假MOLA(pts), bag=bag,
                      read_scans=lambda *a: iter(()), clock_offset=lambda b, t: 0.0)
    info = json.loads((tmp_path / "out2" / "build.json").read_text())["geo"]
    written = (tmp_path / "out2" / GEO_FILE).is_file()
    assert info["written"] is written and (GEO_FILE in files) is written, info
    if written:
        g = GeoRef.from_wire(json.loads((tmp_path / "out2" / GEO_FILE).read_text()))
        assert g.yaw_deg == pytest.approx(25.0, abs=0.5) and g.rms_m < 0.05
    else:
        assert "范围" in info["why"], info



def test_配准出错不让整次建图失败(tmp_path, monkeypatch):
    """W09e 内审小:geo.json 是可选的。"""
    import d1max_localizer.geo as G
    work, pts = _scene(tmp_path, False)
    bag = tmp_path / "bag"
    bag.mkdir()
    (bag / "rtk.jsonl").write_text('{"t": 1, "lat": 5, "lon": 100, "fix": "fixed"}\n')

    def 炸(*a, **k):
        raise ValueError("坏数")
    monkeypatch.setattr(G, "georeference", 炸)
    files = B.package(work, tmp_path / "out", run=假MOLA(pts), bag=bag,
                      read_scans=lambda *a: iter(()), clock_offset=lambda b, t: 1.5)
    info = json.loads((tmp_path / "out" / "build.json").read_text())["geo"]
    assert info["written"] is False and "坏数" in info["why"] and info["clock_offset_s"] == 1.5
    assert "geo.json" not in files


# ------------------------------------------------------------ 回环(可选)


def _lc_tool(tmp_path):
    """装在自己前缀下的回环工具(像 colcon --merge-install 装出来的):bin/ 里是命令,share/ 里记着
    版本。"""
    prefix = tmp_path / "lc-prefix"
    exe = prefix / "bin" / "mola-sm-lc-cli"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    share = prefix / "share" / "mola_sm_loop_closure"
    share.mkdir(parents=True, exist_ok=True)
    share.joinpath("package.xml").write_text("<package><version>1.2.2</version></package>\n")
    share.joinpath("d1max-build.txt").write_text("mola_sm_loop_closure e737aa8 (MRPT 2.15)\n")
    return str(exe)


_SAME = ("frames.json", "floor.pgm", "floor.yaml", "coverage.json", "prior.mm")


def _plain(tmp_path, pts, work):
    """不回环打出来的(对照)。"""
    fake = 假MOLA(pts)
    B.package(work, tmp_path / "plain", run=fake)
    return fake, tmp_path / "plain"


def test_有回环工具_跑回环_从回环后的simplemap重出点云先验与轨迹_build_json记下(tmp_path):
    from d1max_localizer.replay import read_tum
    work, pts = _scene(tmp_path, False)
    cli = _lc_tool(tmp_path)
    fake = 假MOLA(pts, closures=3, shift=(0.0, 0.01, 0.0))
    files = B.package(work, tmp_path / "out", run=fake, loop_closure=cli)
    assert sorted(files) == sorted(B.FILES)
    [lc] = [c for c in fake.calls if Path(c[0]).name == "mola-sm-lc-cli"]
    assert lc[0] == cli
    assert lc[lc.index("-i") + 1] == str(work / "map.simplemap")
    assert lc[lc.index("-o") + 1] == str(work / "lc" / "map.simplemap")
    assert lc[lc.index("-p") + 1] == str(B.LC_PIPELINE) and B.LC_PIPELINE.is_file()
    env = fake.envs[fake.calls.index(lc)]
    assert env["LD_LIBRARY_PATH"].split(":")[0] == str(Path(cli).parent.parent / "lib"), \
        "自己前缀下装的回环工具:它的库不在 ROS 的库路径里"
    assert fake.kws[fake.calls.index(lc)].get("cwd") == str(work / "lc"), \
        "回环工具往当前目录写调试文件(sm_lc__*.tum):在中间目录里跑"
    sm2mm = [c for c in fake.calls if c[0] == "sm2mm"]
    assert {c[c.index("-i") + 1] for c in sm2mm} == {str(work / "lc" / "map.simplemap")}, \
        "点云、先验都从回环后的 simplemap 重出"
    [prior] = [c for c in sm2mm if "-p" in c]
    assert prior[prior.index("-p") + 1] == str(B.PRIOR_PIPELINE) and B.PRIOR_PIPELINE.is_file()
    assert (tmp_path / "out" / "prior.mm").read_bytes() == b"prior-from:sm-lc"
    [ply] = [c for c in fake.calls if c[0] == "mm2ply"]
    assert ply[ply.index("-i") + 1] == str(work / "lc" / "points.mm")
    # 轨迹(栅格的射线、frames、走过的路都用它)按关键帧的改动挪:关键帧 k 挪了 0.01·k
    fixed, orig = read_tum(work / "lc" / "traj.tum"), read_tum(work / "traj.tum")
    assert [f[0] for f in fixed] == [o[0] for o in orig]
    dy = [f[1][1] - o[1][1] for f, o in zip(fixed, orig, strict=True)]
    assert dy[0] == pytest.approx(0.0005, abs=1e-6) and dy[-1] == pytest.approx(0.11, abs=1e-6)
    assert all(b >= a - 1e-9 for a, b in zip(dy, dy[1:], strict=False))
    b = json.loads((tmp_path / "out" / "build.json").read_text())
    lcj = b["loop_closure"]
    assert lcj["ran"] is True and lcj["used"] is True and lcj["closures"] == 3, lcj
    assert lcj["cli"] == cli and lcj["version"] == "mola_sm_loop_closure e737aa8 (MRPT 2.15)"
    assert lcj["keyframes"] == 12 and lcj["max_shift_m"] == pytest.approx(0.11, abs=1e-3)
    assert {"lc_s", "keyframes_s", "prior_s"} <= set(lcj["timings_s"])
    assert "loop_closure_s" in b["timings_s"]


def test_没有回环工具_出的文件跟原来一样_build_json说没有(tmp_path):
    work, pts = _scene(tmp_path, False)
    fake, out = _plain(tmp_path, pts, work)
    assert [c[0] for c in fake.calls] == ["sm2mm", "mm2ply"], "不多调一个命令"
    assert fake.calls[0] == ["sm2mm", "-i", str(work / "map.simplemap"), "-o",
                             str(work / "points.mm"), "--no-progress-bar"]
    assert not (work / "lc").exists()
    import os
    assert os.stat(out / "prior.mm").st_ino == os.stat(work / "raw_prior.mm").st_ino
    b = json.loads((out / "build.json").read_text())
    assert b["loop_closure"] == {"ran": False, "why": "没有回环工具(mola-sm-lc-cli)"}
    assert "loop_closure_s" not in b["timings_s"]


@pytest.mark.parametrize("case, kw, why", [
    ("失败", {"lc_fail": True}, "退出码 1"),
    ("没找到", {"closures": 0}, "没找到回环"),
    ("挪得太多", {"shift": (0.0, 0.5, 0.0)}, "挪得太多"),
    ("关键帧对不上", {"lc_kfs_drop": 1}, "关键帧"),
    ("别的错", {"kf_garbage": True}, "回环出错: ValueError"),
])
def test_回环不成_退回原来的打包_build_json说为什么(tmp_path, case, kw, why):
    work, pts = _scene(tmp_path, False)
    _, plain = _plain(tmp_path, pts, work)
    fake = 假MOLA(pts, **kw)
    out = tmp_path / "out"
    B.package(work, out, run=fake, loop_closure=_lc_tool(tmp_path))
    for name in _SAME:
        assert (out / name).read_bytes() == (plain / name).read_bytes(), (case, name)
    sm2mm = [c for c in fake.calls if c[0] == "sm2mm"]
    assert sm2mm == [["sm2mm", "-i", str(work / "map.simplemap"), "-o", str(work / "points.mm"),
                      "--no-progress-bar"]], "退回去:点云从原来的 simplemap 出,不重出先验"
    lcj = json.loads((out / "build.json").read_text())["loop_closure"]
    assert lcj["ran"] is True and lcj["used"] is False and why in lcj["why"], lcj
    if case == "失败":
        assert "ICP 炸了" in lcj["why"], "带日志尾巴"
    if case == "没找到":
        assert lcj["closures"] == 0
        assert not [c for c in fake.calls if c[0] == "sm-cli"], "没回环就不用再导关键帧"


def test_轨迹按前后两个关键帧的改动插值():
    c10, s10 = math.cos(math.radians(10)), math.sin(math.radians(10))
    ident = (0.0, 0.0, 0.0, 1.0)
    q10 = (0.0, 0.0, math.sin(math.radians(5)), math.cos(math.radians(5)))
    before = [(0.0, (0.0, 0.0, 0.0), ident), (1.0, (1.0, 0.0, 0.0), ident)]
    after = [(0.0, (0.0, 0.0, 0.0), ident), (1.0, (1.0, 0.2, 0.0), q10)]
    traj = [(t, (x, 0.0, 0.0), ident) for t, x in ((-0.5, -0.5), (0.0, 0.0), (0.5, 0.5),
                                                    (1.0, 1.0), (2.0, 2.0))]
    got = B.correct_traj(traj, before, after)
    assert [g[0] for g in got] == [t for t, _, _ in traj]
    p = [g[1] for g in got]
    assert p[0] == pytest.approx((-0.5, 0.0, 0.0)) and p[1] == pytest.approx((0.0, 0.0, 0.0))
    assert p[3] == pytest.approx((1.0, 0.2, 0.0)), "关键帧上就是回环后的位姿"
    assert got[3][2] == pytest.approx(q10)
    assert p[4] == pytest.approx((1 + c10, 0.2 + s10, 0.0)), "最后一个关键帧之后跟着它挪"
    mid = (0.5 * 0.5 + 0.5 * (0.5 * c10 + 1 - c10), 0.5 * (0.5 * s10 + 0.2 - s10), 0.0)
    assert p[2] == pytest.approx(mid), "两个关键帧中间:两边各自挪出来的位置按时刻插"
    yaw = 2 * math.atan2(got[2][2][2], got[2][2][3])
    assert math.degrees(yaw) == pytest.approx(5.0, abs=0.01)
    with pytest.raises(B.BuildError, match="关键帧"):
        B.correct_traj(traj, before, after[:1])


def test_找回环工具(tmp_path):
    exe = _lc_tool(tmp_path)
    nowhere = tmp_path / "nowhere" / "mola-sm-lc-cli"

    def on_path(name):
        return "/opt/ros/humble/bin/mola-sm-lc-cli" if name == "mola-sm-lc-cli" else None

    def none(name):
        return None
    assert B.find_loop_closure("off", env={}, which=on_path) is None
    assert B.find_loop_closure("auto", env={"D1MAX_LOOP_CLOSURE": "off"}, which=on_path) is None
    assert B.find_loop_closure("auto", env={"D1MAX_LOOP_CLOSURE": exe}, which=none) == exe
    assert B.find_loop_closure(exe, env={}, which=none) == exe
    assert B.find_loop_closure("auto", env={}, which=on_path) == \
        "/opt/ros/humble/bin/mola-sm-lc-cli"
    assert B.find_loop_closure("auto", env={}, which=none, default=Path(exe)) == exe
    assert B.find_loop_closure("auto", env={}, which=none, default=nowhere) is None
    assert B.find_loop_closure(str(nowhere), env={}, which=on_path) is None, \
        "给的路径不是能跑的程序:当没有(建图不能因为可选的一步失败)"


def test_建图命令_回环开关交给打包(tmp_path, monkeypatch):
    from d1max_localizer import tools
    seen = {}

    def 打包(work, out, **kw):
        seen.update(kw)
        out.mkdir(parents=True, exist_ok=True)
        (out / "build.json").write_text("{}")
        return []
    monkeypatch.setattr(B, "package", 打包)
    monkeypatch.setattr(B, "find_loop_closure", lambda s: None if s == "off" else f"找:{s}")
    base = ["build", "--bag", str(tmp_path / "bag"), "--out", str(tmp_path / "o"), "--reuse"]
    assert tools.main(base) == 0 and seen["loop_closure"] == "找:auto"
    assert tools.main([*base, "--loop-closure", "off"]) == 0 and seen["loop_closure"] is None


# ------------------------------------------------------------ 对账 S2:地图手性(跟里程同号)


def _转圈(tmp, upside_down, *, n=300, r=1.5):
    """跟 ``_scene`` 同一间屋子,狗绕 (3, 1.2) 朝左走一圈(半径 1.5 m,30 s,都在屋里):朝向一直在变。
    回 (work, 点云, 世界里的朝向 [(时刻, 朝向)])。"""
    work, pts = _scene(tmp, upside_down)
    W2M, B_ = _world(upside_down)
    lines, yaws = [], []
    for i in range(n):
        th = 2 * math.pi * i / n
        R = W2M @ _Rz(th) @ B_.T
        p = W2M @ np.array([3.0 + r * math.sin(th), 1.2 - r * math.cos(th), 0.0])
        q = mat_to_quat(tuple(tuple(v) for v in R))
        t = 100 + 0.1 * i
        lines.append(f"{t:.2f} {p[0]} {p[1]} {p[2]} {q[0]} {q[1]} {q[2]} {q[3]}")
        yaws.append((t, th))
    (work / "traj.tum").write_text("\n".join(lines) + "\n")
    return work, pts, yaws


def _里程(yaws, *, sign=1.0, off=0.7, dt=0.0):
    """狗自己的里程(录包里 ``/odom/mc_odom`` 那样):(报文头时刻, 收到时刻, 朝向);里程系的零朝向
    跟地图不一样(``off``)。``sign=-1`` 就是跟地图反着转。"""
    return [(t + dt, t + dt + 0.02, math.remainder(sign * th + off, 2 * math.pi))
            for t, th in yaws]


def test_S2_手性_同号认_反号判镜像_转得少或对不上时段说不清():
    assert (B.HAND_AGREE, B.HAND_MIN_TURN_RAD, B.HAND_MAX_LAG_S) == (0.8, 1.0, 3.0), \
        "来由见常量注释(两份真录包:同号 100%、转 860–900°、两边差 2.7 s),改了要重核"
    yaws = [(100 + 0.1 * i, 2 * math.pi * i / 300 + 0.6 * math.sin(i / 13)) for i in range(300)]
    odom = [(h, y) for h, _, y in _里程(yaws)]
    ok = B.handedness(yaws, odom)
    assert ok["verdict"] == "ok" and ok["agree"] == 1.0 and ok["turn_deg"] > 200, ok
    assert abs(ok["ratio"] - 1.0) < 0.05 and ok["lag_s"] == 0.0
    bad = B.handedness(yaws, [(h, y) for h, _, y in _里程(yaws, sign=-1.0)])
    assert bad["verdict"] == "mirrored" and bad["agree"] == 0.0, bad
    late = B.handedness(yaws, [(h, y) for h, _, y in _里程(yaws, dt=2.0)])
    assert late["verdict"] == "ok" and late["lag_s"] == pytest.approx(-2.0, abs=0.15), late
    straight = [(t, 0.01 * math.sin(t)) for t, _ in yaws]
    few = B.handedness(straight, [(t, y + 0.3) for t, y in straight])
    assert few["verdict"] == "unclear" and "转得太少" in few["why"], few
    bend = [(t, 0.15 * (t - 110) if 110 <= t < 114 else (0.0 if t < 110 else 0.6))
            for t, _ in yaws]                            # 只拐了一个 34° 的弯
    few = B.handedness(bend, [(t, y + 0.3) for t, y in bend])
    assert few["verdict"] == "unclear" and few["turn_deg"] < 57, few
    apart = B.handedness(yaws, [(t + 1000.0, y) for t, y in odom])
    assert apart["verdict"] == "unclear" and "时段" in apart["why"], apart
    half = [(t, th if t < 115 else -th) for t, th in yaws]   # 前一半同号、后一半反号
    mixed = B.handedness(half, [(t, th) for t, th in yaws])
    assert mixed["verdict"] == "unclear" and 0.2 < mixed["agree"] < 0.8, mixed


@pytest.mark.parametrize("upside_down", [False, True])
def test_S2_打包时拿录包里的里程核手性_对的出图_build_json记下(tmp_path, upside_down):
    work, pts, yaws = _转圈(tmp_path, upside_down)
    got = []

    def 读里程(bag):
        got.append(bag)
        return "/odom/mc_odom", _里程(yaws)
    B.package(work, tmp_path / "out", run=假MOLA(pts), bag=tmp_path / "bag",
              read_scans=lambda *a: iter(()), read_odom=读里程)
    assert got == [tmp_path / "bag"]
    h = json.loads((tmp_path / "out" / "build.json").read_text())["handedness"]
    assert h["verdict"] == "ok" and h["topic"] == "/odom/mc_odom" and h["clock"] == "header", h


def test_S2_地图跟狗反着转_不出图_说清楚(tmp_path):
    work, pts, yaws = _转圈(tmp_path, True)
    out = tmp_path / "out"
    with pytest.raises(B.BuildError, match="镜像.*反号.*「上」"):
        B.package(work, out, run=假MOLA(pts), bag=tmp_path / "bag",
                  read_scans=lambda *a: iter(()),
                  read_odom=lambda bag: ("/odom/mc_odom", _里程(yaws, sign=-1.0)))
    assert not (out / "frames.json").exists() and not (out / "build.json").exists(), \
        "判了镜像:一个版本文件都不写"


def test_S2_上判反了_里程是对的_核出来不出图(tmp_path, monkeypatch):
    """模拟 10-13 狗上那次(怀疑):``orient`` 把「上」判反了(倒装的雷达按装法的正负号标),地图平面
    是镜像的。"""
    from d1max_localizer.frames import calibrate
    work, pts, yaws = _转圈(tmp_path, True)

    def 判反(points, traj):
        poses = [(p, q) for _, p, q in traj]
        f = calibrate(poses, sensor_up=B.SENSOR_UP_HINT, forward_hint=B.SENSOR_FORWARD_HINT)
        return f, "按装法的「上」(没翻)"
    monkeypatch.setattr(B, "orient", 判反)
    with pytest.raises(B.BuildError, match="镜像"):
        B.package(work, tmp_path / "out", run=假MOLA(pts), bag=tmp_path / "bag",
                  read_scans=lambda *a: iter(()),
                  read_odom=lambda bag: ("/odom/mc_odom", _里程(yaws)))


def test_S2_核不了的照出图_build_json写着为什么(tmp_path):
    work, pts, yaws = _转圈(tmp_path, False)

    def 打包(name, **kw):
        B.package(work, tmp_path / name, run=假MOLA(pts), read_scans=lambda *a: iter(()), **kw)
        return json.loads((tmp_path / name / "build.json").read_text())["handedness"]

    assert 打包("o1") == {"verdict": "skipped", "why": "没给录包"}
    h = 打包("o2", bag=tmp_path / "bag", read_odom=lambda bag: None)
    assert h["verdict"] == "skipped" and "/odom/mc_odom" in h["why"], h

    def 坏(bag):
        raise RuntimeError("包坏了")
    h = 打包("o3", bag=tmp_path / "bag", read_odom=坏)
    assert h["verdict"] == "skipped" and "包坏了" in h["why"], h
    h = 打包("o4", bag=tmp_path / "bag")                    # 默认读法:这里没有 ROS / 不是录包
    assert h["verdict"] == "skipped" and "读不了" in h["why"], h
    still = [(t, r, 0.3) for t, r, _ in _里程(yaws)]         # 里程一直不转:说不清
    h = 打包("o5", bag=tmp_path / "bag", read_odom=lambda bag: ("/odom/mc_odom", still))
    assert h["verdict"] == "unclear", h


def test_S2_报文头的钟对不上_按收到的时刻对(tmp_path):
    """里程的报文头用的是别的钟(差得远):退回「收到的时刻」,轨迹按雷达的「收到 − 报文头」挪过去。"""
    work, pts, yaws = _转圈(tmp_path, False)
    odom = [(h - 5e6, r + 50.0, y) for h, r, y in _里程(yaws)]
    B.package(work, tmp_path / "out", run=假MOLA(pts), bag=tmp_path / "bag",
              read_scans=lambda *a: iter(()), read_odom=lambda bag: ("/odom/mc_odom", odom),
              clock_offset=lambda bag, topic: 50.0)
    h = json.loads((tmp_path / "out" / "build.json").read_text())["handedness"]
    assert h["verdict"] == "ok" and h["clock"] == "receive", h


_ODOM_BAG = """
import math, rosbag2_py
from pathlib import Path
from rclpy.serialization import serialize_message
from nav_msgs.msg import Odometry
from d1max_localizer import build as B
bag = Path(BAG)
w = rosbag2_py.SequentialWriter()
w.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
       rosbag2_py.ConverterOptions("cdr", "cdr"))
for name in ("/odom/current_pose", "/odom/mc_odom"):
    w.create_topic(rosbag2_py.TopicMetadata(name=name, type="nav_msgs/msg/Odometry",
                                            serialization_format="cdr"))
for i in range(3):
    for name, k in (("/odom/current_pose", -1), ("/odom/mc_odom", 1)):
        m = Odometry()
        m.header.stamp.sec, m.header.stamp.nanosec = 50 + i, 0
        a = 0.5 * i * k
        m.pose.pose.orientation.z, m.pose.pose.orientation.w = math.sin(a / 2), math.cos(a / 2)
        w.write(name, serialize_message(m), (100 + i) * 10**9 + (k > 0))
del w
for topics in (B.ODOM_TOPICS, ("/odom/current_pose",)):
    topic, rows = B.bag_odom(bag, topics)
    print(topic, [(h, round(r, 3), round(y, 3)) for h, r, y in rows])
print(B.bag_odom(bag, topics=("/nope",)))
"""


@pytest.mark.skipif(not Path("/opt/ros/humble/setup.sh").is_file(), reason="要 ROS")
def test_S2_从真的mcap录包读里程_报文头收到时刻朝向(tmp_path):
    import os
    import subprocess
    repo = Path(__file__).resolve().parents[3]
    script = _ODOM_BAG.replace("BAG", repr(str(tmp_path / "bag")))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["OURS"] = f"{repo}/packages/localizer/src:{repo}/packages/contract/src"
    r = subprocess.run(["bash", "-c", "set +u; . /opt/ros/humble/setup.bash; "
                        'PYTHONPATH="$PYTHONPATH:$OURS" /usr/bin/python3 -'],
                       input=script, capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "/odom/mc_odom [(50.0, 100.0, 0.0), (51.0, 101.0, 0.5), (52.0, 102.0, 1.0)]",
        "/odom/current_pose [(50.0, 100.0, 0.0), (51.0, 101.0, -0.5), (52.0, 102.0, -1.0)]",
        "None"], r.stdout
