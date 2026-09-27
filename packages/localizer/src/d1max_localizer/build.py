"""建图打包(W09c1 决定 2、3、4):把一次建图的产物打成一个地图版本的文件 ——

- ``prior.mm``:MOLA 的局部地图(定位先验),按 ``prior_pack`` 打包(默认原样;``regroup`` 要回放验过
  再用);
- ``frames.json``:坐标换算,从建图轨迹标定;**「上」的正负号用点云判**(最密的那层水平面 —— 地面 ——
  要在雷达下面):光看轨迹分不出上下,新狗 C40011 的雷达就是 X 朝下装的,按「X 朝上」标出来的平面是
  镜像的,转向会反(2026-09-27 画 newdog2 的栅格时发现);
- ``floor.pgm`` + ``floor.yaml``:按 ``frames.json`` 的平面画的规划栅格(:mod:`d1max_localizer.grid`);
  有录包就从里面逐帧读扫描打真射线(:func:`bag_scans`,跟着狗走的人清得掉),读不了退回模拟射线,
  ``build.json`` 的 ``grid.rays`` 写着用的哪种;
- ``coverage.json``:「哪里有图」—— 建图时走过的路(地图平面上每 :data:`COVERAGE_STEP_M` 一点);
- ``build.json``:怎么建的(来源、参数、各步耗时、标定说明)。

两步::func:`run_mapping` 用 MOLA 命令行在录包上建图(雷达里程计 + 存局部地图、轨迹、simplemap;边走
边建的 W09c2 用在线跑出来的同样三样);:func:`package` 把这三样打成版本文件。MOLA 的命令都经 ``run``
调(测试换成假的)。点云、栅格要 numpy(狗上用 ROS 系统 Python 里 apt 装的那份)。
"""

from __future__ import annotations

import glob
import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract.maps import GEOMETRY_FILES
from d1max_localizer.frames import Frames, calibrate
from d1max_localizer.replay import MOLA_SHARE, read_tum

#: 前雷达在狗身上的水平偏移(朝前、朝左,米;SDK 开发指南 2.10 节,前激光雷达 x = 404.3 mm)。
FRONT_LIDAR_IN_BASE = (0.4043, 0.0)
#: RS-Airy 装法上「上」「前」是哪根轴(「上」的正负号由点云判)。
SENSOR_UP_HINT = (1.0, 0.0, 0.0)
SENSOR_FORWARD_HINT = (0.0, 0.0, 1.0)
COVERAGE_STEP_M = 0.5
#: 真射线每几帧扫描取一帧、每帧每几个点取一个(newdog2:676 帧,读包 11 s、打射线 10 s)。
SCAN_EVERY = 3
SCAN_POINT_STRIDE = 4
#: 扫描的时刻跟轨迹上最近那一帧差多少以内才配得上。
SCAN_MATCH_S = 0.05
#: 逐帧扫描只认这个话题:MOLA 跟的就是它的坐标系(rslidar_head),点乘轨迹的位姿就进了地图。
SCAN_TOPIC = "/front_lidar"
FILES = GEOMETRY_FILES
Runner = Callable[..., Any]


class BuildError(RuntimeError):
    """建图打包不成(说人话的原因)。"""


@dataclass(frozen=True)
class PriorPack:
    """先验怎么打包:``none`` 原样;``regroup`` = ``mm-kf-regroup`` 合并关键帧并按体素降采样。"""
    mode: str = "none"
    voxel: float = 0.3
    extent: float = 0.5

    @classmethod
    def parse(cls, s: str) -> PriorPack:
        """``none`` 或 ``regroup:<体素米>:<范围倍数>``。"""
        if s == "none":
            return cls()
        parts = s.split(":")
        if parts[0] != "regroup" or len(parts) != 3:
            raise ValueError(f"先验打包写法不对:{s!r}(none 或 regroup:体素:范围倍数)")
        v, e = float(parts[1]), float(parts[2])
        if not (0 < v <= 2 and 0 < e <= 5):
            raise ValueError(f"先验打包参数超出范围:{s!r}")
        return cls("regroup", v, e)

    def label(self) -> str:
        return "none" if self.mode == "none" else f"regroup:{self.voxel:g}:{self.extent:g}"


