"""告警的事实与三级判定(§5.2)。

**判据不是"有多严重",是"人的动作有什么不同"。** P1 要人立刻动身、P2 要人
今天之内处理、P3 只记录。两个 kind 如果会让人做同一件事,那它们就是同一
级 —— 不做 1-5 分制那种伪精细。

**这是纯状态机。** 不碰 IO、不认识 ``app/``,时间一律由调用方以 ``now_ms``
传入(§8.5 第 2 条:时间必须可注入,绝不 ``sleep``/``datetime.now()``)。
调用方是谁 —— 事件从哪来、什么时候升级提醒谁 —— 都不是这个模块的事;
那些是 ``app/alert_sources.py``(任务 6)和确认/升级状态机(任务 5)的事。

**聚合键是 ``robot/kind#seq``。** 同一只狗同一个类型、在 ``AGGREGATE_WINDOW_MS``
窗口内(从 ``last_ms`` 算起)是同一条告警,``count`` 累加、``last_ms`` 前移;
``robot`` 在键里,就是"P1 不跨狗合并"的实现 —— 两只狗同时卡住是两件事,要
跑两趟。窗口过期、被确认(``acked_ms``)、被解决(``resolved_ms``)三者任一
成立,下一次同 kind 的事件就另起一条(``#seq`` 递增)—— 确认过的告警不会
悄悄吸收后来的新事件,那会让人以为一件已经处理过的事还是老样子。
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
    """一条告警的事实。**同一只狗同一个 kind 在聚合窗口内是同一条**(键见
    ``AlertBook`` 的 ``_key``),``count``/``first_ms``/``last_ms`` 记的是这条
    告警被同一根因重复触发的轨迹。

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


#: 聚合窗口(毫秒):同一只狗同一个 ``kind`` 在这个窗口内的事件才合并成一
#: 条,窗口从**最后一次触发**(``last_ms``)算起,不是从第一次。这个数是
#: 拍的 —— 先定 15 分钟,跟 §5.7 那个 2 米一样没有实测依据,真机清单里要
#: 有一条量它、按现场手感调。
AGGREGATE_WINDOW_MS = 15 * 60_000


def _key(robot: str, kind: str, seq: int) -> str:
    """聚合键:``robot/kind#seq``。``robot`` 在键里就是"P1 不跨狗合并"的
    实现;``seq`` 是同一个 ``robot/kind`` 下第几条(§5.4 聚合窗口引入后,
    同一个 ``robot/kind`` 可能同时存在好几条已经互不吸收的告警,单靠
    ``robot/kind`` 已经不够做键了)。"""
    return f"{robot}/{kind}#{seq}"


class AlertBook:
    """所有告警的事实簿。内部一个 ``key -> Alert`` 的字典,没有别的状态,
    外加一个 ``robot/kind -> key`` 的指向表,记着"当前还在吸收新事件的
    是哪一条"。"""

    def __init__(self, *, window_ms: int = AGGREGATE_WINDOW_MS) -> None:
        self._window_ms = window_ms
        self._by_key: dict[str, Alert] = {}
        #: ``robot/kind`` -> 当前正在吸收新事件的那条告警的 ``key``。
        #: 窗口过期、被确认、被解决,这个指向就断开,下次 ``raise_alert``
        #: 另起一条并换一个新指向,不覆盖旧的。
        self._active: dict[str, str] = {}
        #: 每个 ``robot/kind`` 已经发出过的序号计数,只增不减,保证
        #: ``#seq`` 不会跟已经存在过的(哪怕已解决)撞上。
        self._seq: dict[str, int] = {}

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

        group = f"{robot}/{kind}"
        active_key = self._active.get(group)
        existing = self._by_key.get(active_key) if active_key is not None else None
        # 三条任一成立,当前这条就不再吸收新事件,下面另起一条:
        # 窗口过期(从 last_ms 算起)、已经被确认、已经被解决。
        absorbs = (
            existing is not None
            and existing.resolved_ms is None
            and existing.acked_ms is None
            and now_ms - existing.last_ms <= self._window_ms
        )
        if absorbs:
            alert = replace(
                existing,
                title=title,
                detail=detail,
                last_ms=now_ms,
                count=existing.count + 1,
            )
        else:
            seq = self._seq.get(group, 0) + 1
            self._seq[group] = seq
            alert = Alert(
                key=_key(robot, kind, seq),
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
            self._active[group] = alert.key
        self._by_key[alert.key] = alert
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
