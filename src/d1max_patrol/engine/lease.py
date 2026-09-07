"""L1 软件控制权:**语义是租约,不是锁**(§3.5)。

**为什么不是锁。** 锁要显式释放,而手机会没电、会走出信号、会被系统杀掉。
一个不会自己过期的锁,第一个持有者一掉线就把狗锁死 —— 那正是厂商在清单
#46/#47 里犯的错(控制权一旦被拿走就交不回来),在我们自己这一层原样再挖一
遍。租约有 TTL,持有者靠心跳续租,断了自动到期。

**这个模块不看钟。** 每个方法的 ``now_ms`` 都是调用方传进来的墙上钟 UTC 毫
秒(§8.5 第 2 条:时间必须可注入,绝不 sleep)。它也不碰盘、不发网络、不认
识 HTTP —— 分层规矩是 ``engine/`` 绝不 import ``app/``,所以这里只有状态
机,会话和路由在 ``app/control.py``。

**持有者是一个 token 指纹,不是一个人。** 单机档没有可信的人身份来源
(§6.3),``operator`` 只是记账:狗记下来,但不验证。token 一失效租约就得跟
着释放(§6.4),那根线在 ``LeaseBook.keep_only``。

**跟排程的 ``running`` 是两件事。** ``engine/schedule.py`` 的 ``pick`` 管的
是"同一时刻只跑一条排程",对象是排程条目;租约管的是"谁能改变这只狗正在做
什么",对象是会话。两者正交:排程起跑不问租约(不然"没人持租约"就等于"不
巡检了",把一个权限设计变成一次可用性事故),持着租约也不能插队起跑第二条
排程(那由 ``pick`` 的 ``running`` 挡着)。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: 租约多久到期。§6.4 定的 30 秒。
LEASE_TTL_MS = 30_000

#: 持有者多久续一次。TTL 的三分之一 —— 丢两拍还有救,丢三拍才掉,跟守死人
#: 那一层(0.6 秒 / 0.2 秒)是同一个比例,现场排障时"丢了几拍"是同一句话。
LEASE_HEARTBEAT_MS = 10_000

#: 礼貌接管的宽限期:当前持有者收到提示之后有这么久可以同意或者继续干活,
#: **到点就移交**(§3.5 规则 3:同意或超时即移交)。
TAKEOVER_GRACE_MS = 15_000

#: 审计环留多少条。**够值守屏翻一页,不够当档案** —— 要长期存档是服务器的活。
AUDIT_MAX = 200

#: 审计里会出现的那几种事。**续租不在里头**:10 秒一拍的心跳会在半小时内把
#: 这个环冲干净,而它一条信息都不带。
AUDIT_KINDS: frozenset[str] = frozenset({
    "acquired", "released", "expired", "dropped",
    "takeover_asked", "taken_over", "forced",
})

__all__ = (
    "AUDIT_KINDS", "AUDIT_MAX", "LEASE_HEARTBEAT_MS", "LEASE_TTL_MS",
    "TAKEOVER_GRACE_MS", "AuditRecord", "Holder", "LeaseBook", "LeaseBusy",
    "LeaseError", "LeaseLost", "LeaseState",
)


class LeaseError(RuntimeError):
    """租约这一层的拒绝。翻成 HTTP 是调用方的事 —— 这里不认识 HTTP。"""


class LeaseBusy(LeaseError):
    """控制权在别人手上。"""


class LeaseLost(LeaseError):
    """你已经不是持有者了 —— 到期了,或者被接管了。"""


@dataclass(frozen=True, slots=True)
class Holder:
    """谁拿着。``ref`` 是 token 的指纹,**不是 token 本身** —— 审计要能翻,
    而翻档案的人不该顺手捡到一把能开狗的钥匙。
    """

    ref: str
    operator: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"ref": self.ref, "operator": self.operator}


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一条留痕(§3.5 规则 3:接管必须留痕)。"""

    seq: int
    at_ms: int
    kind: str
    ref: str
    operator: str
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"seq": self.seq, "at_ms": self.at_ms, "kind": self.kind,
                "ref": self.ref, "operator": self.operator,
                "detail": self.detail}


