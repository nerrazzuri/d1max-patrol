"""站点的零散设置(W44 第二期,``site_settings`` 表,值是 JSON)。

值班设置(设计 §5.1「布防时间」、§11 第 4 条):交班时间(两个钟点,默认 7:00、19:00,报警记录页「本班」
从这里算)、多久没机器人去过算「没巡过」(小时,默认 2,地图页那一层)。保安页面也要读;改是管理员的事。
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_DUTY: dict[str, Any] = {"shift_hours": [7, 19], "stale_hours": 2}
#: 遥控速度档(POC 2026-11,m/s):手机摇杆满推 = 这一档。要 0 < 慢 ≤ 中 ≤ 快 ≤ 1.5。
DEFAULT_TELEOP_SPEEDS: dict[str, float] = {"slow": 0.3, "mid": 0.6, "fast": 1.0}
TIERS = ("slow", "mid", "fast")


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
            self.put_in(c, key, value)

    @staticmethod
    def put_in(c: Any, key: str, value: Any) -> None:
        """在别人开着的事务里写(跟别的表一起提交,比如布防时间表的开关和时间段)。"""
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
                raise SettingsError("多久未巡过要是 1–48 小时的整数")
            cur["stale_hours"] = st
        self.put("duty", cur)
        return cur

    def teleop_speeds(self) -> dict[str, float]:
        got = self.get("teleop_speeds", {})
        out = dict(DEFAULT_TELEOP_SPEEDS)
        if isinstance(got, dict):
            cand = {k: got.get(k, out[k]) for k in TIERS}
            if _speeds_ok(cand):
                out = {k: float(cand[k]) for k in TIERS}
        return out

    def set_teleop_speeds(self, d: Any, *, by: str) -> dict[str, float]:
        """改一档或几档(没给的保持原样);改完整组要满足 0 < 慢 ≤ 中 ≤ 快 ≤ 1.5。"""
        if not isinstance(d, dict):
            raise SettingsError("要一个对象")
        cur: dict[str, Any] = self.teleop_speeds()
        for k in TIERS:
            if k in d:
                cur[k] = d[k]
        if not _speeds_ok(cur):
            from nexwar_contract.teleop import TELEOP_SPEED_MAX_MPS
            raise SettingsError(f"速度档要是数字,且 0 < 慢 ≤ 中 ≤ 快 ≤ {TELEOP_SPEED_MAX_MPS} m/s")
        out = {k: round(float(cur[k]), 2) for k in TIERS}
        self.put("teleop_speeds", out)
        return out


def _speeds_ok(d: dict[str, Any]) -> bool:
    import math

    from nexwar_contract.teleop import TELEOP_SPEED_MAX_MPS
    vs = [d.get(k) for k in TIERS]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in vs):
        return False
    return 0 < vs[0] <= vs[1] <= vs[2] <= TELEOP_SPEED_MAX_MPS
