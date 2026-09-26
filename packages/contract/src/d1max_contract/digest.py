"""文件与整棵树的 sha256(W00c2a 起住在契约包:站点收任务包要算同一个指纹)。
**只有一个算法** —— 发布包(``release.json``)与任务包(``bundle.yaml``)都走 ``tree_sha256``,
只是跳过的自述文件不同;两套算法就是两个真理源。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

_CHUNK = 1024 * 1024
_CHUNK_JOIN = b"\0"


def sha256_file(path: Path | str) -> str:
    """流式算文件的 sha256。**不整个读进内存** —— 包可能有好几个 G。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path | str, *, skip: str,
                keep: Callable[[str], bool] | None = None) -> str:
    """整棵树的指纹:按相对路径排序,逐条喂「路径 + 这个文件的 sha256」。

    **路径要进指纹。** 只把内容首尾相接算一遍的话,改个文件名、把 a 的内容
    挪进 b,指纹一点不变 —— 那种改动就成了隐形的。

    ``skip`` 那个文件自己不算 —— 它里头存着这个值,算自己是个死循环。默认是
    ``release.json``;任务包传的是 ``bundle.yaml``(§3.2 的整包 content_hash
    跟 §7.3 是同一套算法,**不该有第二个真理源**)。

    排序按**相对路径的 posix 串**,不按 ``Path`` 对象:后者在 Windows 上按
    ``parts`` 比,和 Linux 上的结果不保证一样,而两边算出不同指纹的那天,
    整套对账就废了。

    ``keep``(W00c6d):只算它说要的那些相对路径 —— 核槽里的包时跳过装好之后才长出来的 venv、
    ``__pycache__``(打包时本来就不收)。不给就全算。
    """
    root = Path(root)
    digest = hashlib.sha256()
    files = sorted((p.relative_to(root).as_posix(), p)
                   for p in root.rglob("*") if p.is_file())
    for rel, path in files:
        if rel == skip or (keep is not None and not keep(rel)):
            continue
        digest.update(rel.encode())
        digest.update(_CHUNK_JOIN)
        digest.update(sha256_file(path).encode())
        digest.update(_CHUNK_JOIN)
    return digest.hexdigest()
