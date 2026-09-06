"""狗认到哪些外插盘,各自是什么角色,哪些拦起飞。

**认到取走盘就不许起飞**(spec §7.5)。凸出二十毫米的盘挂在走动的狗侧面是个
**杠杆** —— 先坏的不是盘,是**接口**,而接口撬坏了整个扩展仓的 USB 就废了,
那不可现场维修。取走盘凸出来无所谓,因为它只在"狗站着不动、人就在旁边"的
那几分钟里存在;这条门槛就是把"凸出来"从一个持续风险压缩成一个有人看着的
短窗口。

**角色写在盘上,不写在狗上。** 判断角色的依据必须跟着盘走 —— 换一台狗、
换一个挂载点,这块盘还是那块盘。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

#: 盘上的标记文件。带 SN 和 role,由备份那一层写(见后续计划)。
MARKER_REL = ".d1max-backup/target.json"

#: 默认去哪儿找挂载点。Linux 上自动挂载落在这两处。
DEFAULT_MOUNT_ROOTS: tuple[Path, ...] = (Path("/media"), Path("/mnt"))


class DiskRole(str, Enum):
    #: 装机时装进机器内部,不外露,没人碰。**不拦起飞。**
    MIRROR = "mirror"
    #: 人来的时候插一下,用完拔走。**拦起飞。**
    TRANSFER = "transfer"
    #: 没有标记文件,或者标记文件读不出来。**按取走盘拦。**
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Removable:
    mount: Path
    role: DiskRole
    sn: str = ""


def read_role(mount: Path | str) -> tuple[DiskRole, str]:
    """从盘上的标记文件读角色和 SN。

    **读不出来一律回 UNKNOWN,不猜。** 猜错的那一边是"把取走盘当成镜像盘",
    而那一边的代价是狗带着一根杠杆出门。
    """
    marker = Path(mount) / MARKER_REL
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError 这个大类接住的不只是 json.JSONDecodeError(它本来就是
        # ValueError 的子类):marker.read_text 遇到不是 UTF-8 的字节会抛
        # UnicodeDecodeError,同样是 ValueError 的子类。读不出来的原因不重要,
        # 一律按 UNKNOWN 处理——抛出去的话,这一炸发生在 run_preflight 的
        # _guard 外面(见 machine.py 里 scan() 那一步),会把整趟 preflight
        # 报告都掀翻,而不是让"removable"这一项干净地没过。
        return DiskRole.UNKNOWN, ""
    if not isinstance(raw, dict):
        return DiskRole.UNKNOWN, ""
    sn = raw.get("sn", "")
    sn = sn if isinstance(sn, str) else ""
    try:
        role = DiskRole(raw.get("role"))
    except ValueError:
        # 认不出的角色不是"新角色",是"这个文件我们读不懂"。
        return DiskRole.UNKNOWN, sn
    return role, sn


@runtime_checkable
class RemovableProbe(Protocol):
    async def scan(self) -> tuple[Removable, ...]:
        """现在认到哪些外插盘。"""
        ...


class MountRootProbe:
    """扫 ``/media`` ``/mnt`` 下面**真正是挂载点**的那些目录。

    "是不是挂载点"用 ``os.path.ismount`` 判,而这个判据**可以注入** ——
    临时目录下造不出真挂载点,注入是这一层能被离机测到的唯一办法(spec §8.6
    列的"仿真测不到的东西"里没有这一条,正是因为它被做成了可注入的)。
    """

    def __init__(self, roots: Sequence[Path] = DEFAULT_MOUNT_ROOTS, *,
                 is_mount: Callable[[Path], bool] = os.path.ismount) -> None:
        self._roots = tuple(Path(r) for r in roots)
        self._is_mount = is_mount

    async def scan(self) -> tuple[Removable, ...]:
        found: list[Removable] = []
        for root in self._roots:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir() or not self._is_mount(entry):
                    continue
                role, sn = read_role(entry)
                found.append(Removable(mount=entry, role=role, sn=sn))
        return tuple(found)


#: 默认探针,给 ``MissionEngine`` 和 ``_make_engine`` 当默认值用。
#: ``MountRootProbe`` 无状态,进程里共享一个实例没问题;写成模块级单例而不是
#: 在每处签名里现造一个 ``MountRootProbe()``,也是为了不撞 ruff B008
#: (默认参数里不许调函数)。
DEFAULT_PROBE = MountRootProbe()


def blocks_takeoff(disks: Sequence[Removable]) -> tuple[Removable, ...]:
    """哪些盘拦起飞。**镜像盘不拦,别的都拦。**"""
    return tuple(d for d in disks if d.role is not DiskRole.MIRROR)
