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
from typing import Any

from d1max_patrol.app.auth import CHANNEL_LOCAL, Denied, Guard, Session
from d1max_patrol.engine.lease import AuditRecord, LeaseBook, LeaseState

#: 要 L1 控制权的那几条接口。**判据是"会改变这只狗正在做什么"**(§3.5 规
#: 则 4), 不是"会不会让它动"。
#:
#: **默认要。** 这张表今天是"明确要"的那一半, 下面的 :data:`EXEMPT` 是"明确
#: 不要"的那一半; 两张表都没登记的写请求一律按要控制权处理(见
#: :func:`needs_lease`)。以前是反的 —— 不在这张表上就放行, 于是往
#: ``server.py`` 加一条会改狗的接口而忘了登记, 没人会红, 没拿租约的人直接调得
#: 动。
#:
#: 不在这张表上的, 都是想清楚了才不在的(逐条登记在 :data:`EXEMPT`):
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
#:
#: **``POST /api/mapping/rebuild`` 以前在这张清单上, 现在不在了。** 当初的
#: 理由是"拿录好的包离线重建, 狗本身没在动" —— 那句话写于 ``rebuild()``
#: 里加上 ``forget_home`` 之前, 今天只描述了它的一半。另一半是:它**无条件
#: 删掉这张图的原点**(``app/mapping.py`` 的 ``rebuild`` 第一件事), 而原点
#: 是起飞门槛的硬前置 —— 删完谁都起不了飞, 恢复的唯一办法是有人物理走到
#: 原点上用手机重标一次。判据仍是 §3.5 规则 4:它改变的是这只狗**下一趟还
#: 能不能出发**。所以它进了下面那张表, 跟 ``record/start|stop`` 对齐。
CONTROLLED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/api/teleop$")),
    ("POST", re.compile(r"^/api/teleop/heartbeat$")),
    ("POST", re.compile(r"^/api/teleop/mode$")),
    # 解除急停:按下不设门槛、解除设门槛 —— 按错了多按一次没有代价,解错了
    # 狗会动。``/api/estop`` 本身仍然不在这张表上(规则 2)。
    ("POST", re.compile(r"^/api/estop/release$")),
    ("POST", re.compile(r"^/api/mapping/record/(start|stop)$")),
    # 见上面那段:这一条删原点, 不是"离线活儿"。**跟它配套的还有
    # ``server._rebuild`` 里那道"引擎在跑就不许重建"的闸** —— 这张表拦的是
    # "没有控制权的人", 那道闸拦的是"握着控制权的人自己手滑", 两道缺一不可。
    ("POST", re.compile(r"^/api/mapping/rebuild$")),
    ("POST", re.compile(r"^/api/missions/[^/]+/run$")),
    ("POST", re.compile(r"^/api/run/(pause|resume|abort|suspend)$")),
    ("POST", re.compile(r"^/api/maps/load$")),
    ("POST", re.compile(r"^/api/pose/(initial|reset)$")),
    # 告警的记名确认/解决(§5.3)。**这两条是这张表上唯一不让狗动腿的行**,
    # 所以理由要说准: ``ack`` 会把 P1 的升级链停下来 —— 匿名的任何一个人
    # 都能把声音关掉, 那条升级链就白做了; ``resolve`` 会把一条告警从值守
    # 屏的主表上拿掉, 那是"这件事我处理完了"的签字。两条都是**记名的处置
    # 动作**, 判据仍是 §3.5 规则 4 的那一句: 它改变的是这只狗接下来会不会
    # 有人管。读那两张表(``GET /api/alerts``、``/api/alerts/all``)照旧不
    # 要控制权 —— 规则 1, 看永远不要。
    #
    # ``.+`` 跨斜杠是有意的: 告警键形如 ``robot/kind#seq``, 键本身带斜杠
    # (见 ``engine/alerts.py`` 的 ``_key``)。结尾的 ``$`` 也是必须的 ——
    # ``needs_lease`` 用的是 ``re.Pattern.match``, 只锚开头不锚结尾, 少一个
    # ``$`` 就会把 ``/api/alerts/x/ack/随便什么`` 也算成要控制权。
    ("POST", re.compile(r"^/api/alerts/.+/ack$")),
    ("POST", re.compile(r"^/api/alerts/.+/resolve$")),
)


