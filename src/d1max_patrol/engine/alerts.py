"""告警的事实与三级判定(§5.2)。

**判据不是"有多严重",是"人的动作有什么不同"。** P1 要人立刻动身、P2 要人
今天之内处理、P3 只记录。两个 kind 如果会让人做同一件事,那它们就是同一
级 —— 不做 1-5 分制那种伪精细。

**这是纯状态机。** 不碰 IO、不认识 ``app/``,时间一律由调用方以 ``now_ms``
传入(§8.5 第 2 条:时间必须可注入,绝不 ``sleep``/``datetime.now()``)。
调用方是谁 —— 事件从哪来、什么时候升级提醒谁 —— 都不是这个模块的事;
那些是 ``app/alert_sources.py``(任务 6)和确认/升级状态机(任务 5)的事。

**聚合键是 ``robot/kind``。** 这是 §5.4 聚合的全部机制:同一只狗同一个类型
就是同一条告警,``count`` 累加、``last_ms`` 前移;``robot`` 在键里,就是
"P1 不跨狗合并"的实现 —— 两只狗同时卡住是两件事,要跑两趟。任务 4 会在这
个键上加聚合窗口(``AGGREGATE_WINDOW_MS``),把键变成
``f"{robot}/{kind}#{seq}"``;这里先不做窗口。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


class Level(str, Enum):
    """告警级别 —— 按"人要做什么"分三档,不是按严重程度打分。"""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


#: kind 到级别的唯一对应表。**判级只许在这一处说** —— 散在各个调用点上,
#: 迟早会有两处说法不一样的那天,而不一样的那天正好是该响的那次没响。
#:
#: 任务 12 要新增 kind(``suspend_stale``)时,必须先在这里登记一行,否则
#: ``raise_alert`` 会抛 ``KeyError`` —— 这是设计,不是缺陷。
LEVEL_OF: dict[str, Level] = {
    # P1:立刻动身。狗动不了、狗倒了、狗失控、控制权没了 —— 这些事拖到"今
    # 天之内"处理,狗可能已经在原地卡了一整天,或者没人管的情况下继续乱动。
    "stuck": Level.P1,
    "battery_abort": Level.P1,
    "fallen": Level.P1,
    "loc_lost_paused": Level.P1,
    "estop_pressed": Level.P1,
    "lease_expired": Level.P1,
    # P2:今天之内处理。不影响这一趟巡检能不能跑完,但拖久了会变成 P1
    # (盘满了继续拖,就会变成没法记录;上传积压继续拖,数据就旧到没用)。
    "finding": Level.P2,
    "disk_80": Level.P2,
    "upload_backlog": Level.P2,
    "bundle_lag": Level.P2,
    "clock_skew": Level.P2,
    # P3:只记录。日常的正常事件,不需要谁去处理什么。
    "run_done": Level.P3,
    "run_start": Level.P3,
    "battery_swap": Level.P3,
}


@dataclass(frozen=True, slots=True)
class Alert:
    """一条告警的事实。**同一只狗同一个 kind 是同一条**(键见 ``AlertBook``
    的 ``_key``),``count``/``first_ms``/``last_ms`` 记的是这条告警被同一根因
    重复触发的轨迹。

    ``acked_*`` 和 ``resolved_ms`` 是两个独立字段,不是同一个状态机上的两
    档:"我看见了在处理"(ack)和"这事没了"(resolve)是两回事(§5.3)。修一
    个问题合理地可以花一小时,但"没人看见"才是真正的失败 —— 升级(任务 5)
    只看有没有人确认,不看有没有解决。
    """

    key: str
    level: Level
    kind: str
    robot: str
    title: str
    detail: str
    first_ms: int
    last_ms: int
    count: int
    acked_by: str
    acked_ms: int | None
    resolved_ms: int | None

    def to_wire(self) -> dict[str, Any]:
        """给前端/接口用的纯 ASCII 键字典。"""
        return {
            "key": self.key,
            "level": self.level.value,
            "kind": self.kind,
            "robot": self.robot,
            "title": self.title,
            "detail": self.detail,
            "first_ms": self.first_ms,
            "last_ms": self.last_ms,
            "count": self.count,
            "acked_by": self.acked_by,
            "acked_ms": self.acked_ms,
            "resolved_ms": self.resolved_ms,
        }


def _key(robot: str, kind: str) -> str:
    """聚合键:``robot/kind``。``robot`` 在键里就是"P1 不跨狗合并"的实现。"""
    return f"{robot}/{kind}"


class AlertBook:
    """所有告警的事实簿。内部一个 ``key -> Alert`` 的字典,没有别的状态。"""

    def __init__(self) -> None:
        self._by_key: dict[str, Alert] = {}

    def raise_alert(
        self,
        *,
        kind: str,
        robot: str,
        title: str,
        detail: str = "",
        now_ms: int,
        level: Level | None = None,
    ) -> Alert:
        """记一条告警。

        同一只狗同一个 ``kind`` 已经有未解决的告警时,合成同一条:
        ``count`` 加一、``last_ms`` 前移到 ``now_ms``,``first_ms`` 不动。

        ``level`` 不传就查 ``LEVEL_OF``;传了就必须跟表一致 —— 不一致抛
        ``ValueError``,不许"这一次先这样"。没登记过的 ``kind`` 抛
        ``KeyError``,不许猜一个级别顶上。
        """
        table_level = LEVEL_OF[kind]
        if level is None:
            level = table_level
        elif level is not table_level:
            raise ValueError(
                f"kind={kind!r} 在 LEVEL_OF 里登记的是 {table_level.value},"
                f"跟传入的 level={level.value} 不一致"
            )

        key = _key(robot, kind)
        existing = self._by_key.get(key)
        if existing is not None and existing.resolved_ms is None:
            alert = replace(
                existing,
                title=title,
                detail=detail,
                last_ms=now_ms,
                count=existing.count + 1,
            )
        else:
            alert = Alert(
                key=key,
                level=level,
                kind=kind,
                robot=robot,
                title=title,
                detail=detail,
                first_ms=now_ms,
                last_ms=now_ms,
                count=1,
                acked_by="",
                acked_ms=None,
                resolved_ms=None,
            )
        self._by_key[key] = alert
        return alert

    def ack(self, key: str, *, who: str, now_ms: int) -> Alert:
        """有人确认在处理了。只改 ``acked_by``/``acked_ms``,不碰 ``resolved_ms``。"""
        alert = replace(self._by_key[key], acked_by=who, acked_ms=now_ms)
        self._by_key[key] = alert
        return alert

    def resolve(self, key: str, *, now_ms: int) -> Alert:
        """这件事没了。只改 ``resolved_ms``,不碰 ``acked_by``/``acked_ms`` ——
        解决了不等于确认过,"没人看见"是这个模块要暴露出来的事实,不是要
        替调用方悄悄圆过去的细节。"""
        alert = replace(self._by_key[key], resolved_ms=now_ms)
        self._by_key[key] = alert
        return alert

    def open(self) -> tuple[Alert, ...]:
        """未解决的告警,按级别再按 ``last_ms`` 倒序 —— P1 永远在最上面。"""
        opened = [a for a in self._by_key.values() if a.resolved_ms is None]
        return tuple(sorted(opened, key=lambda a: (a.level.value, -a.last_ms)))

    def all(self) -> tuple[Alert, ...]:
        """所有告警,已解决的也在内,不排序。"""
        return tuple(self._by_key.values())
