"""MOLA 系的三维位姿 ↔ 地图平面位姿(W09b 决定 1)。

MOLA 的位姿、先验都在「建图起点那一刻的雷达系」里;D1 Max 的 RS-Airy 原始系 X 是竖直轴
(C40011 上 X **朝下**:录包里地面在 +X 0.5 m,W11 #64),所以这个系的「上」不是 Z。「上」的正负号
**一定要按数据判**(``build.orient`` 按点云里地面在雷达下面判):弄反了,地图平面是镜像的,朝向角跟
实际转向反着走。
代理要的是**地图平面**上的 ``x, y, yaw``(狗身中心):

- **地图平面系** = 把 MOLA 系转一下,让「上」对准 +Z(最小的那个旋转),平面坐标取转过之后的 x、y;
  原点不动。
- **朝向** = 雷达「朝前」那根轴转到地图平面系之后的方向角。
- **狗身中心** = 雷达位置减去雷达在狗身上的水平偏移(``sensor_in_base``:朝前、朝左,米;按 URDF 取)。

参数存 ``frames.json``(跟先验放一起):``up``(MOLA 系里的「上」)、``sensor_up``/``sensor_forward``
(雷达系里的上、前)、``sensor_height``(建图时雷达在地图平面系里的平均高度,反算三维初值用)、
``sensor_in_base``。:func:`calibrate` 从建图轨迹估出前四项:「上」= 雷达「上」轴的平均;「朝前」=
狗走动时的速度方向在雷达系里的平均。

纯 Python(狗上的节点跑在 ROS 的系统 Python 里,不带 numpy 也能用)。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError

Vec = tuple[float, float, float]
Quat = tuple[float, float, float, float]            # (x, y, z, w),跟 ROS 一样
Mat = tuple[Vec, Vec, Vec]
VERSION = 1
#: frames.json 的合理范围(狗上的文件,写歪了要拒,不能让它算出离谱的位置):雷达离狗身中心的水平偏移、
#: 建图时雷达的高度。
MAX_SENSOR_OFFSET_M = 2.0
MAX_SENSOR_HEIGHT_M = 100.0


# ------------------------------------------------------------ 小工具


def _unit(v: Sequence[float]) -> Vec:
    n = math.sqrt(sum(c * c for c in v))
    if not n > 1e-9:
        raise ValueError("零向量没有方向")
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _apply(m: Mat, v: Sequence[float]) -> Vec:
    return (_dot(m[0], v), _dot(m[1], v), _dot(m[2], v))


def _T(m: Mat) -> Mat:
    return ((m[0][0], m[1][0], m[2][0]), (m[0][1], m[1][1], m[2][1]), (m[0][2], m[1][2], m[2][2]))


def _mul(a: Mat, b: Mat) -> Mat:
    bt = _T(b)
    return tuple(tuple(_dot(a[i], bt[j]) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _cols(c0: Vec, c1: Vec, c2: Vec) -> Mat:
    return ((c0[0], c1[0], c2[0]), (c0[1], c1[1], c2[1]), (c0[2], c1[2], c2[2]))


def quat_to_mat(q: Sequence[float]) -> Mat:
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def mat_to_quat(m: Mat) -> Quat:
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, 0.25 * s)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = (0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = ((m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s, (m[1][0] - m[0][1]) / s)
    return q


def _level(up: Vec) -> Mat:
    """把 ``up`` 转到 +Z 的最小旋转(Rodrigues,绕 up × Z 转)。"""
    ez = (0.0, 0.0, 1.0)
    axis = _cross(up, ez)
    s, c = math.sqrt(_dot(axis, axis)), _dot(up, ez)
    if s < 1e-12:
        if c > 0:
            return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    k = (axis[0] / s, axis[1] / s, axis[2] / s)
    K = ((0.0, -k[2], k[1]), (k[2], 0.0, -k[0]), (-k[1], k[0], 0.0))
    K2 = _mul(K, K)
    return tuple(tuple((1.0 if i == j else 0.0) + s * K[i][j] + (1 - c) * K2[i][j]  # type: ignore
                       for j in range(3)) for i in range(3))


# ------------------------------------------------------------ 参数


@dataclass(frozen=True)
class Frames:
    up: Vec                                         # MOLA 系里的「上」
    sensor_up: Vec                                  # 雷达系里的「上」
    sensor_forward: Vec                             # 雷达系里的「朝前」(跟 sensor_up 垂直)
    sensor_height: float                            # 建图时雷达在地图平面系里的平均高度(米)
    sensor_in_base: tuple[float, float] = (0.0, 0.0)   # 雷达在狗身上的水平偏移(朝前、朝左,米)

    def __post_init__(self) -> None:
        for k in ("up", "sensor_up", "sensor_forward"):
            _vec(getattr(self, k), k)
        if abs(_finite(self.sensor_height, "sensor_height")) > MAX_SENSOR_HEIGHT_M:
            raise ContractError(f"frames:sensor_height 超出 ±{MAX_SENSOR_HEIGHT_M:g} m")
        if not isinstance(self.sensor_in_base, (tuple, list)) or len(self.sensor_in_base) != 2:
            raise ContractError("frames:sensor_in_base 要两个数(朝前、朝左)")
        for v in self.sensor_in_base:
            if abs(_finite(v, "sensor_in_base")) > MAX_SENSOR_OFFSET_M:
                raise ContractError(f"frames:sensor_in_base 超出 ±{MAX_SENSOR_OFFSET_M:g} m")
        if abs(_dot(self.sensor_up, self.sensor_forward)) > 0.2:
            raise ContractError("frames:雷达的「上」跟「朝前」不垂直")

    # ------------------------------------------------------------ 换算

    @property
    def _L(self) -> Mat:
        return _level(_unit(self.up))

    def level_matrix(self) -> Mat:
        """MOLA 系 → 地图平面系的旋转(「上」对准 +Z);点云乘它,x、y 就是地图平面坐标、z 是高度。"""
        return self._L

    def _sensor_basis(self) -> Mat:
        """雷达系里的(前、左、上)三根轴当列。"""
        u = _unit(self.sensor_up)
        f = _unit(tuple(a - _dot(self.sensor_forward, u) * b
                        for a, b in zip(self.sensor_forward, u, strict=True)))
        return _cols(f, _cross(u, f), u)

    def to_map2d(self, p: Sequence[float], q: Sequence[float]) -> tuple[float, float, float]:
        """MOLA 系里雷达的位姿 → 地图平面上狗身中心的 ``x, y, yaw``。"""
        L = self._L
        pl = _apply(L, p)
        fwd = _apply(L, _apply(quat_to_mat(q), _unit(self.sensor_forward)))
        yaw = math.atan2(fwd[1], fwd[0])
        dx, dy = self.sensor_in_base
        c, s = math.cos(yaw), math.sin(yaw)
        return (pl[0] - (c * dx - s * dy), pl[1] - (s * dx + c * dy), yaw)

    def to_mola(self, x: float, y: float, yaw: float) -> tuple[Vec, Quat]:
        """地图平面上狗身中心的 ``x, y, yaw`` → MOLA 系里雷达的位姿(重定位的三维初值;高度取建图时的
        平均,按狗站正了算)。"""
        dx, dy = self.sensor_in_base
        c, s = math.cos(yaw), math.sin(yaw)
        sx, sy = x + c * dx - s * dy, y + s * dx + c * dy
        Lt = _T(self._L)
        p = _apply(Lt, (sx, sy, self.sensor_height))
        target = _cols((c, s, 0.0), (-s, c, 0.0), (0.0, 0.0, 1.0))   # 前、左、上在水平系里
        R_level = _mul(target, _T(self._sensor_basis()))
        return p, mat_to_quat(_mul(Lt, R_level))

    def with_sensor_in_base(self, forward: float, left: float) -> Frames:
        return replace(self, sensor_in_base=(float(forward), float(left)))

    # ------------------------------------------------------------ 存取

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d = {k: list(v) if isinstance(v, tuple) else v for k, v in d.items()}
        return {"version": VERSION, **d}

    @classmethod
    def from_json(cls, d: Any) -> Frames:
        if not isinstance(d, dict):
            raise ContractError("frames:要是 JSON 对象")
        if d.get("version") != VERSION:
            raise ContractError(f"frames:版本要是 {VERSION},给的是 {d.get('version')!r}")
        try:
            return cls(up=_vec(d["up"], "up"), sensor_up=_vec(d["sensor_up"], "sensor_up"),
                       sensor_forward=_vec(d["sensor_forward"], "sensor_forward"),
                       sensor_height=_finite(d["sensor_height"], "sensor_height"),
                       sensor_in_base=_pair2(d.get("sensor_in_base", (0.0, 0.0))))
        except KeyError as exc:
            raise ContractError(f"frames:缺 {exc.args[0]}") from None

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> Frames:
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError(f"frames:读不了 {path}:{exc}") from None
        return cls.from_json(d)


def _finite(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ContractError(f"frames:{what} 要是有限数:{v!r}")
    return float(v)


def _pair2(v: Any) -> tuple[float, float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ContractError("frames:sensor_in_base 要两个数(朝前、朝左)")
    return (_finite(v[0], "sensor_in_base"), _finite(v[1], "sensor_in_base"))


def _vec(v: Any, what: str) -> Vec:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise ContractError(f"frames:{what} 要三个数")
    t = tuple(_finite(c, what) for c in v)
    if abs(math.sqrt(_dot(t, t)) - 1.0) > 1e-3:
        raise ContractError(f"frames:{what} 要是单位向量")
    return t  # type: ignore[return-value]


# ------------------------------------------------------------ 标定


def calibrate(poses: Sequence[tuple[Sequence[float], Sequence[float]]], *,
              sensor_up: Sequence[float], forward_hint: Sequence[float] | None = None,
              min_step_m: float = 0.03, explain: bool = False) -> Any:
    """从建图轨迹(MOLA 系里雷达的 ``(位置, 四元数)``,按时间顺序)估 ``frames.json`` 的前四项。

    - 「上」:先用 ``sensor_up``(装法上知道的,RS-Airy 是 X)转到 MOLA 系取平均,再反过来把雷达系里的
      「上」校一遍(装得有点歪也估得出);
    - 「朝前」:相邻两帧挪了 ``min_step_m`` 以上的,把位移转到雷达系、去掉竖直分量,取平均方向。狗大多
      朝前走;给了 ``forward_hint`` 就拿它核,估出来的跟它差过 90° 说明这段大多在倒着走(或者轴给错了);
    - 高度:转到地图平面系之后雷达 z 的平均。

    ``explain`` 为真时回 ``(Frames, 说明)``。"""
    if len(poses) < 2:
        raise ValueError("轨迹太短")
    Rs = [quat_to_mat(q) for _, q in poses]
    su = _unit(sensor_up)
    up = _unit(_mean(_apply(R, su) for R in Rs))
    su = _unit(_mean(_apply(_T(R), up) for R in Rs))
    steps = []
    for (p0, _), (p1, _), R0, R1 in zip(poses, poses[1:], Rs, Rs[1:], strict=False):
        d = tuple(b - a for a, b in zip(p0, p1, strict=True))
        if math.sqrt(_dot(d, d)) < min_step_m:
            continue
        # 起点、终点两帧各转一次再平均:转弯时位移是弦,方向在两帧朝向中间,单用一帧会偏
        ds = tuple(a + b for a, b in zip(_apply(_T(R0), d), _apply(_T(R1), d), strict=True))
        ds = tuple(a - _dot(ds, su) * b for a, b in zip(ds, su, strict=True))
        if math.sqrt(_dot(ds, ds)) > 1e-9:
            steps.append(_unit(ds))
    if not steps:
        raise ValueError("轨迹里狗没怎么动,估不出朝前")
    fwd = _unit(_mean(steps))
    share = sum(1 for s in steps if _dot(s, fwd) > 0) / len(steps)
    why = f"「朝前」按 {len(steps)} 段位移估,{share:.0%} 跟它同向"
    if forward_hint is not None:
        h = _unit(forward_hint)
        if _dot(h, fwd) < 0:
            why += f";跟装法上的朝前({_fmt(h)})反着 —— 这段大多在倒着走?已按装法翻过来"
            fwd = tuple(-c for c in fwd)  # type: ignore[assignment]
        else:
            why += f";跟装法上的朝前差 {math.degrees(math.acos(min(1.0, _dot(h, fwd)))):.1f}°"
    L = _level(up)
    height = sum(_apply(L, p)[2] for p, _ in poses) / len(poses)
    f = Frames(up=up, sensor_up=su, sensor_forward=fwd, sensor_height=height)
    return (f, why) if explain else f


def _mean(vs: Any) -> Vec:
    n, s = 0, [0.0, 0.0, 0.0]
    for v in vs:
        n += 1
        for i in range(3):
            s[i] += v[i]
    return (s[0] / n, s[1] / n, s[2] / n)


def _fmt(v: Sequence[float]) -> str:
    return "(" + ", ".join(f"{c:.2f}" for c in v) + ")"
