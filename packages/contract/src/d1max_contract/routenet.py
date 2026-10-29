"""路网(W48,`docs/W48-路线网络-设计.md`):在地图上画好的「可以走的路」—— 点和双向连线。

站点存、站点发(``route_net_set``),机器人落盘、去一个点时先在路网上找路(:func:`route`),得出一串途经点,
每一段照常规划、避障。站点、机器人、仿真共用这一份。
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

#: 一份路网最多多少个点、多少条线。
MAX_NODES = 500
MAX_EDGES = 1000
#: 一条连线最长多少米(画错一个点就连出几百米的线,早点拦下)。
MAX_EDGE_M = 100.0
#: 离开路网的那两小段(起点上路网、下路网到终点)按直线距离乘这个算代价:宁可多沿路走。
OFF_NET_FACTOR = 3.0
#: 只考虑离起点、终点这么近以内的点上下路网(米)。
ENTRY_MAX_M = 15.0
#: 离起点、终点不到这么近的途经点去掉(米)。
DROP_NEAR_M = 0.5

_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ContractError(f"路网:{what} 要是有限的数")
    if abs(v) > 100_000:
        raise ContractError(f"路网:{what} 超出范围")
    return float(v)


@dataclass(frozen=True)
class Node:
    id: str
    x: float
    y: float


@dataclass(frozen=True)
class RouteNet:
    map_id: str
    map_version: str
    revision: int
    nodes: tuple[Node, ...]
    #: 双向连线(两个点的 id;按字典序存,同一条只有一条)。
    edges: tuple[tuple[str, str], ...]

    # ------------------------------------------------------------ 线上格式

    def to_wire(self) -> dict[str, Any]:
        return {"map_id": self.map_id, "map_version": self.map_version,
                "revision": self.revision,
                "nodes": [{"id": n.id, "x": n.x, "y": n.y} for n in self.nodes],
                "edges": [[a, b] for a, b in self.edges]}

    @classmethod
    def from_wire(cls, d: Any) -> RouteNet:
        if not isinstance(d, dict):
            raise ContractError("路网要是对象")
        map_id, ver, rev = d.get("map_id"), d.get("map_version"), d.get("revision", 0)
        if not isinstance(map_id, str) or not map_id or not isinstance(ver, str):
            raise ContractError("路网:map_id、map_version 要是字符串")
        if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
            raise ContractError("路网:revision 要是不小于 0 的整数")
        raw_nodes, raw_edges = d.get("nodes", []), d.get("edges", [])
        if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_NODES:
            raise ContractError(f"路网:nodes 要是最多 {MAX_NODES} 个的列表")
        if not isinstance(raw_edges, list) or len(raw_edges) > MAX_EDGES:
            raise ContractError(f"路网:edges 要是最多 {MAX_EDGES} 条的列表")
        nodes: list[Node] = []
        seen: dict[str, Node] = {}
        for n in raw_nodes:
            if not isinstance(n, dict) or not isinstance(n.get("id"), str) \
                    or not _ID.fullmatch(n["id"]):
                raise ContractError("路网:点的 id 要是 1–32 个字母、数字、下划线、减号")
            if n["id"] in seen:
                raise ContractError(f"路网:点 {n['id']} 重名")
            node = Node(n["id"], _num(n.get("x"), f"点 {n['id']} 的 x"),
                        _num(n.get("y"), f"点 {n['id']} 的 y"))
            seen[node.id] = node
            nodes.append(node)
        edges: list[tuple[str, str]] = []
        have: set[tuple[str, str]] = set()
        for e in raw_edges:
            if not isinstance(e, list) or len(e) != 2 or not all(isinstance(v, str) for v in e):
                raise ContractError("路网:连线要是两个点的 id")
            a, b = e
            if a not in seen or b not in seen:
                raise ContractError(f"路网:连线 {a}–{b} 连到了没有的点")
            if a == b:
                raise ContractError(f"路网:点 {a} 自己连自己")
            key = (a, b) if a < b else (b, a)
            if key in have:
                raise ContractError(f"路网:连线 {a}–{b} 重复")
            if math.dist((seen[a].x, seen[a].y), (seen[b].x, seen[b].y)) > MAX_EDGE_M:
                raise ContractError(f"路网:连线 {a}–{b} 超过 {MAX_EDGE_M:.0f} 米")
            have.add(key)
            edges.append(key)
        return cls(map_id, ver, rev, tuple(nodes), tuple(edges))

    def digest(self) -> str:
        """内容摘要(不含修订号):站点核对机器人收到的是不是这一份。"""
        body = {"nodes": [[n.id, n.x, n.y] for n in sorted(self.nodes, key=lambda n: n.id)],
                "edges": sorted([a, b] for a, b in self.edges)}
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    def length_m(self) -> float:
        by = {n.id: n for n in self.nodes}
        return sum(math.dist((by[a].x, by[a].y), (by[b].x, by[b].y)) for a, b in self.edges)


def route(net: RouteNet, start: tuple[float, float],
          goal: tuple[float, float]) -> list[tuple[float, float]]:
    """在路网上找从 ``start`` 到 ``goal`` 的路 → 途经点(地图坐标);
    不上路网更好(或者够不着)回 ``[]``。

    代价:离开路网的两小段按直线 × :data:`OFF_NET_FACTOR`,沿连线按长度;直接走也按直线 × 那个系数。
    只考虑离起点、终点 :data:`ENTRY_MAX_M` 以内的点上下路网;
    离起点、终点不到 :data:`DROP_NEAR_M` 的途经点去掉。"""
    if not net.nodes or not net.edges:
        return []
    by = {n.id: n for n in net.nodes}
    adj: dict[str, list[tuple[str, float]]] = {n.id: [] for n in net.nodes}
    for a, b in net.edges:
        d = math.dist((by[a].x, by[a].y), (by[b].x, by[b].y))
        adj[a].append((b, d))
        adj[b].append((a, d))
    direct = math.dist(start, goal) * OFF_NET_FACTOR
    exits = {n.id: math.dist((n.x, n.y), goal) * OFF_NET_FACTOR for n in net.nodes
             if adj[n.id] and math.dist((n.x, n.y), goal) <= ENTRY_MAX_M}
    if not exits:
        return []
    dist: dict[str, float] = {}
    prev: dict[str, str | None] = {}
    heap: list[tuple[float, str]] = []
    for n in net.nodes:
        d0 = math.dist((n.x, n.y), start)
        if adj[n.id] and d0 <= ENTRY_MAX_M:
            dist[n.id] = d0 * OFF_NET_FACTOR
            prev[n.id] = None
            heapq.heappush(heap, (dist[n.id], n.id))
    best, best_end = direct, None
    done: set[str] = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done or d > dist.get(u, math.inf):
            continue
        done.add(u)
        if d >= best:
            break
        if u in exits and d + exits[u] < best:
            best, best_end = d + exits[u], u
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    if best_end is None:
        return []
    chain: list[str] = []
    cur: str | None = best_end
    while cur is not None:
        chain.append(cur)
        cur = prev[cur]
    chain.reverse()
    pts = [(by[i].x, by[i].y) for i in chain]
    return [p for p in pts
            if math.dist(p, start) >= DROP_NEAR_M and math.dist(p, goal) >= DROP_NEAR_M]
