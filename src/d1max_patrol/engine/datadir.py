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

上传队列、基线、导出三处在 ``app/server.py`` 里是 ``runs_root.parent / <名字>``,
所以 ``runs_root`` 落对了它们就跟着落对,这里不另算。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .release import Layout, installed

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


#: 槽里要搬走的东西。跟 app/server.py 里 ``runs_root`` 及其兄弟位置的名字一致:
#: ``QUEUE_FILE_NAME``、``BASELINE_DIR_NAME``、``EXPORTS_DIR_NAME``。
SLOT_DATA_ITEMS = ("runs", "queue.jsonl", "baselines", "exports")
#: 搬完在槽里留下的名字。release.prune 的保险只看 ``runs``,改了名它就不再拦。
MIGRATED_SUFFIX = ".migrated"


@dataclass(frozen=True)
class MigrationReport:
    slots: tuple[str, ...]
    copied: int
    skipped: int


def migrate_slot_data(layout: Layout, data_root: Path) -> MigrationReport:
    """把每个版本槽里的巡检数据搬到 ``data_root``。**只增不覆盖,可重跑。**

    搬完把槽里的源改名加 :data:`MIGRATED_SUFFIX`,所以第二次跑什么都不做。
    目标已有同路径文件时跳过(算进 ``skipped``),源照样改名 —— 那份留在
    ``*.migrated`` 里,人想核对随时能看。
    """
    data_root = Path(data_root)
    touched: list[str] = []
    copied = skipped = 0
    for name in installed(layout):
        slot = layout.releases / name
        hit = False
        for item in SLOT_DATA_ITEMS:
            src = slot / item
            if not src.exists():
                continue
            hit = True
            c, s = _merge_into(src, data_root / item)
            copied += c
            skipped += s
            _retire(src, slot / (item + MIGRATED_SUFFIX))
        if hit:
            touched.append(name)
    return MigrationReport(slots=tuple(touched), copied=copied, skipped=skipped)


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


def _merge_into(src: Path, dst: Path) -> tuple[int, int]:
    """``src``(文件或目录)并进 ``dst``:目标已有的文件不动。返回 (拷了, 跳过)。"""
    if src.is_file():
        if dst.exists():
            return 0, 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
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
        shutil.copy2(p, target)
        copied += 1
    return copied, skipped
