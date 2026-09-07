"""L1 控制权接到会话上: 租约绑 token, 留痕上事件流。

``engine/lease.py`` 是纯状态机, 不认识 token、不认识 HTTP。这个模块是它跟
``app/auth.py`` 之间那根线, 只做三件事:

1. **token 一失效, 租约立即释放**(§6.4)。做法是每次结算都把"还活着的
   token 指纹"交给 ``LeaseBook.keep_only``。
2. **哪几条接口要控制权。** 判据是 §3.5 规则 4: "我能改变它正在做什么",
   不是"我能动它"。只读永远不要(规则 1), 急停永远不要(规则 2)。
3. **把审计增量地推给事件流。** 值守屏和第 8 卷的告警都从那条流上读。

**两个钟并存, 不许混。** 这个模块所有 ``now_ms`` 都是**墙上钟 UTC 毫秒**
(``AppContext.clock``), 由调用方读一次传进来 —— 不是单调钟。理由是租约的
每一条都要留痕给人看(§3.5 规则 3), ``expires_ms``/``AuditRecord.at_ms``
都要上线、要跟其它系统的时间戳对得上; 而 ``app/auth.py`` 那边判断 token 闲
不闲置用的是单调钟(``time.monotonic``, Task 4/5 已经这样交付), 这个模块
不去改它, 也不该改 —— 闲置期量的是"过了多久", 校时跳一下不该让一个还在
干活的人被判成掉线。

**代价写在这儿: 一次校时跳变会直接改变租约判定。** 现场如果 NTP 突然把墙
上钟往前拨(哪怕只拨几十秒), 一个正在作业、心跳发得很规律的人可能被
``sweep``/``require`` 静默判成租约过期 —— 日志里只会看到一条"到期"的审计,
看不出这是校时干的, 不是他真的断了。这不是本模块能解决的问题: 修法要么是
"狗上不许跑会自己跳变的 NTP(只允许平滑校时)", 要么是"``AppContext.clock``
本身就不是墙上钟", 两条都要等第 8 卷接进 ``server.py`` 时去确认狗上到底有
没有 NTP、``AppContext.clock`` 具体是什么钟。这里只能把代价钉在纸面上,
别让后面的人以为这两个钟是随手挑的。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from d1max_patrol.app.auth import CHANNEL_LOCAL, Denied, Guard, Session
from d1max_patrol.engine.lease import AuditRecord, LeaseBook, LeaseState

#: 要 L1 控制权的那几条接口。**判据是"会改变这只狗正在做什么"**(§3.5 规
#: 则 4), 不是"会不会让它动"。
#:
#: 不在这张表上的, 都是想清楚了才不在的:
#:
#: * ``POST /api/estop`` —— 规则 2, 急停永远不要控制权。
#: * 一切 GET —— 规则 1, 看永远不要控制权。
#: * ``PUT /api/missions/<id>``、``PUT /api/maps/<id>/home`` —— 编任务是
#:   案头活。常常是第二个人在改航点, 而第一个人在开狗; 要控制权只会让两个人
#:   为了改一个数去抢方向盘。
#: * 导出、备份、清盘 —— 都不改变这只狗正在做什么。
#: * 升级(``/api/release/*``) —— 尤其不能要: 一次恢复性的回滚不该被一个已经
#:   掉线的会话挡住。
#: * 任务包(``POST /api/bundle/apply``、``/api/bundle/rollback``) ——
#:   **这一条的理由要说准, 别读成"有闸挡着"**: 代码里并没有一道"跑着的时候
#:   不许切包"的闸(``server.py`` 的 ``_bundle_apply``、``engine/bundle.py``
#:   都没有)。它安全是因为**没有耦合**: ``bundles_root`` 跟正在跑的任务读的
#:   ``missions_dir``/``maps_dir``/``ctx.nav`` 之间没有写路径相交, 切包对在跑
#:   的那一趟是惰性的 —— 下一趟才生效。**这是一个前提, 不是一道防线。**
#:   哪天有人把 ``bundles_root`` 接进热加载, 这条排除就悄悄变错, 而且不会有
#:   任何测试变红(见 docs/第2卷待办.md 第 49 条)。
#: * ``POST /api/mapping/rebuild`` —— 拿录好的包离线重建, 狗本身没在动。
CONTROLLED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/api/teleop$")),
    ("POST", re.compile(r"^/api/teleop/heartbeat$")),
    ("POST", re.compile(r"^/api/mapping/record/(start|stop)$")),
    ("POST", re.compile(r"^/api/missions/[^/]+/run$")),
    ("POST", re.compile(r"^/api/run/(pause|resume|abort)$")),
    ("POST", re.compile(r"^/api/maps/load$")),
    ("POST", re.compile(r"^/api/pose/(initial|reset)$")),
)


def needs_lease(method: str, path: str) -> bool:
    """这条接口要不要 L1 控制权。"""
    return any(m == method and p.match(path) for m, p in CONTROLLED)


class ControlDesk:
    """把 ``LeaseBook`` 跟 ``Guard`` 拴在一起。

    自己不存状态(游标除外), 所有真状态都在 ``book`` 和 ``guard`` 里。
    """

    def __init__(self, guard: Guard, *, clock_ms: Callable[[], int],
                 book: LeaseBook | None = None) -> None:
        self._guard = guard
        self._clock_ms = clock_ms
        self.book = book if book is not None else LeaseBook()
        #: 已经推给事件流的最后一条留痕序号。
        self._cursor = 0

    def sweep(self, *, now_ms: int) -> LeaseState:
        """结算一次: 该过期的过期, token 没了的租约释放(§6.4)。"""
        return self.book.keep_only(self._guard.live_refs(), now_ms=now_ms)

    def drain(self) -> tuple[AuditRecord, ...]:
        """上次问过之后新出现的留痕。事件流靠它增量地推。"""
        self._cursor, fresh = self.book.audit_since(self._cursor)
        return fresh

    def snapshot(self, *, now_ms: int) -> dict[str, Any]:
        """``/api/state`` 里 ``control`` 那一段。

        **只放不会每拍都变的量。** ``_StateHub`` 靠"这份快照跟上一份一样就
        不发"来保持安静; 往里放一个"离上次用过多久"这种每拍都变的数, 这条
        SSE 就会每半秒响一次 —— 在热点上那是实打实的带宽, 而且会把真正的状态
        变化淹掉。会话明细去 ``GET /api/sessions`` 拿。

        ``expires_ms`` 会跟着心跳每 10 秒变一次, 那是有意的: 第 8 卷要靠它算
        "还剩多久到期"。10 秒一帧不吵。

        **``sessions`` 跟 ``max_sessions`` 是同一个分母。** ``Guard.sessions()``
        含本机(``CHANNEL_LOCAL``)会话, 而 §3.6 的 ``max_sessions`` 只算非
        本机通道(见 ``Guard._issue``)——两个数天生不是一回事。这里数的
        ``sessions`` 因此**只算非本机会话**, 跟 ``max_sessions`` 对得上;
        本机(SSH/控制台到场的人)不占这 3 个名额, 也不进这个计数。屏幕上
        绝不允许出现"8 条会话, 上限 3"这种数字对不上的假象。想看含本机的
        全量会话表(比如运维在本机排障), 走 ``GET /api/sessions``, 那里用
        的是 ``Guard.sessions()`` 的原始列表, 自己就说明白了含本机。
        """
        state = self.book.state(now_ms=now_ms)
        remote = tuple(s for s in self._guard.sessions()
                       if s.channel != CHANNEL_LOCAL)
        return {**state.to_wire(),
                "sessions": len(remote),
                "max_sessions": self._guard.max_sessions}

    def require(self, sess: Session | None, method: str, path: str, *,
                now_ms: int) -> None:
        """要控制权的接口就得有控制权。放行什么都不做, 不放行抛 ``Denied``。

        ``sess`` 是 ``None`` 时直接放行: 那是没设 PIN 的部署, 按定义只听本机
        (见 ``server.check_exposure``), 没有"谁是谁"这个问题, L1 仲裁没有
        对象。

        **只读会话(``sess.readonly``)一律挡在这儿, 不管它租约在不在手上。**
        正常路径下 ``Guard.gate`` 已经先把只读凭证挡在写请求之外(它是在狗
        的热点上用明文 PIN 换的, §6.5), 这条要控制权的接口根本轮不到这里;
        这里再判一次是**纵深防御**, 假设的攻击者/失误不是"绕过了 gate 的
        网络对手"(那超出了这一层能挡的范围), 而是"未来某条调用路径没有先
        经过 ``Guard.gate`` 就直接调了 ``require``"——那种失误不该让只读凭证
        意外拿到写权限。判据是"只读永远不能改变这只狗正在做什么", 跟规则 1
        是同一条道理, 只是换了一层来守。
        """
        if sess is None or not needs_lease(method, path):
            return
        if sess.readonly:
            raise Denied(403, "这个凭证只能看, 不能操作",
                         "它是在狗的热点上用明文 PIN 换的。热点的密码是出厂"
                         "固定的, 射程之内谁都能解开报文 —— 所以这条通道上只"
                         "发只读凭证。要操作, 用手机 app(它走质询-应答),"
                         "或者从别的网连进来。")
        state = self.sweep(now_ms=now_ms)
        if state.holder is not None and state.holder.ref == sess.ref:
            return
        if state.holder is None:
            raise Denied(409, "先取控制权",
                         "这条接口会改变这只狗正在做什么。先 POST "
                         "/api/control/acquire 拿一份租约, 之后每 10 秒 POST "
                         "/api/control/heartbeat 续一次; 30 秒不续自动到期。")
        raise Denied(409,
                     f"控制权在 {state.holder.operator or '别人'} 手上",
                     "要接手就 POST /api/control/takeover —— 他同意、或者 15 "
                     "秒不回应, 就移交给你。紧急情况带上 force 和理由, 那一下"
                     "会进审计。急停不受这条管, 任何时候都按得下去。")
