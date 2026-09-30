"""禁行区、限速区(W10 设计稿 §2):站点是权威,每个几何版本一份,修订号每改 +1。

- 改(``put``):整份换,带 ``base_revision``(改之前看到的修订号),跟库里的对不上 →
  :class:`ZonesConflict`(两个人同时改,后到的不许悄悄盖掉先到的)。
- **发布前人工确认**(``confirm``,W08 决定 5):确认的是「水体、落差(台阶下沿、路缘、池边)、
  陡坡、花坛都已画成禁行区」这一句,记确认人、时刻、确认的是哪一版修订;改过之后要重新确认。
- 换图门槛:这个几何版本的区域**已确认、且确认的就是当前修订**(:meth:`confirmed_current`)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.zones import ZoneSet, tightens

CONFIRM_TEXT = "水体、落差(台阶下沿、路缘、池边)、陡坡、花坛都已画成禁行区"


class ZonesError(Exception):
    pass


class ZonesConflict(ZonesError):
    pass


class NavZones:
    def __init__(self, db: Any, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def _row(self, map_id: str, version: str) -> Any:
        rows = self.db.query("SELECT * FROM nav_zones WHERE map_id=? AND map_version=?",
                             (map_id, version))
        return rows[0] if rows else None

    def current(self, map_id: str, version: str) -> ZoneSet:
        """当前那一份;没画过是修订 0、空的。"""
        r = self._row(map_id, version)
        if r is None:
            return ZoneSet(map_id, version, 0, ())
        return ZoneSet.from_wire(json.loads(r["body"]))

    def view(self, map_id: str, version: str) -> dict[str, Any]:
        r = self._row(map_id, version)
        zs = self.current(map_id, version)
        out: dict[str, Any] = zs.to_wire() | {"confirm_text": CONFIRM_TEXT, "confirmed": None,
                                              "updated_by": None, "updated_ms": None}
        if r is not None:
            out["updated_by"], out["updated_ms"] = r["updated_by"], r["updated_ms"]
            if r["confirmed_rev"] is not None:
                out["confirmed"] = {"revision": r["confirmed_rev"], "by": r["confirmed_by"],
                                    "at_ms": r["confirmed_ms"],
                                    "current": r["confirmed_rev"] == r["revision"]}
        return out

    def put(self, map_id: str, version: str, zones: Any, *, base_revision: Any,
            by: str) -> tuple[ZoneSet, bool]:
        """整份换 → (新的那份, 是不是只收紧)。``base_revision`` 对不上抛 :class:`ZonesConflict`;
        区域不合规矩抛 :class:`ZonesError`。"""
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ZonesError("base_revision 要是整数(改之前看到的修订号)")
        with self.db.tx() as tx:
            rows = tx.execute("SELECT revision, body FROM nav_zones WHERE map_id=? AND "
                              "map_version=?", (map_id, version)).fetchall()
            cur = (ZoneSet.from_wire(json.loads(rows[0]["body"])) if rows
                   else ZoneSet(map_id, version, 0, ()))
            if base_revision != cur.revision:
                raise ZonesConflict(f"区域已经被改过(现在是第 {cur.revision} 版,你看到的是第 "
                                    f"{base_revision} 版):重新拉一下再改")
            try:
                new = ZoneSet.from_wire({"map_id": map_id, "map_version": version,
                                         "revision": cur.revision + 1, "zones": zones})
            except ContractError as exc:
                raise ZonesError(str(exc)) from exc
            body = json.dumps(new.to_wire(), ensure_ascii=False)
            if rows:
                tx.execute("UPDATE nav_zones SET revision=?, body=?, updated_by=?, updated_ms=? "
                           "WHERE map_id=? AND map_version=?",
                           (new.revision, body, by, self._now(), map_id, version))
            else:
                tx.execute("INSERT INTO nav_zones(map_id, map_version, revision, body, updated_by,"
                           " updated_ms) VALUES (?,?,?,?,?,?)",
                           (map_id, version, new.revision, body, by, self._now()))
        return new, tightens(cur, new)

    def confirm(self, map_id: str, version: str, *, revision: Any, by: str) -> None:
        """人工确认这一版修订(没画过的就是确认第 0 版:这张图没什么要画的)。"""
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ZonesError("revision 要是整数(确认的是哪一版)")
        with self.db.tx() as tx:
            rows = tx.execute("SELECT revision FROM nav_zones WHERE map_id=? AND map_version=?",
                              (map_id, version)).fetchall()
            cur = rows[0]["revision"] if rows else 0
            if revision != cur:
                raise ZonesConflict(f"区域现在是第 {cur} 版,确认的是第 {revision} 版:"
                                    "看过最新的再确认")
            if rows:
                tx.execute("UPDATE nav_zones SET confirmed_rev=?, confirmed_by=?, confirmed_ms=? "
                           "WHERE map_id=? AND map_version=?",
                           (cur, by, self._now(), map_id, version))
            else:
                empty = json.dumps(ZoneSet(map_id, version, 0, ()).to_wire())
                tx.execute("INSERT INTO nav_zones(map_id, map_version, revision, body, updated_by,"
                           " updated_ms, confirmed_rev, confirmed_by, confirmed_ms) "
                           "VALUES (?,?,?,?,?,?,?,?,?)",
                           (map_id, version, 0, empty, by, self._now(), 0, by, self._now()))

    def confirmed_current(self, map_id: str, version: str) -> bool:
        r = self._row(map_id, version)
        return r is not None and r["confirmed_rev"] is not None \
            and r["confirmed_rev"] == r["revision"]
