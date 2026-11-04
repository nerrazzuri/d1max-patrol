"""外部设备按位置绑定(W73 第五期,设计 §8)。

摄像头、报警主机、门磁先登记成一台**设备**、在图上点出位置(摄像头再给朝向、视角、看多远)。
站点据此算:

- **查看点**:摄像头取视线中线上约一半距离处,门磁 / 报警主机取设备旁;再挪到离它最近、机器人
  站得下、走得到的地方(``ZonePosts.pick``,跟防区查看点同一套);
- **所在防区**:查看点落在哪个圈好的防区里(判这个防区此刻布不布防);不在任何防区里 = 按全站;
- **专属回调**:每台设备一个事件源 ``dev-<id>``(自己的密钥,沿用现在的签名回调),设备只说「我被触发
  了」(不用带防区名);拦截点 ``dev-<id>`` 就是查看点。

老的按防区名报的 ``POST /api/incidents`` 照收(``IncidentDesk.zone_point``)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from nexwar_site.ca import SAFE_ID

log = logging.getLogger(__name__)

KINDS = ("camera", "contact", "alarm", "other")
PREFIX = "dev-"
CCTV = "cctv-"
CAM = "cam:"
#: 摄像头默认看多远(W44 放图只记了张角,没记距离):查看点取中线上一半,5 m。
CAMERA_RANGE_M = 10.0
#: 查看点从目标往外最多挪这么远去找站得下的地方。
SNAP_M = 6.0


class DeviceError(ValueError):
    pass


def _num(d: dict[str, Any], k: str, lo: float, hi: float, default: float | None) -> float:
    v = d.get(k, default)
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(float(v)) or not lo <= float(v) <= hi:
        raise DeviceError(f"{k} 要是 {lo:g}–{hi:g} 之间的数")
    return float(v)


class DeviceBook:
    def __init__(self, db: Any, *, incidents: Any, posts: Any, areas: Any,
                 now_ms: Callable[[], int]) -> None:
        self.db, self.incidents, self.posts, self.areas, self._now = db, incidents, posts, areas, \
            now_ms
        self._cams: dict[str, tuple[Any, dict[str, Any] | None]] = {}

    def list(self) -> list[dict[str, Any]]:
        return [self._wire(r) for r in self.db.query("SELECT * FROM ext_devices ORDER BY id")]

    def get(self, dev_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM ext_devices WHERE id=?", (dev_id,))
        return self._wire(rows[0]) if rows else None

    def by_source(self, source: str) -> dict[str, Any] | None:
        """事件源 → 设备(``dev-<id>`` 登记的;``cctv-<名字>`` 放到图上的摄像头);别的 None。"""
        if not isinstance(source, str):
            return None
        if source.startswith(CCTV):
            return self._camera(source[len(CCTV):])
        if not source.startswith(PREFIX):
            return None
        return self.get(source[len(PREFIX):])

    def _camera(self, name: str) -> dict[str, Any] | None:
        """W44 放到图上的摄像头(ONVIF 订阅,来源 ``cctv-<名字>``):查看点按它的位置、朝向算。
        没放到图上、算不出来 → None(照老的防区映射)。"""
        rows = self.db.query("SELECT * FROM cameras WHERE name=?", (name,))
        if not rows or not rows[0]["map_id"]:
            return None
        r = rows[0]
        key = (r["map_id"], r["map_version"], r["x"], r["y"], r["yaw"])
        got = self._cams.get(name)
        if got is None or got[0] != key:
            try:
                vx, vy, face, zone = self._view("camera", r["map_id"], r["map_version"],
                                                float(r["x"]), float(r["y"]),
                                                float(r["yaw"] or 0.0), CAMERA_RANGE_M)
            except DeviceError as exc:
                log.warning("摄像头 %s 的查看点算不出来:%s(照老的防区映射派)", name, exc)
                self._cams[name] = (key, None)
                return None
            got = (key, {"id": name, "zone": zone, "source": CCTV + name,
                         "intercept": CAM + name,
                         "point": {"name": CAM + name, "map_id": r["map_id"],
                                   "map_version": r["map_version"], "x": vx, "y": vy,
                                   "yaw": round(face, 3), "reach": ""}})
            self._cams[name] = got
        if got[1] is None:
            return None
        # 外审 r1 #10:防区按现在圈的算(圈改了、删了跟着变);查看点只跟摄像头位置、地图版本走
        p = got[1]["point"]
        zone = self._zone(p["map_id"], p["map_version"], p["x"], p["y"])
        # 外审 r2 #10:接了防区簿、算出来不在任何防区(``""``)就是全站,不退回旧的登记防区
        return {**got[1], "zone": (r["zone"] or "") if zone is None else zone}

    def point(self, name: str) -> dict[str, Any] | None:
        """``cam:<名字>`` → 那台摄像头的查看点(拦截点的样子)。"""
        if not name.startswith(CAM):
            return None
        d = self._camera(name[len(CAM):])
        return dict(d["point"]) if d is not None else None

    def _zone(self, map_id: str, ver: str, x: float, y: float) -> str | None:
        """这一点现在落在哪个防区(``""`` = 不在任何防区);没接防区簿 → ``None``。"""
        if self.areas is None:
            return None
        return self.areas.zone_at(map_id, ver, x, y) or ""

    def _wire(self, r: Any) -> dict[str, Any]:
        d = dict(r)
        d["source"] = d["intercept"] = PREFIX + d["id"]
        zone = self._zone(d["map_id"], d["map_version"], d["view_x"], d["view_y"])
        if zone is not None:                             # 外审 r1 #10:按现在圈的防区判
            d["zone"] = zone
        return d

    def put(self, dev_id: Any, d: Any, *, by: str) -> dict[str, Any]:
        """登记 / 改一台设备 → ``{"device", "source", "secret"}``(密钥只在第一次登记时给)。"""
        if not isinstance(dev_id, str) or not SAFE_ID.match(dev_id) or len(dev_id) > 48:
            raise DeviceError("设备编号只许 ASCII 字母、数字、. _ -(最多 48 个字符)")
        if not isinstance(d, dict):
            raise DeviceError("要一个对象")
        kind = d.get("kind")
        if kind not in KINDS:
            raise DeviceError(f"kind 要是 {'/'.join(KINDS)} 之一")
        map_id, ver = d.get("map_id"), d.get("map_version")
        if not isinstance(map_id, str) or not map_id or not isinstance(ver, str) or not ver:
            raise DeviceError("要 map_id、map_version")
        x, y = _num(d, "x", -1e5, 1e5, None), _num(d, "y", -1e5, 1e5, None)
        yaw = _num(d, "yaw", -10.0, 10.0, 0.0)
        fov = _num(d, "fov_deg", 5.0, 360.0, 90.0)
        rng = _num(d, "range_m", 0.5, 100.0, 10.0)
        note = str(d.get("note") or "")[:200]
        vx, vy, face, zone = self._view(kind, map_id, ver, x, y, yaw, rng)
        from nexwar_site.incidents import IncidentError
        name = PREFIX + dev_id
        try:
            self.incidents.set_intercept(name, map_id=map_id, map_version=ver, x=vx, y=vy,
                                         yaw=round(face, 3))
        except IncidentError as exc:
            raise DeviceError(f"查看点 ({vx:.2f}, {vy:.2f}) 用不了:{exc}") from exc
        secret = None
        known = {s["name"] for s in self.incidents.sources()}
        if name not in known:
            secret = self.incidents.add_source(name)
        with self.db.tx() as c:
            c.execute("INSERT INTO ext_devices(id, kind, map_id, map_version, x, y, yaw, fov_deg, "
                      "range_m, view_x, view_y, zone, note, updated_ms, updated_by) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                      "kind=excluded.kind, map_id=excluded.map_id, "
                      "map_version=excluded.map_version,"
                      " x=excluded.x, y=excluded.y, yaw=excluded.yaw, fov_deg=excluded.fov_deg, "
                      "range_m=excluded.range_m, view_x=excluded.view_x, view_y=excluded.view_y, "
                      "zone=excluded.zone, note=excluded.note, updated_ms=excluded.updated_ms, "
                      "updated_by=excluded.updated_by",
                      (dev_id, kind, map_id, ver, x, y, yaw, fov, rng, vx, vy, zone, note,
                       self._now(), by))
        return {"device": self.get(dev_id), "source": name, "secret": secret}

    def _view(self, kind: str, map_id: str, ver: str, x: float, y: float, yaw: float,
              rng: float) -> tuple[float, float, float, str]:
        """→ (查看点 x, y, 机器人到了朝哪, 落在哪个防区)。"""
        if kind == "camera":
            target = (x + math.cos(yaw) * rng / 2, y + math.sin(yaw) * rng / 2)
        else:
            target = (x, y)
        box = [[target[0] - SNAP_M, target[1] - SNAP_M], [target[0] + SNAP_M, target[1] - SNAP_M],
               [target[0] + SNAP_M, target[1] + SNAP_M], [target[0] - SNAP_M, target[1] + SNAP_M]]
        p, why = self.posts.pick(map_id, ver, box, target, radius_m=SNAP_M)
        if p is None:
            raise DeviceError(f"这台设备附近找不到机器人站得下、走得到的查看点:{why}")
        vx, vy = p
        face = math.atan2(target[1] - vy, target[0] - vx) if (vx, vy) != target else yaw
        if kind != "camera" and (vx, vy) != (x, y):
            face = math.atan2(y - vy, x - vx)
        return vx, vy, face, self._zone(map_id, ver, vx, vy) or ""

    def rotate(self, dev_id: str) -> str:
        if self.get(dev_id) is None:
            raise DeviceError(f"没有设备 {dev_id}")
        return self.incidents.rotate_secret(PREFIX + dev_id)

    def remove(self, dev_id: str) -> None:
        from nexwar_site.incidents import IncidentError
        if self.get(dev_id) is None:
            raise DeviceError(f"没有设备 {dev_id}")
        for fn in (self.incidents.remove_source, self.incidents.remove_intercept):
            try:
                fn(PREFIX + dev_id)
            except IncidentError:
                pass                                     # 已经没了
        with self.db.tx() as c:
            c.execute("DELETE FROM ext_devices WHERE id=?", (dev_id,))
