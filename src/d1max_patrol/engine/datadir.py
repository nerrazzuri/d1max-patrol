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
  (由后续任务添加)

上传队列、基线、导出三处在 ``app/server.py`` 里是 ``runs_root.parent / <名字>``,
所以 ``runs_root`` 落对了它们就跟着落对,这里不另算。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
