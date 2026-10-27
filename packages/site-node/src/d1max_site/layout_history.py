"""「布置」的历史版本(W44 第三期,设计 §5.2 第 2 条):每次保存留一版,写谁、什么时候改的,一键退回。

种类(``kind``)和快照(``body``,JSON):

- ``zones``:这张图这一版上的全部防区 ``[{"zone", "points"}]``;
- ``nogo``:禁行区、限速区、虚拟墙那一份的区域列表(``ZoneSet.zones`` 的线格式);
- ``route``:``item`` = 路线名,``{"map_id", "map_version", "waypoints", "policy"}``,删了是 ``null``;
- ``standby``:``item`` = 机器人,这张图上它的待命点;
- ``cameras``:全部摄像头(``map_id``、``map_version`` 都是空串),**不带口令**,地址里的账号口令也抹掉。

快照在各保存接口成功之后由 ``api.py`` 记(失败不记)。退回也在 ``api.py`` 里,走各自原来的保存
路径:禁行区放宽照样要确认、路线照样带修订号、摄像头的口令用现在那一台的。每个(种类、图、版本、
item)最多留 :data:`KEEP` 份。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from d1max_site.db import SiteDB

KINDS = ("zones", "nogo", "route", "standby", "cameras")
KEEP = 50


class HistoryError(ValueError):
    """问法不对(接口回 400)。"""


def summary(kind: str, body: Any) -> str:
    """一行给人看的:几个防区 / 几块区域 / 几个点 / 删了 / 几台。"""
    if body is None:
        return "删了" if kind == "route" else "空"
    if kind == "route":
        return f"{len(body.get('waypoints') or [])} 个点"
    if kind == "zones":
        return "、".join(a["zone"] for a in body)[:120] or "没有防区"
    if kind == "nogo":
        return f"{len(body)} 块" + ("" if not body else ":" + "、".join(
            str(z.get("label") or z.get("id")) for z in body)[:100])
    if kind == "standby":
        return "、".join(p["name"] for p in body)[:120] or "没有待命点"
    if kind == "cameras":
        return f"{len(body)} 台"
    return ""


class LayoutHistory:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def record(self, kind: str, map_id: str, version: str, item: str, body: Any, *, by: str,
               note: str = "") -> int:
        if kind not in KINDS:
            raise HistoryError(f"没有这种:{kind}")
        with self.db.tx() as c:
            hid = c.execute("INSERT INTO layout_history(kind, map_id, map_version, item, body, "
                            "note, by, at_ms) VALUES (?,?,?,?,?,?,?,?)",
                            (kind, map_id, version, item,
                             None if body is None else json.dumps(body, ensure_ascii=False),
                             note[:200], by[:128], self._now())).lastrowid
            c.execute("DELETE FROM layout_history WHERE kind=? AND map_id=? AND map_version=? "
                      "AND item=? AND id NOT IN (SELECT id FROM layout_history WHERE kind=? AND "
                      "map_id=? AND map_version=? AND item=? ORDER BY id DESC LIMIT ?)",
                      (kind, map_id, version, item, kind, map_id, version, item, KEEP))
        return int(hid)

    def list(self, kind: str, map_id: str, version: str, item: str | None = None,
             limit: int = KEEP) -> list[dict[str, Any]]:
        """新的在前,不带快照本身(带一行 ``summary``)。``item`` 不给 = 这种这张图上的都要。"""
        if kind not in KINDS:
            raise HistoryError(f"没有这种:{kind}")
        sql = "SELECT * FROM layout_history WHERE kind=? AND map_id=? AND map_version=?"
        args: list[Any] = [kind, map_id, version]
        if item is not None:
            sql += " AND item=?"
            args.append(item)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit)))
        return [{"id": r["id"], "kind": r["kind"], "map_id": r["map_id"],
                 "map_version": r["map_version"], "item": r["item"], "by": r["by"],
                 "at_ms": r["at_ms"], "note": r["note"],
                 "summary": summary(r["kind"], None if r["body"] is None
                                    else json.loads(r["body"]))} for r in rows]

    def get(self, hid: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM layout_history WHERE id=?", (hid,))
        if not rows:
            return None
        r = dict(rows[0])
        r["body"] = None if r["body"] is None else json.loads(r["body"])
        return r
