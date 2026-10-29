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

布防时间表(W44 第二期,:mod:`d1max_site.arming_schedule`):开着的时候按时间表判防区;有人手动切了模式,
到时间表下一个边界之前按手动(访客按它自己的结束时间),过了回时间表。``view()`` 写明谁说了算
(``source``)、整体是布防 / 部分 / 撤防(``state``)、下一次变是几点。时间表管着时 ``mode`` 写成
``armed``(全部布防)或 ``home``(有撤防的),老手机、受力、看见人按它照旧能用。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from d1max_site.arming_schedule import ArmingSchedule
from d1max_site.db import SiteDB
from d1max_site.sitetime import site_tz

log = logging.getLogger(__name__)

MODES = ("armed", "home", "visitor")
LABEL = {"armed": "布防", "home": "在家", "visitor": "访客", "schedule": "按时间表"}
STATE_LABEL = {"armed": "布防", "partial": "部分布防", "disarmed": "撤防"}
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
        #: 名单(B1c,:class:`d1max_site.authz.AuthzBook`):此刻生效的时段 / 人员授权。站点主程序接上。
        self.authz: Any = None
        #: 布防时间表(W44 第二期)。测试换成别的时区。
        self.schedule: ArmingSchedule | None = ArmingSchedule(db, now_ms=now_ms,
                                                              tz=lambda: site_tz(db))
        #: 时间表管着时防区的布防状态变了(``tick`` 里):回当时的 ``view()``。站点接到审计上。
        self.on_schedule: Callable[[dict[str, Any]], None] | None = None
        self._last_fp: tuple | None = None
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO site_mode(id, mode, prev_mode, visitor_zones, "
                      "until_ms, set_by, set_ms) VALUES (1, 'armed', '', '[]', NULL, '', 0)")

    def authorized(self, zone: str | None) -> str | None:
        """此刻 ``zone`` 上生效的授权名字(B1c);没有、没接名单都是 ``None``。
        读不出来当没有(宁可多报)。"""
        if self.authz is None:
            return None
        try:
            return self.authz.active(zone)
        except Exception:
            log.exception("名单读不出来:当没有授权(宁可多报)")
            return None

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

    def _governed(self, r: dict[str, Any]) -> bool:
        """此刻按时间表(开着、不在手动期里)。"""
        if self.schedule is None or not self.schedule.enabled():
            return False
        mu = r.get("manual_until_ms")
        return mu is None or self._now() >= mu

    def home_disarmed(self) -> list[str]:
        return [r["zone"] for r in self.db.query(
            "SELECT zone FROM zone_arming WHERE home_armed=0 ORDER BY zone")]

    def armed(self, zone: str) -> tuple[bool, str]:
        """→ (这个防区现在布不布防, 当前模式)。整个判断拿着同一把读锁(W44 复查 R1:读完「按不按时间表」
        之后另一个线程关掉时间表,不能拼成「按时间表 + 没开」= 撤防)。"""
        with self.db.snapshot():
            return self._armed_locked(zone)

    def _armed_locked(self, zone: str) -> tuple[bool, str]:
        r = self._effective(self._raw())
        if self._governed(r):
            assert self.schedule is not None
            on = self.schedule.scheduled(zone, self._now())
            return on is not False, "schedule"          # None(锁里不该有)按布防:宁可报
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
            "SELECT zone FROM zone_arming UNION SELECT zone FROM zone_areas UNION "
            "SELECT zone FROM arming_slots WHERE zone<>'' ORDER BY zone")
        return [r["zone"] for r in rows]

    def robot_armed(self, reliable: bool, zone: str | None) -> bool:
        """机器人此刻所在的地方布不布防(看见人报不报、被抱起响不响警笛;W44 复查 R2)。

        - 手动(不按时间表):老约定,只有「布防」模式才算(在家、访客一律不报)。
        - 按时间表:各具名防区和「没画到的地方」(``''`` 那份)**全都**撤防才不报;不然位置可靠就按它
          所在防区(不在任何防区里按 ``''`` 那份),位置说不清宁可报。
        整个判断拿着同一把读锁(复查 R1)。"""
        with self.db.snapshot():
            r = self._effective(self._raw())
            if not self._governed(r):
                return r["mode"] == "armed"
            assert self.schedule is not None
            now = self._now()
            parts = [self.schedule.scheduled(z, now) for z in self.known_zones()]
            parts.append(self.schedule.scheduled("", now))
            if all(p is False for p in parts):
                return False
            if not reliable:
                return True
            return self.schedule.scheduled(zone or "", now) is not False

    def view(self) -> dict[str, Any]:
        with self.db.snapshot():
            return self._view_locked()

    def _view_locked(self) -> dict[str, Any]:
        r = self._effective(self._raw())
        now = self._now()
        governed = self._governed(r)
        home_off = set(self.home_disarmed())
        zones = []
        for z in sorted(set(self.known_zones()) | set(r["visitor_zones"])):
            if governed:
                assert self.schedule is not None
                on = bool(self.schedule.scheduled(z, now))
            else:
                on = not (r["mode"] != "armed" and (
                    z in home_off or (r["mode"] == "visitor" and z in r["visitor_zones"])))
            zones.append({"zone": z, "home_armed": z not in home_off, "armed": on})
        default_on: bool | None = None
        if zones and governed:
            # 没画进任何具名防区的地方按「全部防区一样」那份(W44 复查 R2):它也算一份
            assert self.schedule is not None
            default_on = self.schedule.scheduled("", now) is not False
        if zones:
            parts = [z["armed"] for z in zones] + ([default_on] if default_on is not None else [])
            n = sum(parts)
            state = "armed" if n == len(parts) else "disarmed" if n == 0 else "partial"
        elif governed:
            assert self.schedule is not None
            state = "armed" if self.schedule.scheduled(None, now) else "disarmed"
        else:
            state = "armed" if r["mode"] == "armed" else "partial"
        mode, label = r["mode"], LABEL[r["mode"]]
        if governed:
            assert self.schedule is not None
            mode = "armed" if state == "armed" else "home"
            label = f"{LABEL['schedule']}:{STATE_LABEL[state]}"
            nxt = self.schedule.next_boundary(now)
        else:
            nxt = r.get("manual_until_ms") or r["until_ms"]
        return {"mode": mode, "label": label, "prev_mode": r["prev_mode"],
                "visitor_zones": r["visitor_zones"], "until_ms": r["until_ms"],
                "set_by": r["set_by"], "set_ms": r["set_ms"], "zones": zones,
                "source": "schedule" if governed else "manual", "state": state,
                "manual_until_ms": None if governed else r.get("manual_until_ms"),
                "default_armed": default_on,
                "next_change_ms": nxt}

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
        # 时间表开着:手动的管到下一个边界(访客管到它自己的结束时间),过了回时间表
        manual_until = None
        if self.schedule is not None and self.schedule.enabled():
            manual_until = until if mode == "visitor" else self.schedule.next_boundary(self._now())
        with self.db.tx() as c:
            cur = self._effective(self._raw())
            # 访客叠访客(延长、改防区):退回的还是开第一次访客之前的那个模式
            prev = (cur["prev_mode"] if cur["mode"] == "visitor" else cur["mode"]) \
                if mode == "visitor" else ""
            c.execute("UPDATE site_mode SET mode=?, prev_mode=?, visitor_zones=?, until_ms=?, "
                      "set_by=?, set_ms=?, manual_until_ms=? WHERE id=1",
                      (mode, prev, json.dumps(vz, ensure_ascii=False), until, by, self._now(),
                       manual_until))
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
        """站点告警循环每拍调。访客到点:落库退回到开访客之前的模式(时间表开着就回时间表);手动期过了:
        回时间表;时间表管着时防区的布防状态变了:报给手机、审计。变了回真。"""
        now = self._now()
        expired: tuple[str, dict[str, Any]] | None = None
        manual_over = False
        with self.db.tx() as c:
            r = self._raw()
            if r["mode"] == "visitor" and r["until_ms"] is not None and now >= r["until_ms"]:
                back = r["prev_mode"] or "armed"
                c.execute("UPDATE site_mode SET mode=?, prev_mode='', visitor_zones='[]', "
                          "until_ms=NULL, manual_until_ms=NULL, set_by='site:visitor_expired', "
                          "set_ms=? WHERE id=1", (back, now))
                expired = (back, r)
            elif r.get("manual_until_ms") is not None and now >= r["manual_until_ms"]:
                c.execute("UPDATE site_mode SET manual_until_ms=NULL WHERE id=1")
                manual_over = True
        if expired is not None:
            log.info("访客模式到点,退回%s", LABEL[expired[0]])
            if self.on_expired is not None:
                try:
                    self.on_expired(*expired)
                except Exception:
                    log.exception("访客到点退回的审计记不下来")
        v = self.view()
        fp = (tuple((z["zone"], z["armed"]) for z in v["zones"]) + (v["state"],)
              if v["source"] == "schedule" else None)
        flipped = fp is not None and self._last_fp is not None and fp != self._last_fp
        self._last_fp = fp
        if flipped:
            log.info("按时间表:%s", v["label"])
            if self.on_schedule is not None:
                try:
                    self.on_schedule(v)
                except Exception:
                    log.exception("按时间表切换的审计记不下来")
        if expired is not None or manual_over or flipped:
            self._push(v)
            return True
        return False

    def _changed(self) -> dict[str, Any]:
        v = self.view()
        self._push(v)
        return v

    def _push(self, v: dict[str, Any]) -> None:
        if self._publish is not None:
            try:
                self._publish({"kind": "mode", "mode": v})
            except Exception:
                log.exception("模式变了,推给手机没推出去")
