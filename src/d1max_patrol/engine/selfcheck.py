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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_patrol.engine.preflight import CheckResult

#: 升级期间的电量下限。整机重启途中断电是最脏的一种坏法(§7.2)。
#: 不带上装时不整机重启,这条门槛照样保留 —— 升级期间不该同时在换电池。
MIN_UPGRADE_BATTERY_PCT = 50.0

#: 除了新版本本身,还要留出这么多余量。装得下不等于装完还能动 ——
#: 日志、临时文件、下一次升级的落槽位都在同一块盘上。
UPGRADE_FREE_MARGIN_MB = 1024.0

#: 多久没备份就该提示了。**提示,不阻断**(§7.5)。
BACKUP_NAG_DAYS = 14.0


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
    BLOCKING: tuple[str, ...] = ("package", "schema", "disk", "battery",
                                 "busy", "payload")

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