@dataclass(frozen=True, slots=True)
class LeaseState:
    """这一刻的租约。``ttl_ms``/``heartbeat_ms`` 跟着一起发出去 —— 客户端
    不该把心跳间隔硬编在自己那头。
    """

    holder: Holder | None = None
    expires_ms: int | None = None
    challenger: Holder | None = None
    grace_ends_ms: int | None = None
    ttl_ms: int = LEASE_TTL_MS
    heartbeat_ms: int = LEASE_HEARTBEAT_MS

    def to_wire(self) -> dict[str, Any]:
        return {
            "holder": self.holder.to_wire() if self.holder else None,
            "expires_ms": self.expires_ms,
            "challenger": (self.challenger.to_wire()
                           if self.challenger else None),
            "grace_ends_ms": self.grace_ends_ms,
            "ttl_ms": self.ttl_ms,
            "heartbeat_ms": self.heartbeat_ms,
        }


def _显示名(who: Holder) -> str:
    """审计和拒绝信息里怎么称呼他。名字是自己报的,没报就这么写。"""
    return who.operator or "(没报名字)"


class LeaseBook:
    """一只狗的 L1 控制权。**同一时刻只有一份**(§3.5)。

    线程安全:HTTP 是一请求一线程的,照 ``app/auth.py`` 的 ``TokenStore``
    上一把锁。用 ``RLock`` 是因为结算会在持锁时回头调 ``_grant``。
    """

    def __init__(self, *, ttl_ms: int = LEASE_TTL_MS,
                 heartbeat_ms: int = LEASE_HEARTBEAT_MS,
                 grace_ms: int = TAKEOVER_GRACE_MS,
                 audit_max: int = AUDIT_MAX) -> None:
        self._ttl = ttl_ms
        self._beat = heartbeat_ms
        self._grace = grace_ms
        self._audit_max = audit_max
        self._lock = threading.RLock()
        self._holder: Holder | None = None
        self._expires: int | None = None
        self._challenger: Holder | None = None
        self._grace_ends: int | None = None
        self._audit: list[AuditRecord] = []
        self._seq = 0

    # ---------------------------------------------------------------- 读

    def state(self, *, now_ms: int) -> LeaseState:
        """这一刻的租约。**读也会结算** —— "到期"这件事没有人会来通知我们,
        只能每次被问到的时候顺手把过去的时间算掉。
        """
        with self._lock:
            self._settle(now_ms)
            return self._snapshot()

    @property
    def audit(self) -> tuple[AuditRecord, ...]:
        with self._lock:
            return tuple(self._audit)

    def audit_since(self, seq: int) -> tuple[int, tuple[AuditRecord, ...]]:
        """``seq`` 之后的新记录,连同新游标。事件流靠它增量地推。"""
        with self._lock:
            fresh = tuple(r for r in self._audit if r.seq > seq)
            return (self._seq, fresh)

    # ---------------------------------------------------------------- 写

    def acquire(self, ref: str, operator: str = "", *,
                now_ms: int) -> LeaseState:
        """拿控制权。别人正拿着就抛 :class:`LeaseBusy`。

        自己已经拿着时是**幂等**的:app 断线重连之后不该因为"你已经拿着了"
        被挡回去 —— 它自己也不知道那 30 秒里到底掉没掉。
        """
        with self._lock:
            self._settle(now_ms)
            if self._holder is not None and self._holder.ref == ref:
                self._expires = now_ms + self._ttl
                return self._snapshot()
            if self._holder is not None:
                raise LeaseBusy(
                    f"控制权在 {_显示名(self._holder)} 手上,"
                    f"{self._剩余秒(now_ms)} 秒后到期")
            self._grant(Holder(ref, operator), now_ms, "acquired", "")
            return self._snapshot()

    def renew(self, ref: str, *, now_ms: int) -> LeaseState:
        """续租。**不取消正在走的接管请求** —— §3.5 规则 3 说的是"同意或超时
        即移交",让持有者靠不停心跳把接管拖死,等于把规则 3 废掉。
        """
        with self._lock:
            self._settle(now_ms)
            if self._holder is None or self._holder.ref != ref:
                raise LeaseLost("你已经不是持有者了 —— 租约到期或者被接管了")
            self._expires = now_ms + self._ttl
            return self._snapshot()

    def release(self, ref: str, *, now_ms: int) -> LeaseState:
        """交回。**幂等**:交回一份本来就不属于你的租约不是错误 —— app 退出
        时无脑发一次,不该因为租约刚好过期就收到一个错。
        """
        with self._lock:
            self._settle(now_ms)
            if self._holder is not None and self._holder.ref == ref:
                self._log(now_ms, "released", self._holder, "")
                self._clear()
            return self._snapshot()

    def keep_only(self, refs: Iterable[str], *, now_ms: int) -> LeaseState:
        """只留下这些 token 指纹的租约。**token 一失效,租约立即释放**(§6.4)。

        反过来不成立:一个 token 可以没有租约 —— 那就是只读观众,§3.6 那 3
        个名额里的另外两个正是干这个的。

        为什么是"把活着的交进来"而不是让 ``TokenStore`` 回调:回调要在
        ``TokenStore`` 的锁里回头去拿 ``LeaseBook`` 的锁,而这里又可能反过来
        问会话 —— 两把锁两个方向,那是死锁的写法。
        """
        live = frozenset(refs)
        with self._lock:
            self._settle(now_ms)
            if (self._challenger is not None
                    and self._challenger.ref not in live):
                gone = self._challenger
                self._challenger = None
                self._grace_ends = None
                self._log(now_ms, "dropped", gone, "请求接管的那个会话没了")
            if self._holder is not None and self._holder.ref not in live:
                gone = self._holder
                waiting = self._challenger
                self._clear()
                self._log(now_ms, "dropped", gone,
                          "token 失效了,租约跟着走(§6.4)")
                if waiting is not None:
                    self._grant(waiting, now_ms, "taken_over",
                                "原持有者的 token 失效了")
            return self._snapshot()

    def ask_takeover(self, ref: str, operator: str = "", *,
                     now_ms: int) -> LeaseState:
        """礼貌接管:当前持有者收到提示,**同意或超时即移交**(§3.5 规则 3)。

        没人持有时直接给他 —— 排队等一个空位没有任何意义。

        **威胁模型。** 单机档没有可信身份源(§6.3),``ref`` 只是 token 指纹、
        ``operator`` 只是自报家门,狗不验证谁在说谎。能触发这个方法的前提是
        先过了 token 校验那一关(``app/auth.py``)——所以这里假设的攻击者
        不是"局域网上随便一台设备"(§6.5 明令不许把局域网当可信区),而是
        "已经拿到一枚有效 token 的人",他能做的最坏的事就是排队请求接管,
        排队本身不动权限,真正移交要么等宽限期、要么持有者点头。
        """
        with self._lock:
            self._settle(now_ms)
            if self._holder is None:
                self._grant(Holder(ref, operator), now_ms, "acquired", "")
                return self._snapshot()
            if self._holder.ref == ref:
                raise LeaseError("控制权已经在你手上了")
            if self._challenger is not None and self._challenger.ref != ref:
                raise LeaseBusy(
                    f"{_显示名(self._challenger)} 已经在请求接管了,"
                    f"等他那一轮走完")
            who = Holder(ref, operator)
            self._challenger = who
            self._grace_ends = now_ms + self._grace
            self._log(now_ms, "takeover_asked", who,
                      f"要从 {_显示名(self._holder)} 手里接管")
            return self._snapshot()

    def approve(self, ref: str, *, now_ms: int) -> LeaseState:
        """当前持有者同意移交。立刻交,不等宽限期走完。

        **威胁模型。** 只有当前持有者的 ``ref`` 能触发移交——挑战者自己、
        第三方、甚至过期的旧持有者都不行(见下面的判断)。假设的攻击者是
        "手上有 token 但不是当前持有者的人",他不能靠调用这个方法伪装成
        持有者点头同意,只能老老实实走 ``force`` 那条留痕更重的路。
        """
        with self._lock:
            self._settle(now_ms)
            if self._challenger is None:
                raise LeaseError("没有人在请求接管")
            if self._holder is None or self._holder.ref != ref:
                raise LeaseLost("只有当前持有者能同意移交")
            detail = f"{_显示名(self._holder)} 同意移交"
            self._grant(self._challenger, now_ms, "taken_over", detail)
            return self._snapshot()

    def force(self, ref: str, operator: str = "", *, now_ms: int,
              reason: str) -> LeaseState:
        """强制接管。**必须写理由,理由进审计**(§3.5 规则 3)。

        这条必须存在:持有者可能已经不在了 —— 手机没电、人走了、网断了。
        没有它,一只狗会被一个已经不存在的会话占到 TTL 走完为止,而现场正等
        着有人把它从带电设备旁边挪开。**代价是它能把正在操作的人挤下去**,
        所以理由是硬性的,事后必须查得出是谁、为什么。

        **威胁模型:谁能强夺、凭什么、留什么痕。** 这是本模块里最危险的一个
        动作——它不问持有者同不同意,直接把控制权拿走。触发前提跟其它写方法
        一样是先过 token 校验(§6.5:局域网不是信任边界,一枚有效 token 才
        是),所以假设的攻击者是"已经拿到有效 token、但想绕开礼貌接管流程
        的人"。这一层不能也不该判断"理由是不是真的"——那需要人的判断,不是
        状态机的活。它能做、也必须做的是把代价钉死:理由不能是空字符串
        (``LeaseError``),且不管理由是什么都无条件写入审计
        (``ref``/``operator``/前任是谁/理由原文全部留痕,见 ``_log``),
        这样事后翻审计环一定能查到"是谁在什么时候把谁挤下去、说的什么理
        由"——留痕本身就是唯一的事前代价,而不是拦一道"验证"关卡去装出一种
        并不存在的可信度。
        """
        with self._lock:
            self._settle(now_ms)
            if not reason.strip():
                raise LeaseError("强制接管必须写明理由 —— 这一条会进审计")
            前任 = _显示名(self._holder) if self._holder else "(本来没人持有)"
            self._grant(Holder(ref, operator), now_ms, "forced",
                        f"从 {前任} 手里强制接管:{reason.strip()}")
            return self._snapshot()

    # -------------------------------------------------------------- 内部

    def _settle(self, now_ms: int) -> None:
        """把"时间过去了"这件事结算掉。**调用方已经拿着锁。**

        两条时间线要按顺序算:先看持有者过没过期,再看宽限期到没到。反过来
        算会出现"持有者已经过期了,却还在等他回应接管"这种荒唐状态。
        """
        if (self._holder is not None and self._expires is not None
                and now_ms >= self._expires):
            gone = self._holder
            waiting = self._challenger
            self._clear()
            self._log(now_ms, "expired", gone, "TTL 到了没人续租")
            if waiting is not None:
                # 持有者过期时正好有人在排队,直接给他,不让他再抢一次:
                # "抢"那一下之间谁都可能插进来,而他已经排过队了。
                self._grant(waiting, now_ms, "taken_over",
                            "原持有者的租约到期了")
                return
        if (self._challenger is not None and self._grace_ends is not None
                and now_ms >= self._grace_ends):
            self._grant(self._challenger, now_ms, "taken_over",
                        "宽限期到了,持有者没有回应")

    def _clear(self) -> None:
        self._holder = None
        self._expires = None
        self._challenger = None
        self._grace_ends = None

    def _grant(self, who: Holder, now_ms: int, kind: str,
               detail: str) -> None:
        self._holder = who
        self._expires = now_ms + self._ttl
        self._challenger = None
        self._grace_ends = None
        self._log(now_ms, kind, who, detail)

    def _log(self, at_ms: int, kind: str, who: Holder, detail: str) -> None:
        self._seq += 1
        self._audit.append(AuditRecord(self._seq, at_ms, kind, who.ref,
                                        who.operator, detail))
        if len(self._audit) > self._audit_max:
            del self._audit[:len(self._audit) - self._audit_max]

    def _snapshot(self) -> LeaseState:
        return LeaseState(self._holder, self._expires, self._challenger,
                           self._grace_ends, self._ttl, self._beat)

    def _剩余秒(self, now_ms: int) -> int:
        if self._expires is None:
            return 0
        return max(0, (self._expires - now_ms + 999) // 1000)
