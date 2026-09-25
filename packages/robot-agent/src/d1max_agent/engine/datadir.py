"""数据根:巡检数据摆在哪。**在版本槽外面。**

``--runs-root`` 原来默认相对路径 ``runs``,而服务的 ``WorkingDirectory`` 是
``/opt/d1max/current`` —— 一条指着版本槽的符号链接。于是证据落在
``releases/<版本>/runs``:切版本之后旧槽不再是当前目录(旧槽里待传的队列就此
停传),第三版 ``commit()`` 调 ``prune()`` 把最老的槽整个删掉,证据一起没了。

这里只做两件事:

* :func:`resolve_paths` —— 给定数据根(来自 ``$D1MAX_DATA_ROOT``),算出
  ``runs_root / maps_dir / bags_dir / missions_dir`` 四个默认值。**没给就退回
  今天的相对路径**,开发机在仓库里跑的行为一个字不变。
* :func:`migrate_slot_data` —— 老机器槽里已经有的数据,一次性搬到数据根。

(老服务 ``app/server.py`` 的上传队列、基线、导出三处是 ``runs_root.parent / <名字>``,
随 W00c5e 退役;老机器槽里留下的这几样仍由 :func:`migrate_slot_data` 搬走。)
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .release import SLOT_DATA_ITEMS, Layout, installed

#: 服务单元设它;开发机不设。
DATA_ROOT_ENV = "D1MAX_DATA_ROOT"
#: 真机上的数据根。**装机脚本、服务单元、这里三处要一致。**
DEFAULT_DATA_ROOT = Path("/var/lib/d1max")


@dataclass(frozen=True)
class DataPaths:
    runs_root: Path
    maps_dir: Path
    bags_dir: Path
    missions_dir: Path


def resolve_paths(data_root: str | Path | None) -> DataPaths:
    """数据根 → 四个目录。``None``/空白 = 没设 = 今天的相对路径。"""
    if data_root is None or not str(data_root).strip():
        base = Path(".")
    else:
        base = Path(str(data_root).strip())
    runs = base / "runs"
    return DataPaths(runs_root=runs, maps_dir=runs / "slam",
                     bags_dir=runs / "bags", missions_dir=base / "missions")


#: 搬完在槽里留下的名字。``release.prune`` 的保险看的是 ``SLOT_DATA_ITEMS``
#: 那几个原名,改了名它就不再拦 —— 单一真理源在 ``release.py``,这里 import。
MIGRATED_SUFFIX = ".migrated"

#: 预检盘余量时,算完「要搬多少」还要再留的安全垫。**不是为了装下数据本身**
#: (那部分是 ``needed * 1.1``),是给同一块盘上其它东西留出呼吸的空间 ——
#: 巡检数据根往往跟系统盘是同一块盘,搬完把盘挤到 0 字节剩余,下一次写日志
#: 都会失败。
MIN_FREE_BYTES = 64 * 1024 * 1024
#: 上传队列那一份**不是普通文件**,搬法不同(见 :func:`_merge_queue`)。名字跟老服务的
#: ``QUEUE_FILE_NAME`` 一致(W00c5e 退役),也在 ``SLOT_DATA_ITEMS`` 里。
QUEUE_FILE = "queue.jsonl"


@dataclass(frozen=True)
class MigrationReport:
    slots: tuple[str, ...]
    copied: int
    skipped: int


def migrate_slot_data(layout: Layout, data_root: Path) -> MigrationReport:
    """把每个版本槽里的巡检数据搬到 ``data_root``。**只增不覆盖,可重跑。**

    **撞名时新槽赢。** 按 ``installed(layout)`` 倒序(新到旧)搬:单文件的
    ``queue.jsonl``、``baselines/<x>.json`` 这种在两个槽里都存在的东西,搬进
    数据根的是**当前槽**(较新那份)的内容,较旧槽里撞名的那份跳过
    (算进 ``skipped``),照样改名进 ``*.migrated`` —— 数据没丢,只是没被
    当成数据根里那份的来源,人想核对随时能翻。

    搬完把槽里的源改名加 :data:`MIGRATED_SUFFIX`,所以第二次跑什么都不做。
    目标已有同路径文件时跳过(算进 ``skipped``),源照样改名 —— 那份留在
    ``*.migrated`` 里,人想核对随时能看。

    **动手之前先查一遍盘够不够。** 见 :func:`_check_free_space`;不够就直接
    报错,一个文件都不碰。

    **调用前提:没有任何进程还在往槽里写。** 装机脚本要先停掉服务再跑它 ——
    服务还活着的话,它可能正往 ``queue.jsonl``/``runs/`` 里追加,搬到一半的
    文件被这里当成"已完整"拷走,数据就裂开了。
    """
    data_root = Path(data_root)
    names = tuple(reversed(installed(layout)))
    _check_free_space(layout, names, data_root)
    touched: list[str] = []
    copied = skipped = 0
    for name in names:
        slot = layout.releases / name
        hit = False
        for item in SLOT_DATA_ITEMS:
            src = slot / item
            if not src.exists():
                continue
            hit = True
            if item == QUEUE_FILE:
                c, s = _merge_queue(src, data_root / item)
            else:
                c, s = _merge_into(src, data_root / item)
            copied += c
            skipped += s
            _retire(src, slot / (item + MIGRATED_SUFFIX))
        if hit:
            touched.append(name)
    return MigrationReport(slots=tuple(touched), copied=copied, skipped=skipped)


def _check_free_space(layout: Layout, names: tuple[str, ...],
                      data_root: Path) -> None:
    """算一遍所有槽里 ``SLOT_DATA_ITEMS`` 加起来有多少字节,跟数据根所在盘的
    剩余空间比一比。**不够就报错,不碰任何文件** —— 搬到一半才发现盘满,
    槽里的源已经有一部分改名、数据根里也躺着半截数据,两头都不干净。

    ``needed == 0``(没有数据要搬)时跳过 —— 空槽不该被盘满拦下来。

    **数据根本身可能还不存在。** ``shutil.disk_usage`` 要一个存在的路径,
    这里顺着 ``data_root`` 往上找到第一个已经在盘上的祖先目录去问。
    """
    needed = 0
    for name in names:
        slot = layout.releases / name
        for item in SLOT_DATA_ITEMS:
            src = slot / item
            if src.is_file():
                needed += src.stat().st_size
            elif src.is_dir():
                needed += sum(p.stat().st_size for p in src.rglob("*")
                             if p.is_file())
    if needed == 0:
        return
    anchor = data_root
    while not anchor.exists():
        parent = anchor.parent
        if parent == anchor:
            break
        anchor = parent
    free = shutil.disk_usage(anchor).free
    if free < needed * 1.1 + MIN_FREE_BYTES:
        raise OSError(
            f"数据根 {data_root} 所在盘剩 {free // 2**20} MB,搬迁需要约 "
            f"{needed // 2**20} MB,不够 —— 先清盘或删掉已核对过的 *.migrated")


def _retire(src: Path, dest: Path) -> None:
    """把搬完的源 ``src`` 改名成 ``dest``(加了 :data:`MIGRATED_SUFFIX` 的那个名字)。

    通常一步 ``rename`` 就完事。``dest`` 已经在(上一趟跑到一半断了电)的话,
    ``rename`` 碰上非空目录会失败 —— 这时并进去(同样不覆盖),源再删掉。
    """
    if not dest.exists():
        src.rename(dest)
        return
    _merge_into(src, dest)
    if src.is_dir():
        shutil.rmtree(src)
    else:
        src.unlink()


def _merge_queue(src: Path, dst: Path) -> tuple[int, int]:
    """把老槽的 ``queue.jsonl`` **并进**数据根的活动队列。返回 (并入, 跳过)。

    队列是日志结构(同 key 后写覆盖先写,启动整个重放),所以它不能按"目标已有
    就跳过"处理 —— 那会让老槽里没传完的条目全部停传,正是 W01 要解决的事。
    ``Uploader.scan()`` 虽然会把搬进数据根的文件重新发现并 ``offer``,但那是从
    offset 0 重来、而且丢掉 ``done`` 状态:已经传完的全部重传,``.uploaded`` 的
    判定也被打回。并队列把 offset/done 一起带过来。

    调用顺序是槽从新到旧,所以 ``dst`` 里已有的 key 是**更新的槽**的状态,保留它;
    老槽同 key 的那行算 ``skipped``,原件留在 ``queue.jsonl.migrated`` 里可核对。
    坏行照 ``UploadQueue._replay`` 的规矩跳过,不拦整体。
    """
    from .upload_queue import QueueItem, UploadQueue

    if dst.exists() and not dst.is_file():
        raise OSError(f"数据根里 {dst} 不是文件,不敢当队列合并")
    old: list[QueueItem] = []
    seen: dict[str, int] = {}
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = QueueItem.from_wire(json.loads(line))
            except (ValueError, KeyError, TypeError):
                continue
            if item.key in seen:          # 同 key 后写覆盖先写
                old[seen[item.key]] = item
            else:
                seen[item.key] = len(old)
                old.append(item)
    merged = skipped = 0
    q = UploadQueue(dst)
    try:
        for item in old:
            if q.get(item.key) is not None:
                skipped += 1
                continue
            q._write(item)
            merged += 1
    finally:
        q.close()
    return merged, skipped


def _merge_into(src: Path, dst: Path) -> tuple[int, int]:
    """``src``(文件或目录)并进 ``dst``:目标已有的文件不动。返回 (拷了, 跳过)。

    **类型对不上就不敢猜。** ``src`` 是目录而 ``dst`` 已经是个文件(或反过来)
    说明数据根跟槽里的东西对不上 —— 两次迁移用了不同的 ``--data-root``,或者
    有人手改过。这种局面没有安全的合并做法,只能报错让人来看,不能瞎猜一个
    方向搬。

    **合并开始前先清一遍上一轮崩溃剩下的 ``*.part``。** 不清的话它们会被
    ``target.exists()`` 当成"已经在目标里"而被跳过,那份半截数据就永远留在
    数据根里,而它本该被这一轮重新、完整地拷一遍。
    """
    if (src.is_dir() and dst.exists() and not dst.is_dir()) or (
            src.is_file() and dst.is_dir()):
        raise OSError(f"数据根里 {dst} 的类型跟槽里的 {src} 对不上"
                      f"(一个是文件一个是目录),不敢合并")
    _clean_stale_parts(dst)
    if src.is_file():
        if dst.exists():
            return 0, 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(src, dst)
        return 1, 0
    copied = skipped = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        target = dst / p.relative_to(src)
        if target.exists():
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(p, target)
        copied += 1
    return copied, skipped


def _clean_stale_parts(dst: Path) -> None:
    """清掉 ``dst`` 底下(或者紧挨着它自己的)上一轮崩溃剩下的 ``*.part``。"""
    if dst.is_dir():
        for p in list(dst.rglob("*.part")):
            if p.is_file():
                p.unlink()
        return
    part = dst.parent / (dst.name + ".part")
    if part.is_file():
        part.unlink()


def _atomic_copy(src: Path, dst: Path) -> None:
    """把 ``src`` 拷到 ``dst``。**先拷到 ``.part`` 再 ``os.replace()`` 过去**,
    中途死掉(盘满、断电)不会在 ``dst`` 留下一个看着拷完了、其实是半截的文件
    —— 那种半截文件会被下一轮的 ``target.exists()`` 当成"已经搬完",永远不
    会被补全。
    """
    part = dst.with_name(dst.name + ".part")
    shutil.copy2(src, part)
    os.replace(part, dst)
