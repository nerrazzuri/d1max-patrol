"""备份盘:把归档**只增不删**地同步到一块认得出身份的盘上。

**两种盘挡的是两种事故,谁也代替不了谁**(spec §7.6):

- **镜像盘**挡的是盘坏了、盘被误删、盘被清扫器扫掉了。它装在机器内部,
  没人碰,不依赖任何人记得插。**"一直在起作用"不是它自己做到的** ——
  这个模块只提供动作,真正让它按时跑起来的是 app 那一层一条每分钟醒一次的
  后台协程(``app/server.py`` 的 ``AppServer._autosync_once``):它只碰镜像盘,
  引擎在跑的时候整趟让路。没有那条协程,这里写的每一行都要等人点一下才会跑。
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
import os
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from d1max_patrol.engine.export import sha256_file
from d1max_patrol.engine.removable import MARKER_REL, NO_IDENTITY, DiskRole, Removable
from d1max_patrol.engine.retention import MIN_NOTICE_DAYS, RunInfo, run_key, scan_runs, unique_tmp

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
    """原子写一个 JSON。**先序列化,fsync,再改名。**

    临时名用 ``retention.unique_tmp`` 而不是固定的 ``.tmp``:备份跑在后台线程
    里,盘况页和接口都在 HTTP 线程里,两边撞上同一个固定名字的那次,先改名的
    那个会把另一个写了一半的内容改成正式文件。

    **``replace`` 一个人兑现不了原子性。** ``write_text`` 返回时数据只到了
    页缓存;``rename`` 在 ext4 上是有序的,但"有序"保证的是改名不早于写入落盘,
    **不保证两者都落了盘**。而这两个文件的工况恰恰是断电:热插拔换电池是这台
    机器的日常操作,拔盘更是天天在发生。写坏了的 ``state.json`` 意味着整块盘
    的同步进度归零(``read_state`` 读不出来就回 ``EMPTY_STATE``),下次同步把
    几十 GB 重拷一遍;写坏了的 ``target.json`` 意味着这块盘不再被认出来 ——
    ``read_role`` 回 UNKNOWN,于是它开始拦起飞。写法照 ``homing.save_home``。

    只 fsync 文件、不 fsync 父目录:目录项没落盘的最坏结果是"改名丢了,还剩
    上一份完整的",而上一份是完整的。半个 JSON 才是要防的那一种。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = unique_tmp(target)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
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
    try:
        _atomic_json(marker_path(mount), target.to_wire())
    except OSError as exc:
        # **包成 BackupError。** 这一句是这个函数唯一会漏出裸 ``OSError`` 的
        # 地方,而它的调用方 ``app/server.py`` 的 ``_backup_init`` 只接
        # ``BackupError`` —— 一块写保护的盘(相机的 SD 卡带物理锁片、只读挂载
        # 的 U 盘)于是变成一个 500,人拿着盘站在狗边上,屏上是"服务器内部错误"。
        raise BackupError(
            f"这块盘写不进去({exc})—— 盘可能是只读的(有些盘身上有写保护的"
            f"锁片),也可能挂载的时候就是只读的,或者盘已经满了") from exc
    return target


@dataclass(frozen=True, slots=True)
class TargetStatus:
    """一块盘现在能不能拿来备份,以及**为什么**。

    ``detail`` 会原样出现在手机上。写"不可用"没有用 —— 人手里正拿着这块盘,
    他要知道的是"这是别人的盘"还是"这块盘还没认过"。
    """

    mount: Path
    role: DiskRole
    sn: str
    label: str
    usable: bool
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "mount": self.mount.as_posix(),
            "role": self.role.value,
            "sn": self.sn,
            "label": self.label,
            "usable": self.usable,
            "detail": self.detail,
        }


