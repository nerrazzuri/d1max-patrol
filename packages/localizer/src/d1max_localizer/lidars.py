"""两台雷达的相对外参(W09i 设计稿 §2):``/etc/d1max/lidars.json``,每台狗一份(不跟图走)。

- ``T_front_rear``:后雷达系 → 前雷达系(4×4,行优先)。前雷达系是建图、定位的系(``frames.json``
  照旧按图标)。
- 没有这份文件 = **几何初值**:两台 RS-Airy 头尾对称装 —— 绕竖直轴转 π、沿「前」相距 2 × 0.4043 m;
  ``calibrated: false``。
  竖直轴、前方向取自前雷达的 ``frames.json``(C40011 上原始系 X 朝下、Z 朝前,见真机待验证清单 #64)。
- ``d1max-loc calibrate-rear`` 拿录包精修,过了守门才写 ``calibrated: true``。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("/etc/d1max/lidars.json")
#: 前后雷达在狗身上沿「前」的距离(米;SDK 开发指南 2.10 节:前后雷达 x = ±404.3 mm)。
BASELINE_M = 2 * 0.4043


class LidarsError(Exception):
    pass


def _rot_about(axis: tuple[float, float, float], ang: float) -> list[list[float]]:
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / n, y / n, z / n
    c, s, C = math.cos(ang), math.sin(ang), 1 - math.cos(ang)
    return [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
            [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
            [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]


def geometry_guess(up: tuple[float, float, float],
                   forward: tuple[float, float, float]) -> list[list[float]]:
    """头尾对称装的初值:后雷达系 = 前雷达系绕「上」转 π、平移到 −BASELINE × 前。"""
    R = _rot_about(up, math.pi)
    n = math.sqrt(sum(v * v for v in forward))
    t = [-BASELINE_M * v / n for v in forward]
    return [R[0] + [t[0]], R[1] + [t[1]], R[2] + [t[2]], [0.0, 0.0, 0.0, 1.0]]


@dataclass
class Lidars:
    T_front_rear: list[list[float]]
    calibrated: bool = False
    residual_m: float | None = None
    stamp_offset_s: float | None = None
    calibrated_at: str = ""
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"version": 1, "T_front_rear": self.T_front_rear, "calibrated": self.calibrated,
                "residual_m": self.residual_m, "stamp_offset_s": self.stamp_offset_s,
                "calibrated_at": self.calibrated_at, "note": self.note, **self.extra}

    @classmethod
    def from_json(cls, d: Any) -> Lidars:
        if not isinstance(d, dict):
            raise LidarsError("lidars.json:要是对象")
        T = d.get("T_front_rear")
        if (not isinstance(T, list) or len(T) != 4
                or not all(isinstance(r, list) and len(r) == 4 for r in T)
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for r in T for v in r)):
            raise LidarsError("lidars.json:T_front_rear 要是 4×4 的有限数")
        R = [r[:3] for r in T[:3]]
        for i in range(3):                               # 旋转部分要正交、行列式 +1
            for j in range(3):
                dot = sum(R[i][k] * R[j][k] for k in range(3))
                if abs(dot - (1.0 if i == j else 0.0)) > 1e-3:
                    raise LidarsError("lidars.json:T_front_rear 的旋转部分不正交")
        det = (R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
               - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
               + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0]))
        if abs(det - 1.0) > 1e-3 or [round(v, 6) for v in T[3]] != [0, 0, 0, 1]:
            raise LidarsError("lidars.json:T_front_rear 不是刚体变换")
        if any(abs(T[i][3]) > 3.0 for i in range(3)):
            raise LidarsError("lidars.json:两台雷达相距超过 3 m,不像话")
        cal = d.get("calibrated", False)
        if not isinstance(cal, bool):
            raise LidarsError("lidars.json:calibrated 要是真假")
        return cls(T_front_rear=[[float(v) for v in r] for r in T], calibrated=cal,
                   residual_m=d.get("residual_m"), stamp_offset_s=d.get("stamp_offset_s"),
                   calibrated_at=str(d.get("calibrated_at", ""))[:64],
                   note=str(d.get("note", ""))[:200])

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)


def load(path: Path | str | None, *, up: tuple[float, float, float],
         forward: tuple[float, float, float]) -> Lidars:
    """读 ``lidars.json``;没有就按几何初值(``calibrated: false``)。坏了抛 :class:`LidarsError`
    (坏的标定比没标定危险:不悄悄退回初值)。"""
    p = Path(path) if path is not None else DEFAULT_PATH
    if not p.exists():
        return Lidars(T_front_rear=geometry_guess(up, forward), calibrated=False,
                      note="没有 lidars.json:几何初值(头尾对称)")
    try:
        d = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise LidarsError(f"lidars.json 读不了:{exc}") from exc
    return Lidars.from_json(d)
