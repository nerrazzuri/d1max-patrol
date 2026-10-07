"""布防模式(W20)。全站一个当前模式,决定**入侵来了派不派狗、响不响铃**;排程巡检、狗自己的健康告警
不看模式,照常。

- ``armed`` 布防:所有防区都布防。
- ``home`` 在家:标了「在家时撤防」的防区撤防(院内、屋边),别的(外围)照常布防。
- ``visitor`` 访客:在「在家」的基础上,再撤掉这次指定的几个防区;**必须有结束时间**(默认 4 小时,
  最长 24 小时),到点自动回到开访客之前的模式。

**没配过的防区一律布防**(新接的摄像头报了个没人见过的防区名,宁可多响一次)。新站点默认布防。
撤防的防区来了入侵:照样记一条(``disarmed``),不派狗、不报告警(事件页看得见)。

谁能切:业主、管理员随便切(``set_mode``);保安只能切到布防(``arm``),撤防是业主的决定。
哪些防区「在家时撤防」由管理员配(``manage``)。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from d1max_site.db import SiteDB

log = logging.getLogger(__name__)

MODES = ("armed", "home", "visitor")
LABEL = {"armed": "布防", "home": "在家", "visitor": "访客"}
DEFAULT_VISITOR_MIN = 4 * 60
MAX_VISITOR_MIN = 24 * 60
#: 访客一次最多撤这么多个防区(一个庄园远到不了;防一个请求塞几千个名字进库)。
MAX_VISITOR_ZONES = 64
#: 防区名跟事件派遣收的一样:1–128 个字符(摄像头、NVR 报什么就是什么)。
_ZONE = re.compile(r"[^\x00-\x1f]{1,128}")


class ModeError(ValueError):
    """不合规矩的切换(站点 API 回 400)。"""


class ArmingDesk:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int],
                 publish: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.db = db
        self._now = now_ms
        self._publish = publish
        #: 访客到点自动退回时(``tick``):``(退回到的模式, 当时的访客那一行)``。站点接到审计上。
        self.on_expired: Callable[[str, dict[str, Any]], None] | None = None
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO site_mode(id, mode, prev_mode, visitor_zones, "
                      "until_ms, set_by, set_ms) VALUES (1, 'armed', '', '[]', NULL, '', 0)")

    # ------------------------------------------------------------ 读

    def _raw(self) -> dict[str, Any]:
        r = dict(self.db.query("SELECT * FROM site_mode WHERE id=1")[0])
        r["visitor_zones"] = json.loads(r["visitor_zones"] or "[]")
        return r

    def _effective(self, r: dict[str, Any]) -> dict[str, Any]:
        """访客过了点还没被 ``tick`` 收回的那几秒:**按已经退回算**(判防区不等那一拍)。"""
        if r["mode"] == "visitor" and r["until_ms"] is not None and self._now() >= r["until_ms"]:
            return r | {"mode": r["prev_mode"] or "armed", "prev_mode": "", "visitor_zones": [],
                        "until_ms": None}
        return r

    def home_disarmed(self) -> list[str]:
        return [r["zone"] for r in self.db.query(
            "SELECT zone FROM zone_arming WHERE home_armed=0 ORDER BY zone")]

    def armed(self, zone: str) -> tuple[bool, str]:
        """→ (这个防区现在布不布防, 当前模式)。"""
        r = self._effective(self._raw())
        mode = r["mode"]
        if mode == "armed":
            return True, mode
        if zone in self.home_disarmed():
            return False, mode
        if mode == "visitor" and zone in r["visitor_zones"]:
            return False, mode
        return True, mode

    def known_zones(self) -> list[str]:
        """手机挑防区用:绑过拦截点的、摄像头登记的、配过在家撤防的。"""
        rows = self.db.query(
            "SELECT zone FROM zones UNION SELECT zone FROM cameras UNION "
            "SELECT zone FROM zone_arming ORDER BY zone")
        return [r["zone"] for r in rows]

    def view(self) -> dict[str, Any]:
        r = self._effective(self._raw())
        home_off = set(self.home_disarmed())
        zones = []
        for z in sorted(set(self.known_zones()) | set(r["visitor_zones"])):
            off = r["mode"] != "armed" and (
                z in home_off or (r["mode"] == "visitor" and z in r["visitor_zones"]))
            zones.append({"zone": z, "home_armed": z not in home_off, "armed": not off})
        return {"mode": r["mode"], "label": LABEL[r["mode"]], "prev_mode": r["prev_mode"],
                "visitor_zones": r["visitor_zones"], "until_ms": r["until_ms"],
                "set_by": r["set_by"], "set_ms": r["set_ms"], "zones": zones}

    # ------------------------------------------------------------ 改

    def set_mode(self, mode: Any, *, by: str, zones: Any = None, minutes: Any = None
                 ) -> dict[str, Any]:
        """切模式。权限在 API 那一层按目标模式查(保安只能切到布防)。"""
        if mode not in MODES:
            raise ModeError(f"模式只有 {'、'.join(MODES)},给的是 {mode!r}")
        vz: list[str] = []
        until = None
        if mode == "visitor":
            if minutes is None:
                minutes = DEFAULT_VISITOR_MIN
            if isinstance(minutes, bool) or not isinstance(minutes, int) \
                    or not 1 <= minutes <= MAX_VISITOR_MIN:
                raise ModeError(f"访客要有结束时间:minutes 是 1–{MAX_VISITOR_MIN} 的整数分钟")
            if zones is None:
                zones = []
            if not isinstance(zones, list) or not all(
                    isinstance(z, str) and _ZONE.fullmatch(z) for z in zones):
                raise ModeError("zones 要是防区名的列表")
            vz = sorted(set(zones))
            if len(vz) > MAX_VISITOR_ZONES:
                raise ModeError(f"访客一次最多撤 {MAX_VISITOR_ZONES} 个防区")
            until = self._now() + minutes * 60_000
        elif zones is not None or minutes is not None:
            raise ModeError("只有访客模式带 zones、minutes")
        with self.db.tx() as c:
            cur = self._effective(self._raw())
            # 访客叠访客(延长、改防区):退回的还是开第一次访客之前的那个模式
            prev = (cur["prev_mode"] if cur["mode"] == "visitor" else cur["mode"]) \
                if mode == "visitor" else ""
            c.execute("UPDATE site_mode SET mode=?, prev_mode=?, visitor_zones=?, until_ms=?, "
                      "set_by=?, set_ms=? WHERE id=1",
                      (mode, prev, json.dumps(vz, ensure_ascii=False), until, by, self._now()))
        return self._changed()

    def set_zone_home(self, zone: Any, home_armed: Any) -> dict[str, Any]:
        """这个防区「在家」(以及访客)时布不布防。"""
        if not isinstance(zone, str) or not _ZONE.fullmatch(zone):
            raise ModeError(f"防区名不合规矩: {zone!r}")
        if not isinstance(home_armed, bool):
            raise ModeError("home_armed 要是 true/false")
        with self.db.tx() as c:
            if home_armed:                                 # 布防是默认:删掉这一行就是
                c.execute("DELETE FROM zone_arming WHERE zone=?", (zone,))
            else:
                c.execute("INSERT OR REPLACE INTO zone_arming(zone, home_armed) VALUES (?, 0)",
                          (zone,))
        return self._changed()

    def tick(self) -> bool:
        """访客到点:落库退回到开访客之前的模式,报给手机、审计。站点告警循环每拍调。退了回真。"""
        with self.db.tx() as c:
            r = self._raw()
            if r["mode"] != "visitor" or r["until_ms"] is None or self._now() < r["until_ms"]:
                return False
            back = r["prev_mode"] or "armed"
            c.execute("UPDATE site_mode SET mode=?, prev_mode='', visitor_zones='[]', "
                      "until_ms=NULL, set_by='site:visitor_expired', set_ms=? WHERE id=1",
                      (back, self._now()))
        log.info("访客模式到点,退回%s", LABEL[back])
        if self.on_expired is not None:
            try:
                self.on_expired(back, r)
            except Exception:
                log.exception("访客到点退回的审计记不下来")
        self._changed()
        return True

    def _changed(self) -> dict[str, Any]:
        v = self.view()
        if self._publish is not None:
            try:
                self._publish({"kind": "mode", "mode": v})
            except Exception:
                log.exception("模式变了,推给手机没推出去")
        return v
