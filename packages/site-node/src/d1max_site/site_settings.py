"""站点的零散设置(W44 第二期,``site_settings`` 表,值是 JSON)。

值班设置(设计 §5.1「布防时间」、§11 第 4 条):交班时间(两个钟点,默认 7:00、19:00,报警记录页「本班」
从这里算)、多久没机器人去过算「没巡过」(小时,默认 2,地图页那一层)。保安页面也要读;改是业主的事。
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_DUTY: dict[str, Any] = {"shift_hours": [7, 19], "stale_hours": 2}


class SettingsError(ValueError):
    """设置写得不对(接口回 400)。"""


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class SiteSettings:
    def __init__(self, db: Any) -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        rows = self.db.query("SELECT value FROM site_settings WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except ValueError:
            return default

    def put(self, key: str, value: Any) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO site_settings(key, value) VALUES (?, ?)",
                      (key, json.dumps(value, ensure_ascii=False)))

    def duty(self) -> dict[str, Any]:
        got = self.get("duty", {})
        out = dict(DEFAULT_DUTY)
        if isinstance(got, dict):
            sh = got.get("shift_hours")
            if isinstance(sh, list) and len(sh) == 2 and all(_int(h) and 0 <= h <= 23 for h in sh) \
                    and sh[0] != sh[1]:
                out["shift_hours"] = sorted(sh)
            st = got.get("stale_hours")
            if _int(st) and 1 <= st <= 48:
                out["stale_hours"] = st
        return out

    def set_duty(self, d: Any, *, by: str) -> dict[str, Any]:
        if not isinstance(d, dict):
            raise SettingsError("要一个对象")
        cur = self.duty()
        if "shift_hours" in d:
            sh = d["shift_hours"]
            if not isinstance(sh, list) or len(sh) != 2 \
                    or not all(_int(h) and 0 <= h <= 23 for h in sh):
                raise SettingsError("交班时间要两个 0–23 的整点")
            if sh[0] == sh[1]:
                raise SettingsError("两个交班时间不能一样")
            cur["shift_hours"] = sorted(sh)
        if "stale_hours" in d:
            st = d["stale_hours"]
            if not _int(st) or not 1 <= st <= 48:
                raise SettingsError("多久没巡过要是 1–48 小时的整数")
            cur["stale_hours"] = st
        self.put("duty", cur)
        return cur
