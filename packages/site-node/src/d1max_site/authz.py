"""名单第一版:时段 / 人员授权(商业化 B1c)。

一条授权 = 谁(名字,比如「园丁」)、在哪(几个防区,或者全站)、星期几、几点到几点(站点本地时间,可以跨
午夜)、有效到哪天(可选)。授权生效的时候:

- 摄像头报的那个防区的入侵事件:只记录、不派狗(跟撤防一样,记下是谁的授权);
- 狗看见人、狗在授权的防区里(按 :mod:`d1max_site.areas` 画的多边形算)或者授权是全站的:不报 P1,
  记一条 P3「授权在场」(历史里查得到)。

人脸名单、车牌名单另行立项(要识别、牵涉隐私),不在这里。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from d1max_site.db import SiteDB

_ZONE = re.compile(r"[^\x00-\x1f]{1,128}")
#: 星期一 … 星期日 → 位(bit 0 = 星期一,跟 ``time.localtime().tm_wday`` 一样)。
ALL_DAYS = 0b1111111
MAX_ENTRIES = 200


class AuthzError(ValueError):
    """授权写得不对(接口回 400)。"""


def _minutes(v: Any, what: str) -> int:
    if not isinstance(v, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
        raise AuthzError(f"{what} 要写成 HH:MM(24 小时)")
    h, m = v.split(":")
    return int(h) * 60 + int(m)


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


class AuthzBook:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int],
                 localtime: Callable[[float], time.struct_time] = time.localtime) -> None:
        self.db = db
        self._now = now_ms
        self._local = localtime

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM authorizations ORDER BY name, id")
        return [{"id": r["id"], "name": r["name"], "zones": json.loads(r["zones"]),
                 "days": r["days"], "start": _hhmm(r["start_min"]), "end": _hhmm(r["end_min"]),
                 "until_ms": r["until_ms"], "note": r["note"], "created_by": r["created_by"],
                 "created_ms": r["created_ms"]} for r in rows]

    def add(self, d: dict[str, Any], *, by: str) -> int:
        name = str(d.get("name") or "").strip()
        if not 1 <= len(name) <= 64:
            raise AuthzError("要写是谁(1–64 个字)")
        zones = d.get("zones") or []
        if not isinstance(zones, list) or len(zones) > 32 or not all(
                isinstance(z, str) and _ZONE.fullmatch(z) for z in zones):
            raise AuthzError("防区要是名字的列表(最多 32 个;空 = 全站)")
        days = d.get("days", ALL_DAYS)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= ALL_DAYS:
            raise AuthzError("星期要至少选一天")
        start, end = _minutes(d.get("start"), "开始时间"), _minutes(d.get("end"), "结束时间")
        if start == end:
            raise AuthzError("开始和结束不能是同一个时间")
        until = d.get("until_ms")
        if until is not None and (not isinstance(until, int) or isinstance(until, bool)
                                  or until <= self._now()):
            raise AuthzError("有效期要在以后")
        note = str(d.get("note") or "")[:200]
        with self.db.tx() as c:
            if c.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] >= MAX_ENTRIES:
                raise AuthzError(f"最多 {MAX_ENTRIES} 条")
            return c.execute(
                "INSERT INTO authorizations(name, zones, days, start_min, end_min, until_ms, note, "
                "created_by, created_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, json.dumps(sorted(set(zones)), ensure_ascii=False), days, start, end,
                 until, note, by, self._now())).lastrowid

    def remove(self, entry_id: int) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM authorizations WHERE id=?", (entry_id,)).rowcount > 0

    def active(self, zone: str | None, now_ms: int | None = None) -> str | None:
        """此刻在 ``zone``(``None`` = 说不清在哪个防区)生效的授权的名字;没有就 ``None``。

        说不清防区的时候只认全站授权。跨午夜的(22:00–06:00):开始那天的晚上和第二天的早上都算,
        星期按开始那天算。"""
        now = self._now() if now_ms is None else now_ms
        lt = self._local(now / 1000)
        minute, wday = lt.tm_hour * 60 + lt.tm_min, lt.tm_wday
        for e in self.list():
            if e["until_ms"] is not None and now >= e["until_ms"]:
                continue
            if e["zones"] and (zone is None or zone not in e["zones"]):
                continue
            s, t = _minutes(e["start"], ""), _minutes(e["end"], "")
            if s < t:
                ok = s <= minute < t and e["days"] >> wday & 1
            else:                                        # 跨午夜
                ok = (minute >= s and e["days"] >> wday & 1) or (
                    minute < t and e["days"] >> ((wday - 1) % 7) & 1)
            if ok:
                return e["name"]
        return None
