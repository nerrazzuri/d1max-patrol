"""排程。**不用 cron。**

cron 表达不了「错过了怎么办」,而错过是常态 —— 上一轮超时、正在换电池、
人还没到。所以这里的每一条排程都必须自己回答两个 cron 答不了的问题:
迟到多久就不跑了(``window_min``),以及超了之后怎么办(``on_missed``)。

**时区跟着任务包走,绝不读系统时区**(§3.3 第 1 条)。刷机、OTA、换主板
都可能把系统时区打回 UTC,然后整晚的巡检时间全错 —— 而且没有任何一条
告警会响,狗会一脸认真地在错误的时间巡逻。

这个模块**不碰盘、不看钟、不发网络**。当前时刻一律由调用方传进来
(§8.5:时间必须可注入)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 星期的写法。**顺序有意义** —— 索引就是 ``datetime.weekday()``。
DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: 迟到之后怎么办。三个值的区别是「人的动作有什么不同」。
ON_MISSED: frozenset[str] = frozenset({"skip", "run_late", "alarm"})

#: 迟到窗口的上界:一整天。比这还长等于「永远不算迟到」。
MAX_WINDOW_MIN = 24 * 60

_AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleError(ValueError):
    """排程写得不对。**写包的人当场就该看见,不许兜底。**"""


def _是整数(v: Any) -> bool:
    """``True`` 不算整数。

    ``isinstance(True, int)`` 是真的 —— 用 isinstance 判的话 YAML 里写
    ``window_min: true`` 会被当成 1 分钟静静收下。
    """
    return type(v) is int


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """一条排程。"""

    id: str
    mission: str
    at_h: int
    at_m: int
    days: frozenset[str]
    window_min: int
    on_missed: str
    priority: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mission": self.mission,
            "at": f"{self.at_h:02d}:{self.at_m:02d}",
            # 按 DAYS 的顺序出,不按 set 的迭代顺序 —— 后者每次运行都可能不同,
            # 那会让「同一份包在两台狗上的接口输出不一样」。
            "days": [d for d in DAYS if d in self.days],
            "window_min": self.window_min,
            "on_missed": self.on_missed,
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class Schedule:
    """一份排程:一个时区,加若干条。"""

    timezone: str
    entries: tuple[ScheduleEntry, ...] = ()

    def tz(self) -> ZoneInfo:
        """这份排程的时区。**解析时已经验过一次,这里不会炸。**"""
        return ZoneInfo(self.timezone)

    def to_wire(self) -> dict[str, Any]:
        return {"timezone": self.timezone,
                "entries": [e.to_wire() for e in self.entries]}


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ScheduleError(msg)


def _parse_entry(raw: Any, index: int) -> ScheduleEntry:
    where = f"entries[{index}]"
    _require(isinstance(raw, dict), f"{where} 应为映射,实际为 {raw!r}")

    ident = raw.get("id")
    _require(isinstance(ident, str) and bool(ident.strip()), f"{where} 缺少 id")
    mission = raw.get("mission")
    _require(isinstance(mission, str) and bool(mission.strip()),
             f"{where}(id={ident}) 缺少 mission")

    at = raw.get("at")
    m = _AT_RE.match(at) if isinstance(at, str) else None
    _require(m is not None,
             f"{where}(id={ident}) 的 at 要写成 HH:MM 的 24 小时制,实际为 {at!r}")
    assert m is not None

    days_raw = raw.get("days")
    _require(isinstance(days_raw, list) and bool(days_raw),
             f"{where}(id={ident}) 的 days 要是个非空列表,实际为 {days_raw!r}")
    assert isinstance(days_raw, list)
    for d in days_raw:
        _require(d in DAYS,
                 f"{where}(id={ident}) 的 days 里有认不出来的 {d!r},"
                 f"只认 {'、'.join(DAYS)}")

    window = raw.get("window_min")
    _require(_是整数(window) and 1 <= window <= MAX_WINDOW_MIN,
             f"{where}(id={ident}) 的 window_min 要是 1..{MAX_WINDOW_MIN} 的整数,"
             f"实际为 {window!r} —— 每条排程都必须回答「迟到多久就不跑了」")

    on_missed = raw.get("on_missed")
    _require(on_missed in ON_MISSED,
             f"{where}(id={ident}) 的 on_missed 只认 "
             f"{'、'.join(sorted(ON_MISSED))},实际为 {on_missed!r}")

    priority = raw.get("priority", 0)
    _require(_是整数(priority),
             f"{where}(id={ident}) 的 priority 要是整数,实际为 {priority!r}")

    return ScheduleEntry(
        id=ident,                                   # type: ignore[arg-type]
        mission=mission,                            # type: ignore[arg-type]
        at_h=int(m.group(1)), at_m=int(m.group(2)),
        days=frozenset(days_raw),
        window_min=window,                          # type: ignore[arg-type]
        on_missed=on_missed,                        # type: ignore[arg-type]
        priority=priority,                          # type: ignore[arg-type]
    )


def parse_schedule(raw: Any) -> Schedule:
    """从已经解出来的映射构一份排程。"""
    _require(isinstance(raw, dict), f"schedule 应为映射,实际为 {raw!r}")

    tzname = raw.get("timezone")
    _require(isinstance(tzname, str) and bool(tzname.strip()),
             "schedule 缺少 timezone —— **绝不读系统时区**: 刷机、OTA、换主板"
             "都会把它打回 UTC,而整晚的巡检时间全错这件事没有任何告警会响")
    try:
        ZoneInfo(tzname)                            # type: ignore[arg-type]
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"timezone 认不出来: {tzname!r}") from exc

    entries_raw = raw.get("entries", [])
    _require(isinstance(entries_raw, list),
             f"schedule 的 entries 要是个列表,实际为 {entries_raw!r}")
    assert isinstance(entries_raw, list)
    entries = tuple(_parse_entry(e, i) for i, e in enumerate(entries_raw))

    seen: set[str] = set()
    for e in entries:
        _require(e.id not in seen,
                 f"排程 id 重复: {e.id} —— id 是「上次跑过」那份记录的键,"
                 f"重了的话后一条永远被前一条压住,而且不报错")
        seen.add(e.id)

    return Schedule(timezone=tzname, entries=entries)  # type: ignore[arg-type]


# ------------------------------------------------------------ 到点了没有


class DecisionKind(str, Enum):
    """一条排程此刻的处境。**这五个字符串是接口的一部分。**"""

    NOT_YET = "not_yet"
    DUE = "due"
    LATE = "late"
    SKIP = "skip"
    ALARM = "alarm"


@dataclass(frozen=True, slots=True)
class Decision:
    """对一条排程的决定。"""

    kind: DecisionKind
    #: 这个决定说的是哪一轮。``NOT_YET`` 且今天根本没有这一轮时是 ``None``。
    scheduled: datetime | None
    scheduled_ms: int | None
    #: 现在比那一轮的点晚了多少分钟。够不着任何一轮时是 0。
    late_min: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "scheduled": self.scheduled.isoformat() if self.scheduled else "",
            "late_min": self.late_min,
        }


_NOT_YET = Decision(DecisionKind.NOT_YET, None, None, 0)


def _occurrence(entry: ScheduleEntry, day: date, tz: Any) -> datetime | None:
    """``day`` 那天这条排程的那一刻。那天不在 ``days`` 里就回 ``None``。

    **查的是 ``day`` 自己的星期几**,不是「今天」的 —— 跨午夜的那一轮属于
    昨天(见 ``decide`` 的注释)。
    """
    if DAYS[day.weekday()] not in entry.days:
        return None
    return datetime(day.year, day.month, day.day,
                    entry.at_h, entry.at_m, tzinfo=tz)


def decide(entry: ScheduleEntry, *, now: datetime,
           last_started_ms: int | None) -> Decision:
    """这条排程此刻该不该跑。**纯函数:不读盘、不看钟、不发网络。**

    ``now`` 必须带时区,而且应当是用**包里那个时区**转过的(见模块开头)。
    裸 datetime 会被 ``timestamp()`` 按系统时区解释 —— 那正是 §3.3 第 1 条
    要堵死的事,而且它不报错,只是悄悄算错。

    **看两天,不是一天。** 23:30 那一轮配 60 分钟窗口,到了第二天 00:15 还
    在窗口里;只看今天那一轮的话,每一条跨午夜的排程都会在午夜整点被静静
    丢掉。两轮都够得着时取**晚的那一轮** —— 早的那一轮已经该放弃了,为它
    出发是拿昨天的理由干今天的活。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("decide() 的 now 必须带时区 —— 裸 datetime 会被按"
                         "系统时区解释,那正是排程绝不能碰的东西")

    today = now.date()
    # 只有这条排程自己的窗口会跨过午夜时,才去看昨天那一轮 —— 否则一条
    # 22:00 + 45 分钟窗口的排程,会在第二天一整天里都被昨天那个早就该
    # 放弃的窗口占着,答出 SKIP/LATE/ALARM 而不是 NOT_YET。
    跨午夜 = entry.at_h * 60 + entry.at_m + entry.window_min >= 24 * 60
    候选日 = (today, today - timedelta(days=1)) if 跨午夜 else (today,)
    候选 = [c for c in (_occurrence(entry, d, now.tzinfo) for d in 候选日)
           if c is not None and c <= now]
    if not 候选:
        return _NOT_YET
    这一轮 = max(候选)

    这一轮毫秒 = int(这一轮.timestamp() * 1000)
    if last_started_ms is not None and last_started_ms >= 这一轮毫秒:
        return _NOT_YET                       # 这一轮已经起跑过了

    迟了 = int((now - 这一轮).total_seconds() // 60)
    if 迟了 <= entry.window_min:
        kind = DecisionKind.DUE
    elif entry.on_missed == "run_late":
        kind = DecisionKind.LATE
    elif entry.on_missed == "alarm":
        kind = DecisionKind.ALARM
    else:
        kind = DecisionKind.SKIP
    return Decision(kind, 这一轮, 这一轮毫秒, 迟了)