#: 看的方法。规则 1: 看永远不要控制权。
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: **明确不要**控制权的写接口, 每条带一句理由。判据的完整论证在
#: :data:`CONTROLLED` 上方那段注释里, 这里只记结论。
#:
#: 加一条之前先回答: 它会不会改变这只狗**正在做什么**, 或者**下一趟还能不能
#: 出发**? 会的话它不属于这里。``tests/app/test_control_gate.py`` 里有一份
#: 手抄的豁免表跟这张逐条对账 —— 往这里加一条而不去那边登记, 当场红。
EXEMPT: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/api/auth$"), "登录: 还没有会话, 谈不上租约"),
    ("POST", re.compile(r"^/api/auth/logout$"), "登出: 顺带还控制权"),
    ("POST", re.compile(r"^/api/control/(acquire|heartbeat|release|takeover)$"),
     "控制权本身: 要控制权才能取控制权是个死结"),
    ("POST", re.compile(r"^/api/control/takeover/approve$"),
     "同意移交的是持有者自己, 也在控制权本身这一组"),
    ("POST", re.compile(r"^/api/estop$"),
     "§3.5 规则 2: 急停永远不要控制权。这一条错了会死人"),
    ("PUT", re.compile(r"^/api/operator$"),
     "换署名: 下一个人正是在拿到控制权之前报名字的"),
    ("PUT", re.compile(r"^/api/missions/[^/]+$"),
     "编任务是案头活, 不改在跑的那一趟"),
    ("PUT", re.compile(r"^/api/maps/[^/]+/home$"),
     "跟编任务同属案头活, 两个人不该为改一个数去抢方向盘"),
    ("POST", re.compile(r"^/api/runs/[^/]+/judge$"), "判读归档: 改的是纸面不是狗"),
    ("POST", re.compile(r"^/api/runs/[^/]+/review/[^/]+$"),
     "复核归档: 改的是纸面不是狗"),
    ("POST", re.compile(r"^/api/storage/sweep$"), "清盘: 不改变狗正在做什么"),
    ("POST", re.compile(r"^/api/exports$"), "导出: 不改变狗正在做什么"),
    ("POST", re.compile(r"^/api/exports/[^/]+/confirm$"), "导出确认"),
    ("POST", re.compile(r"^/api/backup/(init|sync|eject)$"),
     "备份: 不改变狗正在做什么"),
    ("POST", re.compile(r"^/api/release/(install|activate|rollback)$"),
     "升级尤其不能要: 恢复性的回滚不该被一个掉线的会话挡住"),
    ("PUT", re.compile(r"^/api/identity/payload$"),
     "登记有没有装上装: 改的是身份记录, 不是动作"),
    ("POST", re.compile(r"^/api/bundle/(apply|rollback)$"),
     "任务包对在跑的那一趟是惰性的(下一趟才生效), 见上方注释"),
)


def needs_lease(method: str, path: str) -> bool:
    """这条接口要不要 L1 控制权。**写请求默认要。**

    顺序是承重的:

    1. 看的方法一律不要(规则 1)。
    2. :data:`CONTROLLED` 先于 :data:`EXEMPT` —— 哪天有人把一条豁免写宽了
       (比如 ``^/api/estop``少了 ``$``), 它也吞不掉 ``/api/estop/release``
       这种明确要的。
    3. 两张表都没登记的写请求: 要。忘了登记的新接口从此是"被挡住、有人来
       问", 而不是"静悄悄地谁都调得动"。不存在的路径也一样回 409 而不是
       404 —— 跟没解锁一律先 401 同一条立场, 不给没权限的人当存在性探针。
    """
    if method in SAFE_METHODS:
        return False
    if any(m == method and p.match(path) for m, p in CONTROLLED):
        return True
    return not any(m == method and p.match(path) for m, p, _why in EXEMPT)


