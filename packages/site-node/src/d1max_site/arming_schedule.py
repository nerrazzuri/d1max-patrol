"""布防时间表(W44 第二期,设计 §5.1「布防时间」、§11 第 5 条)。

一周七天,每天若干段「布防」的时间(站点时区,``HH:MM``,结束可以写 ``24:00``);``zone=''`` 那一份是
「全部防区一样」,单独设过的防区用自己的。再加例外日期(节假日、出远门):那几天(两头都算)全天全站布防或
撤防,盖过时间段。

谁说了算(跟 :mod:`d1max_site.modes` 合起来):时间表开着、而且不在手动期里 → 按这里;业主手动切了模式,
到下一个边界(:meth:`ArmingSchedule.next_boundary`)之前按手动;访客到点回时间表。

- 防区有自己的段用自己的;没有用 ``''`` 的;都没有 → 布防(没配过一律布防,跟 ``modes`` 同一条规矩)。
- 时间表开着、一段都没画 → 全天撤防(业主画的就是这样)。
- 24:00 按明天 0 点算(``datetime.combine`` 下一天);夏令时那一天按墙上的钟。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from d1max_site.db import SiteDB

_ZONE = re.compile(r"[^\x00-\x1f]{0,128}")          # '' = 全部防区一样
_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d|24:00")
MAX_SLOTS = 500
MAX_EXCEPTIONS = 100
MAX_EXCEPTION_DAYS = 366
STATES = ("armed", "disarmed")
_KEY = "arming_schedule"


class ScheduleError(ValueError):
    """时间表写得不对(接口回 400)。"""


def _min(v: Any, what: str) -> int:
    if not isinstance(v, str) or not _HHMM.fullmatch(v):
        raise ScheduleError(f"{what} 要写成 HH:MM(24 小时,结束可以写 24:00)")
    h, m = v.split(":")
    return int(h) * 60 + int(m)


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _day(v: Any, what: str) -> date:
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ScheduleError(f"{what} 要写成 YYYY-MM-DD")
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise ScheduleError(f"{what} 不是一个日子:{v}") from exc


class ArmingSchedule:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int],
                 tz: Callable[[], tzinfo]) -> None:
        self.db = db
        self._now = now_ms
        self._tz = tz

    # ------------------------------------------------------------ 读

    # 开关和时间段一起存(一个事务)、一起读(:meth:`SiteDB.snapshot`):外审 R1,不会读到「开着 + 空时间表」
    # 那种半截的样子(那就是全天撤防)。

    @staticmethod
    def _enabled_in(c: Any) -> bool:
        row = c.execute("SELECT value FROM site_settings WHERE key=?", (_KEY,)).fetchone()
        if row is None:
            return False
        try:
            return json.loads(row["value"]).get("enabled") is True
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def _slots_in(c: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in c.execute(
            "SELECT zone, day, start_min, end_min FROM arming_slots ORDER BY zone, day, start_min")]

    def enabled(self) -> bool:
        with self.db.snapshot() as c:
            return self._enabled_in(c)

    def _slots(self) -> list[dict[str, Any]]:
        with self.db.snapshot() as c:
            return self._slots_in(c)

    def _state(self, day: date) -> tuple[bool, list[dict[str, Any]], str | None]:
        """开没开、时间段、这一天的例外:同一时刻的一份。"""
        with self.db.snapshot() as c:
            return self._enabled_in(c), self._slots_in(c), self._exception_in(c, day)

    def exceptions(self) -> list[dict[str, Any]]:
        return [{k: r[k] for k in ("id", "first_day", "last_day", "state", "note", "created_by")}
                for r in self.db.query("SELECT * FROM arming_exceptions ORDER BY first_day, id")]

    def zones(self) -> list[str]:
        """时间表里单独设过的防区。"""
        return [r["zone"] for r in self.db.query(
            "SELECT DISTINCT zone FROM arming_slots WHERE zone<>'' ORDER BY zone")]

    def view(self) -> dict[str, Any]:
        now = datetime.fromtimestamp(self._now() / 1000, self._tz())
        with self.db.snapshot():
            on, slots, ex = self._state(now.date())
            excs = self.exceptions()
        return {"enabled": on,
                "slots": [{"zone": s["zone"], "day": s["day"], "start": _hhmm(s["start_min"]),
                           "end": _hhmm(s["end_min"])} for s in slots],
                "exceptions": excs, "today": self._today(slots, now, ex)}

    @staticmethod
    def _today(slots: list[dict[str, Any]], now: datetime, ex: str | None) -> list[dict[str, Any]]:
        """今天(站点时区)每一份(``''`` 和单独设的防区)的布防时间。例外的日子按例外。"""
        zones = [""] + sorted({s["zone"] for s in slots if s["zone"]})
        out = []
        for z in zones:
            if ex is not None:
                wins = [["00:00", "24:00"]] if ex == "armed" else []
            else:
                wins = [[_hhmm(s["start_min"]), _hhmm(s["end_min"])] for s in slots
                        if s["zone"] == z and s["day"] == now.weekday()]
            out.append({"zone": z, "windows": wins})
        return out

    @staticmethod
    def _exception_in(c: Any, d: date) -> str | None:
        row = c.execute("SELECT state FROM arming_exceptions WHERE first_day<=? AND "
                        "last_day>=? ORDER BY id DESC LIMIT 1",
                        (d.isoformat(), d.isoformat())).fetchone()
        return row["state"] if row else None

    def scheduled(self, zone: str | None, now_ms: int) -> bool | None:
        """``zone`` 此刻按时间表布不布防;没开时间表回 ``None``。"""
        now = datetime.fromtimestamp(now_ms / 1000, self._tz())
        on, slots, ex = self._state(now.date())
        if not on:
            return None
        if ex is not None:
            return ex == "armed"
        if not slots:
            return False                        # 开着、一段都没画:业主画的就是全天撤防
        mine = [s for s in slots if zone and s["zone"] == zone]
        if not mine:
            mine = [s for s in slots if s["zone"] == ""]
            if not mine:
                return True                     # 没自己的、也没「全部一样」那一份:一律布防
        minute = now.hour * 60 + now.minute
        return any(s["day"] == now.weekday() and s["start_min"] <= minute < s["end_min"]
                   for s in mine)

    def next_boundary(self, now_ms: int) -> int | None:
        """下一个布防状态可能变的时刻:今天剩下的段头段尾,最晚是今天 24:00(明天 0 点,例外按天
        换)。没开回 ``None``。"""
        tz = self._tz()
        today = datetime.fromtimestamp(now_ms / 1000, tz).date()
        on, slots, _ = self._state(today)
        if not on:
            return None
        edges: dict[int, set[int]] = {}
        for s in slots:
            edges.setdefault(s["day"], set()).update((s["start_min"], s["end_min"]))
        midnight = datetime.combine(today, time(0), tz)
        nxt = datetime.combine(today + timedelta(days=1), time(0), tz)
        best = int(nxt.timestamp() * 1000)
        for m in edges.get(today.weekday(), set()):
            t = int((midnight + timedelta(minutes=m)).timestamp() * 1000)
            if now_ms < t < best:
                best = t
        return best

    # ------------------------------------------------------------ 改

    def put(self, d: Any, *, by: str) -> dict[str, Any]:
        """整份换:``{"enabled": bool, "slots": [{zone, day, start, end}]}``。"""
        if not isinstance(d, dict):
            raise ScheduleError("要一个对象")
        enabled = d.get("enabled")
        if not isinstance(enabled, bool):
            raise ScheduleError("enabled 要是 true/false")
        raw = d.get("slots")
        if not isinstance(raw, list):
            raise ScheduleError("slots 要是一个列表")
        if len(raw) > MAX_SLOTS:
            raise ScheduleError(f"时间段最多 {MAX_SLOTS} 段")
        rows: list[tuple[str, int, int, int]] = []
        for s in raw:
            if not isinstance(s, dict):
                raise ScheduleError("每一段要是一个对象")
            zone, day = s.get("zone", ""), s.get("day")
            if not isinstance(zone, str) or not _ZONE.fullmatch(zone):
                raise ScheduleError(f"防区名不合规矩: {zone!r}")
            if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6:
                raise ScheduleError("day 要是 0(星期一)到 6(星期日)")
            start, end = _min(s.get("start"), "开始"), _min(s.get("end"), "结束")
            if start >= 1440:
                raise ScheduleError("开始最晚 23:59")
            if start >= end:
                raise ScheduleError(f"开始要早于结束:{_hhmm(start)}–{_hhmm(end)}")
            rows.append((zone, day, start, end))
        rows.sort()
        for a, b in zip(rows, rows[1:], strict=False):
            if a[:2] == b[:2] and b[2] < a[3]:
                where = a[0] or "全部防区"
                raise ScheduleError(f"{where} 星期{'一二三四五六日'[a[1]]}的两段重叠了:"
                                    f"{_hhmm(a[2])}–{_hhmm(a[3])} 和 {_hhmm(b[2])}–{_hhmm(b[3])}")
        # 开没开存在站点设置里(那张表的 SQL 只在 site_settings.py,数据访问层边界),跟时间段同一个事务
        from d1max_site.site_settings import SiteSettings
        with self.db.tx() as c:
            c.execute("DELETE FROM arming_slots")
            c.executemany("INSERT INTO arming_slots(zone, day, start_min, end_min) "
                          "VALUES (?,?,?,?)", rows)
            SiteSettings.put_in(c, _KEY, {"enabled": enabled, "by": by, "at": self._now()})
        return self.view()

    def add_exception(self, d: Any, *, by: str) -> int:
        if not isinstance(d, dict):
            raise ScheduleError("要一个对象")
        first, last = _day(d.get("first_day"), "从哪天"), _day(d.get("last_day"), "到哪天")
        if last < first:
            raise ScheduleError("到哪天不能早于从哪天")
        if (last - first).days + 1 > MAX_EXCEPTION_DAYS:
            raise ScheduleError(f"一个例外最长 {MAX_EXCEPTION_DAYS} 天")
        state = d.get("state")
        if state not in STATES:
            raise ScheduleError("例外要么全天布防(armed)、要么全天撤防(disarmed)")
        note = str(d.get("note") or "")[:200]
        with self.db.tx() as c:
            if c.execute("SELECT COUNT(*) FROM arming_exceptions").fetchone()[0] >= MAX_EXCEPTIONS:
                raise ScheduleError(f"例外最多 {MAX_EXCEPTIONS} 条,先删掉过去的")
            return c.execute(
                "INSERT INTO arming_exceptions(first_day, last_day, state, note, created_by, "
                "created_ms) VALUES (?,?,?,?,?,?)",
                (first.isoformat(), last.isoformat(), state, note, by, self._now())).lastrowid

    def remove_exception(self, eid: int) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM arming_exceptions WHERE id=?", (eid,)).rowcount > 0
