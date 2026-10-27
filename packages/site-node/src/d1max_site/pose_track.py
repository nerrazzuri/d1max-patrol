"""位置轨迹(W44,设计 §4.3、§4.4、§8):每台机器人走过哪里,落库留 30 天。

录像页拿它画「那一刻机器人在哪」(之前实线、之后虚线,点轨迹跳到那个时刻);地图页拿它算「多久没巡过」。
运行可视化(:mod:`d1max_site.runviz`)的轨迹只在内存里、只画这一趟,不够用。

- **时刻用遥测的 stamp(机器人的钟)**,跟录像同一个钟(录像的开头时刻也是机器人的钟);
  不用站点收到的时刻,不然录像页地图上的位置跟画面差几秒到几十秒。
- 挪了 :data:`STEP_M` 才记一个,没挪也每 :data:`MAX_GAP_MS` 记一个(录像页靠它判断那一刻知不知道在哪);
  换了图、换了版本马上记。定位不可信时遥测没有位姿:那段不记。
- 留 :data:`KEEP_MS`(跟录像一样 30 天),每小时那一拍删过期的。
"""

from __future__ import annotations

import math
from typing import Any

STEP_M = 1.0
MAX_GAP_MS = 10_000
KEEP_MS = 30 * 86_400_000
CELL_M = 2.0
#: 一次最多回多少个点(超了均匀抽稀,回包里写明)。
TRACK_LIMIT = 20_000
PRUNE_EVERY_MS = 3_600_000


class PoseTrack:
    def __init__(self, db: Any) -> None:
        self.db = db
        #: 每台最后记下的那个点:(stamp, map_id, map_version, x, y)
        self._last: dict[str, tuple[int, str, str, float, float]] = {}
        self._pruned_at = 0

    def attach(self, dispatcher: Any) -> None:
        dispatcher.on_telemetry(self.on_telemetry)

    def on_telemetry(self, robot_id: str, t: Any) -> None:
        p = t.pose
        if p is None:
            return
        stamp = int(t.stamp)
        x, y = float(p.x), float(p.y)
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        last = self._last.get(robot_id)
        if last is not None and last[1] == p.map_id and last[2] == p.map_version:
            moved = math.hypot(x - last[3], y - last[4])
            if moved < STEP_M and stamp - last[0] < MAX_GAP_MS:
                return
            if stamp <= last[0]:
                return                                  # 钟往回走了 / 重发的旧遥测:不记
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO pose_track(robot_id, stamp_ms, map_id, map_version, "
                      "x, y) VALUES (?, ?, ?, ?, ?, ?)",
                      (robot_id, stamp, p.map_id, p.map_version, round(x, 2), round(y, 2)))
        self._last[robot_id] = (stamp, p.map_id, p.map_version, x, y)

    def track(self, robot_id: str, since_ms: int, until_ms: int,
              limit: int = TRACK_LIMIT) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT stamp_ms, map_id, map_version, x, y FROM pose_track WHERE robot_id=? AND "
            "stamp_ms >= ? AND stamp_ms < ? ORDER BY stamp_ms", (robot_id, since_ms, until_ms))
        pts = [{"t": r["stamp_ms"], "map_id": r["map_id"], "map_version": r["map_version"],
                "x": r["x"], "y": r["y"]} for r in rows]
        if len(pts) <= limit:
            return pts
        step = (len(pts) - 1) / (limit - 1)
        return [pts[round(i * step)] for i in range(limit)]

    def last_visit(self, map_id: str, version: str, since_ms: int) -> list[dict[str, int]]:
        """按 :data:`CELL_M` 的格子,每格最后一次有机器人到过的时刻(``since_ms`` 起)。"""
        rows = self.db.query("SELECT x, y, stamp_ms FROM pose_track WHERE map_id=? AND "
                             "map_version=? AND stamp_ms >= ?", (map_id, version, since_ms))
        best: dict[tuple[int, int], int] = {}
        for r in rows:
            k = (math.floor(r["x"] / CELL_M), math.floor(r["y"] / CELL_M))
            best[k] = max(best.get(k, r["stamp_ms"]), r["stamp_ms"])
        return [{"i": i, "j": j, "t": t} for (i, j), t in sorted(best.items())]

    def purge(self, now_ms: int) -> int:
        with self.db.tx() as c:
            n = c.execute("DELETE FROM pose_track WHERE stamp_ms < ?", (now_ms - KEEP_MS,)).rowcount
        return int(n)

    def tick(self, now_ms: int) -> None:
        """每小时删一次过期的。"""
        if now_ms - self._pruned_at >= PRUNE_EVERY_MS:
            self._pruned_at = now_ms
            self.purge(now_ms)
