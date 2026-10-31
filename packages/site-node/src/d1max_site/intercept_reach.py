"""拦截点走不走得到(W23,决策 38)。设拦截点时查、站点每 30 秒对账一次;派单只读查好的结果。

查这几样(能查的才查,查不了的写在 ``note`` 里、不挡):
1. 离建图时走过的路不超过 5 m、在可通行的格子上(``MapCatalog.reach_problem``,同巡检点,W09c)。
2. 狗站得下:不在障碍、禁行区里,离障碍够远(机体外接圆半径 + 余量)。
3. **从这张图这一版上的待命点(或原点)走得到**:用**跟狗同一份**的规划(``d1max_contract.planning``:
   同样的代价图、同样的 A*),只要有一个待命点规划得出来就算走得到。

查不了的:站点没有这一版图(不认识的不挡,同 ``reach_problem``)、这一版没有规划栅格(``floor.pgm``,
比如厂商导入的图)、这张图上一个待命点、原点都没有、规划超时 —— 这些不当成「走不到」。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

PLAN_TIMEOUT_S = 10.0
CACHE = 2


@dataclass(frozen=True)
class Reach:
    problem: str = ""        # 非空 = 走不到 / 站不下 / 不在图上(派单不派)
    note: str = ""           # 查不了的那几样(不挡)


class InterceptReach:
    def __init__(self, db: Any, maps: Any, zones: Any, *,
                 timeout_s: float = PLAN_TIMEOUT_S) -> None:
        self.db = db
        self.maps = maps
        self.zones = zones
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._costmaps: dict[tuple[str, str, int], Any] = {}

    # ------------------------------------------------------------ 指纹(对账用)

    def starts(self, map_id: str, version: str) -> list[tuple[str, float, float]]:
        """这张图这一版上所有狗的待命点、原点。"""
        rows = self.db.query(
            "SELECT name, x, y FROM standby_points WHERE map_id=? AND map_version=? "
            "UNION SELECT name, x, y FROM homes WHERE map_id=? AND map_version=? ORDER BY name",
            (map_id, version, map_id, version))
        return [(r["name"], float(r["x"]), float(r["y"])) for r in rows]

    def key(self, map_id: str, version: str, x: float, y: float) -> str:
        """影响结论的东西变了,指纹就变:点、区域修订、待命点、这一版图在不在站点上。"""
        rev = self.zones.current(map_id, version).revision
        st = ";".join(f"{n}:{sx:.2f},{sy:.2f}" for n, sx, sy in self.starts(map_id, version))
        try:
            self.maps.get(map_id, version)
            have = "1"
        except Exception:                                # noqa: BLE001 - 站点不认识这一版
            have = "0"
        raw = f"{map_id}:{version}:{x:.3f},{y:.3f}:{rev}:{have}:{st}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ------------------------------------------------------------ 查

    def _costmap(self, map_id: str, version: str) -> Any:
        from d1max_contract.planning import costmap
        zs = self.zones.effective(map_id, version)    # 狗守的那份(外审 W38-I1;内容变修订号必变)
        k = (map_id, version, zs.revision)
        with self._lock:
            if k in self._costmaps:
                return self._costmaps[k]
        d = self.maps.root / map_id / version
        if not (d / "floor.pgm").is_file() or not (d / "floor.yaml").is_file():
            cm = None
        else:
            cm = costmap.from_map_dir(d, zs.zones)
        with self._lock:
            self._costmaps[k] = cm
            while len(self._costmaps) > CACHE:
                self._costmaps.pop(next(iter(self._costmaps)))
        return cm

    def reachable(self, map_id: str, version: str, start: tuple[float, float],
                  goal: tuple[float, float]) -> bool | None:
        """W44 第三期(路线逐段查):走得通 True、走不通 False(被墙或禁行区隔开、点在图外)、
        查不了 None(不认识的图、没有栅格)。"""
        try:
            self.maps.get(map_id, version)
            cm = self._costmap(map_id, version)
        except Exception:                                # noqa: BLE001 - 不认识的图、栅格坏了
            return None
        if cm is None:
            return None
        return self.path_m(map_id, version, start, goal) is not None

    def path_m(self, map_id: str, version: str, start: tuple[float, float],
               goal: tuple[float, float]) -> float | None:
        """W44:从 ``start`` 到 ``goal`` 按跟机器人同一份的规划要走多少米。

        走不通、栅格没有、出图 → None。"""
        import math
        import time

        from d1max_contract.planning import astar
        try:
            self.maps.get(map_id, version)
            cm = self._costmap(map_id, version)
        except Exception:                                # noqa: BLE001 - 不认识的图、栅格坏了
            return None
        if cm is None:
            return None
        s, g = cm.cell_of(*start), cm.cell_of(*goal)
        if s is None or g is None:
            return None
        h, w = cm.shape
        try:
            cells = astar.plan(cm.cost.tobytes(), cm.hard.astype("uint8").tobytes(), w, h, s, g,
                               deadline=time.monotonic() + self.timeout_s)
        except astar.PlanError:
            return None
        res = float(cm.res)
        steps = sum(math.hypot(a[0] - b[0], a[1] - b[1])
                    for a, b in zip(cells, cells[1:], strict=False))
        return round(steps * res, 2)

    def check(self, name: str, map_id: str, version: str, x: float, y: float) -> Reach:
        try:
            self.maps.get(map_id, version)
        except Exception:                                # noqa: BLE001 - 不认识的不挡(同巡检点)
            return Reach(note=f"站点没有 {map_id}:{version} 此版本地图,未检查能否到达")
        problem = self.maps.reach_problem(map_id, version, [(name, x, y)])
        if problem:
            return Reach(problem=problem)
        from d1max_contract.planning import astar
        try:
            cm = self._costmap(map_id, version)
        except Exception as exc:                         # noqa: BLE001 - 栅格坏了:查不了
            return Reach(note=f"规划栅格无法读取,未检查能否到达: {exc}")
        if cm is None:
            return Reach(note="此版本地图没有规划栅格(floor.pgm),未检查能否到达")
        g = cm.cell_of(x, y)
        if g is None:
            return Reach(problem=f"{name} 在规划栅格外面")
        if cm.hard[g]:
            return Reach(problem=f"{name} 在障碍或禁行区里")
        if cm.lethal[g]:
            return Reach(problem=f"{name} 离障碍太近,机器人站不下(机身半径 0.52 m 加余量)")
        starts = self.starts(map_id, version)
        if not starts:
            return Reach(note="此地图上尚无待命点、原点,未检查从何处可以到达")
        h, w = cm.shape
        cost, hard = cm.cost.tobytes(), cm.hard.astype("uint8").tobytes()
        import time
        why = []
        timed_out = False
        for sname, sx, sy in starts:
            s = cm.cell_of(sx, sy)
            if s is None:
                why.append(f"待命点 {sname} 在规划栅格外")
                continue
            try:
                astar.plan(cost, hard, w, h, s, g, deadline=time.monotonic() + self.timeout_s)
                return Reach()
            except astar.PlanError as exc:
                if exc.reason == "timeout":
                    timed_out = True
                why.append(f"{sname}:{exc}")
        if timed_out:
            return Reach(note="规划超时,未完成可达性检查")
        return Reach(problem=f"从待命点无法到达 {name}(" + ";".join(why)[:300] + ")")
