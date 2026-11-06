"""站点的零散设置(W44 第二期,``site_settings`` 表,值是 JSON)。

值班设置(设计 §5.1「布防时间」、§11 第 4 条):交班时间(两个钟点,默认 7:00、19:00,报警记录页「本班」
从这里算)、多久没机器人去过算「没巡过」(小时,默认 2,地图页那一层)。保安页面也要读;改是管理员的事。
"""

from __future__ import annotations

import json
from typing import Any

#: ``self_protect_siren``(W74 四期):布防时被遮挡、推倒、抱起来自动鸣笛;关了只开警灯
#: (现场不许自动鸣笛)。
DEFAULT_DUTY: dict[str, Any] = {"shift_hours": [7, 19], "stale_hours": 2,
                                "self_protect_siren": True}
#: 遥控速度档(POC 2026-11,m/s):手机摇杆满推 = 这一档。要 0 < 慢 ≤ 中 ≤ 快 ≤ 1.5。
#: 遥控速度表(m/s,POC;用户 11-05:按速度分档,目前最高到 SDK 中速档 2.0)。超过 1.0 的机器人自己换
#: 中速档;机器人到不了的档手机上不显示。
DEFAULT_TELEOP_SPEEDS: tuple[float, ...] = (0.3, 0.6, 1.0, 1.3, 1.6, 2.0)
TELEOP_SPEEDS_MAX_N = 8


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
            if isinstance(got.get("self_protect_siren"), bool):
                out["self_protect_siren"] = got["self_protect_siren"]
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
        if "self_protect_siren" in d:
            if not isinstance(d["self_protect_siren"], bool):
                raise SettingsError("自动鸣笛要是 true / false")
            cur["self_protect_siren"] = d["self_protect_siren"]
        self.put("duty", cur)
        return cur

    def teleop_speeds(self) -> list[float]:
        """遥控速度表(m/s,升序)。没设、存的不成形(包括改版前的慢 / 中 / 快)按缺省。"""
        got = self.get("teleop_speeds", None)
        return [float(v) for v in got] if _speeds_ok(got) else list(DEFAULT_TELEOP_SPEEDS)

    def set_teleop_speeds(self, d: Any, *, by: str) -> list[float]:
        """整张换掉:``{"speeds": [...]}``,1–8 个,严格升序,每个 0 < v ≤ 2.0。"""
        from nexwar_contract.teleop import TELEOP_SPEED_MAX_MPS
        got = d.get("speeds") if isinstance(d, dict) else None
        if _speeds_ok(got):
            got = [round(float(v), 2) for v in got]     # 取整之后还要不重复
        if not _speeds_ok(got):
            raise SettingsError(f"速度表要 1–{TELEOP_SPEEDS_MAX_N} 个数字,从小到大不重复,"
                                f"每个在 0–{TELEOP_SPEED_MAX_MPS} m/s")
        out = list(got)
        self.put("teleop_speeds", out)
        return out


def _speeds_ok(vs: Any) -> bool:
    import math

    from nexwar_contract.teleop import TELEOP_SPEED_MAX_MPS
    if not isinstance(vs, list) or not 1 <= len(vs) <= TELEOP_SPEEDS_MAX_N:
        return False
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               and 0 < v <= TELEOP_SPEED_MAX_MPS for v in vs):
        return False
    return all(a < b for a, b in zip(vs, vs[1:], strict=False))