def resolve_targets(disks: Sequence[Removable],
                    *, robot_sn: str = "") -> tuple[TargetStatus, ...]:
    """把扫到的外插盘筛成"能不能往上面备份"。**顺序不变。**

    **认盘只认标记文件,绝不"看见一块盘就往上写"**(spec §7.6)。客户完全
    可能插一张相机卡进来 —— 默默往上面写归档是我们能干出的最没礼貌的一件事,
    而且事后没人分得清那些目录是谁建的。

    **这里判的是"能不能备份",不是"拦不拦起飞"。** 同一块交付盘两个结论
    相反:拿来备份是可以的(人就是插它来拷数据的),同时它拦起飞(见
    ``removable.blocks_takeoff``)。合成一个布尔值的那天,要么交付盘拷不了
    数据,要么狗带着一根杠杆出门。
    """
    out: list[TargetStatus] = []
    for disk in disks:
        mount = Path(disk.mount)
        marker = read_marker(mount)
        if marker is None:
            out.append(TargetStatus(
                mount=mount, role=DiskRole.UNKNOWN, sn="", label="",
                usable=False,
                detail="这块盘还没初始化成备份盘 —— 它可能是客户自己的存储卡,"
                       "在初始化之前一个字节都不会往上面写。要用它备份,"
                       "先在这一页上初始化"))
            continue
        # 两边都认得出身份、而且对不上,才拦。有一边是 unknown 就不拿 SN 去卡:
        # ``app/identity.py`` 查不到机身 SN 时明写 "unknown",拿它去比,一只
        # 读不出身份的狗会被拦得连自己的镜像盘都用不了。
        known = marker.sn not in NO_IDENTITY and robot_sn not in NO_IDENTITY
        if known and marker.sn != robot_sn:
            out.append(TargetStatus(
                mount=mount, role=marker.role, sn=marker.sn, label=marker.label,
                usable=False,
                detail=f"这块盘是 {marker.sn} 的备份盘,这台狗是 {robot_sn} —— "
                       f"不会往上面写。两只狗的归档混在一块盘上事后分不开;"
                       f"确实要改用途,先在别处清掉盘上的 .d1max-backup 目录"))
            continue
        out.append(TargetStatus(
            mount=mount, role=marker.role, sn=marker.sn, label=marker.label,
            usable=True, detail=""))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class SyncState:
    """这块盘上的同步进度。**记在盘上,不记在狗上。**

    盘会被拔走插到另一台机器上,狗会被重装系统 —— 进度必须跟着数据走。记在
    狗上的那一边,重装一次系统之后狗以为一趟都没同步过,于是把整盘归档重拷
    一遍;只增不删所以不毁数据,但一块 2T 的盘要白拷几个小时,而且每次重装都
    会再来一次。
    """

    robot_sn: str = ""
    last_sync_ms: int = 0
    #: 已经拷完并核对过的归档,键是 ``retention.run_key`` —— ``任务名/目录名``。
    done: frozenset[str] = frozenset()

    def with_done(self, keys: Iterable[str], *, now_ms: int) -> SyncState:
        """记上新拷完的几趟。**并集,不覆盖。**"""
        return SyncState(robot_sn=self.robot_sn, last_sync_ms=now_ms,
                         done=self.done | frozenset(keys))

    def to_wire(self) -> dict[str, Any]:
        return {
            "robot_sn": self.robot_sn,
            "last_sync_ms": self.last_sync_ms,
            # 排序是为了让盘上那个文件在两次同步之间可比 —— 集合的迭代顺序
            # 每个进程都不一样,不排的话每次写出来的文件都"变了"。
            "done": sorted(self.done),
        }

    @classmethod
    def from_wire(cls, raw: Any) -> SyncState:
        """从盘上那份 JSON 拼回来。**拼不出来的字段一律取默认值,不抛。**"""
        if not isinstance(raw, dict):
            return EMPTY_STATE
        sn = raw.get("robot_sn", "")
        last = raw.get("last_sync_ms", 0)
        done = raw.get("done", [])
        ok_last = isinstance(last, int) and not isinstance(last, bool)
        return cls(
            robot_sn=sn if isinstance(sn, str) else "",
            last_sync_ms=last if ok_last else 0,
            done=frozenset(k for k in done if isinstance(k, str))
            if isinstance(done, list) else frozenset(),
        )