def _plugin() -> list[str]:
    found = sorted(glob.glob("/opt/ros/humble/lib/**/libmola_metric_maps.so", recursive=True))
    return ["-l", found[0]] if found else []


def run_mapping(bag: Path, work: Path, *, lidar_topic: str = "/front_lidar",
                run: Runner = subprocess.run) -> dict[str, float]:
    """MOLA 命令行在录包上建图:``work`` 里得到 ``raw_prior.mm``、``traj.tum``、``map.simplemap``。
    回耗时。"""
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MOLA_LIDAR_TOPIC": lidar_topic,
           "MOLA_TF_BASE_LINK": "rslidar_head" if lidar_topic == "/front_lidar" else "base_link",
           "MOLA_SAVE_MM": str(work / "raw_prior.mm"), "MOLA_LOCAL_MAP_MAX_SIZE": "0"}
    cmd = ["mola-lidar-odometry-cli", "-c", str(MOLA_SHARE / "pipelines/lidar3d-default.yaml"),
           "--state-estimator-param-file",
           str(MOLA_SHARE / "state-estimator-params/state-estimation-simple.yaml"),
           "--input-rosbag2", str(bag), "--lidar-sensor-label", lidar_topic,
           "--output-tum-path", str(work / "traj.tum"),
           "--output-simplemap", str(work / "map.simplemap"), "--progress-bar-period", "0"]
    t0 = time.monotonic()
    _run(run, cmd, env, work / "mola.log", "MOLA 建图")
    return {"mapping_s": round(time.monotonic() - t0, 1)}


