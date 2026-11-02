"""Super-LIO 做巡逻定位(W68)的纯计算部分:初值换算、坐标换算、重定位参数文件、点云套图打分、PCD。

2026-11-02 用户定:Super-LIO 做主定位和重定位,MOLA 不再运行(``--engine mola`` 只留给工程师手动切)。
依据是离线 A/B(``runs/loc-ab/``):商场第二圈在第一圈的图上定位,Super-LIO 最大误差 13 cm、无跳变;
MOLA 最大 2.6 m。

Super-LIO 的重定位模式(``relocation_node``,GPL-3.0,单独进程跑)只认**一次**初值(参数
``lio.relocation.init_pose``,度),换位置 = 换参数文件重启进程。它要的初值是 **IMU 在地图里的位姿**,
不是机身、也不是雷达(11-02 实跑:把机身位姿当初值,NDT/ICP 锁到错的地方还过了它自己的检查,
之后跑飞到 100 km)。地图点云按 ``lio.map.save_map_dir`` 找,那个路径**拼在它的源码目录后面**。

它不报每帧对得好不好,所以质量我们自己算(:class:`MapFit`):每隔几帧取一帧点云,按它给的位姿放进
地图,数落在地图占用体素附近的比例。不改 Super-LIO 源码。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from d1max_localizer import superlio as S
from d1max_localizer.frames import Frames, mat_to_quat, quat_to_mat

#: 定位用的 Super-LIO 发的话题都改到这下面(跟竞品、建图的 ``/lio/*``、``/d1max_mapping/*`` 不串)。
NS = "/d1max_loc"
POSE_TOPIC = f"{NS}/lio/odom"
#: 地图包里给 Super-LIO 的点云(建图打包时从回环后的先验导出,跟 ``frames.json`` 同一个坐标系)。
MAP_FILE = "superlio_map.pcd"
#: 导出时的降采样(Super-LIO 自己载图后还会按 ``submap_resolution`` 再降)。
MAP_VOXEL_M = 0.25
#: 重定位模式要改的参数:取自上游 ``config/relocation.yaml``(局部图分辨率、容量),``update_map`` 关掉
#: (巡逻时不往图里加东西,图只在建图时改)。
RELOC_PARAMS: tuple[tuple[str, str], ...] = (
    ("lio.hash_map.insert_resolution", "0.5"),
    ("lio.submap.submap_resolution", "0.5"),
    ("lio.submap.submap_capacity", "1000000"),
    ("lio.relocation.update_map", "false"),
)
#: 套图打分:占用体素边长;点落在占用体素或它的 26 个邻居里算对上(容差约 0.1–0.2 m)。11-02 商场真数据
#: (第二圈按 Super-LIO 重定位位姿套第一圈的图,44 帧):对的中位 0.99、最低 0.82;偏 1 m 中位 0.50、
#: 偏 3 m 0.23、转 10° 0.52。0.2 m 时分不开(偏 3 m 还有 0.45)。
FIT_VOXEL_M = 0.1
#: 每帧最多取这么多点来算(均匀抽)。
FIT_POINTS = 2000
#: 能算的点少于这些就不打分(雷达被挡、全是地面)。
FIT_MIN_POINTS = 100
#: 比雷达低这么多以下的点当地面,不算:位姿偏了,地面上的点照样落在地面上,算进去会把错锁掩盖掉。
FIT_FLOOR_BELOW_M = 0.2

Vec = tuple[float, float, float]
Quat = tuple[float, float, float, float]
Extrinsic = tuple[Sequence[Sequence[float]], Sequence[float]]


class SlioLocError(RuntimeError):
    """Super-LIO 定位起不来的原因(给人看的)。"""


# ---------------------------------------------------------------- 坐标换算

def mat_quat(R: Any) -> Quat:
    return mat_to_quat([list(map(float, r)) for r in np.asarray(R, dtype=float).tolist()])


def imu_pose(frames: Frames, ext: Extrinsic, x: float, y: float, yaw: float
             ) -> tuple[Vec, list[list[float]]]:
    """地图平面上机身中心的 ``x, y, yaw`` → IMU 在地图里的位姿 ``(p, R)``。

    先按 ``frames`` 换成雷达位姿(``T_wl``,高度取建图时的平均),再 ``T_wi = T_wl · T_il⁻¹``。"""
    p_l, q_l = frames.to_mola(x, y, yaw)
    R_wl = np.array(quat_to_mat(q_l))
    R_il, t_il = np.array(ext[0], dtype=float), np.array(ext[1], dtype=float)
    R_wi = R_wl @ R_il.T
    p_wi = np.array(p_l) - R_wi @ t_il
    return (float(p_wi[0]), float(p_wi[1]), float(p_wi[2])), R_wi.tolist()


def init_param(p: Sequence[float], R: Any) -> tuple[float, float, float, float, float, float]:
    """``lio.relocation.init_pose``:``x, y, z, 横滚, 俯仰, 航向``(度)。横滚、俯仰给 0 —— 它起来时
    按 IMU 量的重力自己对;航向取 IMU 的 x 轴在水平面上的朝向。"""
    R = np.asarray(R, dtype=float)
    yaw = math.degrees(math.atan2(R[1, 0], R[0, 0]))
    return (float(p[0]), float(p[1]), float(p[2]), 0.0, 0.0, float(yaw))


def lidar_pose(p: Sequence[float], q: Sequence[float], ext: Extrinsic) -> tuple[Vec, Quat]:
    """Super-LIO 发的 IMU 位姿 → 雷达位姿(核心要的就是雷达位姿)。"""
    p_wl, R_wl = S.lidar_pose(p, q, ext[0], ext[1])
    return (float(p_wl[0]), float(p_wl[1]), float(p_wl[2])), mat_quat(R_wl)


# ---------------------------------------------------------------- 进程与参数

def source_dir(root: Path) -> Path:
    """Super-LIO 的源码目录(它拼地图路径的基准):机器人上 ``<root>/ws/src/...``,开发机工作区
    ``<root>/src/...``。"""
    for sub in ("ws/src/Super-LIO/src/super_lio", "src/Super-LIO/src/super_lio"):
        d = Path(root) / sub
        if (d / "CMakeLists.txt").is_file():
            return d
    raise SlioLocError(f"找不到 Super-LIO 源码目录({root}):重新跑 deploy/d1max-superlio-build")


def reloc_exe(root: Path) -> Path:
    return Path(root) / "install/super_lio/lib/super_lio/relocation_node"


def _set(lines: list[str], key: str, value: str) -> None:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*:")
    lines[:] = [ln for ln in lines if not pat.match(ln)]
    for i, ln in enumerate(lines):
        if ln.strip() == "ros__parameters:":
            lines.insert(i + 1, f"    {key}: {value}")
            return
    raise SlioLocError("Super-LIO 参数文件里没有 ros__parameters")


def reloc_config(base: str, *, map_dir: Path, src_dir: Path,
                 init: Sequence[float]) -> str:
    """在建图用的参数文件(同一份外参、滤波)上改出重定位用的:载 ``map_dir/MAP_FILE``、不存图、
    不往图里加、初值 ``init``(6 个小数;给整数它不认)。"""
    if len(init) != 6 or not all(math.isfinite(float(v)) for v in init):
        raise SlioLocError(f"初值要 6 个有限的数:{list(init)}")
    rel = os.path.relpath(os.path.realpath(map_dir), os.path.realpath(src_dir))
    lines = base.splitlines()
    settings = [("lio.map.save_map", "false"), ("lio.map.save_map_dir", f'"{rel}"'),
                ("lio.map.map_name", f'"{MAP_FILE}"'), *RELOC_PARAMS,
                ("lio.relocation.init_pose",
                 "[" + ", ".join(repr(float(v)) for v in init) + "]")]
    for k, v in reversed(settings):
        _set(lines, k, v)
    return "\n".join(lines) + "\n"


def node_argv(exe: Path, cfg: Path) -> list[str]:
    """``relocation_node`` 的命令行:参数文件、输入话题,发的话题全改到 :data:`NS` 下。"""
    args = [str(exe), "--ros-args", "--params-file", str(cfg),
            "-p", f"lio.ros.lidar_topic:={S.LIDAR_TOPIC}",
            "-p", f"lio.ros.imu_topic:={S.IMU_TOPIC}"]
    for t in S.PUBLISHED:
        args += ["-r", f"{t}:={NS}{t}"]
    return args


# ---------------------------------------------------------------- 点云

def read_pcd(path: Path) -> np.ndarray:
    """二进制 PCD(Super-LIO 存的、我们导出的)→ ``(N, 3)`` float32。"""
    raw = Path(path).read_bytes()
    head: dict[str, list[str]] = {}
    pos = 0
    while True:
        end = raw.index(b"\n", pos)
        line = raw[pos:end].decode("ascii", "replace").strip()
        pos = end + 1
        if line and not line.startswith("#"):
            k, *v = line.split()
            head[k.upper()] = v
            if k.upper() == "DATA":
                break
    if head["DATA"][0] != "binary":
        raise SlioLocError(f"{path}:只认二进制 PCD(DATA {head['DATA'][0]})")
    fields, sizes = head["FIELDS"], [int(s) for s in head["SIZE"]]
    counts = [int(c) for c in head.get("COUNT", ["1"] * len(fields))]
    n = int(head["POINTS"][0])
    widths = [s * c for s, c in zip(sizes, counts, strict=True)]
    step = sum(widths)
    offs = dict(zip(fields, np.cumsum([0, *widths])[:-1], strict=True))
    buf = np.frombuffer(raw, dtype=np.uint8, count=n * step, offset=pos).reshape(n, step)
    return np.stack([np.ascontiguousarray(buf[:, offs[a]:offs[a] + 4]).view("<f4")[:, 0]
                     for a in ("x", "y", "z")], axis=1)


def write_pcd(path: Path, xyz: Any) -> None:
    """``(N, 3)`` → 二进制 PCD(字段 x y z intensity,跟 Super-LIO 存的一样;强度给 0)。"""
    pts = np.zeros((len(xyz), 4), dtype="<f4")
    pts[:, :3] = np.asarray(xyz, dtype=np.float32)
    head = ("# .PCD v0.7 - Point Cloud Data file format\nVERSION 0.7\nFIELDS x y z intensity\n"
            "SIZE 4 4 4 4\nTYPE F F F F\nCOUNT 1 1 1 1\n"
            f"WIDTH {len(pts)}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\n"
            f"POINTS {len(pts)}\nDATA binary\n")
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(head.encode("ascii") + pts.tobytes())
    os.replace(tmp, path)


def voxel_down(xyz: Any, res: float) -> np.ndarray:
    """每个 ``res`` 体素留一个点(第一个)。"""
    a = np.asarray(xyz, dtype=np.float64)
    a = a[np.isfinite(a).all(axis=1)]
    _, idx = np.unique(np.floor(a / res).astype(np.int64), axis=0, return_index=True)
    return a[np.sort(idx)]


def _keys(v: np.ndarray) -> np.ndarray:
    """体素下标 → 一个 int64(每轴 21 位,±1 百万个体素 = ±200 km 够了)。"""
    v = v + (1 << 20)
    return (v[:, 0] << 42) | (v[:, 1] << 21) | v[:, 2]


_NEIGH = np.array([(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1)],
                  dtype=np.int64)


class MapFit:
    """地图的占用体素;:meth:`score` 给一帧点云按位姿放进来对得上的比例。"""

    def __init__(self, map_xyz: Any, res: float = FIT_VOXEL_M) -> None:
        self.res = float(res)
        a = np.asarray(map_xyz, dtype=np.float64)
        a = a[np.isfinite(a).all(axis=1)]
        self._occ = np.unique(_keys(np.floor(a / self.res).astype(np.int64)))

    def score(self, scan: Any, p: Sequence[float], q: Sequence[float]) -> float | None:
        """``scan``:雷达系里的点 ``(N, 3)``;``p, q``:雷达在地图里的位姿。离雷达太近(机身)、太远、
        地面上的点不算;能算的点不够 :data:`FIT_MIN_POINTS` 回 ``None``。"""
        a = np.asarray(scan, dtype=np.float64).reshape(-1, 3)
        a = a[np.isfinite(a).all(axis=1)]
        r2 = (a * a).sum(axis=1)
        a = a[(r2 >= S.RANGE_MIN_M ** 2) & (r2 <= S.RANGE_MAX_M ** 2)]
        if len(a) == 0:
            return None
        w = a @ np.array(quat_to_mat(q)).T + np.asarray(p, dtype=np.float64)
        w = w[w[:, 2] > float(p[2]) - FIT_FLOOR_BELOW_M]
        if len(w) < FIT_MIN_POINTS:
            return None
        if len(w) > FIT_POINTS:
            w = w[np.linspace(0, len(w) - 1, FIT_POINTS).astype(np.int64)]
        v = np.floor(w / self.res).astype(np.int64)
        k = _keys((v[:, None, :] + _NEIGH[None, :, :]).reshape(-1, 3)).reshape(len(v), -1)
        i = np.searchsorted(self._occ, k)
        hit = (i < len(self._occ)) & (self._occ[np.minimum(i, len(self._occ) - 1)] == k)
        return float(hit.any(axis=1).mean())