class ControlDesk:
    """把 ``LeaseBook`` 跟 ``Guard`` 拴在一起。

    自己不存状态(游标除外), 所有真状态都在 ``book`` 和 ``guard`` 里。
    """

    def __init__(self, guard: Guard, *,
                 book: LeaseBook | None = None) -> None:
        self._guard = guard
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

    def seats(self) -> tuple[int, int]:
        """``(非本机会话数, max_sessions)`` —— 这两个数**只有这一处定义**。

        ``Guard.sessions()`` 含本机(``CHANNEL_LOCAL``)会话, 而 §3.6 的
        ``max_sessions`` 只算非本机通道(见 ``Guard._issue``): 回环不占那条
        无线上行, 所以本机会话(SSH/控制台到场的人)不占这 3 个名额, 也不该
        进这个计数。两个数天生不是一回事, 屏幕上绝不允许出现"8 条会话, 上限
        3"这种对不上的假象。

        **谁都不许再抄一遍这条过滤条件。** ``ControlDesk.snapshot()``
        (``/api/state`` 的 ``control`` 段)和 ``server.py`` 的
        ``_control_wire``(``GET /api/control`` 及其余六条控制权路由)读的
        是同一时刻的同一份东西 —— 手机端两个接口都读, 分母如果在两处各写一
        份过滤, 迟早会因为一处改了另一处没跟着改, 在同一时刻对同一件事报出
        两个不同的 ``sessions``。想看含本机的全量会话表(比如运维在本机排
        障), 走 ``GET /api/sessions``, 那里用的是 ``Guard.sessions()`` 的原
        始列表, 自己就说明白了含本机。
        """
        remote = tuple(s for s in self._guard.sessions()
                       if s.channel != CHANNEL_LOCAL)
        return len(remote), self._guard.max_sessions

    def snapshot(self, *, now_ms: int) -> dict[str, Any]:
        """``/api/state`` 里 ``control`` 那一段。

        **只放不会每拍都变的量。** ``_StateHub`` 靠"这份快照跟上一份一样就
        不发"来保持安静; 往里放一个"离上次用过多久"这种每拍都变的数, 这条
        SSE 就会每半秒响一次 —— 在热点上那是实打实的带宽, 而且会把真正的状态
        变化淹掉。会话明细去 ``GET /api/sessions`` 拿。

        **这就是为什么这里用 ``to_wire_stable()`` 不用 ``to_wire()``。**
        ``LeaseState.expires_in_ms``/``grace_in_ms`` 是给轮询接口做倒计时用
        的相对量(狗上没有 NTP, 见 ``engine/lease.py``), 握着控制权的时候
        它们每拍都变 —— 混进这条常连的 SSE 就正犯了上面那句话说的错(修复轮
        2 N1)。``GET /api/control`` 及其余六条控制权路由(``server.py`` 的
        ``_control_wire``)是轮询, 那边继续用 ``to_wire()``, 相对量正是要
        送到手机上的东西, 不受这里影响。

        ``expires_ms`` 会跟着心跳每 10 秒变一次, 那是有意的: 第 8 卷要靠它算
        "还剩多久到期"。10 秒一帧不吵, ``to_wire_stable()`` 不去掉它。

        ``remote_sessions``/``max_sessions`` 这两个数的定义在 :meth:`seats`,
        不在这儿重复。

        **为什么这个整数叫 ``remote_sessions`` 而不是 ``sessions``。** 手机端
        要同时读这一段和 ``GET /api/sessions``, 而那条接口里 ``sessions`` 是
        一个**数组**(每条会话一行)。同一个名字在两条接口上一个是整数、一个
        是数组, 是最容易写出"看起来能跑、偶尔炸一下"的那种客户端代码 —— 而
        ``GET /api/sessions`` 早就把这个整数叫 ``remote_sessions`` 了。所以对
        齐到那个名字: 一个概念一个名字, 一个名字一种类型。第 7 卷开工前改是
        免费的(今天没有任何客户端在读它), 写完再改就不是了。
        """
        state = self.book.state(now_ms=now_ms)
        sessions, max_sessions = self.seats()
        return {**state.to_wire_stable(),
                "remote_sessions": sessions,
                "max_sessions": max_sessions}

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