def package(work: Path, out: Path, *, prior_pack: PriorPack | None = None,
            sensor_in_base: tuple[float, float] = FRONT_LIDAR_IN_BASE, source: str = "",
            run: Runner = subprocess.run, timings: dict[str, float] | None = None,
            bag: Path | None = None, lidar_topic: str = SCAN_TOPIC,
            read_scans: Callable[..., Iterator[Any]] | None = None) -> list[str]:
    """``work`` 里的 ``raw_prior.mm``、``traj.tum``、``map.simplemap`` → ``out`` 里的版本文件。回
    文件名。给了 ``bag``(建图的那个录包)就从里面逐帧读扫描打真射线。"""
    prior_pack = prior_pack or PriorPack()
    import numpy as np

    from d1max_localizer import grid

    for name in ("raw_prior.mm", "traj.tum", "map.simplemap"):
        if not (work / name).is_file():
            raise BuildError(f"建图没出 {name}")
    out.mkdir(parents=True, exist_ok=True)
    timings = dict(timings or {})
    traj = read_tum(work / "traj.tum")
    if len(traj) < 10:
        raise BuildError(f"建图轨迹只有 {len(traj)} 帧,太短")
    t0 = time.monotonic()
    points = _points(work, run)
    timings["points_s"] = round(time.monotonic() - t0, 1)
    sensor = np.array([p for _, p, _ in traj])
    frames, why = orient(points, traj)
    frames = frames.with_sensor_in_base(*sensor_in_base)
    frames.save(out / "frames.json")
    t0 = time.monotonic()
    body = np.array([frames.to_map2d(p, q)[:2] for _, p, q in traj])
    g, rays = _render(points, sensor, frames, body, traj, bag, lidar_topic,
                      read_scans or bag_scans)
    grid.write(g, out / "floor")
    timings["grid_s"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    _pack_prior(work / "raw_prior.mm", out / "prior.mm", prior_pack, run, work)
    timings["prior_s"] = round(time.monotonic() - t0, 1)
    cov = coverage(traj, frames)
    (out / "coverage.json").write_text(json.dumps({"version": 1, "step_m": COVERAGE_STEP_M,
                                                   "path": cov}) + "\n")
    (out / "build.json").write_text(json.dumps({
        "version": 1, "source": source, "builder": "mola-lidar-odometry", "prior_pack":
        prior_pack.label(), "frames": why, "frames_count": len(traj),
        "grid": {"res": g.res, "size": list(g.image.shape[::-1]), "origin": list(g.origin),
                 "rays": rays},
        "timings_s": timings}, ensure_ascii=False, indent=2) + "\n")
    return list(FILES)


def _render(points: Any, sensor: Any, frames: Frames, body: Any, traj: Sequence[Any],
            bag: Path | None, topic: str, read_scans: Callable[..., Iterator[Any]]
            ) -> tuple[Any, str]:
    """画栅格:有录包用逐帧扫描的真射线,读不了(没有 ROS、话题不对、包坏了)退回模拟射线。回
    (栅格, 用的哪种)。"""
    from d1max_localizer import grid

    if bag is None:
        why = "没给录包"
    elif topic != SCAN_TOPIC:
        why = f"话题 {topic} 不是 {SCAN_TOPIC}"
    else:
        used = [0]

        def counted() -> Iterator[Any]:
            for s in read_scans(bag, traj, topic):
                used[0] += 1
                yield s
        try:
            g = grid.render(points, sensor, frames, scans=counted(), body_path=body)
        except Exception as exc:               # noqa: BLE001 - 读不了扫描就退回,原因写进 build.json
            why = f"读不了逐帧扫描:{type(exc).__name__}: {exc}"[:300]
        else:
            if used[0]:
                return g, f"scans:{used[0]}"
            why = "录包里没有配得上轨迹的扫描"
    return grid.render(points, sensor, frames, body_path=body), f"synthetic:{why}"


def cloud_xyz(msg: Any) -> Any:
    """``sensor_msgs/PointCloud2`` → (N,3) 的 x、y、z(按字段偏移取,别的字段类型不管),丢掉 NaN。"""
    import numpy as np

    off = {f.name: f.offset for f in msg.fields}
    order = ">" if getattr(msg, "is_bigendian", False) else "<"
    dt = np.dtype({"names": ["x", "y", "z"], "formats": [order + "f4"] * 3,
                   "offsets": [off["x"], off["y"], off["z"]], "itemsize": msg.point_step})
    a = np.frombuffer(bytes(msg.data), dtype=dt)
    xyz = np.stack([a["x"], a["y"], a["z"]], 1).astype(float)
    return xyz[np.isfinite(xyz).all(1)]


def bag_scans(bag: Path, traj: Sequence[Any], topic: str = SCAN_TOPIC, *,
              every: int = SCAN_EVERY) -> Iterator[tuple[Any, Any]]:
    """从录包逐帧读扫描(每 ``every`` 帧取一帧),按时刻配上轨迹里的位姿,回 ``(雷达位置, 这一帧的点)``
    (MOLA 系)。要 ROS 的系统 Python(``rosbag2_py``)。"""
    import numpy as np
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2

    from d1max_localizer.frames import quat_to_mat

    stamps = np.array([t for t, _, _ in traj])
    storage = "mcap" if any(Path(bag).glob("*.mcap")) else "sqlite3"
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=storage),
           rosbag2_py.ConverterOptions("", ""))
    r.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    i = -1
    while r.has_next():
        _, raw, _ = r.read_next()
        i += 1
        if i % every:
            continue
        m = deserialize_message(raw, PointCloud2)
        st = m.header.stamp.sec + m.header.stamp.nanosec * 1e-9
        k = int(np.clip(np.searchsorted(stamps, st), 1, len(stamps) - 1))
        k = k - 1 if abs(stamps[k - 1] - st) <= abs(stamps[k] - st) else k
        if abs(stamps[k] - st) > SCAN_MATCH_S:
            continue
        _, p, q = traj[k]
        R = np.array(quat_to_mat(q))
        xyz = cloud_xyz(m)[::SCAN_POINT_STRIDE]
        yield np.array(p, float), xyz @ R.T + np.array(p, float)


