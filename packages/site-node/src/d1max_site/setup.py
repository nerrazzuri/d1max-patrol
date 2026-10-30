"""开站清单与上手向导的记录(W53 §8,用户 2026-10-31:只买设备没买交付服务的客户要能自己学会)。

**开站清单**:站点按现在的数据判断每一项做完没有(不靠人勾);App、网页给每一项配「去做」。项的说明
文字在 App、网页里(跟着语言走),这里只出 ``id``、做完没有、细节(哪几台没设待命点之类)。人可以把
某一项标成「不用了」(比如不排布防时间、一直手动切),记在自己账号的 ``guide.skip`` 里。

**上手向导**:每个账号看过哪一版导览(``guide.tour`` 是 App 的、``guide.web_tour`` 是网页的)、
开站清单收没收起来(``guide.setup_hidden``),
记在账号表 ``accounts.guide``(JSON)。换手机不用再看一遍。
"""

from __future__ import annotations

import json
from typing import Any

#: 清单的项,顺序就是给人看的顺序。
ITEMS = ("robots", "map", "zones", "standby", "routes", "schedule", "arming", "accounts", "push")
#: ``guide`` 里认的键。
_GUIDE_KEYS = {"tour": int, "web_tour": int, "setup_hidden": bool, "skip": list}


class GuideError(ValueError):
    pass


def _count(db: Any, sql: str, args: tuple = ()) -> int:
    return int(db.query(sql, args)[0][0])


def setup_view(db: Any, dispatcher: Any, *, skip: set[str] | None = None) -> dict[str, Any]:
    """在事件循环线程上调(要读派遣器里各台机器人报的已加载地图)。"""
    skip = skip or set()
    robots = [r.robot_id for r in dispatcher.registry.list() if not r.revoked]
    loaded: dict[str, tuple[str, str]] = {}
    for rid in robots:
        got = dispatcher._loaded(dispatcher.clients.get(rid))
        if got is not None:
            loaded[rid] = got
    zones = getattr(dispatcher, "zones", None)
    unconfirmed = sorted({f"{m}:{v}" for m, v in loaded.values()
                          if zones is not None and not zones.confirmed_current(m, v)})
    no_standby = [rid for rid, (m, v) in loaded.items()
                  if not _count(db, "SELECT COUNT(*) FROM standby_points WHERE robot_id=? AND "
                                "map_id=? AND map_version=? AND is_default=1", (rid, m, v))]
    has_map = bool(loaded) or _count(db, "SELECT COUNT(*) FROM maps") > 0
    people = _count(db, "SELECT COUNT(*) FROM accounts WHERE disabled=0 AND role != 'engineer'")
    facts: dict[str, tuple[bool, dict[str, Any]]] = {
        "robots": (bool(robots), {"count": len(robots)}),
        "map": (has_map, {}),
        "zones": (has_map and not unconfirmed, {"maps": unconfirmed}),
        "standby": (bool(loaded) and not no_standby, {"robots": no_standby}),
        "routes": (_count(db, "SELECT COUNT(*) FROM routes") > 0, {}),
        "schedule": (_count(db, "SELECT COUNT(*) FROM route_schedules") > 0, {}),
        "arming": (_count(db, "SELECT COUNT(*) FROM arming_slots") > 0, {}),
        "accounts": (people >= 2, {"count": people}),
        "push": (_count(db, "SELECT COUNT(*) FROM push_devices") > 0, {}),
    }
    items = [{"id": k, "done": facts[k][0], "skipped": k in skip and not facts[k][0],
              **facts[k][1]} for k in ITEMS]
    left = [i for i in items if not i["done"] and not i["skipped"]]
    return {"items": items, "done": len(items) - len(left), "total": len(items)}


def _parse(raw: str) -> dict[str, Any]:
    try:
        g = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        g = {}
    return g if isinstance(g, dict) else {}


def read_guide(accounts: Any, name: str) -> dict[str, Any]:
    """``accounts`` 是 ``accounts.Accounts``(账号表只由它写)。"""
    g = _parse(accounts.guide(name))
    return {"tour": int(g.get("tour") or 0), "web_tour": int(g.get("web_tour") or 0),
            "setup_hidden": bool(g.get("setup_hidden")),
            "skip": [s for s in g.get("skip") or [] if s in ITEMS]}


def write_guide(accounts: Any, name: str, patch: Any) -> dict[str, Any]:
    """合进去(只认 ``tour``、``setup_hidden``、``skip``)。"""
    if not isinstance(patch, dict) or not patch or set(patch) - set(_GUIDE_KEYS):
        raise GuideError(f"只认 {'、'.join(_GUIDE_KEYS)}")
    for k, typ in _GUIDE_KEYS.items():
        if k in patch and (not isinstance(patch[k], typ) or isinstance(patch[k], bool)
                           and typ is int):
            raise GuideError(f"{k} 类型不对")
    for k in ("tour", "web_tour"):
        if k in patch and not 0 <= patch[k] <= 1000:
            raise GuideError(f"{k} 是 0–1000 的版本号")
    if "skip" in patch and (len(patch["skip"]) > len(ITEMS)
                            or any(s not in ITEMS for s in patch["skip"])):
        raise GuideError(f"skip 只能是 {'、'.join(ITEMS)}")
    def merge(raw: str) -> str:
        g = _parse(raw)
        g.update(patch)
        return json.dumps(g, ensure_ascii=False)
    if not accounts.set_guide(name, merge):
        raise GuideError("没有这个账号")
    return read_guide(accounts, name)
