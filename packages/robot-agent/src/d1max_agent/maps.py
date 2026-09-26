"""狗上正在用的那一张图(W00c5d 第二部分,决策 8:站点是地图的唯一权威,狗上只有工作副本)。

- 站点下发 ``map_activate``(地图号、版本、每个文件的大小与 sha256)→ 从站点的狗专用口下载
  (mTLS),**每个文件边下边算哈希,大小或哈希对不上整张不装**;装好了交给适配器载入
  (适配器没有载入这一步的,这台狗就不报这项能力,站点也不会发)。
- 狗上只留正在用的这一张:``<store>/maps/<地图号>/<版本>/``,旁边 ``active.json`` 记着是哪张;
  别的一律删。
  重启时按 ``active.json`` 接着用(文件大小先核一遍,缺了就当没有)。
- 地图里可以带 ``home.json``(``{"x", "y", "yaw"}``):这张图上的原点。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.maps import MANIFEST, MapRef

log = logging.getLogger(__name__)

#: 一张图最多下多久(秒):4G 上几百 MB 的点云图也够;超了就算没下成,不在那儿一直挂着。
DOWNLOAD_DEADLINE_S = 1800.0

#: 下载:给地图号、版本、文件名,返回一块一块的字节(调用方在线程里跑它)。
Fetch = Callable[[str, str, str], Iterable[bytes]]


class MapInstallError(RuntimeError):
    """这张图装不上(下载失败、大小或哈希对不上、适配器载不进去)。消息给站点看。"""


class MapKeeper:
    def __init__(self, root: Path | str, *, fetch: Fetch) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._fetch = fetch

    # ------------------------------------------------------------ 现在是哪张

    def active(self) -> MapRef | None:
        """``active.json`` 记着的那张;文件缺了或大小对不上就当没有(不信一个残缺的工作副本)。"""
        try:
            ref = MapRef.from_wire(json.loads((self.root / "active.json").read_text("utf-8")))
        except (OSError, ValueError, ContractError):
            return None
        d = self.dir_of(ref)
        for f in ref.files:
            p = d / f.name
            if not p.is_file() or p.stat().st_size != f.size:
                log.warning("正在用的图 %s:%s 缺了 %s,当没有", ref.map_id, ref.version, f.name)
                return None
        return ref

    def dir_of(self, ref: MapRef) -> Path:
        return self.root / ref.map_id / ref.version

    def home_of(self, ref: MapRef) -> tuple[float, float, float] | None:
        """这张图上的原点:先看站点下发时给的(记在 ``active.json`` 里,是这台狗在这张图上的待命点),
        再看图里带的 ``home.json``。都没有返回 None。"""
        sources = ((self.root / "active.json", "home"), (self.dir_of(ref) / "home.json", None))
        for path, key in sources:
            try:
                d = json.loads(path.read_text("utf-8"))
                if key is not None:
                    if (d.get("map_id"), d.get("version")) != (ref.map_id, ref.version):
                        continue
                    d = d.get(key)
                    if d is None:
                        continue
                x, y, yaw = (float(d[k]) for k in ("x", "y", "yaw"))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            return (x, y, yaw)
        return None

    # ------------------------------------------------------------ 装

    def install(self, ref: MapRef) -> Path:
        """下载、逐个核对、装好(**阻塞,在线程里调**)。返回图的目录。不改 ``active.json``。
        要装的就是正在用的那张(文件都在):不重下 —— 先删了再下,半路断电就一张图都没了。"""
        cur = self.active()
        if cur is not None and (cur.map_id, cur.version) == (ref.map_id, ref.version) \
                and cur.files == ref.files:
            return self.dir_of(ref)
        tmp = self.root / ".incoming" / f"{ref.map_id}@{ref.version}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        deadline = time.monotonic() + DOWNLOAD_DEADLINE_S
        try:
            for f in ref.files:
                h, n = hashlib.sha256(), 0
                with open(tmp / f.name, "wb") as fh:
                    try:
                        for chunk in self._fetch(ref.map_id, ref.version, f.name):
                            n += len(chunk)
                            if n > f.size:
                                raise MapInstallError(f"{f.name} 比清单里说的大")
                            if time.monotonic() > deadline:
                                raise MapInstallError(f"下载超过 {DOWNLOAD_DEADLINE_S:g} s 没下完")
                            h.update(chunk)
                            fh.write(chunk)
                    except MapInstallError:
                        raise
                    except Exception as exc:
                        raise MapInstallError(f"{f.name} 下载失败: {type(exc).__name__}: {exc}"
                                              ) from exc
                    fh.flush()
                    os.fsync(fh.fileno())
                if n != f.size or h.hexdigest() != f.sha256:
                    raise MapInstallError(f"{f.name} 大小或哈希对不上")
            (tmp / MANIFEST).write_text(json.dumps(ref.to_wire(), ensure_ascii=False),
                                        encoding="utf-8")
            dst = self.dir_of(ref)
            shutil.rmtree(dst, ignore_errors=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dst)
            return dst
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def commit(self, ref: MapRef, *, home: tuple[float, float, float] | None = None) -> None:
        """这张图载入成功了:记成正在用的(连同站点给的原点),别的图都删掉(狗上只留这一张)。"""
        tmp = self.root / "active.json.tmp"
        rec = ref.to_wire()
        if home is not None:
            rec["home"] = {"x": home[0], "y": home[1], "yaw": home[2]}
        with open(tmp, "w", encoding="utf-8") as fh:     # 换完图往往接着断电换电池:落了盘再换名
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.root / "active.json")
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        keep = self.dir_of(ref).resolve()
        for m in self.root.iterdir():
            if not m.is_dir():
                continue
            for v in m.iterdir():
                if v.is_dir() and v.resolve() != keep:
                    shutil.rmtree(v, ignore_errors=True)
            if m.name != ref.map_id and not any(m.iterdir()):
                m.rmdir()

    def discard(self, ref: MapRef) -> None:
        """装好了但没载进去:删掉,狗上照旧用原来那张。"""
        cur = self.active()
        if cur is None or (cur.map_id, cur.version) != (ref.map_id, ref.version):
            shutil.rmtree(self.dir_of(ref), ignore_errors=True)


def https_fetch(base_url: str, ssl_context: Any, *, timeout_s: float = 60.0) -> Fetch:
    """从站点的狗专用口拉地图文件(mTLS,跟上传同一套证书)。"""
    import urllib.request
    from urllib.parse import quote

    def fetch(map_id: str, version: str, name: str) -> Iterable[bytes]:
        url = f"{base_url.rstrip('/')}/maps/{quote(map_id)}/{quote(version)}/{quote(name)}"
        with urllib.request.urlopen(url, context=ssl_context, timeout=timeout_s) as r:
            while chunk := r.read(1 << 20):
                yield chunk
    return fetch
