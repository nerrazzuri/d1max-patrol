"""备份盘:把归档**只增不删**地同步到一块认得出身份的盘上。

**两种盘挡的是两种事故,谁也代替不了谁**(spec §7.6):

- **镜像盘**挡的是盘坏了、盘被误删、盘被清扫器扫掉了。它装在机器内部,
  没人碰,不依赖任何人记得插 —— 所以它是唯一一种"平时一直在起作用"的备份。
- **交付盘**挡的是**整只狗**丢了、烧了、摔了。人来的时候插一下,拷走,拔掉。
  它依赖人,所以它会漏;但**镜像盘再可靠也挡不住这一种,因为它跟狗在一起。**

**这个模块不碰引擎状态,也不 import app。** 狗自己的 SN 由调用方传进来:
``app/identity.py`` 在 app 层,engine 反过来 import 它就破了分层,而破了分层
之后这个模块就没法在没有 app 的地方被测 —— 备份恰恰是最需要离机测的一层。

**盘上的布局:**

- ``.d1max-backup/target.json`` —— 这块盘是谁的、是什么角色。**第 1 卷的
  ``removable.read_role`` 读的就是它**,本模块是它的写方。装机时写一次。
- ``.d1max-backup/state.json`` —— 上次同步到哪儿了。每次同步都写。
- ``runs/<任务名>/<时间戳>/`` —— 归档本体,跟狗上的 ``runs_root`` 一个形状。

**为什么身份和进度分成两个文件:** 身份写一次就不该再动。跟每次同步都要重写
的进度放在一个文件里,等于让"每天写几十次"的那支笔去碰"写错一次就认不出
这块盘"的那行字。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_patrol.engine.removable import MARKER_REL, DiskRole
from d1max_patrol.engine.retention import unique_tmp

#: 盘上记同步进度的文件。跟 :data:`~d1max_patrol.engine.removable.MARKER_REL`
#: 挨着放,同一个隐藏目录里。
STATE_REL = ".d1max-backup/state.json"

#: 归档在备份盘上落在哪一层。**不直接落在盘根上** —— 盘根上还可能有客户自己
#: 的东西,而"只增不删"意味着我们永远不会去清理盘根;圈进一个目录里,至少人
#: 一眼看得出哪些是我们写的。
RUNS_DIR_NAME = "runs"

#: 可以主动写到盘上的角色。``UNKNOWN`` 是"读不出来"的**结论**,不是一个可以
#: 写下去的角色 —— 写得下去的话,盘上就会出现一块"明确地不知道自己是什么"的
#: 盘,而下游没有一处分得清它和一块没初始化的盘。
WRITABLE_ROLES = (DiskRole.MIRROR, DiskRole.TRANSFER)


class BackupError(Exception):
    """备份盘上的操作没能做成。**每一条都要说清楚为什么** —— 这些话会原样
    出现在手机上,而看到它的人手里正拿着一块盘。"""


@dataclass(frozen=True, slots=True)
class Target:
    """一块**已经初始化过**的备份盘。"""

    mount: Path
    role: DiskRole
    sn: str
    label: str = ""
    created_at_ms: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "mount": self.mount.as_posix(),
            "role": self.role.value,
            "sn": self.sn,
            "label": self.label,
            "created_at_ms": self.created_at_ms,
        }


def marker_path(mount: Path | str) -> Path:
    """标记文件的位置。**路径来自第 1 卷的常量,不在这里重写一遍。**"""
    return Path(mount) / MARKER_REL


def state_path(mount: Path | str) -> Path:
    """同步进度文件的位置。"""
    return Path(mount) / STATE_REL


def _atomic_json(target: Path, payload: dict[str, Any]) -> None:
    """原子写一个 JSON。**先序列化,再落临时文件,再改名。**

    临时名用 ``retention.unique_tmp`` 而不是固定的 ``.tmp``:备份跑在后台线程
    里,盘况页和接口都在 HTTP 线程里,两边撞上同一个固定名字的那次,先改名的
    那个会把另一个写了一半的内容改成正式文件。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = unique_tmp(target)
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def read_marker(mount: Path | str) -> Target | None:
    """读盘上的标记。**读不出来回 ``None``,不抛。**

    抛出去的那一边,一块坏盘会把整趟"认盘"炸掉 —— 连边上那几块好盘都列不
    出来,而人正等着看那份列表决定往哪块盘上拷。
    """
    mount = Path(mount)
    try:
        raw = json.loads(marker_path(mount).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        role = DiskRole(raw.get("role"))
    except ValueError:
        return None
    sn = raw.get("sn", "")
    label = raw.get("label", "")
    created = raw.get("created_at_ms", 0)
    ok_created = isinstance(created, int) and not isinstance(created, bool)
    return Target(
        mount=mount,
        role=role,
        sn=sn if isinstance(sn, str) else "",
        label=label if isinstance(label, str) else "",
        created_at_ms=created if ok_created else 0,
    )


def init_target(mount: Path | str, *, robot_sn: str, role: DiskRole,
                label: str = "", now_ms: int) -> Target:
    """把一块盘认成这台狗的备份盘。**装机时做一次。**

    **盘上已经有别的狗的标记就拒绝,绝不覆盖**(spec §7.6)。覆盖掉的那一边
    最坏:两只狗的归档写到同一块盘上,目录名撞不上所以谁也不报错,直到有人去
    查那块盘上到底是谁的数据 —— 而那通常是出了事之后。
    """
    mount = Path(mount)
    if role not in WRITABLE_ROLES:
        raise BackupError(
            f"不能把盘标成 {role.value!r}: 'unknown' 是读不出来的结论,不是一个"
            f"能写下去的角色。要么镜像盘(mirror),要么交付盘(transfer)")
    existing = read_marker(mount)
    if existing is not None and existing.sn and existing.sn != robot_sn:
        raise BackupError(
            f"这块盘是 {existing.sn} 的备份盘,这台狗是 {robot_sn} —— 拒绝初始化。"
            f"接着写会把两只狗的归档混到一块盘上,而且事后分不开。要给这台狗用,"
            f"先在别处把盘上的 .d1max-backup 目录清掉")
    target = Target(mount=mount, role=role, sn=robot_sn, label=label,
                    created_at_ms=now_ms)
    # 键名要跟 ``removable.read_role`` 认的一致: role、sn。多写的字段它会忽略。
    _atomic_json(marker_path(mount), target.to_wire())
    return target
