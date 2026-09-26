"""升级前检查(W00c6d,决策 9 P0)。老服务的 ``selfcheck`` 升级前七项随 W00c5e 退役;这里是它在代理上
的样子。

**取值和判断分开**(老 ``selfcheck`` 的做法):运行时把事实取好放进 :class:`PrecheckInputs`,
:func:`precheck` 是纯函数,同样的输入永远同样的清单。切版本(``release_activate``)用**同一份**判 ——
:func:`first_block` 给第一个拦住的项的原因码,码跟以前切版本回的一样;站点点「检查」
(``release_precheck``)拿到的是整份清单。

老七项各归谁见设计稿 ``docs/superpowers/specs/2026-09-26-W00c6d-release-precheck-design.md``:任务包
schema 与备份归站点(站点合进清单),上装那一项不再单列(没有自动升级;重启只重启代理,SDK 会话由旁路
进程握着)。

**定位器、感知、外参**(W08 追加):现在没有这些进程 —— 清单里写「还没部署」、不拦。W09 定位器、
W11 感知接上之后,把各自的健康挂到运行时的 ``precheck_sources`` 上,不健康就拦。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 切版本的电量下限(W00c5d 定的;老服务 50% 是因为带上装要整机重启,现在只重启代理)。
MIN_BATTERY_PCT = 30.0

#: 由外部来源报健康的三项(W08):W09 定位器、W11 感知进程与外参自检。
SOURCE_ITEMS: tuple[tuple[str, str], ...] = (
    ("localizer", "定位器(W09)"),
    ("perception", "感知进程(W11)"),
    ("extrinsic", "外参自检(W11)"),
)


@dataclass(frozen=True)
class SourceCheck:
    """外部来源报的一项:定位器、感知、外参。"""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PrecheckInputs:
    """升级前检查要的全部事实。**全是取好的值,这里不做 I/O。**"""

    #: 要切到哪一版。
    name: str
    #: 在跑哪一版。
    current: str
    #: 这一版落了槽、venv 也建好了。
    installed: bool
    #: 槽里的包现在对不对:空串 = 对(没装的版本不核,也是空串)。
    package_error: str
    #: 切过去代理起得来(启动脚本在、有执行位;不是老服务那一代)。
    can_switch: bool
    #: 双槽根与下载目录所在的盘够写在途标记、幂等记录、事件簿。
    disk_ok: bool
    #: 电量;``None`` = 读不到。
    battery_pct: float | None
    #: 在忙什么;空串 = 空闲。
    busy: str
    #: 外部来源报的健康(W09/W11 之后才有)。
    sources: tuple[SourceCheck, ...] = ()
    #: 站点当前任务包的 schema(站点在命令里给;老站点不给就是 ``None``,这一项不列)。
    mission_schema: int | None = None
    #: 槽里那一版自述要的任务包 schema;没装、读不到是 ``None``。
    requires_mission_schema: int | None = None
    #: 开查的时候狗在忙:槽没核、来源没问(W00c6d 内审:握着命令锁,别让监护心跳等)。
    skipped: bool = False


@dataclass(frozen=True)
class PrecheckItem:
    name: str
    ok: bool
    #: 不过的话拦不拦切版本。
    blocking: bool
    detail: str
    #: 拦住时切版本回的原因码。
    code: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "blocking": self.blocking,
                "detail": self.detail}


def precheck(inputs: PrecheckInputs) -> tuple[PrecheckItem, ...]:
    """升级前那一遍。**顺序就是切版本判的先后**(原因码跟以前一样):现在合不合适(忙、电、盘)在前,
    这一版本身(是不是在跑的、装没装、包对不对、起不起得来)在后,外部来源最后。"""
    i = inputs
    out: list[PrecheckItem] = []
    out.append(PrecheckItem("busy", not i.busy, True,
                            "空闲:不跑任务、不换图、不录包、不重建、不在装版本" if not i.busy
                            else f"{i.busy} —— 重启那几秒谁都停不了它,等它完", "busy"))
    if i.battery_pct is None:
        out.append(PrecheckItem("battery", False, True, "读不到电量(适配器还没收到第一帧)",
                                "battery_unknown"))
    else:
        ok = i.battery_pct >= MIN_BATTERY_PCT
        out.append(PrecheckItem("battery", ok, True,
                                f"电量 {i.battery_pct:.0f}%,门槛 {MIN_BATTERY_PCT:.0f}%"
                                + ("" if ok else " —— 切过去起不来还要再退、再起一次"),
                                "low_battery"))
    out.append(PrecheckItem("disk", i.disk_ok, True,
                            "盘够写在途标记、幂等记录、事件簿" if i.disk_ok
                            else "双槽所在的盘不够 —— 先清掉用不着的版本或数据", "storage_full"))
    same = i.name == i.current
    out.append(PrecheckItem("version", not same, True,
                            f"在跑 {i.current or '(不知道)'},要切到 {i.name}" if not same
                            else f"已经在跑 {i.name}", "already_running"))
    out.append(PrecheckItem("installed", i.installed, True,
                            "落了槽、venv 也建好了" if i.installed
                            else f"{i.name} 没装(或者 venv 没建成)—— 先装", "not_installed"))
    if not i.installed:
        out.append(PrecheckItem("package", False, True, "没装,没得核", "package_corrupt"))
    elif i.skipped:
        out.append(PrecheckItem("package", False, False, "狗在忙,槽里的包没核(空闲时再查)"))
    else:
        out.append(PrecheckItem("package", not i.package_error, True,
                                "槽里的包跟自述的指纹对得上" if not i.package_error
                                else f"{i.package_error} —— 重新装一遍", "package_corrupt"))
    if i.mission_schema is not None and i.requires_mission_schema is not None:
        # 没装(读不到自述)就不列:站点按自己的登记目录比的那一份补上。
        ok = i.requires_mission_schema <= i.mission_schema
        out.append(PrecheckItem("schema", ok, True,
                                f"这一版要任务包 schema ≥ {i.requires_mission_schema},站点"
                                f"当前任务包是 {i.mission_schema}"
                                + ("" if ok else " —— 先导入新格式的任务包"),
                                "schema_mismatch"))
    out.append(PrecheckItem("agent_start", i.can_switch, True,
                            "切过去代理起得来(启动脚本在)" if i.can_switch
                            else "切过去代理起不来(没有启动脚本、没有执行位,或者是老服务那一代)",
                            "no_agent_start"))
    labels = dict(SOURCE_ITEMS)
    got = {s.name for s in i.sources}
    for name, label in SOURCE_ITEMS:
        if name in got:
            continue
        if i.skipped:
            out.append(PrecheckItem(name, False, False, f"{label}:狗在忙,没问(空闲时再查)"))
        else:
            out.append(PrecheckItem(name, True, False,
                                    f"{label}还没部署,这一项现在不查(接上之后不健康就拦)"))
    # 接上来的来源一个不落地列出来(不认识的名字也列、也拦 —— 内审:以前被悄悄丢掉、照样切)。
    for s in i.sources:
        out.append(PrecheckItem(s.name, s.ok, True, f"{labels.get(s.name, s.name)}:{s.detail}",
                                s.name))
    return tuple(out)


def first_block(items: tuple[PrecheckItem, ...]) -> str:
    """第一个拦住切版本的项的原因码;都过是空串。"""
    for it in items:
        if not it.ok and it.blocking:
            return it.code
    return ""


def report(name: str, items: tuple[PrecheckItem, ...],
           requires_mission_schema: int | None = None) -> dict[str, Any]:
    """回执 ``data`` 里的那份清单(带上槽里那一版自述要的任务包 schema,站点显示用)。"""
    blocking = [it.name for it in items if not it.ok and it.blocking]
    return {"name": name, "ok": not blocking, "blocking": blocking,
            "checks": [it.to_wire() for it in items],
            "requires_mission_schema": requires_mission_schema}
