"""起飞前检查。主规范 §6.2 的那几条,一次全查完。

**两条原则:**

1. **不许短路。** 每一项挨个查,前面挂了后面照查。现场最烦的是修好一个毛病
   再跑一遍又冒出下一个 —— 一次把话说完。
2. **不确定不放行。** 后端读不到状态时算这一项没过,而不是当成"好的"。
   "不知道有没有急停"和"确认没有急停"是两回事。
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path

from d1max_agent.engine.form import STANDALONE, Form
from d1max_agent.engine.homing import (
    DEFAULT_RETURN_PARAMS,
    HomePoint,
    ReturnParams,
    estimate_cost_pct,
    route_length_m,
)
from d1max_agent.engine.mission import Mission
from d1max_agent.engine.removable import DiskRole, Removable, blocks_takeoff
from d1max_agent.engine.storage import (
    STOP_USED_RATIO,
    WARN_USED_RATIO,
    storage_verdict,
)
from d1max_patrol.backends.base import DeviceBackend, NavBackend
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus

#: 预计耗电要乘的安全系数。**1.5 不是保守,是因为预计耗电本身不准**
#: (spec §1.2):地面摩擦、载重、温度、绕路,每一项都在往上抬。
#: 这就是唯一那层余量 —— 不要再叠第二层,叠了之后没人说得清哪层在起作用。
ESTIMATE_SLACK = 1.5

#: 归档目录至少要有这么多空间,MB。一次运行几十张照片加事件流。
MIN_FREE_MB = 500.0


log = logging.getLogger(__name__)


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


async def _check_home(mission: Mission, home: HomePoint | None) -> CheckResult:
    """原点必须标过,而且必须是这张图上的。

    原点同时是换电位、待命位和返航目标(spec §1.3)。没有它,返航就没有目标,
    出发线也算不出来。**一个别的图上的原点比没有原点更危险** —— 坐标在另一个
    坐标系里,狗不会拒绝,它会一声不吭地走到一个错地方。
    """
    if home is None:
        return CheckResult("home", False,
                           f"地图 {mission.map_id!r} 没标过原点,先去标一个换电位")
    if home.map_id != mission.map_id:
        return CheckResult("home", False,
                           f"原点记的是 {home.map_id!r},任务跑的是 "
                           f"{mission.map_id!r} —— 两个坐标系,不能混用")
    return CheckResult("home", True,
                       f"原点在 ({home.pose.position.x:.1f}, "
                       f"{home.pose.position.y:.1f})")


def departure_line_pct(mission: Mission, home: HomePoint, *,
                       slack: float = ESTIMATE_SLACK,
                       params: ReturnParams = DEFAULT_RETURN_PARAMS) -> float:
    """出发线:低于它就不出发(spec §1.2)。

    ::

        出发线 = 中止线 + 全程预计耗电 × slack

    **"返航储备"就是中止线本身。** ``route_length_m`` 算的全程已经含回原点
    那一段,回来的电已经在"预计耗电"里了;真正的储备是"走完全程回到家的
    那一刻手上还剩多少",而那个数就是中止线 —— 低于它狗本来就该趴下。

    这样这条线才解释得清:**出发时的电,够走完全程,回到家时还高于中止线。**

    **出发线必须同时高过返航线的两支。** 返航线是
    ``max(battery_return_pct, 中止线 + 回家成本)``(见 ``safety.return_line_pct``)
    —— 上面那条式子只压过了动态那一支,静态那一支(``battery_return_pct``)
    完全没进来。而 ``abort=25 / return=60`` 这组参数是过得了 ``Mission``
    校验的(它只查 ``abort <= return``):出发线算出来 29.5%,返航线 60%,
    30% 的电能起飞,第一帧电量遥测到达就该返航 —— **刚起飞就该回来。**
    所以这里再取一次 ``max``。
    """
    poses = [w.pose for w in mission.waypoints]
    cost = estimate_cost_pct(route_length_m(home.pose, poses), params)
    return max(mission.policy.battery_return_pct,
               mission.policy.battery_abort_pct + cost * slack)


async def _check_battery(device: DeviceBackend, mission: Mission,
                         home: HomePoint | None, slack: float,
                         params: ReturnParams) -> CheckResult:
    """电量要够走完全程、回到家时还高于中止线。"""
    pct = await device.battery()
    if home is None or home.map_id != mission.map_id:
        # 算不出线的时候不放行 —— 这是本模块开头那条"不确定不放行"。
        return CheckResult("battery", False,
                           f"电量 {pct:.1f}%,但算不出出发线:这张图的原点不可用"
                           f"(见 home 项)")
    line = departure_line_pct(mission, home, slack=slack, params=params)
    ok = pct > line
    return CheckResult("battery", ok,
                       f"电量 {pct:.1f}%,出发线 {line:.1f}%" if ok
                       else f"电量 {pct:.1f}%,不到出发线 {line:.1f}%"
                            f"(中止线 {mission.policy.battery_abort_pct:.0f}% + "
                            f"全程预计 × {slack:g},且不低于返航线的静态下限 "
                            f"{mission.policy.battery_return_pct:.0f}%)")


async def _check_storage(runs_root: Path, min_free_mb: float, form: Form,
                         last_upload_age_days: float | None) -> CheckResult:
    """真写一个探针文件再删掉。

    "目录存在"不等于"写得进去":只读挂载、权限不对、名字被一个同名文件占了
    —— 这些都要等到第一张照片存不下去才暴露,那时候狗已经在外面了。

    写得进去之后再看水位,两条门槛并列(``storage.storage_verdict``)。
    """
    root = Path(runs_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult("storage", False, f"归档目录 {root} 写不了: {exc}")
    usage = shutil.disk_usage(root)
    verdict = storage_verdict(
        free_mb=usage.free / (1024 * 1024),
        used_ratio=usage.used / usage.total if usage.total else 1.0,
        min_free_mb=min_free_mb,
        has_upload=form.has_upload,
        last_upload_age_days=last_upload_age_days,
    )
    if verdict.ok and verdict.warn:
        # 85% 是"过了,但该清盘了"。丢掉 warn 的话页面上这一项纯绿,
        # spec §4.7「80% 就要报警」在这一卷就没有出口 —— 人第一次知道盘要
        # 满,会是它满到 90% 拦停的那一天。告警通道是后面那份计划的事,
        # 这里能做的是把通过的这句话说重。
        return CheckResult("storage", True,
                           f"{verdict.detail} —— 已经过了 "
                           f"{WARN_USED_RATIO * 100:.0f}% 报警线,该清盘了;"
                           f"到 {STOP_USED_RATIO * 100:.0f}% 就不许出发")
    return CheckResult("storage", verdict.ok, verdict.detail)


async def _check_removable(disks: Sequence[Removable] | None,
                           robot_sn: str = "") -> CheckResult:
    """认到外插的取走盘就不许出发(spec §7.5),别的狗的镜像盘也拦
    (spec §7.6)。

    跟盘水位一样是个 start gate,狗自己看得见、自己拦。

    ``disks is None`` 的意思是**没扫过**,不是"没有盘" —— 这一项判没过。
    本模块开头那条"不确定不放行"对这一项同样成立:空元组 ``()`` 才是
    "扫过了,一块都没有"。两者混成一个默认值的话,调用方漏传参数就会白得
    一项绿的,而实际插着的那块盘要等引擎自己那道 preflight 才拦得住 ——
    操作员看到的是"七项全绿,然后狗自己中止了"。
    """
    if disks is None:
        return CheckResult("removable", False,
                           "没扫过外插盘,不放行 —— 认不出插着什么,"
                           "就不能说没插")
    blocking = blocks_takeoff(disks, robot_sn=robot_sn)
    if not blocking:
        return CheckResult("removable", True,
                           f"没有外插的取走盘(共认到 {len(disks)} 块)")
    # 挂载点永远是 Linux 路径(/media、/mnt),用 as_posix() 而不是 str():
    # 后者在 Windows 开发机上跑测试时会把 "/media/u1" 印成 "\media\u1"。
    names = ", ".join(d.mount.as_posix() for d in blocking)
    # 别的狗的镜像盘要单独说一句:那块盘是装在机器内部的,照着"拔下来"这句
    # 话去找,人会在外面找一块根本不在外面的盘。它的病也不是杠杆,是串台。
    strangers = [d for d in blocking if d.role is DiskRole.MIRROR]
    if strangers:
        which = ", ".join(f"{d.mount.as_posix()}(SN {d.sn})" for d in strangers)
        return CheckResult("removable", False,
                           f"还插着盘: {names} —— 其中 {which} 是别的狗的"
                           f"镜像盘,这台狗是 {robot_sn};接着跑会把两只狗的"
                           f"数据写到一块盘上")
    return CheckResult("removable", False,
                       f"还插着盘: {names} —— 拔下来再出发。"
                       f"盘挂在走动的狗身上是个杠杆,先坏的是接口")


async def _guard(name: str, coro: Awaitable[CheckResult]) -> CheckResult:
    """某一项炸了就算这一项没过,不让它掀掉整份报告。

    现场要的是"还差哪几项",不是一个 traceback;异常本身就是这一项没过的理由。

    **七项一律走这里,没有例外。** ``_check_home`` ``_check_storage``
    ``_check_removable`` 里其实没有一处 await —— 它们写成 ``async`` 只为了
    能进这个保护圈。少一项没进来,就等于那一项炸了会掀掉另外六项的结论,
    而"哪几项是纯算的"这件事以后是会变的(``_check_home`` 早晚要去读盘上的
    原点),那时候没人会记得回来补这一行。宁可现在把这条边抹齐。
    """
    try:
        return await coro
    except Exception as exc:
        # **这里必须兜住一切**:异常来自厂商 SDK、文件系统、盘符探测,写不出
        # 一张穷尽的类型表,而漏掉的那一类会掀掉另外六项的结论 —— 恰恰是这个
        # 函数存在的理由。原来靠一句 ``noqa: BLE001`` 压着,而本项目的 ruff 配置
        # 写明 ``BLE`` 不许用 noqa 绕。改成带 ``exc_info`` 记一条日志:兜住但
        # 留证据。``CheckResult.detail`` 只有 ``str(exc)`` 一句,给现场的人看
        # 够用,要查"为什么这一项会炸"就得有 traceback。
        log.warning("起飞检查 %s 自己炸了,按这一项没过处理", name, exc_info=True)
        return CheckResult(name, False, str(exc))


async def run_preflight(nav: NavBackend, device: DeviceBackend,
                        mission: Mission, runs_root: Path,
                        *, home: HomePoint | None = None,
                        min_free_mb: float = MIN_FREE_MB,
                        estimate_slack: float = ESTIMATE_SLACK,
                        return_params: ReturnParams = DEFAULT_RETURN_PARAMS,
                        form: Form = STANDALONE,
                        last_upload_age_days: float | None = None,
                        removable: Sequence[Removable] | None = None,
                        robot_sn: str = "",
                        ) -> PreflightReport:
    """全项全查,顺序固定,**一项都不跳**。

    **拿不到的东西一律往"没过"的方向倒。** ``home=None`` 和
    ``removable=None`` 都是这个意思:调用方漏传一个参数,得到的是一项红的,
    不是一项白送的绿。两个默认值一个 fail-closed 一个 fail-open 的话,
    同一个函数里就有了两种哲学,而漏掉的那一处不会有任何测试红。
    """
    checks = [
        await _guard("nav_ready", _check_nav_ready(nav)),
        await _guard("device_ready", _check_device_ready(device)),
        await _guard("localized", _check_localized(nav)),
        await _guard("home", _check_home(mission, home)),
        await _guard("battery",
                     _check_battery(device, mission, home, estimate_slack,
                                    return_params)),
        await _guard("storage",
                     _check_storage(Path(runs_root), min_free_mb, form,
                                    last_upload_age_days)),
        await _guard("removable", _check_removable(removable, robot_sn)),
    ]
    return PreflightReport(tuple(checks))
