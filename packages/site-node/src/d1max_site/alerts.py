"""告警的事实与三级判定(§5.2)。W00c5a 从狗上(robot-agent 的 ``engine/``)整个搬到站点:
总设计 §5 归站点,决策 8「信息都在服务器上」。判级表、聚合、升级规则原样;持久化从狗上的
jsonl 换成站点库(``sink`` 写穿、``restore`` 读回,见 ``d1max_site.alert_store``)。

**判据不是"有多严重",是"人的动作有什么不同"。** P1 要人立刻动身、P2 要人
今天之内处理、P3 只记录。两个 kind 如果会让人做同一件事,那它们就是同一
级 —— 不做 1-5 分制那种伪精细。

**这是纯状态机。** 不碰 IO、不认识 ``app/``,时间一律由调用方以 ``now_ms``
传入(§8.5 第 2 条:时间必须可注入,绝不 ``sleep``/``datetime.now()``)。
调用方是谁 —— 事件从哪来、什么时候升级提醒谁 —— 都不是这个模块的事;
那些是 ``d1max_site.alert_sources``(站点从 MQTT 的事实里判)与 ``alert_store`` 的事。

**聚合键是 ``robot/kind``。** 这是 §5.4 聚合的全部机制:同一只狗同一个类型
就是同一条告警,``count`` 累加、``last_ms`` 前移;``robot`` 在键里,就是
"P1 不跨狗合并"的实现 —— 两只狗同时卡住是两件事,要跑两趟。任务 4 会在这
个键上加聚合窗口(``AGGREGATE_WINDOW_MS``),把键变成
``f"{robot}/{kind}#{seq}"``;这里先不做窗口。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


class Level(str, Enum):
    """告警级别 —— 按"人要做什么"分三档,不是按严重程度打分。"""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Channel(str, Enum):
    """升级(§5.3)走的通道,由弱到强。P1 没人确认,超时就换更吵的通道
    —— 不是重复提醒,是换一种方式提醒。"""

    SCREEN = "screen"
    PUSH = "push"
    SOUND = "sound"


class AlertNotFound(KeyError):
    """``ack``/``resolve`` 传入一个不存在(或已经不在)的 ``key``。**继承
    ``KeyError``** 以免打破任何已有的"捕获 KeyError"假设,但用一个独立的
    类型跟"kind 没注册"(``raise_alert`` 抛的裸 ``KeyError``)区分开 ——
    前者是前端点了一条已经不在的告警(正常的用户误操作,该映射成 404),
    后者是代码里写了个没登记的 kind(我们自己写错了,该映射成 500)。裸
    ``KeyError`` 分不出这两件事,调用方(任务 9 的路由层)只能全按一种处
    理。"""


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
    # 不是因为没电的整趟中止(关节过温、导航反复失败……)。**跟
    # ``battery_abort`` 分成两个 kind,不合并**:两条告警要让人做的事不一
    # 样 —— 没电是"去把狗抱回来充电",别的中止是"去看它到底怎么了",而
    # §5.2 判级的口径就是"人得做什么"。合成一个,人看到的标题会把他往错
    # 的方向支;而"认不出原因就不报"更坏:没人在的时候中止,狗就在原地站
    # 到天亮,一声不响。
    "run_abort": Level.P1,
    #: 狗上归档写不进去(W00c6a):那一趟的照片、记录没存下,证据缺了。
    "archive_failed": Level.P1,
    "fallen": Level.P1,
    "loc_lost_paused": Level.P1,
    "estop_pressed": Level.P1,
    # lease_expired、suspend_stale、watchdog_died 三类原来由狗上老服务报;W00c5e 之后没有来源
    # (遥控租约在站点上、接管不挂起),种类留着是为了库里的老记录读得懂。
    "lease_expired": Level.P1,
    # 人点了"让开腿"接管,接管完忘了还回来 —— 这一趟从此永远挂着,而屏幕上
    # 一切如常(挂账 67a)。**处置是升 P1,不是自动 resume**:一只狗在"最后
    # 已知状态是人正在接管"的情况下自己动起来,是这套系统里最不该发生的事
    # (§5.8 同理)。判定原来在老服务里;W00c5e 之后接管直接结束这一趟、不挂起,这一类没有来源了。
    "suspend_stale": Level.P1,
    # 值守那条闸门协程自己死了(``server._StateHub._lease_watchdog``)。
    # **这一条报的是"报警器坏了"**,所以它是 P1 而不是 P2:从它死的那一刻
    # 起,租约到期没人处置 —— 人揣着手机走了狗也不会停,而屏幕上一切如常。
    # 它跟别的 P1 不一样的地方在于:能做的事只有"重启服务",不是去现场。
    "watchdog_died": Level.P1,
    # 排程执行器那条协程死了(W06):从此到点没人起跑。**跟上面那条分开登记**,
    # 簿子按 robot/kind 聚合,同一个 kind 的话两条协程各死一次会合成一条、后死
    # 的把先死的诊断盖掉。处置同样只有"重启服务"。
    "schedule_died": Level.P1,
    # 狗掉线(W00c5a,站点才看得见这件事)。**跑着任务时掉线是 P1**:狗可能停在半路、也可能按断线
    # 策略还在自己走,人得去看;空闲时掉线是 P2:今天之内查网络就行。两件事人要做的不一样,分两个 kind。
    "robot_offline": Level.P1,
    # P2:今天之内处理。不影响这一趟巡检能不能跑完,但拖久了会变成 P1
    # (盘满了继续拖,就会变成没法记录;上传积压继续拖,数据就旧到没用)。
    "finding": Level.P2,
    "disk_80": Level.P2,
    # 站点自己的备份超过 25 小时没成(W00c5d):站点是唯一权威,盘坏了就什么都没了。
    "backup_stale": Level.P2,
    # 站点下发的图没装上、在狗上重建没成(W00c5d 第二部分):狗照旧用原来那张,要人看原因。
    "map_failed": Level.P2,
    # 站点永远不收狗传来的某个文件(名字不合规、不是狗能产生的):那一趟留在狗上,要人看(W00c5d)。
    "upload_refused": Level.P2,
    # 站点下发的版本没装上、切不过去、退不回去(W00c5d 第三部分):狗照旧跑原来那一版,要人看。
    "release_failed": Level.P2,
    "upload_backlog": Level.P2,
    "bundle_lag": Level.P2,
    "clock_skew": Level.P2,
    "robot_offline_idle": Level.P2,
    # P3:只记录。日常的正常事件,不需要谁去处理什么。
    "run_done": Level.P3,
    "run_start": Level.P3,
    "battery_swap": Level.P3,
}


#: P1 未确认的升级时限(毫秒),从 ``first_ms`` 算起 —— 一条持续刷新的告警
#: 用 ``last_ms`` 算就永远升不上去,而"一直卡着没人管"正是最该把声音打开
#: 的那种。第 0 档满了转 app 推送、第 1 档满了再加声音。这两个数是拍的,
#: 跟聚合窗口一样没有实测依据 —— 先定 2 分钟/5 分钟,真机清单里要量、按
#: 现场手感调。只有 P1 会升级,P2/P3 不在这条链路里(§5.2:两级如果会导致
#: 人做同样的事,那它们就是同一级 —— 升级 P2 就是把"今天之内"变成"立刻",
#: 那它本来就该是 P1)。
ESCALATE_AFTER_MS: tuple[int, ...] = (2 * 60_000, 5 * 60_000)

#: 升级档位 -> 通道,下标就是 ``Alert.escalated`` 的值。
_CHANNEL_BY_TIER: tuple[Channel, ...] = (Channel.SCREEN, Channel.PUSH, Channel.SOUND)


@dataclass(frozen=True, slots=True)
class Alert:
    """一条告警的事实。**同一只狗同一个 kind 在聚合窗口内是同一条**(键见
    ``AlertBook`` 的 ``_key``),``count``/``first_ms``/``last_ms`` 记的是这条
    告警被同一根因重复触发的轨迹。

    ``acked_*`` 和 ``resolved_*`` 是两组独立字段,不是同一个状态机上的两
    档:"我看见了在处理"(ack)和"这事没了"(resolve)是两回事(§5.3)。修一
    个问题合理地可以花一小时,但"没人看见"才是真正的失败 —— 升级只看有没
    有人确认,不看有没有解决,所以 ``resolve`` 不会让 ``escalated`` 停下来。

    ``escalated`` 记这条告警已经升到第几档通道(``0`` = 只在大屏上,见
    ``channel``),由 ``AlertBook.due_escalations`` 前移,不会倒退。
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
    resolved_by: str
    resolved_ms: int | None
    escalated: int

    @property
    def channel(self) -> Channel:
        """当前应该用哪个通道播这条告警 —— 由 ``escalated`` 直接算出,不是
        另存一份可能跟 ``escalated`` 对不上的状态。"""
        return _CHANNEL_BY_TIER[self.escalated]

    def to_wire(self) -> dict[str, Any]:
        """给前端/接口用的纯 ASCII 键字典。

        ``escalated`` 和 ``channel`` 两个都上线,不是只上前一个(裁决十一)。
        光给 ``escalated`` 这个整数,等于让每个客户端自己再算一遍
        ``escalated -> Channel`` 那张表 —— 同一份判据落在两处,早晚分叉,而
        分叉的那天正好是该出声的那次没出声。``channel`` 本来就是这个类上的
        property,由 ``escalated`` 直接算出,白送。
        """
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
            "resolved_by": self.resolved_by,
            "resolved_ms": self.resolved_ms,
            "escalated": self.escalated,
            "channel": self.channel.value,
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

    def __init__(
        self,
        *,
        window_ms: int = AGGREGATE_WINDOW_MS,
        sink: Callable[[Alert], None] | None = None,
        keep_closed: int = 200,
    ) -> None:
        self._window_ms = window_ms
        self._by_key: dict[str, Alert] = {}
        #: ``robot/kind`` -> 当前正在吸收新事件的那条告警的 ``key``。
        #: 窗口过期、被确认、被解决,这个指向就断开,下次 ``raise_alert``
        #: 另起一条并换一个新指向,不覆盖旧的。
        self._active: dict[str, str] = {}
        #: 每个 ``robot/kind`` 已经发出过的序号计数,只增不减,保证
        #: ``#seq`` 不会跟已经存在过的(哪怕已解决)撞上。
        self._seq: dict[str, int] = {}
        #: 每一次变化(新起、吸收、确认、解决、升级)都交给它一份最新的 ``Alert``。站点把它
        #: 接到库上(写穿)。``None`` 时只在内存里,也不修剪 —— 那时内存是唯一的一份。
        self._sink = sink
        #: 修剪时内存里保留多少条已解决的(挂账 75)。剪掉的库里一条不少。
        self._keep_closed = keep_closed

    def restore(self, alerts: Iterable[Alert], *, max_seq: dict[str, int] | None = None) -> None:
        """站点重启:把库里的告警读回来(调用方只给未解决的与最近的,不是整张历史表)。未解决、未确认
        的那条重新当作「正在吸收」的那条;序号从每个 ``robot/kind`` 见过的最大号往后走(``max_seq``
        由调用方从整张表的键里算,读回的只是其中一部分),不撞号。只在开张时调一次。"""
        for group, seq in (max_seq or {}).items():
            self._seq[group] = max(self._seq.get(group, 0), seq)
        for a in sorted(alerts, key=lambda a: (a.first_ms, a.key)):
            self._by_key[a.key] = a
            group = f"{a.robot}/{a.kind}"
            seq = int(a.key.rsplit("#", 1)[1]) if "#" in a.key else 0
            self._seq[group] = max(self._seq.get(group, 0), seq)
            if a.resolved_ms is None and a.acked_ms is None:
                cur = self._by_key.get(self._active.get(group, ""))
                if cur is None or cur.last_ms <= a.last_ms:
                    self._active[group] = a.key

    def _spill(self, alert: Alert) -> None:
        """**先写出去,再改内存**(每个调用点都是这个顺序):写库失败抛出来的时候内存还没动 ——
        不然接口回了 500,内存里却已经算确认、升级停了,库里没记,重启后又回到没确认。"""
        if self._sink is not None:
            self._sink(alert)

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
        seq = None
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
                resolved_by="",
                resolved_ms=None,
                escalated=0,
            )
        self._spill(alert)
        if seq is not None:
            self._seq[group] = seq
            self._active[group] = alert.key
        self._by_key[alert.key] = alert
        return alert

    def ack(self, key: str, *, who: str, now_ms: int) -> Alert:
        """有人确认在处理了。只改 ``acked_by``/``acked_ms``,不碰 ``resolved_ms``。

        ``who`` 必须是非空姓名 —— 空姓名的确认等于没人负责,升级(任务 5)
        就失去了"停下来"的依据,所以直接抛 ``ValueError``。
        """
        if not who:
            raise ValueError("ack 需要非空姓名,空姓名等于没人确认")
        existing = self._by_key.get(key)
        if existing is None:
            raise AlertNotFound(key)
        alert = replace(existing, acked_by=who, acked_ms=now_ms)
        self._spill(alert)
        self._by_key[key] = alert
        return alert

    def resolve(self, key: str, *, who: str = "", now_ms: int) -> Alert:
        """这件事没了。只改 ``resolved_by``/``resolved_ms``,**不碰
        ``acked_by``/``acked_ms``** —— 解决了不等于确认过,"没人看见"是这个
        模块要暴露出来的事实,不是要替调用方悄悄圆过去的细节。

        ``who`` **可以是空的**,这一点跟 ``ack`` 不一样:空姓名的确认会把升级
        链关掉(所以那边是 ValueError),而空姓名的解决只是交接班那张表上少一
        个名字 —— 拿它去挡住"这件事没了"这个事实,代价大得多。
        """
        existing = self._by_key.get(key)
        if existing is None:
            raise AlertNotFound(key)
        alert = replace(existing, resolved_by=who, resolved_ms=now_ms)
        self._spill(alert)
        self._by_key[key] = alert
        return alert

    def due_escalations(self, *, now_ms: int) -> tuple[tuple[Alert, Channel], ...]:
        """**这个方法有副作用**:凡是被这次判定为"该升一档"的告警,内部记录
        的 ``escalated`` 会被立即前移到新的档位 —— 同一档不会被再问出来一
        次(§5.3:升级是换通道,不是重复提醒,否则声音会一直响)。

        只看 P1、只看未确认的(``acked_ms is None``);``resolved_ms`` 不参
        与判断 —— 解决了但没人确认,照样升级,升级看的是有没有人确认。

        计时从 ``first_ms`` 算起,不是 ``last_ms``:一条持续刷新的告警用
        ``last_ms`` 算就永远升不上去,而"一直卡着没人管"正是最该把声音打
        开的那种。
        """
        due: list[tuple[Alert, Channel]] = []
        for alert in self._by_key.values():
            if alert.level is not Level.P1 or alert.acked_ms is not None:
                continue
            elapsed = now_ms - alert.first_ms
            tier = 0
            for i, threshold in enumerate(ESCALATE_AFTER_MS):
                if elapsed > threshold:
                    tier = i + 1
            if tier > alert.escalated:
                updated = replace(alert, escalated=tier)
                self._spill(updated)
                self._by_key[alert.key] = updated
                due.append((updated, _CHANNEL_BY_TIER[tier]))
        return tuple(due)

    def open(self) -> tuple[Alert, ...]:
        """未解决的告警,按级别再按 ``last_ms`` 倒序 —— P1 永远在最上面。"""
        opened = [a for a in self._by_key.values() if a.resolved_ms is None]
        return tuple(sorted(opened, key=lambda a: (a.level.value, -a.last_ms)))

    def all(self) -> tuple[Alert, ...]:
        """所有告警,已解决的也在内,不排序。"""
        return tuple(self._by_key.values())

    def trim(self) -> int:
        """把内存里多余的已解决告警放掉。返回清掉的条数。

        **挂账 75 的解。** 告警存在站点库里(``sink``),修剪不是丢数据 —— 库里一条不少,
        只是不再在内存里拿着。

        **没有 sink 就不剪。** 那种情况下内存是唯一的一份,剪了就真丢了。

        ``_seq`` 不跟着剪:它记的是"这个 robot/kind 发过几号",剪掉内存里的
        条目之后下一条的号还得往后走 —— 撞号会让服务器把两条不同的告警
        当成同一条。它每个 ``robot/kind`` 只占一个整数,不是挂账 75 说的那种增长。
        """
        if self._sink is None:
            return 0
        closed = [a for a in self._by_key.values() if a.resolved_ms is not None]
        if len(closed) <= self._keep_closed:
            return 0
        closed.sort(key=lambda a: (a.resolved_ms or 0, a.key))
        drop = closed[: len(closed) - self._keep_closed]
        for alert in drop:
            del self._by_key[alert.key]
        return len(drop)
