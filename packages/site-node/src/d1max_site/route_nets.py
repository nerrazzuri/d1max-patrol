"""路网(W48,`docs/W48-路线网络-设计.md`),站点这一头:每个几何版本一份,整份存、修订号加一。

站点是权威:改了发给加载着这一版的机器人(``route_net_set``),机器人报的修订号旧了每拍补发
(:meth:`d1max_site.dispatcher.Dispatcher.sync_route_nets`)。存的时候核:点不许放进禁行区、连线不许穿过
禁行区(说清是哪个点、哪条线);别人刚改过(``base_revision`` 对不上)拒。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.routenet import RouteNet
from d1max_contract.zones import Zone, point_in_polygon, segments_cross
from d1max_site.db import SiteDB


class RouteNetError(ValueError):
    """路网写错了、碰了禁行区(接口回 400)。"""


class RouteNetConflict(RuntimeError):
    """别人刚改过(接口回 409)。"""


def _edge_hits(a: tuple[float, float], b: tuple[float, float], poly: tuple) -> bool:
    if point_in_polygon(*a, poly) or point_in_polygon(*b, poly):
        return True
    ring = list(poly)
    return any(segments_cross(a, b, p, q) for p, q in zip(ring, ring[1:] + ring[:1], strict=True))


class RouteNets:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def _row(self, map_id: str, version: str) -> Any:
        rows = self.db.query("SELECT * FROM route_nets WHERE map_id=? AND map_version=?",
                             (map_id, version))
        return rows[0] if rows else None

    def get(self, map_id: str, version: str) -> RouteNet:
        """这一版的路网;没画过是空的第 0 版。"""
        r = self._row(map_id, version)
        if r is None:
            return RouteNet(map_id, version, 0, (), ())
        return RouteNet.from_wire(json.loads(r["body"]))

    def view(self, map_id: str, version: str) -> dict[str, Any]:
        r = self._row(map_id, version)
        net = self.get(map_id, version)
        return net.to_wire() | {"updated_ms": r["updated_ms"] if r else None,
                                "updated_by": r["updated_by"] if r else "",
                                "length_m": round(net.length_m(), 1)}

    def put(self, map_id: str, version: str, d: Any, *, by: str,
            keep_out: tuple[Zone, ...] = (),
            place_problem: Callable[[list[tuple[str, float, float]]], str] | None = None
            ) -> RouteNet:
        """整份换。``d`` = ``{nodes, edges, base_revision}``;
        ``keep_out`` = 这一版在生效的禁行区;``place_problem`` 核点的位置(``[(名字, x, y)]`` → 原因,
        都行回 ``""``;站点用 ``MapCatalog.reach_problem``,跟巡检点一个口径,W48-M2)。"""
        if not isinstance(d, dict):
            raise RouteNetError("要一个对象:nodes、edges、base_revision")
        base = d.get("base_revision")
        if isinstance(base, bool) or not isinstance(base, int):
            raise RouteNetError("要 base_revision(改之前看到的修订号)")
        with self.db.tx() as c:
            row = c.execute("SELECT revision FROM route_nets WHERE map_id=? AND map_version=?",
                            (map_id, version)).fetchone()
            cur = row["revision"] if row else 0
            if base != cur:
                raise RouteNetConflict(f"路网改过了(现在是第 {cur} 版,你看到的是第 {base} 版):"
                                       "刷新再改")
            try:
                net = RouteNet.from_wire({"map_id": map_id, "map_version": version,
                                          "revision": cur + 1, "nodes": d.get("nodes", []),
                                          "edges": d.get("edges", [])})
            except ContractError as exc:
                raise RouteNetError(str(exc)) from exc
            by_id = {n.id: n for n in net.nodes}
            for z in keep_out:
                for n in net.nodes:
                    if point_in_polygon(n.x, n.y, z.polygon):
                        raise RouteNetError(f"点 {n.id} 在禁行区「{z.label or z.id}」里")
                for a, b in net.edges:
                    pa, pb = by_id[a], by_id[b]
                    if _edge_hits((pa.x, pa.y), (pb.x, pb.y), z.polygon):
                        raise RouteNetError(f"连线 {a}–{b} 穿过禁行区「{z.label or z.id}」")
            if place_problem is not None:
                why = place_problem([(f"点 {n.id}", n.x, n.y) for n in net.nodes])
                if why:
                    raise RouteNetError(why)
            c.execute("INSERT INTO route_nets(map_id, map_version, body, revision, updated_ms, "
                      "updated_by) VALUES (?,?,?,?,?,?) ON CONFLICT(map_id, map_version) DO UPDATE "
                      "SET body=excluded.body, revision=excluded.revision, "
                      "updated_ms=excluded.updated_ms, updated_by=excluded.updated_by",
                      (map_id, version, json.dumps(net.to_wire(), ensure_ascii=False),
                       net.revision, self._now(), by))
        return net
