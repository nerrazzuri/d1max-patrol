"""值守屏那一份汇总(§5.1):**把静默失败摆到玻璃上。**

六项的共同点是**它们的失败都是静默的**。盘满了、包落好了没生效、钟漂了、
证据在狗上堆着、备份盘早就坏了 —— 每一件都不会自己冒出来喊一声:机器照常
转,页面照常绿,直到需要它的那天才发现。这一屏就是把这几件事变得看得见的
那块玻璃;不显示,人不会发现。

**缺的事实一律 ``None``,不用 ``0``、不用 ``-1``、不用空串。** ``0`` 是
「查过了,没有」,``None`` 是「这一档还没有这个能力 / 这一拍不知道」——
两句话在屏幕上长得一样,在现场差着一次事故。``upload_backlog`` 是这条纪律
最锋利的一处:回传队列是第 9 卷的东西,这台狗上还没有这个事实,报 ``0``
等于告诉人「证据都传上去了」,而它们全在狗上堆着。

配套的 ``detail`` 是这条纪律的另一半:一个光秃秃的 ``null`` 摆在屏上,人分
不清是「没查」还是「查了没事」—— 那本身就是一次新的静默失败。所以每一项报
不出来的时候都附一句人话,说清楚为什么。

**这个模块只读,一个会改狗的动作都不许有。** 这是挂账 66 那条教训的同一根:
盘况屏的例外名单按 ``refreshKey`` 认,而名单挡不住有人换个 key 把功能挂进去
—— 名单本身没写错,错在它认的是名字。

守这条纪律的**真正那根钉子是一条行为测试**:
``tests/app/test_watch.py::test_只读这条纪律_拿探针钉住`` 塞进来一个假引擎,
它只放行白名单里那几个只读属性,**别的属性访问一律当场炸**,然后拿它把这份
汇总和那条路由各跑一遍。取别名、改方法名、挪去 ``server.py`` 的处理器里 ——
全部在运行时炸,因为它认的是属性访问,不是文本。

同一个文件里还留着一条**读源码字面量**的旧守卫
(``test_盘况屏那条规矩_这个模块只读``),它降级成了一条便宜的绊线:能在评审
之前抓住手滑,但挡不住换个名字。副作用是这份 docstring 自己也在被那条绊线检查
的文本里,所以上面提到那几个方法时只能绕着说;具体禁哪几个字面量,去那条测试
里看。

**拿不到的事实由调用方注入,注不进来就是「不知道」。** 三项在这一层读不到:
``_disk`` 住在 ``app/server.py``(import 回去就是一个环),电量在
``_StateHub`` 备好的快照里(现问后端是一次真实的链路往返,而这一屏是按秒刷
的),扫盘是个协程(要过线程桥)。照 ``AlertSources`` 那条既有的成例办 ——
那边的 ``disk=lambda: _disk(ctx.runs_root)`` 边上写着「一个口径,两个出口」,
这里是第三个出口。

**判据一律借现成的,不另写一份。** ``bundle_lag`` 直接用
``alert_sources._落了但没生效``:那条 P2 告警认的就是它。在这儿另写一份的
话,同一件事会有两套说法 —— 屏上写着「没落差」,告警栏里挂着一条
``bundle_lag``,现场没人知道该信哪个。钟偏同理,走 ``engine.schedule``
的那个纯函数。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from d1max_patrol.app.alert_sources import _落了但没生效
from d1max_patrol.engine.backup import (
    BackupError,
    TargetStatus,
    plan_sync,
    read_sync_state,
)
from d1max_patrol.engine.removable import DiskRole
from d1max_patrol.engine.retention import scan_runs
from d1max_patrol.engine.schedule import clock_skew

if TYPE_CHECKING:                       # pragma: no cover - 只为标注,不进运行期
    from d1max_patrol.app.server import AppContext

log = logging.getLogger(__name__)

#: 回传积压为什么是 ``None``。**这一句必须出现在屏上** —— 见模块 docstring。
#: 这句话是印给值班的人看的,所以里面不许有排期黑话(「第 N 卷」那种):
#: 站在屏前面的人不知道那是什么,他要知道的是「证据现在在哪儿」。
NO_UPLOADER = ("这台狗还没装回传功能,现场证据全存在本机 —— "
               "所以这一档是「不知道」,不是「没有积压」")

#: 电量取不到时的那句话。
NO_BATTERY = "还没收到过电量遥测 —— 这一档是「不知道」,不是「电量为 0」"

#: 没有外部时间参照时的那句话(§3.3 第 4 条)。
NO_TIME_REF = "没有外部时间参照 —— 漂移是「不知道」,报 0 等于说「钟是准的」"


def watch_summary(ctx: AppContext, *, now_ms: int,
                  disk: Callable[[], tuple[int, int]] | None = None,
                  battery_pct: float | None = None,
                  battery_as_of_ms: int | None = None,
                  targets: Sequence[TargetStatus] | None = None,
                  ) -> dict[str, Any]:
    """§5.1 那六项,一次答齐。**只汇总,不判级,不改任何东西。**

    ``now_ms`` 由调用方读一次传进来(``ctx.clock()``),这儿不读钟:一次请求
    里钟偏和归档年龄用两个不同瞬间的读数,是一处可以省掉的糊涂账。

    ``disk`` / ``battery_pct`` / ``battery_as_of_ms`` / ``targets`` 是几处注入
    口,留空就是「不知道」。为什么要注入见模块 docstring。
    """
    lag, lag_why = _包落差(ctx.bundles_root)
    skew, skew_why = _钟偏(ctx, now_ms)
    pct, pct_why = _盘水位(disk)
    mirror, mirror_why = _镜像(ctx, targets, now_ms=now_ms)
    got: dict[str, Any] = {
        # 单位:槽名的列表(比 ``current`` 新的那几个),空列表 = 没落差。
        # 不显示的话:人以为改生效了,其实没有(§3.4)。
        "bundle_lag": lag,
        # 单位:**秒**,正数 = 本地钟走快了。
        # 不显示的话:狗一脸认真地在错误的时间巡逻(§3.3)。
        "clock_skew_s": skew,
        # 单位:条数(这一卷恒为 ``None``,见 ``NO_UPLOADER``)。
        # 不显示的话:证据在狗上堆着,没人知道(§4.3)。
        "upload_backlog": None,
        # 单位:**已用比例 0-1**,不是 0-100 —— 0.83 的意思是这块盘 83% 满。
        # **渲染这一格的时候千万别跟 ``battery_pct`` 共用一个格式化函数**:
        # ``${disk_pct}%`` 会把一块 83% 满的盘画成「0.83%」,而这一格存在的
        # 全部理由就是「盘快满了要看得见」—— 那一手滑会让它反着报,而且是
        # 以最安静的方式(挂账 77 要把它改名成 ``disk_used_ratio``)。
        # 不显示的话:直到它拒绝出发那天才发现(§4.7)。
        "disk_pct": pct,
        # 单位:**百分数 0-100**,不是 0-1。跟上面那一行量纲不同,见上面那段。
        # 不显示的话:平均 36% 看着健康,随时趴下(§1.1)。
        "battery_pct": battery_pct,
        # 单位:UTC 毫秒 —— 上面那个电量是**什么时候**收到的。
        # **这儿不设阈值,也不替人判「多久算旧」。** 电量是事件推出来的,链路
        # 断了它不会自己变回 ``None``,只会一直停在最后一个读数上:狗在地下室
        # 断链 40 分钟,屏上稳稳写着 31%。一个不再更新的数比 ``None`` 更危险,
        # 因为它看起来像在更新。把时刻摆出来,让看的人自己算这个数多老了 ——
        # 带 ``as_of`` 的不是撒谎,不带的才是。
        "battery_as_of_ms": battery_as_of_ms,
        # 单位:条数。**只报个数,不报详情。** 告警簿有自己的生命周期(挂起、
        # 确认、清除),并进这份只读汇总就把它绑上了那台状态机;详情走
        # ``/api/alerts`` 那几条路由。但一个数是要带的:「六项全绿 + 屏上没有
        # 告警入口」会被读成「这台狗没事」。
        "alerts_open": len(ctx.alerts.open()),
        # 不显示的话:一块坏掉或者被刷没了的备份盘,一切看起来都正常,直到
        # 你需要它(§7.6)。
        "mirror": mirror,
    }
    why = {"upload_backlog": NO_UPLOADER}
    if battery_pct is None:
        why["battery_pct"] = NO_BATTERY
    for 名, 说 in (("bundle_lag", lag_why), ("clock_skew_s", skew_why),
                   ("disk_pct", pct_why), ("mirror", mirror_why)):
        if 说:
            why[名] = 说
    got["detail"] = why
    return got


def _包落差(bundles_root: Path) -> tuple[list[str] | None, str]:
    """盘上有比 ``current`` 新的槽吗。**判据借告警簿那一份,不另写。**

    空列表和 ``None`` 是两件事:空列表是「翻过盘了,没有落差」,``None`` 是
    「盘上那份局面读不出来」。
    """
    try:
        落后 = _落了但没生效(bundles_root)
    except (OSError, ValueError) as exc:
        # ``BundleError`` 是 ``ValueError`` 的子类,坏掉的 landed.json 也在
        # 这一网里。口径跟 ``AlertSources.on_tick`` 一致:读不出局面就是
        # 「不知道」,不猜。
        log.warning("读不出任务包的局面,这一档按不知道答", exc_info=True)
        return None, f"读不出盘上任务包的局面({exc})"
    return list(落后), ""


def _钟偏(ctx: AppContext, now_ms: int) -> tuple[float | None, str]:
    """本地钟跟外头差多少秒。**没有参照就是「不知道」,不是 0。**

    判据整条走 ``engine.schedule.clock_skew``:``/api/schedule`` 上那个
    ``clock`` 段用的是同一个函数,两处不会分岔。
    """
    try:
        ref = ctx.time_reference()
    except OSError as exc:
        # 第 9 卷接上服务器之后这一跳会真的走网络,那时候它会抛。
        log.warning("取不到外部时间参照", exc_info=True)
        return None, f"取不到外部时间参照({exc})"
    reference_ms, source = ref if ref is not None else (None, "")
    skew = clock_skew(local_ms=now_ms, reference_ms=reference_ms, source=source)
    return skew.skew_s, "" if skew.skew_s is not None else NO_TIME_REF


def _盘水位(disk: Callable[[], tuple[int, int]] | None) -> tuple[float | None, str]:
    """已用比例,0-1。口径跟 ``/api/storage`` 和 ``disk_80`` 那条告警同一个。

    ``disk=None`` 这条分支**生产路径走不到**:那条路由无条件传
    ``lambda: _disk(ctx.runs_root)``。留着是给别的调用方兜底 —— 任务书里的签名
    就是 ``watch_summary(ctx, *, now_ms)``,哪天卷 9/10 照着签名写个 CLI 直接调,
    就会踩进来。踩进来的时候屏上不能只是安静地少一格,所以那句话要说清楚这是
    **接线错了**,不是盘的问题:另外两个注入口(电量、扫盘)留空是正当的现场
    事实,这一个留空是 bug,三者不该被压进同一句「不知道」。
    """
    if disk is None:
        return None, ("这一拍没量盘 —— 调这一屏的人没把盘水位的取值口接上,"
                      "这是接线的问题,不是盘的问题")
    try:
        used, total = disk()
    except OSError as exc:
        log.warning("量不出盘水位", exc_info=True)
        return None, f"量不出这块盘还剩多少({exc})"
    if total <= 0:
        return None, "盘的总容量报回来是 0,比例算不出来"
    return used / total, ""


def _镜像(ctx: AppContext, targets: Sequence[TargetStatus] | None, *,
          now_ms: int) -> tuple[dict[str, Any] | None, str]:
    """镜像盘现在什么状态,落后多少趟(§7.6)。

    **一块坏掉或者被人格式化掉的备份盘,平时一切看起来都正常** —— 直到你需要
    它的那天。所以这一档报的是三件事:认到几块、能用几块、落后多少趟。

    **这一档整个是悲观聚合:最差的那块盘代表这台狗。** ``behind`` 取几块盘里
    最大的那个,跟 ``/api/storage`` 上那句 ``behind = max(behind, plan.behind)``
    同一个口径;``full`` 取或;``last_sync_ms`` 取**最旧**的那个 —— 那是同一句
    话的另一半,理由见循环里那段注释。

    ``disks`` 是 0 的时候 ``behind`` 给 ``None`` 而不是 0:一块镜像盘都没有的
    时候,「落后 0 趟」是一句听着让人放心的假话。

    ``usable`` 是盘上的标记**声称**能用的块数,``measured`` 是这一拍**真的读
    出来了**的块数。两个数都报,因为它们会不一样:一块盘掉线、``sync_state``
    被拔坏,这一块就只剩声称。两者不等的时候 ``detail`` 里必有一句话 ——
    在一块以消灭静默失败为职责的屏上,不许自己造一个静默失败。
    """
    if targets is None:
        return None, "认不出现在插着什么盘 —— 镜像盘这一档这一拍不知道"
    盘 = [t for t in targets if t.role is DiskRole.MIRROR]
    能用 = [t for t in 盘 if t.usable]
    out: dict[str, Any] = {"disks": len(盘), "usable": len(能用), "measured": 0,
                           "behind": None, "behind_bytes": None,
                           "full": None, "last_sync_ms": None}
    if not 能用:
        return out, ("一块镜像盘都没认到 —— 这台狗的归档现在只有一份"
                     if not 盘 else "认到的镜像盘现在一块也不能用")
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    try:
        # 扫一遍就够:每块盘各扫一遍的话,``scan_runs`` 会把整个 runs_root
        # 递归 stat 一次,而这一屏是按秒刷的。
        runs = scan_runs(ctx.runs_root, now=now)
    except OSError as exc:
        log.warning("扫不了归档目录,落后量这一档不知道", exc_info=True)
        return out, f"扫不了归档目录({exc})—— 落后多少趟量不出来"
    量到, behind, behind_bytes, full = 0, 0, 0, False
    last: int | None = None
    for t in 能用:
        try:
            plan = plan_sync(ctx.runs_root, t.mount, robot_sn=ctx.identity.sn,
                             now=now, runs=runs)
            state = read_sync_state(t.mount, robot_sn=ctx.identity.sn)
        except (OSError, BackupError):
            # 一块坏盘不许把整档掀翻:边上那几块好盘还得报得出来。
            log.warning("这块镜像盘读不了,不算进落后量", exc_info=True)
            continue
        量到 += 1
        # ``behind`` 和 ``behind_bytes`` **各取各的 max,报出来的两个数可能来自
        # 两块不同的盘** —— 这是有意的:这一档要的是每一项里最悲观的那个数,
        # 不是某一块盘的完整画像。想知道具体哪块盘落后多少、有多少字节,
        # 看 ``/api/backup``,那里是一块盘一行。
        behind = max(behind, plan.behind)
        behind_bytes = max(behind_bytes, plan.behind_bytes)
        # 任何一块镜像盘满了都要顶出来:满盘不删旧的,同步就此停住,不说的话
        # 屏上只看得到「落后」在涨。
        full = full or plan.full
        # **取最旧的那个。** 跟 ``behind = max(...)`` 是同一句话的两半:最差的
        # 那块盘代表这台狗。取最新的那一边,一块三周前被拔去格式化过的盘会被
        # 边上那块昨晚刚同步过的好盘整个盖住 —— 屏上写着「两块盘都在,昨晚
        # 刚同步」,而事实是其中一块早就没了。§7.6 那一行原话要拦的就是这个:
        # 「一块坏掉或者被刷没了的备份盘,一切看起来都正常,直到你需要它」。
        last = (state.last_sync_ms if last is None
                else min(last, state.last_sync_ms))
    if not 量到:
        return out, "认到的镜像盘一块也读不了 —— 落后多少趟量不出来"
    out |= {"measured": 量到, "behind": behind, "behind_bytes": behind_bytes,
            "full": full,
            # ``0`` 是 ``read_sync_state`` 拼不出进度时的默认值,意思是「这块盘
            # 上一趟也没同步过」。原样报上去,屏上画出来是 1970-01-01 —— 一个
            # 看着像真事的假时刻,比 ``null`` 更坏。翻成 ``None``,并在下面配一
            # 句为什么。
            "last_sync_ms": last or None}
    话 = []
    if not last:
        话.append("认到的镜像盘里有一块一趟也没同步过 —— 上次同步时刻这一档是"
                  "「不知道」,不是 1970 年")
    if 量到 < len(能用):
        话.append(f"认到 {len(能用)} 块能用的镜像盘,其中 {len(能用) - 量到} 块"
                  "这一拍读不了 —— 上面这几个数只覆盖读得了的那些")
    return out, ";".join(话)