#: "这块盘上什么也没同步过"。模块级单例,避开 ruff B008(默认参数里不许调函数)。
EMPTY_STATE = SyncState()


def read_state(mount: Path | str, *, robot_sn: str = "") -> SyncState:
    """读盘上的同步进度。**读不出来、或者不是这台狗的,一律当成没同步过。**

    这个方向是安全的:当成没同步过,最坏是重拷一遍(只增不删,不毁任何东西);
    抛出去,备份就此彻底停摆 —— 而没有人会发现,因为备份本来就是那个"平时
    看不见它在不在工作"的东西。

    ``robot_sn`` 对不上的意思是:这块盘被重新初始化给了这台狗,但旧进度还在,
    那些键指的是另一只狗的归档。信它就会漏拷。
    """
    try:
        raw = json.loads(state_path(mount).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EMPTY_STATE
    state = SyncState.from_wire(raw)
    known = state.robot_sn not in NO_IDENTITY and robot_sn not in NO_IDENTITY
    if known and state.robot_sn != robot_sn:
        return EMPTY_STATE
    return state


def write_state(mount: Path | str, state: SyncState) -> None:
    """把进度写回盘上。原子写,理由同 :func:`_atomic_json`。"""
    _atomic_json(state_path(mount), state.to_wire())


#: 备份盘上要留出的余量。盘上除了归档还要写标记和进度文件,填到一个字节不剩
#: 的那一刻,连"我满了"这句话都记不下去。64 MiB 是一个不心疼的数。
FREE_MARGIN_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SyncItem:
    """要拷的一趟。"""

    key: str
    src: Path
    dest: Path
    size_bytes: int

    def to_wire(self) -> dict[str, Any]:
        return {"key": self.key, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """这一次要拷哪几趟。**这个结构里没有"要删什么"这个概念。**

    有那个字段,早晚有人往里面填东西 —— 而狗按水位删掉的那些,正是备份存在
    的全部理由(spec §7.6)。
    """

    mount: Path
    items: tuple[SyncItem, ...]
    #: 还在写、这次不碰的那几趟。
    skipped_unsettled: tuple[str, ...]
    #: 已经拷过的那几趟。
    already: tuple[str, ...]
    #: 落后多少趟 —— 所有还没同步的**安定**归档,装不装得下都算。
    behind: int
    behind_bytes: int
    free_bytes: int
    #: ``items`` 的总字节。盘装不下的时候它小于 ``behind_bytes``。
    need_bytes: int
    full: bool
    #: 这一次有什么额外情况要说。**空串 = 没有额外情况**,不是"缺失"、不是
    #: "还没算出来"。``full`` 为假时它就是空的 —— 调用方要判有没有话说,判
    #: ``if detail`` 就够了(``apply_sync`` 里拼失败那句话正是这么干的);
    #: 不要写成 ``detail or "..."`` 之外的任何"补一个默认值"的形状。
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "mount": self.mount.as_posix(),
            "items": [i.to_wire() for i in self.items],
            "skipped_unsettled": list(self.skipped_unsettled),
            "already": list(self.already),
            "behind": self.behind,
            "behind_bytes": self.behind_bytes,
            "free_bytes": self.free_bytes,
            "need_bytes": self.need_bytes,
            "full": self.full,
            "detail": self.detail,
        }


def _free_bytes(mount: Path) -> int:
    """盘上还剩多少。取不到就当 0 —— 当 0 的结果是"报满、什么也不拷",
    而当无穷大的结果是"拷到盘炸"。"""
    try:
        return shutil.disk_usage(mount).free
    except OSError:
        return 0


def plan_sync(runs_root: Path | str, mount: Path | str, *, robot_sn: str = "",
              now: datetime | None = None,
              free_bytes: int | None = None,
              runs: Sequence[RunInfo] | None = None) -> SyncPlan:
    """排一次同步。**只增不删,跳过正在写的,装不下就先拷能装下的。**

    **跳过正在写的**用第 2 卷的 ``RunInfo.settled``(有 summary,或者已经过了
    24 小时安定期),不另造一套判据。拷一趟还在写的归档,拿到的是半份 —— 而
    下次它安定了,我们已经把它记成拷过了,于是盘上**永远**是半份。

    **装不下的时候照拷能装下的那些**(从最老的开始)。只增不删的前提下,多拷
    一趟严格优于少拷一趟,而且不删掉任何东西;全停的那一边,一块只差 1% 就
    装满的盘会从此一趟也不再备份。最老在前是因为最老的那几趟离到期最近,
    也就是最快会被狗自己删掉的那几趟。

    ``free_bytes`` 是给测试注入用的。临时目录上量不出"盘还剩多少",而"装不下
    怎么办"恰恰是这个函数里最需要被穷举的那一段。

    ``runs`` 是给调用方手里已经有一份归档名单时用的:``scan_runs`` 每一趟都要
    ``rglob("*")`` 递归 stat 整棵目录树,调用方(比如 ``/api/storage``)如果
    在同一次请求里已经扫过一遍,这里不该再替它重扫一遍——这个函数挂在 HTTP
    请求路径上,归档一多,重扫的代价不是可以忽略的。不给就照旧自己扫。
    """
    mount = Path(mount)
    state = read_state(mount, robot_sn=robot_sn)
    free = free_bytes if free_bytes is not None else _free_bytes(mount)
    dest_root = mount / RUNS_DIR_NAME

    skipped: list[str] = []
    already: list[str] = []
    pending: list[SyncItem] = []
    # scan_runs 就是最老在前,不用再排;调用方传进来的 runs 沿用同一个约定。
    for info in (runs if runs is not None else scan_runs(runs_root, now=now)):
        key = run_key(info)
        if not info.settled:
            skipped.append(key)
            continue
        if key in state.done:
            # 拷过就不再拷,哪怕后来多了 .uploaded / .exported —— 那两个标记是
            # 狗这一侧的台账,不是数据;备份盘上少一个空文件不损失任何东西,而
            # 为它重拷一遍整趟要付出实打实的几百兆。
            already.append(key)
            continue
        pending.append(SyncItem(
            key=key, src=info.path,
            dest=dest_root / info.mission / info.path.name,
            size_bytes=info.size_bytes))

    behind_bytes = sum(i.size_bytes for i in pending)
    budget = max(0, free - FREE_MARGIN_BYTES)
    fit: list[SyncItem] = []
    used = 0
    for item in pending:
        if used + item.size_bytes > budget:
            break
        fit.append(item)
        used += item.size_bytes
    full = len(fit) < len(pending)

    if not full:
        detail = ""
    else:
        detail = (
            f"备份盘装不下了: 还差 {behind_bytes - used} 字节,盘上只剩 "
            f"{free} 字节。**不会删备份盘上的任何东西** —— 狗按水位删掉的那些,"
            f"正是这块盘存在的理由。这次先拷了排在最前面的 {len(fit)} 趟,"
            f"剩下 {len(pending) - len(fit)} 趟得换一块更大的盘,"
            f"或者先把这块盘上的归档取走")
    return SyncPlan(
        mount=mount, items=tuple(fit), skipped_unsettled=tuple(skipped),
        already=tuple(already), behind=len(pending), behind_bytes=behind_bytes,
        free_bytes=free, need_bytes=used, full=full, detail=detail)


def _copy_file(src: Path, dest: Path) -> None:
    """拷一个文件。**先落临时名,再改名。**

    改名在同一个文件系统里是原子的:半路断电或者拔盘,盘上要么是完整的那份,
    要么什么也没有 —— 而不是一个名字对、内容缺一半的文件。后者最坏,因为
    下一趟同步看它名字在、就不会再拷。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(dest)
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def copy_run(src: Path, dest: Path) -> tuple[int, int]:
    """把一趟归档整个拷过去。目录结构照搬。**回 (拷了几个文件, 拷了多少字节)。**

    **回的是真实计数,不是计划里那个数。** 计划是 ``plan_sync`` 排的时候量的,
    从那一刻到真开拷之间源目录可能已经变了(甚至整个没了)——拿计划里的数当
    战果记进账本,盘上零字节而账本是绿的。字节数在**拷之前**从源文件上取:
    拷完再量的是目标文件,而目标文件正是那个可能没写成的东西。
    """
    files = 0
    total = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        _copy_file(path, dest / path.relative_to(src))
        files += 1
        total += size
    return files, total


def verify_run(src: Path, dest: Path) -> str:
    """拷完重新算哈希核对。**一致回空串,不一致回一句话。**

    哈希用 ``export.sha256_file`` —— 它是流式的(1 MiB 一块)。归档里有几百兆
    的视频,``read_bytes()`` 一次读进内存会在 Orin NX 的 8G 上被 OOM killer
    打死,而且是在半夜没人看着的时候。

    **开头那一句 ``src.is_dir()`` 不能省。** ``Path.rglob`` 作用在一个不存在
    的目录上**返回空列表,不抛异常**(py3.10 实测)。源目录在排计划与开拷之间
    消失(并发的清盘、有人手动 rm、导出之后释放)时,下面那个循环一次都不进,
    函数回空串 —— 而空串的意思是"核对通过"。于是这一趟被记进 ``done``,
    "只增不删"保证它再也不会被重新考虑:**盘上零字节,账本是绿的**,接着清盘
    器把狗上那份删掉,两边都没了,而屏上写着有两份。
    """
    if not src.is_dir():
        return "源目录在拷贝期间消失了 —— 这一趟没有拷成"
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        other = dest / rel
        if not other.is_file():
            return f"{rel.as_posix()} 没拷过去"
        if sha256_file(path) != sha256_file(other):
            return f"{rel.as_posix()} 拷过去之后哈希对不上"
    return ""


@dataclass(frozen=True, slots=True)
class SyncResult:
    """这一次同步的结果。"""

    mount: Path
    copied: tuple[str, ...]
    #: ``(哪一趟, 为什么)``。**每一条都要说得出为什么** —— 一句"同步失败"
    #: 在现场等于没说。
    failed: tuple[tuple[str, str], ...]
    bytes_copied: int
    full: bool
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "mount": self.mount.as_posix(),
            "copied": list(self.copied),
            "failed": [{"key": k, "why": w} for k, w in self.failed],
            "bytes_copied": self.bytes_copied,
            "full": self.full,
            "detail": self.detail,
        }


#: 盘不在了的那几趟,记进 ``failed`` 的理由。写成常量是为了让"开拷之前就没了"
#: 和"拷到一半没了"两条路说同一句话 —— 现场看到的是同一件事。
_DISK_GONE = "备份盘不在了(盘上的标记文件读不到)—— 这一趟没有拷"


def apply_sync(plan: SyncPlan, *, now_ms: int, robot_sn: str = "",
               copy: Callable[[Path, Path], tuple[int, int]] = copy_run,
               ) -> SyncResult:
    """照计划拷,**每一趟拷完重新算哈希核对,核对过了才记进进度**。

    记早了,下次同步会跳过它 —— 于是那份坏数据永远不会被修,而值守屏上那块
    "已同步"是绿的。

    ``copy`` 可注入:"拷过去之后内容不对"在真机上是硬件偶发的,离机没法复现,
    注入是这段核对代码能被测到的唯一办法。测不到的话,它在真机上第一次跑就是
    它唯一一次跑。它要回 ``(文件数, 字节数)``,**记进账本的是它回的这个数,
    不是 ``item.size_bytes``** —— 后者是排计划那一刻量的,不是这一趟的战果。

    **空计划也写进度。** "同步过了,没有新东西"是一次成功的同步;不更新时间
    的那一边,值守屏上那块"上次同步"会一直停在很久以前 —— 一块好盘看起来像
    块死盘,而这一卷做的正是让死盘看得出来。

    **"盘还在不在"的判据是标记文件读不读得到,不是 ``mount.exists()``。**
    Linux 上卸载之后 ``/media/<user>/<label>`` 通常**作为一个空目录留在根文件
    系统上** —— ``exists()`` 照样为真,而 ``_copy_file`` 第一行就是
    ``mkdir(parents=True)``,它会老老实实把整棵归档树在根盘上重建,把几个 GB
    写进 Orin 的 eMMC。那个位置在 ``runs_root`` 之外,清盘器看不见也删不掉,
    终点是 EROFS 和一台起不来的狗。标记文件在盘上,盘一卸载就读不到了。

    盘不在的时候**绝不写进度**:写了等于把"同步过"记到一块不在的盘上 ——
    真正的进度还在那块盘里,而根盘上多出来的那份下次挂上会被 ``robot_sn``
    对不上以外的任何理由信任。

    **一趟的 I/O 错误只赔一趟。** 拷贝、核对、写账本三处都各自接住 ``OSError``:
    这个函数一次要跑几十趟、几十 GB,让最后一下 I/O 错误把前面全部的战果一起
    掀翻,是最不值的一种失败。写账本失败尤其要说清楚 —— 数据已经在盘上了,
    只是"记过了"没写下,下次会重拷一遍(重拷是浪费,不是损坏)。
    """
    copied: list[str] = []
    failed: list[tuple[str, str]] = []
    done_bytes = 0

    if not marker_path(plan.mount).is_file():
        return SyncResult(
            mount=plan.mount, copied=(),
            failed=tuple((i.key, _DISK_GONE) for i in plan.items),
            bytes_copied=0, full=plan.full,
            detail="备份盘在开拷之前就不在了 —— 一个字节也没写,进度也没记。"
                   "盘卸载之后挂载点常常作为一个空目录留在根文件系统上,"
                   "照着往里写会把几个 GB 的归档写进狗自己的 eMMC")

    for index, item in enumerate(plan.items):
        # **每一趟之前再查一次。** 拔盘发生在哪一趟中间是没法预测的,而只查
        # 一次的那一边,剩下的几趟会一趟趟地落到根文件系统上。
        if not marker_path(plan.mount).is_file():
            failed.extend((rest.key, _DISK_GONE) for rest in plan.items[index:])
            break
        try:
            _files, size = copy(item.src, item.dest)
        except OSError as exc:
            failed.append((item.key, f"拷不过去: {exc}"))
            continue
        try:
            why = verify_run(item.src, item.dest)
        except OSError as exc:
            # **核对自己也会炸。** 它要把两边的文件从头读一遍,而"拷完那一刻
            # 盘被拔了"正是这一段最可能撞上的事。不接住的话,这个异常会从
            # ``apply_sync`` 里蹿出去,前面已经拷成的那几趟一趟都记不上账 ——
            # 下次全部重拷。接住只赔掉这一趟。
            failed.append((item.key, f"拷完之后核对不了: {exc}"))
            continue
        if why:
            failed.append((item.key, why))
            continue
        copied.append(item.key)
        done_bytes += size

    detail = plan.detail
    try:
        state = read_state(plan.mount, robot_sn=robot_sn)
        if not state.robot_sn:
            state = SyncState(robot_sn=robot_sn, last_sync_ms=state.last_sync_ms,
                              done=state.done)
        write_state(plan.mount, state.with_done(copied, now_ms=now_ms))
    except OSError as exc:
        # **账本没记上不等于这一趟白拷了。** 数据已经落在盘上,只是"拷过了"
        # 这件事没记下来 —— 下次同步会把这几趟当成新的重拷一遍。重拷是浪费,
        # 不是损坏(``_copy_file`` 覆盖写)。而把这一下抛出去的那一边,调用方
        # 看到的是一次彻头彻尾的失败,连"哪几趟拷成了"都拿不到。
        detail = (f"这一趟拷成了但进度没记上,下次会重拷(写盘上的账本时出错: "
                  f"{exc})")
    if failed and not detail:
        detail = f"有 {len(failed)} 趟没拷成,见 failed"
    elif failed:
        detail = f"{detail};另有 {len(failed)} 趟没拷成,见 failed"
    return SyncResult(mount=plan.mount, copied=tuple(copied),
                      failed=tuple(failed), bytes_copied=done_bytes,
                      full=plan.full, detail=detail)


def _noop_sync() -> None:
    """这台机器上没有 ``os.sync``(Windows 开发机)。什么也不做。"""


#: 把缓冲刷到盘上。``os.sync`` 是 Unix 独有的 —— 直接写 ``os.sync()`` 会在
#: import 阶段就把整个模块在 Windows 开发机上炸掉,而这个模块的每一条测试
#: 都跑在那台机器上。写成模块级常量而不是默认参数里的表达式,是为了不撞
#: ruff B008。
DEFAULT_SYNC: Callable[[], None] = getattr(os, "sync", _noop_sync)


@dataclass(frozen=True, slots=True)
class Ejection:
    """能不能拔。"""

    mount: Path
    ok: bool
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {"mount": self.mount.as_posix(), "ok": self.ok,
                "detail": self.detail}


def eject(mount: Path | str, *, busy: bool = False,
          sync_fs: Callable[[], None] = DEFAULT_SYNC) -> Ejection:
    """安全弹出。**停同步 -> 刷缓冲 -> 才说可以拔了。**

    Linux 上写文件是先进页缓存的。人在手机上看到"同步完成"就伸手把备份盘拔了,
    而那几百兆可能还有一部分在内存里 —— 盘上那趟归档因此是残的,**而我们
    已经把它记成拷完了**,下次同步不会再碰它。

    ``busy`` 由 app 那一层给:哪几个挂载点上正跑着同步是 HTTP 这一侧的事,
    不是这个模块的事(它连"有没有别人在跑"都不该知道)。
    """
    mount = Path(mount)
    if busy:
        return Ejection(mount=mount, ok=False,
                        detail="这块盘正在同步,现在不能拔。拔一块正在写的盘,"
                               "坏的不只是这一趟 —— 文件系统元数据写到一半,"
                               "整块盘可能就挂不上了。等这一轮跑完再弹出")
    try:
        sync_fs()
    except OSError as exc:
        return Ejection(mount=mount, ok=False,
                        detail=f"没能把缓冲刷到盘上({exc})—— **先别拔**。"
                               f"现在拔,盘上最后那几趟归档可能是残的,"
                               f"而我们这边已经把它们记成拷完了")
    return Ejection(mount=mount, ok=True,
                    detail="缓冲已经刷到盘上,可以拔了")


#: 镜像盘落后几趟就该顶到人脸上。落后 1 趟是常态(刚跑完一趟还没同步),
#: 落后 5 趟说明这块盘已经好几天没写进去了 —— 那通常不是"还没轮到",
#: 是它已经不工作了。
BEHIND_PUSH_RUNS = 5


class NoticeLevel(str, Enum):
    #: 配了镜像盘,跟得上。**不说话。**
    OK = "ok"
    #: 没配镜像盘。**中性的一句话,不是红的。**
    NEUTRAL = "neutral"
    #: 该顶到人脸上了。
    PUSH = "push"


@dataclass(frozen=True, slots=True)
class BackupNotice:
    """要不要就备份这件事说话,以及说到什么份上。"""

    level: NoticeLevel
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {"level": self.level.value, "detail": self.detail}


def backup_notice(targets: Sequence[TargetStatus], *, behind: int = 0,
                  days_left: float | None = None,
                  ota_pending: bool = False,
                  full: bool = False) -> BackupNotice:
    """没配镜像盘 / 镜像盘落后了,该怎么说这句话。

    **没配镜像盘不许常年报红**(spec §7.6)。常年报警的东西等于没报警:现场
    的人会先学会忽略它,然后连真的那次也一起忽略。平时是中性的一句"未配
    备份盘",只在**两个时刻**顶到人脸上 —— 授权 OTA 之前(OTA 会刷掉启动盘),
    保留期快到期之前(马上要有东西被永久删掉了)。

    **一块不再同步的镜像盘比没有镜像盘更危险。** 它是个完美的静默故障:
    所有人都以为有第二份。所以配了盘但落后太多,照样顶出来。

    ``days_left`` 是盘上最快到期的那一趟还剩几天;``None`` 表示盘上一趟归档
    也没有。``ota_pending`` 由第 4 卷在授权升级之前传 ``True`` —— 这一卷不
    知道 OTA 这回事,只留好这个入口。

    ``full`` 为真表示镜像盘满了(``plan_sync(...).full``)。**满盘要顶出来,
    而且要说清楚"不会删盘上的旧归档"**(spec §7.6):备份盘上那份常常是唯一
    副本 —— 狗自己那份是按水位清的,清掉之后就只剩盘上这一份。一个"自动腾
    空间"的备份盘会在某个没人看着的夜里把唯一副本删掉。所以这里停下来等人,
    而人必须知道自己在等什么。
    """
    mirrors = [t for t in targets if t.usable and t.role is DiskRole.MIRROR]
    if not mirrors:
        soon = days_left is not None and days_left <= MIN_NOTICE_DAYS
        if not (soon or ota_pending):
            return BackupNotice(
                NoticeLevel.NEUTRAL,
                "未配备份盘。归档现在只有一份,存在这台狗自己的盘上")
        why = []
        if ota_pending:
            why.append("马上要升级系统,而升级会刷掉启动盘")
        if soon:
            why.append(f"再过 {days_left:.0f} 天就有归档要被永久删掉了")
        return BackupNotice(
            NoticeLevel.PUSH,
            "这台狗没有备份盘,归档只有一份 —— " + ";".join(why)
            + "。现在插一块盘初始化成镜像盘,或者先把要留的那些导出来")
    if full:
        # **满盘排在落后前面。** 盘满了必然带着落后,先说根因 —— 换成"落后了
        # 去看三件事"那句话,人会照着单子查一圈才发现是满了。
        return BackupNotice(
            NoticeLevel.PUSH,
            "镜像盘满了,同步已经停下 —— 新的归档现在只有狗自己这一份。"
            "盘上的旧归档不会被自动删掉:那些常常是唯一副本,狗自己那份是"
            "按水位清的。要么换一块盘,要么把盘上的旧归档拷走之后人工删掉,"
            "腾出空间同步会自己接上")
    if behind >= BEHIND_PUSH_RUNS:
        # **不下诊断。** 说"硬件故障"会把现场技术员指向一个可能并不存在的
        # 问题,而落后的原因里"盘满了"和"盘卸载了"都比硬件常见得多。
        # 说现象,给下一步该看什么。
        return BackupNotice(
            NoticeLevel.PUSH,
            f"镜像盘已经落后 {behind} 趟没同步了 —— 自动同步是按分钟巡的,"
            f"落后这么多说明这几趟一直没拷成。一块不再同步的镜像盘比没有更"
            f"危险,所有人都以为有第二份。先看三件事:这块盘还挂着没有、"
            f"盘上还有没有空间、/api/backup/sync 出的方案里这几趟是什么状态")
    return BackupNotice(NoticeLevel.OK, "")
