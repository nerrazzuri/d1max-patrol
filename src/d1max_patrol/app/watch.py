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
—— 名单本身没写错,错在它认的是名字。所以这里换一种守法:配套的那条测试
**读这个文件的源码**,里面一旦出现引擎那几个会动腿的方法、或者那个写请求方
法的字面量,就红。守源码不守名单,换个名字也绕不过去。副作用是这份 docstring
自己也在被检查的文本里,所以上面提到那几个方法时只能绕着说。

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
NO_UPLOADER = ("回传队列是第 9 卷的东西,这台狗上还没有这个能力 ——"
               "所以这一档是「不知道」,不是「没有积压」")

#: 电量取不到时的那句话。
NO_BATTERY = "还没收到过电量遥测 —— 这一档是「不知道」,不是「电量为 0」"

#: 没有外部时间参照时的那句话(§3.3 第 4 条)。
NO_TIME_REF = "没有外部时间参照 —— 漂移是「不知道」,报 0 等于说「钟是准的」"


def watch_summary(ctx: AppContext, *, now_ms: int,
                  disk: Callable[[], tuple[int, int]] | None = None,
                  battery_pct: float | None = None,
                  targets: Sequence[TargetStatus] | None = None,
                  ) -> dict[str, Any]:
    """§5.1 那六项,一次答齐。**只汇总,不判级,不改任何东西。**

    ``now_ms`` 由调用方读一次传进来(``ctx.clock()``),这儿不读钟:一次请求
    里钟偏和归档年龄用两个不同瞬间的读数,是一处可以省掉的糊涂账。

    ``disk`` / ``battery_pct`` / ``targets`` 是三处注入口,留空就是「不知道」。
    为什么要注入见模块 docstring。
    """
    lag, lag_why = _包落差(ctx.bundles_root)
    skew, skew_why = _钟偏(ctx, now_ms)
    pct, pct_why = _盘水位(disk)
    mirror, mirror_why = _镜像(ctx, targets, now_ms=now_ms)
    got: dict[str, Any] = {
        # 不显示的话:人以为改生效了,其实没有(§3.4)。
        "bundle_lag": lag,
        # 不显示的话:狗一脸认真地在错误的时间巡逻(§3.3)。
        "clock_skew_s": skew,
        # 不显示的话:证据在狗上堆着,没人知道(§4.3)。
        "upload_backlog": None,
        # 不显示的话:直到它拒绝出发那天才发现(§4.7)。
        "disk_pct": pct,
        # 不显示的话:平均 36% 看着健康,随时趴下(§1.1)。
        "battery_pct": battery_pct,
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
    """已用比例,0-1。口径跟 ``/api/storage`` 和 ``disk_80`` 那条告警同一个。"""
    if disk is None:
        return None, "这一次没人把盘水位的取值口传进来 —— 这一档量不出来"
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

    ``behind`` 取几块盘里**最大**的那个,跟 ``/api/storage`` 上那句
    ``behind = max(behind, plan.behind)`` 同一个口径:只要有一块盘落后,这台
    狗的备份就是落后的。

    ``disks`` 是 0 的时候 ``behind`` 给 ``None`` 而不是 0:一块镜像盘都没有的
    时候,「落后 0 趟」是一句听着让人放心的假话。
    """
    if targets is None:
        return None, "认不出现在插着什么盘 —— 镜像盘这一档这一拍不知道"
    盘 = [t for t in targets if t.role is DiskRole.MIRROR]
    能用 = [t for t in 盘 if t.usable]
    out: dict[str, Any] = {"disks": len(盘), "usable": len(能用),
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
        behind = max(behind, plan.behind)
        behind_bytes = max(behind_bytes, plan.behind_bytes)
        # 任何一块镜像盘满了都要顶出来:满盘不删旧的,同步就此停住,不说的话
        # 屏上只看得到「落后」在涨。
        full = full or plan.full
        last = (state.last_sync_ms if last is None
                else max(last, state.last_sync_ms))
    if not 量到:
        return out, "认到的镜像盘一块也读不了 —— 落后多少趟量不出来"
    out |= {"behind": behind, "behind_bytes": behind_bytes, "full": full,
            "last_sync_ms": last}
    return out, ""
