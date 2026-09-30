"""地图与经纬度的配准(W09e 决定 6、8):地图版本里可选的 ``geo.json``。

地图平面系(``frames.json`` 定的 x、y,米)→ 当地东北(ENU,米,以 ``origin`` 经纬度为原点)::

    [e, n] = R(yaw) · [x, y] + [tx, ty]

``yaw_deg``:地图 x 轴相对正东、逆时针;``tx, ty``:地图原点在 ENU 里的位置。经纬度 ↔ ENU 在原点处按
WGS84 的子午圈、卯酉圈曲率半径平面展开(庄园一公里以内,误差毫米级)。``antenna_in_base``:RTK 天线在
狗身坐标系里的位置(米,x 朝前、y 朝左),RTK 给的是天线的位置,不是狗身中心。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

GEO_FILE = "geo.json"
_A = 6378137.0
_E2 = 6.69437999014e-3


def _radii(lat0: float) -> tuple[float, float]:
    s = math.sin(math.radians(lat0))
    w = 1 - _E2 * s * s
    return _A * (1 - _E2) / w ** 1.5, _A / math.sqrt(w)        # 子午圈 M、卯酉圈 N


def llh_to_enu(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    m, n = _radii(lat0)
    return (math.radians(lon - lon0) * n * math.cos(math.radians(lat0)),
            math.radians(lat - lat0) * m)


def enu_to_llh(e: float, n: float, lat0: float, lon0: float) -> tuple[float, float]:
    m, nn = _radii(lat0)
    return (lat0 + math.degrees(n / m),
            lon0 + math.degrees(e / (nn * math.cos(math.radians(lat0)))))


@dataclass(frozen=True)
class GeoRef:
    lat0: float
    lon0: float
    alt0: float
    yaw_deg: float
    tx: float
    ty: float
    rms_m: float = 0.0
    pairs: int = 0
    antenna_in_base: tuple[float, float] = (0.0, 0.0)
    #: 建图时基站的 ECEF(米;RTCM 1005 解出来的)。狗上改正里的基站坐标跟它差 5 cm 以上 → 基站挪过、
    #: 这份配准对不上了,不拿 RTK 核对(W09e 内审应修 3)。不知道是 None。
    base_ecef: tuple[float, float, float] | None = None

    def to_wire(self) -> dict[str, Any]:
        d = {"version": 1, "origin": {"lat": self.lat0, "lon": self.lon0, "alt": self.alt0},
             "yaw_deg": self.yaw_deg, "tx": self.tx, "ty": self.ty, "rms_m": self.rms_m,
             "pairs": self.pairs, "antenna_in_base": list(self.antenna_in_base)}
        if self.base_ecef is not None:
            d["base_ecef"] = list(self.base_ecef)
        return d

    @classmethod
    def from_wire(cls, d: Any) -> GeoRef:
        try:
            o = d["origin"]
            vals = [float(v) for v in (o["lat"], o["lon"], o["alt"], d["yaw_deg"], d["tx"],
                                       d["ty"], d.get("rms_m", 0.0))]
            ant = tuple(float(v) for v in d.get("antenna_in_base", (0.0, 0.0)))
            pairs = int(d.get("pairs", 0))
            base = d.get("base_ecef")
            base_t = None if base is None else tuple(float(v) for v in base)
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"geo.json 不成形: {exc}") from exc
        if not all(math.isfinite(v) for v in (*vals, *ant)) or len(ant) != 2 \
                or not -90 <= vals[0] <= 90 or not -180 <= vals[1] <= 180:
            raise ContractError("geo.json 里有不是有限数、或经纬度越界的值")
        if base_t is not None and (len(base_t) != 3 or not all(math.isfinite(v) for v in base_t)):
            raise ContractError("geo.json 的 base_ecef 要是三个有限数")
        return cls(*vals[:6], rms_m=vals[6], pairs=pairs, antenna_in_base=(ant[0], ant[1]),
                   base_ecef=base_t)  # type: ignore[arg-type]

    def map_to_enu(self, x: float, y: float) -> tuple[float, float]:
        c, s = math.cos(math.radians(self.yaw_deg)), math.sin(math.radians(self.yaw_deg))
        return c * x - s * y + self.tx, s * x + c * y + self.ty

    def enu_to_map(self, e: float, n: float) -> tuple[float, float]:
        c, s = math.cos(math.radians(self.yaw_deg)), math.sin(math.radians(self.yaw_deg))
        de, dn = e - self.tx, n - self.ty
        return c * de + s * dn, -s * de + c * dn

    def llh_to_map(self, lat: float, lon: float) -> tuple[float, float]:
        """经纬度(天线的)→ 地图平面上的点(还是天线的位置)。"""
        return self.enu_to_map(*llh_to_enu(lat, lon, self.lat0, self.lon0))

    def body_from_antenna(self, ax: float, ay: float, yaw: float) -> tuple[float, float]:
        """地图上的天线位置 + 狗朝向 → 狗身中心。"""
        bx, by = self.antenna_in_base
        c, s = math.cos(yaw), math.sin(yaw)
        return ax - (c * bx - s * by), ay - (s * bx + c * by)


__all__ = ["GEO_FILE", "GeoRef", "enu_to_llh", "llh_to_enu"]
