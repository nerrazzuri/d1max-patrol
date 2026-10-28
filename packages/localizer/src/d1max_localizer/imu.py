"""IMU 当 MOLA 的转速先验(工单 27)。

不给 IMU 时,MOLA 的运动模型靠前后帧外推:某一帧 ICP 错跳一下(玻璃前丢了一帧之后),就被当成「在转」
(录包里估出 1.66 rad/s 的假转速),下一帧按它外推、越推越远,回不来。给了 IMU,角速度按陀螺仪,
站着就是 0,错一两帧自己回来(``glass-160827`` 前后合并回放:错的帧 180 → 2)。

- :class:`ImuPrior`:话题 + 安装角(IMU 系 → MOLA 的机身系,也就是点云系;定位器按「雷达在原点」起
  MOLA)。狗上 ``ros2 launch`` 用启动参数 + 环境变量,回放的命令行用环境变量。
- :func:`fit_axes`:从回放轨迹的机身角速度和陀螺仪读数核安装角(``d1max-loc imu-axes``),换狗、换装法
  用它,不用手算。
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: 拟合要的最小转动:机身角速度最强那根轴的标准差(rad/s)。
MIN_RATE_STD = 0.1
#: 陀螺仪(转到机身系后)跟机身角速度在转得最多那根轴上的相关系数,低于它不给结论。
MIN_CORR = 0.8
#: 离最近的「直角装法」(轴对轴)不超过这么多度就吸过去。
SNAP_DEG = 20.0
#: 次强的轴的角速度标准差不到最强的这么多倍 = 另两根轴只晃了一点(按残差挑直角装法,不信自由拟合)。
WEAK_RATIO = 0.3
#: 残差最小的两种直角装法至少差这么多(相对)才算分得出来。
MIN_MARGIN = 0.02
#: 外审 R3:另两根轴晃得少时,直角装法比自由拟合多出的残差超过「另两根轴的晃」这么大一份 = 数据明摆着
#: 不是直角装法(真装歪了 20° 也有 12%),不硬吸;噪声把拟合带偏时,两者残差差不多。
WEAK_EXCESS = 0.08


@dataclass(frozen=True)
class ImuPrior:
    topic: str
    ypr_deg: tuple[float, float, float]           # IMU 在机身系里的姿态(MRPT:yaw、pitch、roll,度)

    def launch_args(self) -> list[str]:
        """``ros2 launch mola_lidar_odometry ...`` 要加的参数:安装角不查 tf,按 :meth:`env`。"""
        return [f"imu_topic_name:={self.topic}", "ignore_imu_pose_from_tf:=True"]

    def env(self) -> dict[str, str]:
        y, p, r = self.ypr_deg
        return {"IMU_POSE_YAW": _num(y), "IMU_POSE_PITCH": _num(p), "IMU_POSE_ROLL": _num(r)}

    def cli_env(self) -> dict[str, str]:
        """``mola-lidar-odometry-cli`` 读录包时要的环境变量。"""
        return {"MOLA_IMU_TOPIC": self.topic, "MOLA_USE_FIXED_IMU_POSE": "true", **self.env()}


def parse_ypr(s: str) -> tuple[float, float, float]:
    """``"-90,0,180"`` → ``(-90.0, 0.0, 180.0)``(度)。命令行里负数开头要写成
    ``--imu-ypr=-90,0,180``。"""
    v = [float(c) for c in s.split(",")]
    if len(v) != 3 or not all(math.isfinite(c) for c in v):
        raise ValueError("要三个数:yaw,pitch,roll(度)")
    return (v[0], v[1], v[2])


def _num(v: float) -> str:
    v = round(v, 6) + 0.0                          # -0.0 → 0.0
    if v == -180.0:
        v = 180.0
    return f"{v:g}"


# ------------------------------------------------------------ 安装角 ↔ 旋转矩阵


Mat = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


def matrix_from_ypr(ypr: Sequence[float]) -> Any:
    """MRPT 的约定:R = Rz(yaw)·Ry(pitch)·Rx(roll);ω_机身 = R·ω_IMU。"""
    import numpy as np
    y, p, r = (math.radians(v) for v in ypr)
    cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), \
        math.sin(r)
    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    return rz @ ry @ rx


def ypr_from_matrix(m: Any) -> tuple[float, float, float]:
    sp = max(-1.0, min(1.0, -float(m[2][0])))
    pitch = math.asin(sp)
    if abs(sp) > 1.0 - 1e-9:                       # 万向锁:roll 归 0
        yaw = math.atan2(-float(m[0][1]), float(m[1][1]))
        roll = 0.0
    else:
        yaw = math.atan2(float(m[1][0]), float(m[0][0]))
        roll = math.atan2(float(m[2][1]), float(m[2][2]))
    out = tuple(math.degrees(v) for v in (yaw, pitch, roll))
    return tuple(180.0 if abs(v + 180.0) < 1e-9 else round(v, 9) + 0.0 for v in out)  # type: ignore[return-value]


def _axis_aligned() -> list[Any]:
    import numpy as np
    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1.0, -1.0), repeat=3):
            m = np.zeros((3, 3))
            for i, (j, s) in enumerate(zip(perm, signs, strict=True)):
                m[i, j] = s
            if np.linalg.det(m) > 0:
                out.append(m)
    return out


def _angle_deg(a: Any, b: Any) -> float:
    import numpy as np
    c = (float(np.trace(a.T @ b)) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# ------------------------------------------------------------ 从录包核安装角


@dataclass(frozen=True)
class AxesFit:
    ok: bool
    why: str
    ypr: tuple[float, float, float] = (0.0, 0.0, 0.0)
    snapped: bool = False
    snap_deg: float = 0.0                          # 拟合值离吸过去的直角装法多少度
    yaw_corr: float = 0.0                          # 转得最多那根轴上的相关系数
    frames: int = 0


def fit_axes(w_body: Any, gyro: Any) -> AxesFit:
    """``w_body``:机身系角速度(N×3,来自回放轨迹);``gyro``:同一时段的陀螺仪平均(N×3)。求 R 使
    ω_机身 ≈ R·ω_IMU(去掉均值后正交 Procrustes,零偏顺带去掉)。"""
    import numpy as np
    wb = np.asarray(w_body, dtype=float)
    g = np.asarray(gyro, dtype=float)
    ok = np.isfinite(wb).all(axis=1) & np.isfinite(g).all(axis=1)
    wb, g = wb[ok], g[ok]
    n = len(wb)
    if n < 30:
        return AxesFit(False, f"能配上的帧只有 {n} 个", frames=n)
    wb_c, g_c = wb - wb.mean(axis=0), g - g.mean(axis=0)
    std = wb_c.std(axis=0)
    k = int(np.argmax(std))
    if std[k] < MIN_RATE_STD:
        return AxesFit(False, f"这段没怎么转(角速度标准差最大 {std[k]:.3f} rad/s,"
                              f"要 ≥ {MIN_RATE_STD}):录一段原地左右转", frames=n)
    u, _, vt = np.linalg.svd(g_c.T @ wb_c)
    d = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    pred = g_c @ r.T
    corr = float(np.corrcoef(wb_c[:, k], pred[:, k])[0, 1])
    if not corr > MIN_CORR:
        return AxesFit(False, f"陀螺仪跟机身的转动对不上(相关 {corr:.2f},要 > {MIN_CORR}):"
                              "IMU 话题选对了吗、时间对得上吗", yaw_corr=corr, frames=n)
    # 24 种直角装法各算残差:原地转的录包只有一根轴转得强,自由拟合在另两根上会漂(真包漂过 20°),
    # 挑残差最小的;最小的两个分不出来 = 另两根轴定不下来
    res = sorted((float(np.sum((wb_c - g_c @ m.T) ** 2)), i, m)
                 for i, m in enumerate(_axis_aligned()))
    (r1, _, best), (r2, _, _) = res[0], res[1]
    off = _angle_deg(best, r)
    weak = bool(np.sort(std)[-2] < WEAK_RATIO * std[k])
    if r2 - r1 < MIN_MARGIN * max(r1, 1e-12) + 1e-12:
        return AxesFit(False, "只转了朝向、机身几乎没晃:另两根轴定不下来(录一段走动的)",
                       yaw_corr=corr, frames=n)
    if off > SNAP_DEG and weak:
        free = float(np.sum((wb_c - g_c @ r.T) ** 2))
        shake = float(np.sum(np.delete(wb_c, k, axis=1) ** 2))
        if r1 - free > WEAK_EXCESS * shake:
            return AxesFit(False, f"另两根轴晃得少,拟合值离最近的直角装法 {off:.1f}°、直角装法明显"
                                  "对不上:定不下来(录一段走动的、上下坡或侧倾的)",
                           yaw_corr=corr, frames=n)
    if off <= SNAP_DEG or weak:
        how = "另两根轴晃得少,按残差挑" if weak and off > SNAP_DEG else "吸到直角装法"
        return AxesFit(True, f"{how}(拟合值离它 {off:.1f}°)", ypr_from_matrix(best), True, off,
                       _corr(wb_c, g_c, best, k), n)
    return AxesFit(True, f"离最近的直角装法 {off:.1f}°,按拟合值", ypr_from_matrix(r), False, off,
                   corr, n)


def _corr(wb_c: Any, g_c: Any, m: Any, k: int) -> float:
    """外审 R3:报的相关系数按最后给出的装法算。"""
    import numpy as np
    return float(np.corrcoef(wb_c[:, k], (g_c @ m.T)[:, k])[0, 1])


def body_rates(rows: Sequence[tuple[float, Sequence[float], Sequence[float]]]) -> tuple[Any, Any]:
    """TUM 轨迹(``replay.read_tum``)→ 每两帧之间中点的时刻、机身系角速度(rad/s)。间隔太短 / 太长
    (丢帧)的不要。"""
    import numpy as np

    from d1max_localizer.frames import quat_to_mat
    mats = [np.array(quat_to_mat(q)) for _, _, q in rows]
    mid, w = [], []
    for i in range(len(rows) - 1):
        dt = rows[i + 1][0] - rows[i][0]
        if not _usable(dt):
            continue
        d = mats[i].T @ mats[i + 1]
        ang = math.acos(max(-1.0, min(1.0, (float(np.trace(d)) - 1.0) / 2.0)))
        if ang < 1e-9:
            v = np.zeros(3)
        else:
            v = ang / (2.0 * math.sin(ang)) * np.array([d[2, 1] - d[1, 2], d[0, 2] - d[2, 0],
                                                        d[1, 0] - d[0, 1]])
        mid.append((rows[i][0] + rows[i + 1][0]) / 2.0)
        w.append(v / dt)
    return np.array(mid), np.array(w).reshape(-1, 3)


def _usable(dt: float) -> bool:
    return 0.02 <= dt <= 0.3


def average_between(gyro: Any, stamps: Any) -> Any:
    """``gyro``:N×4(时刻、x、y、z);``stamps``:轨迹的帧时刻。→ 每两帧之间陀螺仪的平均
    (没读数的 NaN)。"""
    import numpy as np
    g = np.asarray(gyro, dtype=float)
    s = np.asarray(stamps, dtype=float)
    out = np.full((max(len(s) - 1, 0), 3), np.nan)
    if not len(g):
        return out
    idx = np.searchsorted(g[:, 0], s)
    for i in range(len(s) - 1):
        a, b = idx[i], idx[i + 1]
        if b > a:
            out[i] = g[a:b, 1:].mean(axis=0)
    return out


def fit_from_bag(bag: Any, rows: Sequence[tuple[float, Sequence[float], Sequence[float]]], *,
                 topic: str) -> AxesFit:
    """录包里的陀螺仪(按消息头时刻)+ 回放轨迹(机身 = 点云系)→ :func:`fit_axes`。要 rosbags。"""
    import numpy as np
    from rosbags.highlevel import AnyReader
    from rosbags.typesys import Stores, get_typestore
    gyro = []
    with AnyReader([bag], default_typestore=get_typestore(Stores.ROS2_HUMBLE)) as r:
        conns = [c for c in r.connections if c.topic == topic]
        if not conns:
            return AxesFit(False, f"录包里没有 {topic}")
        for c, _, raw in r.messages(connections=conns):
            m = r.deserialize(raw, c.msgtype)
            a = m.angular_velocity
            gyro.append((m.header.stamp.sec + m.header.stamp.nanosec * 1e-9, a.x, a.y, a.z))
    gyro.sort()
    stamps = np.array([t for t, _, _ in rows])
    _, w = body_rates(rows)
    g = average_between(np.array(gyro).reshape(-1, 4), stamps)
    usable = np.array([_usable(float(dt)) for dt in np.diff(stamps)], dtype=bool)
    return fit_axes(w, g[usable])
