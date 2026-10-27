"""地图的显示设置(W44 第四期,设计 §6 第 6、7、8 条),跟着版本走(``map_display`` 表):

- ``rotate_deg``:摆正,**只转显示、不改坐标**;``null`` = 用站点按墙算的建议转角(``map_preview``)。
- ``basemap``:底图(上传的卫星截图、平面图)
  ``{"file", "x", "y", "scale", "rotate_deg", "opacity"}``,也只是显示;文件放在站点目录
  ``map-display/<图>/<版本>/``,不进地图文件(不动地图的清单、摘要)。
- ``regions``:区域命名 ``[{"name", "polygon"}]``;报警里「在哪」先看这里,没命名的用防区名
  (设计 §11 第 9 条)。
- ``terrain``:特殊地面 ``[{"id", "kind", "polygon", "slow_mps"}]``。楼梯、台阶、斜坡默认放慢
  0.3 m/s,草地、碎石路默认不放慢只标出来(设计 §11 第 16 条);``slow_mps`` 不是 ``null`` 的,由接口
  那一层落成导航区域里的限速区(``id`` = ``t-<id>``,走 ``NavZones.put``:收紧当场生效,删了是放宽、
  照样要确认)。不挡路、不换步态。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Any

from d1max_site.areas import inside

TERRAIN = ("stairs", "step", "slope", "grass", "gravel")
SLOW_BY_DEFAULT = {"stairs": 0.3, "step": 0.3, "slope": 0.3, "grass": None, "gravel": None}
TERRAIN_LABEL = {"stairs": "楼梯", "step": "台阶", "slope": "斜坡", "grass": "草地",
                 "gravel": "碎石路"}
MAX_ITEMS = 200
MAX_POINTS = 64
TERRAIN_PREFIX = "t-"
_ID = re.compile(r"[A-Za-z0-9_-]{1,24}")


class DisplayError(ValueError):
    """显示设置写得不对(接口回 400)。"""


def empty() -> dict[str, Any]:
    return {"rotate_deg": None, "basemap": None, "regions": [], "terrain": []}


def _num(v: Any, what: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not lo <= v <= hi:
        raise DisplayError(f"{what} 要在 {lo}–{hi}")
    return float(v)


def _poly(v: Any, what: str) -> list[list[float]]:
    if not isinstance(v, list) or not 3 <= len(v) <= MAX_POINTS:
        raise DisplayError(f"{what} 要 3–{MAX_POINTS} 个点")
    out = []
    for p in v:
        if not (isinstance(p, list) and len(p) == 2 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                for x in p)):
            raise DisplayError(f"{what} 的点要是 [x, y](米)")
        out.append([round(float(p[0]), 3), round(float(p[1]), 3)])
    return out


def check(d: Any) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise DisplayError("要一个对象")
    out = empty()
    if d.get("rotate_deg") is not None:
        out["rotate_deg"] = _num(d["rotate_deg"], "转角", -180, 180)
    bm = d.get("basemap")
    if bm is not None:
        if not isinstance(bm, dict) or not isinstance(bm.get("file"), str) or \
                not re.fullmatch(r"basemap\.(png|jpg)", bm["file"]):
            raise DisplayError("底图要先上传")
        out["basemap"] = {"file": bm["file"], "x": _num(bm.get("x", 0), "底图 x", -1e5, 1e5),
                          "y": _num(bm.get("y", 0), "底图 y", -1e5, 1e5),
                          "scale": _num(bm.get("scale", 0.05), "底图每像素多少米", 0.001, 100),
                          "rotate_deg": _num(bm.get("rotate_deg", 0), "底图转角", -180, 180),
                          "opacity": _num(bm.get("opacity", 0.5), "底图透明度", 0, 1)}
    regs = d.get("regions") or []
    if not isinstance(regs, list) or len(regs) > MAX_ITEMS:
        raise DisplayError(f"区域最多 {MAX_ITEMS} 个")
    for r in regs:
        name = r.get("name") if isinstance(r, dict) else None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise DisplayError("区域要有名字(1–64 个字)")
        out["regions"].append({"name": name.strip(), "polygon": _poly(r.get("polygon"), name)})
    ter = d.get("terrain") or []
    if not isinstance(ter, list) or len(ter) > MAX_ITEMS:
        raise DisplayError(f"特殊地面最多 {MAX_ITEMS} 块")
    seen = set()
    for t in ter:
        if not isinstance(t, dict) or t.get("kind") not in TERRAIN:
            raise DisplayError(f"特殊地面的种类只有 {'、'.join(TERRAIN)}")
        tid = t.get("id")
        if not isinstance(tid, str) or not _ID.fullmatch(tid) or tid in seen:
            raise DisplayError("特殊地面要有不重复的 id(字母数字 _ -)")
        seen.add(tid)
        slow = t.get("slow_mps", SLOW_BY_DEFAULT[t["kind"]])
        out["terrain"].append({"id": tid, "kind": t["kind"],
                               "polygon": _poly(t.get("polygon"), TERRAIN_LABEL[t["kind"]]),
                               "slow_mps": None if slow is None else _num(slow, "放慢到", 0.05, 2)})
    return out


def slow_zones(display: dict[str, Any]) -> list[dict[str, Any]]:
    """特殊地面要放慢的那几块 → 导航区域的限速区(线格式)。"""
    return [{"id": f"{TERRAIN_PREFIX}{t['id']}", "kind": "slow",
             "label": TERRAIN_LABEL[t["kind"]], "polygon": t["polygon"],
             "max_speed_mps": t["slow_mps"]}
            for t in display["terrain"] if t["slow_mps"] is not None]


class MapDisplay:
    def __init__(self, db: Any, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def get(self, map_id: str, version: str) -> dict[str, Any]:
        rows = self.db.query("SELECT body FROM map_display WHERE map_id=? AND map_version=?",
                             (map_id, version))
        return json.loads(rows[0]["body"]) if rows else empty()

    def put(self, map_id: str, version: str, d: Any, *, by: str) -> dict[str, Any]:
        body = check(d)
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO map_display(map_id, map_version, body, updated_by, "
                      "updated_ms) VALUES (?,?,?,?,?)",
                      (map_id, version, json.dumps(body, ensure_ascii=False), by, self._now()))
        return body

    def region_at(self, map_id: str, version: str, x: float, y: float) -> str | None:
        """命名区域里落在哪一块(落在好几块里取最先画的)。没有回 ``None``(调用方再看防区)。"""
        for r in self.get(map_id, version)["regions"]:
            if inside(x, y, r["polygon"]):
                return r["name"]
        return None
