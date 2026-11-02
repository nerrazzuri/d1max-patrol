"""Super-LIO 建图前端(工单 21):Super-LIO 的轨迹 + 录包里的原始点云 → 我们原来的出图流程。

为什么:2026-11-02 商场两版图 MOLA 都跟丢(机器人上首尾差 56 m / 12.5 m);同一商场
Super-LIO 在机器人上实时跑首尾 5 cm,台式机离线跑同一个包 0.14 m
(``docs/Super-LIO现场验证-2026-11-02.md``)。MOLA 留着:建图前端可以切回去(``--front mola``),
定位器照旧用 MOLA。

Super-LIO 只用前雷达 + 它自带的 IMU(跟竞品一样)。它是 GPL-3.0,**只作为单独进程跑**
(``super_lio_node``),我们只读它发的位姿(``nav_msgs/Odometry``,IMU 在世界系里;世界系 z 朝上)。

出图要的中间文件(跟 MOLA 出的同名,:func:`d1max_localizer.build.package` 原样收):

- ``traj.tum``:每帧扫描时刻的**雷达**位姿(IMU 位姿 × 雷达 → IMU 外参,按扫描中间时刻插值);
- ``map.simplemap``:隔 :data:`KF_DIST_M` / :data:`KF_TURN_DEG` 取的关键帧,原始整帧点云
  (``kf2sm`` 写);
- ``raw_prior.mm``:``sm2mm`` 按定位先验的流水线从 simplemap 出(同回环那条路)。

位姿从哪来:边走边建时 Super-LIO 在机器人上实时跑,它的位姿跟原始数据录进**同一个包**
(:data:`LIVE_TOPIC`);从录包重建时在本机隔离域里回放给 Super-LIO 重算(:func:`run_offline`)。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

#: 装在哪(``deploy/d1max-superlio-build`` 编出来的;``current`` 指当前版本)。
ROOT = Path(os.environ.get("D1MAX_SUPERLIO_ROOT", "/opt/d1max/superlio/current"))
#: 参数文件:机器人上的(现场调参)优先,没有用随版本带的。
SITE_CONFIG = Path("/etc/d1max/superlio.yaml")
DEFAULT_CONFIG = Path(__file__).resolve().parents[4] / "deploy/superlio/d1max.yaml"
LIDAR_TOPIC = "/front_lidar"
IMU_TOPIC = "/front_lidar/imu"
#: 边走边建时 Super-LIO 的位姿改名到建图的命名空间下(跟定位器、竞品的 ``/lio/*`` 都不串),录进包里。
NS = "/d1max_mapping"
LIVE_TOPIC = f"{NS}/lio/odom"
#: Super-LIO 发的话题(都是绝对名,``__ns`` 管不到,一个个改名)。
PUBLISHED = ("/lio/odom", "/lio/imu/odom", "/lio/robo/odom", "/lio/path", "/lio/cloud_world",
             "/mavros/vision_pose/pose", "/lio/robo/cloud_world", "/lio/body/cloud_pose",
             "/lio/world/cloud_pose", "/lio/processing_time", "/lio/global_map")
#: 关键帧间隔:走这么远或转这么多取一帧(MOLA 建图的关键帧也是这个量级)。
KF_DIST_M = 0.3
KF_TURN_DEG = 10.0
#: 关键帧点云按离雷达的距离裁(太近是机身、太远是噪点)。
RANGE_MIN_M, RANGE_MAX_M = 0.6, 80.0
#: 位姿取在扫描的哪个时刻(Airy 一帧 0.1 s,报文头是起点):取中间。
SCAN_MID_S = 0.05
#: 两个位姿之间断了这么久,中间的扫描不用(插值不可信)。
MAX_GAP_S = 0.5
#: 从录包重建时回放的倍速:慢一点保证 Super-LIO 每帧都算得完(它是实时算法,来不及就发散)。
OFFLINE_RATE = 0.5
#: 一帧扫描最少多少点才当关键帧。
MIN_KF_POINTS = 500
#: 建完把关:相邻两个位姿(10 Hz)水平跳这么远就是跟丢了(机器人 0.1 s 走不了 1 m),不出图。
JUMP_M = 1.0

Pose = tuple[float, tuple[float, float, float], tuple[float, float, float, float]]


class SuperLioError(RuntimeError):
    """Super-LIO 这条路走不通(说人话的原因)。"""


# ---------------------------------------------------------------- 装没装、参数
def installed(root: Path | None = None) -> bool:
    root = ROOT if root is None else root
    return (root / ".ok").is_file() and (root / "install/setup.bash").is_file() \
        and (root / "bin/kf2sm").is_file()


def config_path() -> Path:
    return SITE_CONFIG if SITE_CONFIG.is_file() else DEFAULT_CONFIG


def extrinsic(cfg: Path) -> tuple[list[list[float]], list[float]]:
    """参数文件里的 ``lio.extrinsic.lidar_imu``:``(R, t)``,雷达系 → IMU 系
    (t 3 个 + R 按行 9 个)。"""
    txt = Path(cfg).read_text(encoding="utf-8")
    key = "lio.extrinsic.lidar_imu:"
    i = txt.find(key)
    if i < 0:
        raise SuperLioError(f"{cfg} 里没有 {key}")
    j, k = txt.index("[", i), txt.index("]", i)
    try:
        v = [float(s) for s in txt[j + 1:k].replace("\n", " ").split(",") if s.strip()]
    except ValueError as exc:
        raise SuperLioError(f"{cfg} 的 {key} 不是 12 个数") from exc
    if len(v) != 12:
        raise SuperLioError(f"{cfg} 的 {key} 要 12 个数,给了 {len(v)} 个")
    R = [v[3:6], v[6:9], v[9:12]]
    det = (R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
           - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
           + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0]))
    if abs(det - 1.0) > 0.05:
        raise SuperLioError(f"{cfg} 的 {key} 旋转部分不是旋转矩阵(行列式 {det:.3f})")
    return R, v[0:3]


def node_args(cfg: Path, *, ns_remap: bool) -> list[str]:
    """``super_lio_node`` 的 ROS 参数:参数文件、输入话题;``ns_remap`` 把发的话题全改到
    :data:`NS` 下。"""
    args = ["--ros-args", "--params-file", str(cfg),
            "-p", f"lio.ros.lidar_topic:={LIDAR_TOPIC}", "-p", f"lio.ros.imu_topic:={IMU_TOPIC}",
            "-p", "lio.map.save_map:=false"]
    if ns_remap:
        for t in PUBLISHED:
            args += ["-r", f"{t}:={NS}{t}"]
    return args


# ---------------------------------------------------------------- 小数学(四元数 x, y, z, w)
def quat_to_mat(q: Sequence[float]) -> list[list[float]]:
    x, y, z, w = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def mat_to_quat(R: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    tr = R[0][0] + R[1][1] + R[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = ((R[2][1] - R[1][2]) / s, (R[0][2] - R[2][0]) / s, (R[1][0] - R[0][1]) / s, 0.25 * s)
    elif R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s = math.sqrt(1.0 + R[0][0] - R[1][1] - R[2][2]) * 2
        q = (0.25 * s, (R[0][1] + R[1][0]) / s, (R[0][2] + R[2][0]) / s, (R[2][1] - R[1][2]) / s)
    elif R[1][1] > R[2][2]:
        s = math.sqrt(1.0 + R[1][1] - R[0][0] - R[2][2]) * 2
        q = ((R[0][1] + R[1][0]) / s, 0.25 * s, (R[1][2] + R[2][1]) / s, (R[0][2] - R[2][0]) / s)
    else:
        s = math.sqrt(1.0 + R[2][2] - R[0][0] - R[1][1]) * 2
        q = ((R[0][2] + R[2][0]) / s, (R[1][2] + R[2][1]) / s, 0.25 * s, (R[1][0] - R[0][1]) / s)
    n = math.sqrt(sum(c * c for c in q))
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def _mul(A: Sequence[Sequence[float]], B: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _apply(A: Sequence[Sequence[float]], v: Sequence[float]) -> list[float]:
    return [sum(A[i][k] * v[k] for k in range(3)) for i in range(3)]


def _T(A: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[A[j][i] for j in range(3)] for i in range(3)]


def slerp(q0: Sequence[float], q1: Sequence[float], s: float) -> tuple[float, ...]:
    d = sum(a * b for a, b in zip(q0, q1, strict=True))
    if d < 0:
        q1, d = tuple(-c for c in q1), -d
    if d > 0.9995:
        q = [a + s * (b - a) for a, b in zip(q0, q1, strict=True)]
    else:
        th = math.acos(d)
        q = [(math.sin((1 - s) * th) * a + math.sin(s * th) * b) / math.sin(th)
             for a, b in zip(q0, q1, strict=True)]
    n = math.sqrt(sum(c * c for c in q))
    return tuple(c / n for c in q)


def rot_angle(R: Sequence[Sequence[float]]) -> float:
    return math.acos(max(-1.0, min(1.0, (R[0][0] + R[1][1] + R[2][2] - 1) / 2)))


def _log(R: Sequence[Sequence[float]]) -> list[float]:
    th = rot_angle(R)
    if th < 1e-9:
        return [0.0, 0.0, 0.0]
    k = th / (2 * math.sin(th))
    return [(R[2][1] - R[1][2]) * k, (R[0][2] - R[2][0]) * k, (R[1][0] - R[0][1]) * k]


# ---------------------------------------------------------------- 位姿
def clean(poses: Iterable[Pose]) -> list[Pose]:
    """按时间排好;丢掉非有限值、离谱的(Super-LIO 退出时会吐几帧乱值)、时间倒退或重复的。"""
    out: list[Pose] = []
    for t, p, q in sorted(poses, key=lambda r: r[0]):
        v = (t, *p, *q)
        if not all(math.isfinite(c) for c in v) or max(abs(c) for c in p) > 1e4:
            continue
        n = math.sqrt(sum(c * c for c in q))
        if n < 1e-6 or (out and t <= out[-1][0]):
            continue
        out.append((t, p, (q[0] / n, q[1] / n, q[2] / n, q[3] / n)))
    return out


def pose_at(poses: Sequence[Pose], t: float, times: Sequence[float] | None = None
            ) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """``t`` 时刻的位姿(前后两个之间插值);在范围外、两头断了 :data:`MAX_GAP_S` 以上是 None。"""
    import bisect
    ts = times if times is not None else [r[0] for r in poses]
    if not ts or t < ts[0] or t > ts[-1]:
        return None
    i = min(max(bisect.bisect_left(ts, t), 1), len(ts) - 1)
    (t0, p0, q0), (t1, p1, q1) = poses[i - 1], poses[i]
    if t1 - t0 > MAX_GAP_S:
        return None
    s = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
    return (tuple(a + s * (b - a) for a, b in zip(p0, p1, strict=True)), slerp(q0, q1, s))


def lidar_pose(p_imu: Sequence[float], q_imu: Sequence[float], R_il: Sequence[Sequence[float]],
               t_il: Sequence[float]) -> tuple[list[float], list[list[float]]]:
    """IMU 在世界系的位姿 → 雷达在世界系的位姿:``T_wl = T_wi · T_il``。"""
    R_wi = quat_to_mat(q_imu)
    R_wl = _mul(R_wi, R_il)
    p_wl = [a + b for a, b in zip(_apply(R_wi, t_il), p_imu, strict=True)]
    return p_wl, R_wl


# ---------------------------------------------------------------- 读录包
def read_odom(bag: Path, topic: str) -> list[Pose]:
    """录包里 ``topic`` 的 ``nav_msgs/Odometry`` → 位姿(报文头时刻)。要 ROS 的系统 Python。"""
    import rosbag2_py
    from nav_msgs.msg import Odometry
    from rclpy.serialization import deserialize_message

    storage = "mcap" if any(Path(bag).glob("*.mcap")) else "sqlite3"
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=storage),
           rosbag2_py.ConverterOptions("cdr", "cdr"))
    have = {x.name: x.type for x in r.get_all_topics_and_types()}
    if have.get(topic) != "nav_msgs/msg/Odometry":
        raise SuperLioError(f"录包里没有 Super-LIO 的位姿 {topic}(nav_msgs/Odometry)")
    r.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    out: list[Pose] = []
    while r.has_next():
        _, raw, _ = r.read_next()
        m = deserialize_message(raw, Odometry)
        p, o = m.pose.pose.position, m.pose.pose.orientation
        out.append((m.header.stamp.sec + m.header.stamp.nanosec * 1e-9, (p.x, p.y, p.z),
                    (o.x, o.y, o.z, o.w)))
    return out


def bag_clouds(bag: Path, topic: str = LIDAR_TOPIC) -> Iterator[tuple[float, Any]]:
    """录包里的点云,逐帧 ``(报文头时刻, 原始消息)``(点在 :func:`make_keyframes` 里按需解)。"""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2

    storage = "mcap" if any(Path(bag).glob("*.mcap")) else "sqlite3"
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=storage),
           rosbag2_py.ConverterOptions("cdr", "cdr"))
    r.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    while r.has_next():
        _, raw, _ = r.read_next()
        m = deserialize_message(raw, PointCloud2)
        yield m.header.stamp.sec + m.header.stamp.nanosec * 1e-9, m


# ---------------------------------------------------------------- 轨迹、关键帧
def make_keyframes(poses: Sequence[Pose], clouds: Iterable[tuple[float, Any]], work: Path,
                   R_il: Sequence[Sequence[float]], t_il: Sequence[float],
                   xyz_of: Callable[[Any], Any]) -> dict[str, int]:
    """写 ``work/traj.tum``(每帧扫描的雷达位姿)和 ``work/kf/``
    (关键帧:``index.txt`` + 点云 float32)。
    回 ``{"scans", "traj", "keyframes"}``。"""
    import numpy as np

    kf = work / "kf"
    shutil.rmtree(kf, ignore_errors=True)
    kf.mkdir(parents=True)
    times = [r[0] for r in poses]
    traj: list[str] = []
    index: list[str] = []
    last_kf: tuple[list[float], list[list[float]]] | None = None
    prev: tuple[float, list[float], list[list[float]]] | None = None
    scans = 0
    for t, msg in clouds:
        scans += 1
        got = pose_at(poses, t + SCAN_MID_S, times)
        if got is None:
            continue
        p, R = lidar_pose(got[0], got[1], R_il, t_il)
        q = mat_to_quat(R)
        traj.append(f"{t:.6f} " + " ".join(f"{c:.6f}" for c in (*p, *q)) + "\n")
        v = w = [0.0, 0.0, 0.0]
        if prev is not None and t > prev[0]:
            dt = t - prev[0]
            v = [c / dt for c in _apply(_T(R), [a - b for a, b in zip(p, prev[1], strict=True)])]
            w = [c / dt for c in _log(_mul(_T(prev[2]), R))]
        prev = (t, p, R)
        if last_kf is not None:
            moved = math.dist(p, last_kf[0])
            turned = math.degrees(rot_angle(_mul(_T(last_kf[1]), R)))
            if moved < KF_DIST_M and turned < KF_TURN_DEG:
                continue
        xyz = np.asarray(xyz_of(msg), float)
        if len(xyz):
            rng = np.linalg.norm(xyz, axis=1)
            xyz = xyz[(rng > RANGE_MIN_M) & (rng < RANGE_MAX_M)]
        if len(xyz) < MIN_KF_POINTS:
            continue
        name = f"{len(index):05d}.bin"
        xyz.astype("<f4").tofile(kf / name)
        index.append(f"{t:.6f} " + " ".join(f"{c:.6f}" for c in (*p, *q, *v, *w)) + f" {name}")
        last_kf = (p, R)
    (kf / "index.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    (work / "traj.tum").write_text("".join(traj), encoding="utf-8")
    return {"scans": scans, "traj": len(traj), "keyframes": len(index)}


def quality(poses: Sequence[Pose]) -> dict[str, float]:
    """路程(每秒取一个点)、首尾相距、最大单步:扫图时回到起点的话,首尾相距就是误差。"""
    keep: list[Pose] = []
    for r in poses:
        if not keep or r[0] - keep[-1][0] >= 1.0:
            keep.append(r)
    if poses and keep[-1] is not poses[-1]:
        keep.append(poses[-1])
    path = sum(math.dist(a[1][:2], b[1][:2]) for a, b in zip(keep, keep[1:], strict=False))
    steps = [math.dist(a[1][:2], b[1][:2]) for a, b in zip(poses, poses[1:], strict=False)]
    return {"duration_s": round(poses[-1][0] - poses[0][0], 1) if poses else 0.0,
            "path_m": round(path, 1),
            "start_end_m": round(math.dist(poses[0][1][:2], poses[-1][1][:2]), 2) if poses else 0.0,
            "max_step_m": round(max(steps), 2) if steps else 0.0}


# ---------------------------------------------------------------- 出中间文件
Runner = Callable[..., Any]


def _run(run: Runner, cmd: list[str], log: Path, what: str, env: dict[str, str] | None = None
         ) -> None:
    with open(log, "w", encoding="utf-8") as fh:
        rc = run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env).returncode
    if rc != 0:
        raise SuperLioError(f"{what}失败(退出码 {rc}),日志见 {log}")


def prepare(work: Path, bag: Path, poses: Sequence[Pose], *, cfg: Path, root: Path = ROOT,
            prior_pipeline: Path, plugin: Sequence[str] = (), run: Runner = subprocess.run,
            clouds: Iterable[tuple[float, Any]] | None = None,
            xyz_of: Callable[[Any], Any] | None = None, source: str = "") -> dict[str, Any]:
    """位姿 → ``work`` 里出图要的三样(``traj.tum``、``map.simplemap``、``raw_prior.mm``)
    + ``mola.json``
    (``build.json`` 抄它:这次用的哪个前端、参数、质量)。回汇总。"""
    poses = clean(poses)
    if len(poses) < 20:
        raise SuperLioError(f"Super-LIO 只给了 {len(poses)} 个位姿(没跑起来?)")
    q = quality(poses)
    if q["max_step_m"] > JUMP_M:
        raise SuperLioError(f"Super-LIO 的轨迹有突跳(相邻两帧差 {q['max_step_m']} m):跟丢了,"
                            "这一趟不出图。慢一点重走,开始、结束各静止 10 秒")
    if xyz_of is None:
        from d1max_localizer.build import cloud_xyz as xyz_of
    R_il, t_il = extrinsic(cfg)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    n = make_keyframes(poses, bag_clouds(bag) if clouds is None else clouds, work, R_il, t_il,
                       xyz_of)
    if n["keyframes"] < 3:
        raise SuperLioError(f"关键帧只有 {n['keyframes']} 个(走得太短,或位姿跟点云时间对不上)")
    _run(run, [str(root / "bin/kf2sm"), str(work / "kf"), str(work / "map.simplemap")],
         work / "kf2sm.log", "关键帧转 simplemap")
    _run(run, ["sm2mm", *plugin, "-i", str(work / "map.simplemap"), "-o",
               str(work / "raw_prior.mm"), "-p", str(prior_pipeline), "--no-progress-bar"],
         work / "prior.log", "从关键帧出定位先验")
    shutil.rmtree(work / "kf", ignore_errors=True)        # 几百 MB,simplemap 里都有了
    info = {"builder": "super-lio", "source": source, "config": str(cfg),
            "version": _version(root), "lidar_topic": LIDAR_TOPIC, "imu_topic": IMU_TOPIC,
            "keyframe": {"dist_m": KF_DIST_M, "turn_deg": KF_TURN_DEG}, "quality": q,
            **n, "prepare_s": round(time.monotonic() - t0, 1)}
    (work / "mola.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
    return info


def _version(root: Path) -> str:
    try:
        return Path(os.path.realpath(root)).name
    except OSError:
        return "?"


# ---------------------------------------------------------------- 从录包重算
def run_offline(bag: Path, work: Path, *, cfg: Path, root: Path = ROOT, rate: float = OFFLINE_RATE,
                env: dict[str, str] | None = None) -> Path:
    """在本机隔离域里起 Super-LIO、回放录包的前雷达 + IMU、把它的位姿录成一个小包。回那个包的目录。

    ``env`` 是隔离域三件套(建图编排给的;不给用 93 域、fastrtps、只本机)。"""
    out = work / "lio_bag"
    shutil.rmtree(out, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    e = {**os.environ, **(env or {"ROS_DOMAIN_ID": "93", "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
                                   "ROS_LOCALHOST_ONLY": "1"})}
    src = f"set +u; . /opt/ros/humble/setup.bash; . {root}/install/setup.bash; set -u; exec "
    qos = work / "qos.yaml"
    # 回放要显式给 QoS:这些包里存的 QoS 在别的机器上读不出来时,ros2 bag play 一个话题都不发(11-02)
    qos.write_text("".join(f"{t}:\n  reliability: reliable\n  durability: volatile\n"
                           f"  history: keep_last\n  depth: {d}\n"
                           for t, d in ((LIDAR_TOPIC, 50), (IMU_TOPIC, 500))), encoding="utf-8")
    node_cmd = "ros2 run super_lio super_lio_node " + " ".join(node_args(cfg, ns_remap=False))
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(subprocess.Popen(["bash", "-c", src + node_cmd], env=e,
                                      stdout=open(work / "superlio.log", "wb"),
                                      stderr=subprocess.STDOUT, start_new_session=True))
        time.sleep(3)
        procs.append(subprocess.Popen(["bash", "-c", src + f"ros2 bag record -s mcap -o {out} "
                                       "/lio/odom"], env=e,
                                      stdout=open(work / "record.log", "wb"),
                                      stderr=subprocess.STDOUT, start_new_session=True))
        time.sleep(3)
        play = subprocess.run(["bash", "-c", src + f"ros2 bag play {bag} --rate {rate} --topics "
                               f"{LIDAR_TOPIC} {IMU_TOPIC} --qos-profile-overrides-path {qos}"],
                              env=e, stdout=open(work / "play.log", "wb"),
                              stderr=subprocess.STDOUT)
        time.sleep(3)
        if play.returncode != 0:
            raise SuperLioError(f"回放录包失败,日志见 {work / 'play.log'}")
    finally:
        for p in reversed(procs):         # 先停录(把位姿写完),再停 Super-LIO;按记下的进程组停
            _stop(p)
    if not out.is_dir():
        raise SuperLioError(f"Super-LIO 的位姿没录下来,日志见 {work / 'record.log'}")
    return out


def _stop(p: subprocess.Popen[bytes], grace_s: float = 60.0) -> None:
    for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGKILL, 10.0)):
        if p.poll() is not None:
            return
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------- 机器人上边走边建
def node_exe(root: Path | None = None) -> Path:
    return (ROOT if root is None else root) / "install/super_lio/lib/super_lio/super_lio_node"


def main(argv: Sequence[str] | None = None) -> int:
    """``live``:换成 ``super_lio_node`` 进程(``deploy/d1max-superlio-live`` 先 source 好 ROS 和
    Super-LIO 的工作区)。发的话题全改到 :data:`NS` 下,跟定位器、竞品的 ``/lio/*`` 不串。"""
    import argparse
    ap = argparse.ArgumentParser(prog="d1max-superlio")
    ap.add_argument("cmd", choices=["live"])
    ap.parse_args(argv)
    exe = node_exe()
    if not installed() or not exe.is_file():
        print(f"Super-LIO 未装({ROOT}):先跑 deploy/d1max-superlio-build", flush=True)
        return 2
    cfg = config_path()
    print(f"Super-LIO 建图:参数 {cfg},位姿发到 {LIVE_TOPIC}", flush=True)
    os.execv(str(exe), [str(exe), *node_args(cfg, ns_remap=True)])
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