def orient(points: Any, traj: Sequence[Any]) -> tuple[Frames, str]:
    """从轨迹标 ``frames``;「上」的正负号用点云判:以雷达为准的相对高度里,最密的那层水平面(地面)要在
    雷达下面,在上面就把「上」翻过来重标。回 ``(frames, 说明)``。"""
    import numpy as np

    from d1max_localizer import grid

    poses = [(p, q) for _, p, q in traj]
    sensor = np.array([p for p, _ in poses])
    note = ""
    for hint in (SENSOR_UP_HINT, tuple(-c for c in SENSOR_UP_HINT)):
        f, why = calibrate(poses, sensor_up=hint, forward_hint=SENSOR_FORWARD_HINT, explain=True)
        pts = np.asarray(points, float)
        q = grid.level(pts[:: max(1, len(pts) // 400000)], f)
        rel = q[:, 2] - float(np.median(grid.level(sensor, f)[:, 2]))
        hist, edges = np.histogram(rel, bins=60, range=(-1.5, 1.5))
        k = int(np.argmax(hist))
        peak = float((edges[k] + edges[k + 1]) / 2)
        if peak < 0:
            return f, f"{why};地面在雷达下方 {-peak:.2f} m{note}"
        note = f"(按装法的「上」{_fmt(hint)} 标出来地面在雷达上方 {peak:.2f} m:雷达是倒装的,翻过来)"
    raise BuildError("点云里找不到雷达下方的地面:标不出「上」")


def coverage(traj: Sequence[Any], frames: Frames, step: float = COVERAGE_STEP_M
             ) -> list[list[float]]:
    """建图时走过的路:地图平面上(狗身中心)每 ``step`` 米一点。"""
    out: list[list[float]] = []
    for _, p, q in traj:
        x, y, _ = frames.to_map2d(p, q)
        if not out or math.hypot(x - out[-1][0], y - out[-1][1]) >= step:
            out.append([round(x, 2), round(y, 2)])
    return out


def _points(work: Path, run: Runner) -> Any:
    """simplemap → 点云(``sm2mm`` 出原始点图,``mm2ply -b`` 导出 x、y、z)。"""
    import numpy as np

    _run(run, ["sm2mm", "-i", str(work / "map.simplemap"), "-o", str(work / "points.mm"),
               "--no-progress-bar"], None, work / "sm2mm.log", "simplemap 转点云")
    _run(run, ["mm2ply", *_plugin(), "-i", str(work / "points.mm"), "-o",
               str(work / "points"), "-b", "--export-fields", "x,y,z"], None,
         work / "mm2ply.log", "导出点云")
    plys = sorted(work.glob("points*.ply"))
    if not plys:
        raise BuildError("导出点云没出文件")
    raw = plys[0].read_bytes()
    try:
        head = raw.index(b"end_header\n") + len(b"end_header\n")
    except ValueError:
        raise BuildError("点云文件头不对") from None
    return np.frombuffer(raw, dtype="<f4", offset=head).reshape(-1, 3).astype(float)


def _pack_prior(raw: Path, out: Path, pack: PriorPack, run: Runner, work: Path) -> None:
    if pack.mode == "none":
        shutil.copy2(raw, out)
        return
    _run(run, ["mm-kf-regroup", *_plugin(), "-i", str(raw), "-o", str(out), "--decimate-voxel",
               f"{pack.voxel:g}", "--extent-factor", f"{pack.extent:g}"], None,
         work / "regroup.log", "先验打包")
    if not out.is_file():
        raise BuildError("先验打包没出文件")


def _run(run: Runner, cmd: list[str], env: dict[str, str] | None, log: Path, what: str) -> None:
    with open(log, "w") as fh:
        r = run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT)
    if getattr(r, "returncode", 0) != 0:
        tail = log.read_text(errors="replace")[-300:] if log.exists() else ""
        raise BuildError(f"{what}失败(退出码 {r.returncode}):{tail}")


def _fmt(v: Sequence[float]) -> str:
    return "(" + ", ".join(f"{c:g}" for c in v) + ")"
