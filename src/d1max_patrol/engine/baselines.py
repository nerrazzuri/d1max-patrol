"""基线集:判读拿来做比对基准的那一张。**独立于 run 目录,永不参与水位删除。**

`inspect/judge.py` 原先是这么找基准的:扫本地历史 run 目录,找同点位同相机
最近的一张。**它读的是本地历史。** 一旦水位删除开始工作(spec §4.4),跨日期
比对就**静默失效** —— 不报错,只是从此每张照片都变成"无基准"判读,而没有
人会发现。

所以把两件事拆开:**归档是证据,基线是工具。** 证据按水位删,工具单独留一份。
体积算得过来:50 点位 x 2 相机 x 300KB 约 30MB,一次性占用,不随时间增长
(spec §4.5)。
"""

from __future__ import annotations

import os
from pathlib import Path

# 只借它那条"临时文件名怎么起"的规矩(见 ``retention.unique_tmp``),
# 不借任何保留策略 —— 基线永不参与水位删除,那一条没有变。
from d1max_patrol.engine.retention import unique_tmp

#: 基线目录的惯用名。放在 runs 根目录**旁边**,不是里面 —— 放里面迟早被
#: 某个"清空 runs"的动作连坐。
BASELINE_DIR_NAME = "baselines"

#: 名字里出现这些就不给建基线。前四个是路径穿越,``__`` 是照片名的分隔符
#: (``<点位>__<相机>__<时间戳>.jpg``),混进名字里日后就拆不准了。
_FORBIDDEN = ("/", "\\", "..", "\x00", "__")


class BaselineError(Exception):
    """这张基线建不了。**抛出来,不清洗名字凑合写** —— 清洗过的名字读回来
    对不上,那才是静默失效。"""


def _check(waypoint: str, camera: str) -> None:
    for label, value in (("点位名", waypoint), ("相机名", camera)):
        if not value:
            raise BaselineError(f"{label}是空的,建不了基线")
        for bad in _FORBIDDEN:
            if bad in value:
                raise BaselineError(f"{label}不能含 {bad!r}: {value!r} —— "
                                    f"它会成为基线文件名的一部分")


def baseline_path(baselines_root: Path | str, waypoint: str, camera: str) -> Path:
    """这一点位这一相机的基线在哪儿。"""
    _check(waypoint, camera)
    return Path(baselines_root) / f"{waypoint}__{camera}.jpg"


def save_baseline(baselines_root: Path | str, waypoint: str, camera: str,
                  data: bytes) -> Path:
    """覆盖式更新。**先写临时文件再改名。**

    写到一半断电就是"基线没了",而基线没了只是退回去翻历史 —— 但写出半张
    图会被当成一张能用的基准喂给判读,那是更坏的一种坏。

    **临时名是这次调用独有的**(``retention.unique_tmp``):判读是从 HTTP 线程
    直接跑的,而服务是多线程的 —— 两次判读同时回写同一个点位,固定的 ``.tmp``
    名字会让其中一张变成半张图,而半张图正是上一段说的那种更坏的坏。
    """
    target = baseline_path(baselines_root, waypoint, camera)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(target)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def load_baseline(baselines_root: Path | str, waypoint: str,
                  camera: str) -> bytes | None:
    """读基线。**没有就是 ``None``,不是错** —— 第一次跑这个点位本来就没有。"""
    try:
        path = baseline_path(baselines_root, waypoint, camera)
    except BaselineError:
        # 名字本来就建不了基线,那就一定没有。让调用方退回去翻历史。
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def list_baselines(baselines_root: Path | str) -> list[tuple[str, str]]:
    """有哪些点位/相机存了基线。目录不在就是空的,不抛错。"""
    root = Path(baselines_root)
    if not root.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.jpg")):
        parts = path.stem.split("__")
        if len(parts) == 2 and all(parts):
            out.append((parts[0], parts[1]))
    return out


def baselines_bytes(baselines_root: Path | str) -> int:
    """基线集一共占多少字节。给盘况页面报数用。"""
    root = Path(baselines_root)
    if not root.is_dir():
        return 0
    total = 0
    for path in root.glob("*.jpg"):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total
