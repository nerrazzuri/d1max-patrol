"""升级前后各一遍的自检。

**为什么要重到显得过分。** 装了上装的机器每尝试一次升级都要付一次整机重启,
而重启之后不一定抢得回 SDK 会话(§7.1);所以能在重启之前挡掉的问题,一条都
不该留到重启之后。没装上装的形态代价低得多,但这套自检**照样全跑** —— 它同时
是回滚的判据,而回滚的判据不该因为形态不同而有两套。

**两遍的形状是不一样的。** 升级前那遍输出「哪几项不过」,给人看、给人改;
重启后那遍输出「留着还是退回去」,给机器执行 —— 所以后者的判据是一个纯函数
(规格 §8.5),四项结果进去,一个结论出来,单测把组合跑全。

模块里**取值和判断是分开的**:``gather_*`` 那几个函数做 I/O,``precheck`` /
``postcheck_verdict`` 是纯的。混在一起的话,那些只在真机上才出现的组合
(电量 49%、盘满、租约活着)在仿真里一条也造不出来。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from d1max_agent.engine.preflight import CheckResult
from d1max_patrol.backends.base import DeviceBackendError, NavBackendError

#: 升级期间的电量下限。整机重启途中断电是最脏的一种坏法(§7.2)。
#: 不带上装时不整机重启,这条门槛照样保留 —— 升级期间不该同时在换电池。
MIN_UPGRADE_BATTERY_PCT = 50.0

#: 除了新版本本身,还要留出这么多余量。装得下不等于装完还能动 ——
#: 日志、临时文件、下一次升级的落槽位都在同一块盘上。
UPGRADE_FREE_MARGIN_MB = 1024.0

#: 多久没备份就该提示了。**提示,不阻断**(§7.5)。
BACKUP_NAG_DAYS = 14.0


log = logging.getLogger(__name__)


def mission_schema_floor(missions_dir: Path | str) -> int:
    """盘上那些任务包里,schema 最低的是几。**没有包就回 0。**

    0 的意思是「没有包」,不是「schema 版本 0」 —— 调用方拿它当「这一项没有
    可比的对象,放行」。用 None 也行,但那会让 ``precheck`` 的输入多一种
    可空类型,而它已经够多了。

    ``schema`` 这个键现在还没有任何任务包在写(第 5 卷才加),所以读不到就当 1。
    等第 5 卷把它真加上,这个函数一个字都不用改。
    """
    try:
        paths = sorted(Path(missions_dir).glob("*.json"))
    except OSError:
        return 0
    floor = 0
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 读不动的任务包不该拦住升级 —— 它本来就已经是坏的了。
            continue
        if not isinstance(raw, dict):
            continue
        try:
            schema = int(raw.get("schema", 1))
        except (TypeError, ValueError):
            schema = 1
        floor = schema if floor == 0 else min(floor, schema)
    return floor


@dataclass(frozen=True, slots=True)
class PrecheckInputs:
    """升级前自检要的全部事实。**全是已经取好的值,这里不做 I/O。**"""

    #: 包的哈希对不对得上(``release.verify_package`` 的结论)。
    package_ok: bool
    package_detail: str
    #: 新版要求的任务包 schema。
    requires_mission_schema: int
    #: 盘上任务包里最低的那个 schema。0 = 盘上没有包。
    mission_schema_floor: int
    free_mb: float
    #: 新版本本身占多少。
    need_mb: float
    #: ``min(power1, power2)`` —— 两块电池取低的那块(§7.2)。
    battery_pct: float
    engine_running: bool
    #: 有没有活跃的 L1 租约。第 6 卷之前恒为 False。
    lease_active: bool
    #: 「这台有没有装上装」记过没有(§7.1 纪律 1)。
    payload_recorded: bool
    has_payload: bool
    #: 这次是自动升的还是人点的。
    auto: bool
    #: 距上次备份多少天。None = 从来没备份过。
    backup_age_days: float | None


@dataclass(frozen=True, slots=True)
class PrecheckReport:
    """七项。**``backup`` 那一项不算进 ``ok``** —— 它是提示,不是闸。"""

    checks: tuple[CheckResult, ...]

    #: 不过就拦住升级的那几项。``backup`` 不在里头(§7.5)。
    #: ``ClassVar`` —— 这是个类常量,不是实例字段;不标出来的话
    #: ``@dataclass`` 会把它当成一个有默认值的字段收进 ``fields()``,
    #: ``replace()`` 就能把它覆盖掉,而这条黑名单从来不该因实例而异。
    BLOCKING: ClassVar[tuple[str, ...]] = ("package", "schema", "disk",
                                           "battery", "busy", "payload")

    @property
    def blocking(self) -> tuple[CheckResult, ...]:
        """真拦住这次升级的那几项。"""
        return tuple(c for c in self.checks
                     if not c.ok and c.name in self.BLOCKING)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def to_wire(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                       for c in self.checks],
            "blocking": [c.name for c in self.blocking],
        }


def precheck(inputs: PrecheckInputs) -> PrecheckReport:
    """升级前那一遍。**纯函数** —— 同样的输入永远同样的结论。

    七项的顺序是有讲究的:从「包本身有没有问题」一路排到「现在是不是合适的
    时候」。人从上往下读,先看见的是最不该继续的那种理由。
    """
    checks: list[CheckResult] = []

    checks.append(CheckResult(
        "package", inputs.package_ok,
        inputs.package_detail or ("包校验通过" if inputs.package_ok else "包有问题")))

    schema_ok = (inputs.mission_schema_floor == 0
                 or inputs.requires_mission_schema <= inputs.mission_schema_floor)
    checks.append(CheckResult(
        "schema", schema_ok,
        f"新版要 schema >= {inputs.requires_mission_schema},盘上的任务包是 "
        f"{inputs.mission_schema_floor}" if not schema_ok
        else ("盘上没有任务包,这一项没有可比的对象"
              if inputs.mission_schema_floor == 0
              else f"新版要 {inputs.requires_mission_schema},"
                   f"盘上的包是 {inputs.mission_schema_floor}")))

    need = inputs.need_mb + UPGRADE_FREE_MARGIN_MB
    disk_ok = inputs.free_mb >= need
    checks.append(CheckResult(
        "disk", disk_ok,
        f"空闲 {inputs.free_mb:.0f}MB,要 {inputs.need_mb:.0f}MB 装新版加 "
        f"{UPGRADE_FREE_MARGIN_MB:.0f}MB 余量"
        + ("" if disk_ok else " —— 不够;删几趟归档或者先同步到备份盘")))

    battery_ok = inputs.battery_pct >= MIN_UPGRADE_BATTERY_PCT
    checks.append(CheckResult(
        "battery", battery_ok,
        f"电量 {inputs.battery_pct:.1f}%,门槛 {MIN_UPGRADE_BATTERY_PCT:.0f}%"
        + ("" if battery_ok else " —— 升级途中断电是最脏的一种坏法")))

    busy_ok = not (inputs.engine_running or inputs.lease_active)
    if busy_ok:
        busy_detail = "没有任务在跑,也没有活跃的租约"
    elif inputs.engine_running:
        busy_detail = "有任务在跑 —— 等它跑完,或者先中止"
    else:
        busy_detail = "有活跃的 L1 租约 —— 有人正握着控制权"
    checks.append(CheckResult("busy", busy_ok, busy_detail))

    if not inputs.payload_recorded:
        payload = CheckResult(
            "payload", False,
            "这台有没有装上装,没记过 —— 升级要靠它决定是只重启服务还是整机"
            "重启(§7.1),记不清就不能猜。装机清单第 5 步补上再升")
    elif inputs.has_payload and inputs.auto:
        payload = CheckResult(
            "payload", False,
            "这台装了上装,不许自动升级 —— 重启会放掉 SDK 会话,而放掉之后"
            "不一定抢得回来;凌晨三点没抢回来的话,狗就那么死到天亮")
    else:
        payload = CheckResult(
            "payload", True,
            "装了上装,升级要整机重启" if inputs.has_payload
            else "没装上装,只重启我们的服务")
    checks.append(payload)

    age = inputs.backup_age_days
    backup_ok = age is not None and age <= BACKUP_NAG_DAYS
    checks.append(CheckResult(
        "backup", backup_ok,
        "从来没往备份盘同步过 —— 建议先点一次「升级前备份」(提示,不拦)"
        if age is None else
        (f"上次备份是 {age:.1f} 天前"
         + ("" if backup_ok else " —— 建议先点一次「升级前备份」(提示,不拦)"))))

    return PrecheckReport(tuple(checks))


#: 重启后要过的四项(§7.2)。**顺序就是重要程度**。
POST_CHECKS: tuple[str, ...] = ("process", "control", "bridges", "identity")

#: 抢会话最多试几次。没有上装时第一次几乎必然成;这个循环留着是因为
#: **只在特殊配置下才走的路一定是烂的**(§7.1) —— 让它天天走。
MAX_GRAB_TRIES = 3
#: 两次之间等多久。测试里注入假的,套件里不许真等(§8.5)。
GRAB_WAIT_S = 2.0

#: 三个桥最多探几次。**这一层的宽限不能省。**
#:
#: ``MAX_BOOT_ATTEMPTS = 2`` 的理由是「第一次开机可能撞上别的偶发(网卡没
#: 起来、盘没挂上),给一次机会」 —— 可那是守卫那一层(第二层)的宽限。
#: 第一层(``app/server.py`` 的 ``_boot_postcheck``)在同一次启动里、在守卫
#: 之后跑,而且**第一次不过就直接回滚**,零宽限;于是第二层那两次机会在这
#: 条路上一次都用不上。而 ``_check_bridges`` 问的 ``nav.loc_status()`` /
#: ``nav.list_maps()`` 正是「网卡没起来」时最容易炸的东西:``main()`` 里三个
#: ``connect()`` 各只有 10 秒超时且失败被吞掉,冷启动时导航桥慢一拍,桥就红,
#: 一版好的第一次启动就被退掉。**这是好版本被误判回滚的最短路径。**
#:
#: 宽限只能加在这一层,不能改成「第一次失败不回滚、交给下一次启动」:
#: 那条路没有推动者 —— 服务没崩,只是桥红了,不会有人再启动一次,机器会
#: 挂在半空。
MAX_BRIDGE_TRIES = 3
#: 两次探测之间等多久。测试里注入假的,套件里不许真等(§8.5)。
BRIDGE_WAIT_S = 2.0

#: ``/api/selfcheck`` 等 ``run_postcheck`` 跑完的预算。真机(2026-09-22)上它走
#: 的是 ``_call`` 的默认 10 s,而没有旁路进程时抢会话三次之间就要等 4 s、桥探测
#: 三轮再等 4 s,每次探测本身还各有请求超时 —— 于是人点一下得到的是一句
#: 「后端没在规定时间内回话」,不是四项里哪项没过(W01c)。
#: 最坏情况按常量推:抢 3×15 s(旁路回执超时)+ 2×2 s,桥 3×(2×5 s)+ 2×2 s,
#: 合计 83 s;``tests/engine/test_selfcheck_post.py`` 用同一套常量钉着这个下界。
#: 常见情况(旁路进程压根没起来、导航桥在线)4 s 就回来了。
POSTCHECK_TIMEOUT_S = 90.0

#: 后端自己会说人话的那几类错:连不上、超时、被拒。它们是「这一项没过」的
#: 理由,不是 bug,记一行 ``str(exc)`` 就够了;traceback 只留给意料之外的异常。
_EXPECTED_BACKEND_ERRORS: tuple[type[BaseException], ...] = (
    DeviceBackendError, NavBackendError, TimeoutError, asyncio.TimeoutError, OSError)


class Verdict(str, Enum):
    """重启后自检的结论。**只有两个值** —— 这不是个可以「再看看」的判断。"""

    KEEP = "keep"
    ROLLBACK = "rollback"


def postcheck_verdict(results: Sequence[CheckResult]) -> Verdict:
    """留着,还是退回去。**纯函数**(§8.5),整个自动回滚就靠这一个判断。

    规矩只有一条:``POST_CHECKS`` 里那四项**都在,而且都过**,才留着。

    「都在」这半条容易被漏掉,而它恰恰是最要紧的:少跑一项是「不确定」,
    不是「没问题」。新版本崩在第三项探测里,结果只有两条 —— 那时候按
    「没有红的就算过」判,等于把一次崩溃读成了一次成功。跟起飞门槛那条
    「不确定不放行」是同一个道理,方向相反。

    多出来的项也算数:将来加第五项时,这个函数必须先被改,而不是悄悄
    把新项忽略掉。
    """
    seen: dict[str, bool] = {}
    for item in results:
        seen[item.name] = seen.get(item.name, True) and item.ok
    if set(seen) != set(POST_CHECKS):
        return Verdict.ROLLBACK
    return Verdict.KEEP if all(seen.values()) else Verdict.ROLLBACK


async def grab_control(
    device: Any, *, tries: int = MAX_GRAB_TRIES,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    wait_s: float = GRAB_WAIT_S,
) -> CheckResult:
    """确认 SDK 会话在我们手里,不在就重试着抢(§7.1)。

    **没有上装的机器上这一条几乎必然一次就过。** 留着重试是因为:
    厂商自己的进程、上一轮没退干净的我们自己,都可能在开机那一刻短暂握着;
    更要紧的是客户哪天自己加装了上装,而那一天不该需要我们改软件发新版。

    ``sleep`` 可注入,套件里绝不真等(§8.5)。
    """
    slumber = sleep if sleep is not None else asyncio.sleep
    last = ""
    for n in range(1, max(1, tries) + 1):
        try:
            if await device.has_control():
                return CheckResult("control", True, f"SDK 会话在手里(第 {n} 次确认)")
            await device.acquire_control()
            # **抢完立刻自己确认一次,不把确认推给下一圈。**
            #
            # 推给下一圈的写法在**最后一圈**必错:第 ``tries`` 圈没有下一圈,
            # 那一圈真抢到的会话没人认,函数报"试了 N 次也没拿到"。这一项
            # 是 ``run_postcheck`` 四项之一,``postcheck_verdict`` 四项全过
            # 才 KEEP —— 于是一次**已经成功**的升级被自动退回去,理由还是
            # 一句与事实相反的话。``tries=1`` 时更直白:一次成功的 acquire
            # 也必然报失败。
            #
            # 这里不多睡一轮:确认就在本圈内做完,下面那句 ``slumber`` 只
            # 在"这一圈没拿到、而且还有下一圈"时才走到。
            if await device.has_control():
                return CheckResult("control", True, f"SDK 会话在手里(第 {n} 次抢到)")
        except _EXPECTED_BACKEND_ERRORS as exc:
            # 后端自己说的人话(「还没连上旁路进程」之类),不配 traceback:
            # 真机上这里每次点自检就往 journal 里灌三段栈,说的却是同一句话
            # (W01c)。不认识的错才是 bug 的线索,栈留在下面那个分支里。
            log.info("抢 SDK 会话第 %d 次没成: %s", n, exc)
            last = str(exc)
        except Exception as exc:
            # **必须兜住一切**:``has_control``/``acquire_control`` 是厂商 SDK,
            # 抛什么全看它心情,写不出一张穷尽的类型表;而这一项炸掉只意味着
            # "这一次没抢到",下一圈还要接着抢。认识的那几类(连接、超时、
            # 被拒、C 扩展翻上来的 OSError)已经在上面那个分支里按人话记了;
            # 落到这里的才是意料之外的,带 ``exc_info`` 留证据。``last`` 里
            # 只剩 ``str(exc)``,现场翻日志要看的是卡在哪一步。
            log.warning("抢 SDK 会话第 %d 次炸了,按这一次没抢到处理", n,
                        exc_info=True)
            last = str(exc)
        else:
            # 抢没抢到和会话到没到手是两件事:``acquire_control()`` 不抛异常
            # 不等于会话就在手里(厂商的实现可以是"排队申请")。这句话要能
            # 跟上面 ``except`` 那句区分得开,现场翻日志才知道卡在哪一步。
            last = "抢是抢过了,回头确认会话还不在手里"
        if n < max(1, tries):
            await slumber(wait_s)
    log.warning("试了 %d 次也没拿到 SDK 会话: %s", max(1, tries), last)
    return CheckResult(
        "control", False,
        f"试了 {max(1, tries)} 次也没拿到 SDK 会话: {last}"
        f" —— 装了上装的机器上这一条是唯一真正会失败的那条")


async def run_postcheck(
    nav: Any, device: Any, *, want_sn: str, got_sn: str,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    tries: int = MAX_GRAB_TRIES,
) -> tuple[CheckResult, ...]:
    """重启后那四项(§7.2)。

    **一项炸掉不许掀掉另外三项** —— 现场要的是「哪几项没过」,不是一个
    traceback;而且异常本身就是那一项没过的理由。这跟 ``preflight._guard``
    是同一条纪律,只是这里的后果更重:掀掉整份报告 = 判据跑不全 =
    ``postcheck_verdict`` 判回滚,一次本来会成功的升级就白退了。
    """
    process = CheckResult("process", True, "进程起来了 —— 这行代码本身就是证据")

    control = await grab_control(device, tries=tries, sleep=sleep)

    bridges = await _check_bridges(nav, device, sleep=sleep)

    identity_ok = bool(want_sn) and want_sn == got_sn
    identity = CheckResult(
        "identity", identity_ok,
        f"SN 还是 {got_sn}" if identity_ok
        else f"升级前是 {want_sn or '(空)'},现在是 {got_sn or '(空)'}"
             f" —— 这台机器上跑的不是我们以为的那只狗")

    return (process, control, bridges, identity)


async def _check_bridges(
    nav: Any, device: Any, *, tries: int = MAX_BRIDGE_TRIES,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    wait_s: float = BRIDGE_WAIT_S,
) -> CheckResult:
    """相机、位姿、地图三个桥都能应答吗。**有界重试。**

    三个一起判成一项,是因为它们的坏法是同一种:桥没起来。分成三项只会让
    ``POST_CHECKS`` 变长,而判据那边的结论一模一样。

    重试的次数、间隔、以及为什么这一层非有宽限不可,见 ``MAX_BRIDGE_TRIES``
    上面那段。形状照 ``grab_control``:次数与间隔是常量,``sleep`` 可注入,
    默认走真 sleep,套件里注入假的(§8.5)。**探到一次全通就立刻返回**,
    一秒都不多等 —— 绝大多数启动都会走这一条。
    """
    slumber = sleep if sleep is not None else asyncio.sleep
    bad: list[str] = []
    for n in range(1, max(1, tries) + 1):
        bad = await _probe_bridges(nav, device)
        if not bad:
            return CheckResult(
                "bridges", True, f"位姿、地图、设备三个桥都应答了(第 {n} 次探测)")
        if n < max(1, tries):
            await slumber(wait_s)
    log.warning("探了 %d 次,这几个桥还是不应答: %s", max(1, tries), "、".join(bad))
    return CheckResult(
        "bridges", False,
        f"探了 {max(1, tries)} 次,这几个桥还是不应答: " + "、".join(bad))


async def _probe_bridges(nav: Any, device: Any) -> list[str]:
    """探一遍三个桥,回没应答的那几个。**一项炸掉不许掀掉另外两项。**"""
    bad: list[str] = []
    # 三处都必须兜住一切:桥不应答的表现形式由厂商 SDK 决定,写不出一张穷尽
    # 的类型表,而漏掉的那一类会掀掉另外两项 —— 正是这个函数存在的理由。
    # 分两档记:认识的那几类(``_EXPECTED_BACKEND_ERRORS``:连接、超时、被拒、
    # C 扩展翻上来的 OSError —— 故意收得这么宽,桥探测里 OSError 的任何子类都
    # 只是"没应答")一行人话不带栈;意料之外的才带 ``exc_info`` 留证据(W01c)。
    # ``bad`` 里只装一句 ``str(exc)`` 给现场看。
    try:
        if await nav.loc_status() is None:
            bad.append("位姿")
    except _EXPECTED_BACKEND_ERRORS as exc:
        log.info("探位姿桥没应答: %s", exc)
        bad.append(f"位姿({exc})")
    except Exception as exc:
        log.warning("探位姿桥炸了,按不应答处理", exc_info=True)
        bad.append(f"位姿({exc})")
    try:
        await nav.list_maps()
    except _EXPECTED_BACKEND_ERRORS as exc:
        log.info("探地图桥没应答: %s", exc)
        bad.append(f"地图({exc})")
    except Exception as exc:
        log.warning("探地图桥炸了,按不应答处理", exc_info=True)
        bad.append(f"地图({exc})")
    try:
        await device.has_control()
    except _EXPECTED_BACKEND_ERRORS as exc:
        log.info("探相机/设备桥没应答: %s", exc)
        bad.append(f"相机/设备({exc})")
    except Exception as exc:
        log.warning("探相机/设备桥炸了,按不应答处理", exc_info=True)
        bad.append(f"相机/设备({exc})")
    return bad


#: systemd 里我们那个单元叫什么。
SERVICE_UNIT = "d1max-patrol"


@dataclass(frozen=True, slots=True)
class RestartPlan:
    """该怎么重启。**这个模块只出方案,不执行** —— 执行在 app 那一层。

    engine 里起子进程是条坏边:那会让每一条走到这儿的测试都有机会真去动
    systemd。方案是数据,数据好测;执行是副作用,副作用注入进来。
    """

    #: ``service`` 只重启我们的服务;``machine`` 整机重启。
    kind: str
    argv: tuple[str, ...]
    why: str

    def to_wire(self) -> dict[str, Any]:
        return {"kind": self.kind, "argv": list(self.argv), "why": self.why}


def restart_plan(*, has_payload: bool, recorded: bool = True,
                 unit: str = SERVICE_UNIT) -> RestartPlan:
    """按「有没有上装」定重启粒度(§7.1)。**纯函数** —— 只算方案,不执行。

    装了上装就得整机重启:重启我们自己的进程等于放掉 SDK 会话,而放掉之后
    **不一定抢得回来** —— 上装会在开机窗口里把它拿走,而那个窗口只有整机
    重启才会再来一次。

    **``recorded`` 不能省,也不能跟 ``has_payload`` 合成一位。**
    ``Payload.has`` 在「没记过」时是 ``False``,跟「记过,确认没装上装」
    长得一模一样。只收 ``has_payload`` 一位的话,一台**真装了上装但还没
    登记**的机器会走上「只重启服务」那条路 —— 放掉 SDK 会话,然后要跟上装
    抢开机窗口才拿得回来(§7.1)。``app/identity.py`` 的 ``read_payload``
    docstring 一字不差地点过这个坏法,而知识原来就丢在这个函数的入参上。
    没记过时按整机重启走:**整机重启从不会永久丢会话,不知道的时候站在
    安全那一侧。**
    """
    if not recorded:
        return RestartPlan(
            "machine", ("systemctl", "reboot"),
            "这台有没有装上装还没记过:整机重启从不会永久丢 SDK 会话,而只重启"
            "服务在装了上装的机器上会 —— 不知道的时候站在安全那一侧")
    if has_payload:
        return RestartPlan(
            "machine", ("systemctl", "reboot"),
            "这台装了上装:只重启服务会放掉 SDK 会话,而放掉之后要跟上装抢"
            "开机窗口才拿得回来")
    return RestartPlan(
        "service", ("systemctl", "restart", unit),
        "这台没装上装:没有竞争者,重启服务就够了")
