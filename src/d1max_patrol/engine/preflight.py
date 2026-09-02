"""起飞前检查。主规范 §6.2 的五条,一次全查完。

**两条原则:**

1. **不许短路。** 五项挨个查,前面挂了后面照查。现场最烦的是修好一个毛病
   再跑一遍又冒出下一个 —— 一次把话说完。
2. **不确定不放行。** 后端读不到状态时算这一项没过,而不是当成"好的"。
   "不知道有没有急停"和"确认没有急停"是两回事。
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path

from d1max_patrol.backends.base import DeviceBackend, NavBackend
from d1max_patrol.engine.mission import Mission
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus

#: 电量要高出返航线这么多才让起飞。
#:
#: 刚好卡在返航线上就出发,等于第一个点还没走到就该返航了。10 个百分点是
#: 实测里一趟短巡检的量级(清单 #43: 一天能从 71% 掉到 17%)。
BATTERY_MARGIN_PCT = 10.0

#: 归档目录至少要有这么多空间,MB。一次运行几十张照片加事件流。
MIN_FREE_MB = 500.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    """一项检查的结论。``detail`` 是给人看的,失败时必须说清楚当前是什么状态。"""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.ok)


async def _check_nav_ready(nav: NavBackend) -> CheckResult:
    """导航必须在 StandBy —— 只有这个状态下 start_nav 才会被受理(§3.6)。"""
    status = await nav.nav_status()
    if status is None:
        # 端口约定:认不出的枚举值回 None(固件可能新增)。认不出就不放行。
        return CheckResult("nav_ready", False, "导航状态认不出来,不敢放行")
    ok = status is NavStatus.STANDBY
    return CheckResult("nav_ready", ok,
                       "导航就绪" if ok else f"导航当前是 {status.value},不是 StandBy")


async def _check_device_ready(device: DeviceBackend) -> CheckResult:
    """控制权在手 + 急停没按下。

    这两条都读不到就说明 bridge 不通,异常会被 ``run_preflight`` 收成失败。
    FatalError 级故障不在这里轮询 —— 故障是推上来的 ``FaultEvent``,由安全
    规则表处置(设计 spec §6.4);起飞这一刻拿不到"当前故障列表"这种东西。
    """
    if not await device.has_control():
        return CheckResult("device_ready", False, "控制权不在手里,发出去的指令不会被执行")
    if await device.emergency():
        return CheckResult("device_ready", False, "急停处于按下状态")
    return CheckResult("device_ready", True, "控制权在手,急停未触发")


async def _check_localized(nav: NavBackend) -> CheckResult:
    """定位必须收敛到 ContinuousLoc(§4.3)。没收敛就走,走的是错的地方。"""
    status = await nav.loc_status()
    if status is None:
        return CheckResult("localized", False, "定位状态认不出来,不敢放行")
    ok = status is LocStatus.CONTINUOUS_LOC
    return CheckResult("localized", ok,
                       "定位已收敛" if ok
                       else f"定位当前是 {status.value},不是 ContinuousLoc")


async def _check_battery(device: DeviceBackend, mission: Mission,
                         margin_pct: float) -> CheckResult:
    """电量要高出返航线一截,不能刚好卡在线上。"""
    pct = await device.battery()
    floor = mission.policy.battery_return_pct + margin_pct
    ok = pct > floor
    return CheckResult("battery", ok,
                       f"电量 {pct:.1f}%" if ok
                       else f"电量 {pct:.1f}%,不到返航线 "
                            f"{mission.policy.battery_return_pct:.1f}% 加 "
                            f"{margin_pct:.1f}% 余量")


def _check_storage(runs_root: Path, min_free_mb: float) -> CheckResult:
    """真写一个探针文件再删掉。

    "目录存在"不等于"写得进去":只读挂载、权限不对、名字被一个同名文件占了
    —— 这些都要等到第一张照片存不下去才暴露,那时候狗已经在外面了。
    """
    root = Path(runs_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult("storage", False, f"归档目录 {root} 写不了: {exc}")
    free_mb = shutil.disk_usage(root).free / (1024 * 1024)
    ok = free_mb >= min_free_mb
    return CheckResult("storage", ok,
                       f"剩余 {free_mb:.0f}MB" if ok
                       else f"剩余 {free_mb:.0f}MB,不足 {min_free_mb:.0f}MB")


async def _guard(name: str, coro: Awaitable[CheckResult]) -> CheckResult:
    """某一项炸了就算这一项没过,不让它掀掉整份报告。

    现场要的是"还差哪几项",不是一个 traceback;异常本身就是这一项没过的理由。
    """
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - 理由见上,任何异常都只是一项没过
        return CheckResult(name, False, str(exc))


async def run_preflight(nav: NavBackend, device: DeviceBackend,
                        mission: Mission, runs_root: Path,
                        *, min_free_mb: float = MIN_FREE_MB,
                        battery_margin_pct: float = BATTERY_MARGIN_PCT,
                        ) -> PreflightReport:
    """五项全查,顺序固定,**一项都不跳**。"""
    checks = [
        await _guard("nav_ready", _check_nav_ready(nav)),
        await _guard("device_ready", _check_device_ready(device)),
        await _guard("localized", _check_localized(nav)),
        await _guard("battery", _check_battery(device, mission, battery_margin_pct)),
    ]
    try:
        checks.append(_check_storage(Path(runs_root), min_free_mb))
    except Exception as exc:  # noqa: BLE001 - 同 _guard
        checks.append(CheckResult("storage", False, str(exc)))
    return PreflightReport(tuple(checks))
