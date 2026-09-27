"""狗上正在用的那一张图(W00c5d 第二部分,决策 8:站点是地图的唯一权威,狗上只有工作副本)。

- 站点下发 ``map_activate``(地图号、版本、每个文件的大小与 sha256)→ 从站点的狗专用口下载
  (mTLS),**每个文件边下边算哈希,大小或哈希对不上整张不装**;装好了交给适配器载入
  (适配器没有载入这一步的,这台狗就不报这项能力,站点也不会发)。
- 狗上只留正在用的这一张:``<store>/maps/<地图号>/<版本>/``,旁边 ``active.json`` 记着是哪张;
  别的一律删。
  重启时按 ``active.json`` 接着用(文件大小先核一遍,缺了就当没有)。
- 地图里可以带 ``home.json``(``{"x", "y", "yaw"}``):这张图上的原点。
- **建图的狗不再下回来**(W09c 决定 7):刚建好的版本 :meth:`MapKeeper.adopt` 进本地库(硬链接,不多占
  盘);激活同一个版本时本地那份逐个核得上大小和 sha256 就不下载。
- **下载能续传**(W09c 决定 8):断了按 ``Range`` 从断点接着下(连续 :data:`RESUME_TRIES` 次没进展才
  放弃);整张图的期限按大小算(:func:`download_deadline_s`)。
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

#: 一张图最少给多久下(秒);大的按 :data:`MIN_RATE_BPS` 算。超了就算没下成,不在那儿一直挂着。
DOWNLOAD_DEADLINE_S = 1800.0
#: 期限按这个速度算(1 Mbit/s):530 MB 的先验给 70 分钟。
MIN_RATE_BPS = 125_000
#: 一个文件连续几次没进展就放弃(每次有进展就重新数);两次之间退避 1、2、4… 秒,最多 30 秒。
RESUME_TRIES = 5

#: 下载:给地图号、版本、文件名(续传时再给 ``offset=`` 从第几个字节起),返回一块一块的字节(调用方在
#: 线程里跑它)。
Fetch = Callable[..., Iterable[bytes]]


def download_deadline_s(total_bytes: int) -> float:
    return max(DOWNLOAD_DEADLINE_S, total_bytes / MIN_RATE_BPS)


class FetchRefused(RuntimeError):
    """站点明确说不给(没有这个、不认这只狗):重试也没用。"""


class MapInstallError(RuntimeError):
    """这张图装不上(下载失败、大小或哈希对不上、适配器载不进去)。消息给站点看。"""


class MapKeeper:
    def __init__(self, root: Path | str, *, fetch: Fetch,
                 sleep: Callable[[float], Any] = time.sleep) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._fetch = fetch
        self._sleep = sleep

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

    def local(self, ref: MapRef) -> Path | None:
        """本地库里已经有这个版本、每个文件的大小和 sha256 都核得上:回目录;不然 None。"""
        d = self.dir_of(ref)
        try:
            have = MapRef.from_wire(json.loads((d / MANIFEST).read_text("utf-8")))
        except (OSError, ValueError, ContractError):
            return None
        if have != ref:
            return None
        for f in ref.files:
            p = d / f.name
            if not p.is_file() or p.stat().st_size != f.size or _sha256(p) != f.sha256:
                return None
        return d

    def adopt(self, src: Path, ref: MapRef) -> Path:
        """建图的狗把刚建好的版本(``src`` 里 ``ref`` 列的文件)放进本地库(W09c 决定 7;**阻塞**)。
        硬链接(发件箱传完删了它的那份,这边还在),不在一块盘上就拷。正在用的那张不动。"""
        cur = self.active()
        if cur is not None and (cur.map_id, cur.version) == (ref.map_id, ref.version):
            return self.dir_of(ref)
        tmp = self.root / ".incoming" / f"{ref.map_id}@{ref.version}.adopt"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for f in ref.files:
                try:
                    os.link(Path(src) / f.name, tmp / f.name)
                except OSError:
                    shutil.copy2(Path(src) / f.name, tmp / f.name)
            (tmp / MANIFEST).write_text(json.dumps(ref.to_wire(), ensure_ascii=False),
                                        encoding="utf-8")
            dst = self.dir_of(ref)
            shutil.rmtree(dst, ignore_errors=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dst)
            return dst
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def install(self, ref: MapRef) -> Path:
        """下载、逐个核对、装好(**阻塞,在线程里调**)。返回图的目录。不改 ``active.json``。
        要装的就是正在用的那张(文件都在):不重下 —— 先删了再下,半路断电就一张图都没了。
        本地库里已经有、核得上(这只狗自己建的):也不下。"""
        cur = self.active()
        if cur is not None and (cur.map_id, cur.version) == (ref.map_id, ref.version) \
                and cur.files == ref.files:
            return self.dir_of(ref)
        have = self.local(ref)
        if have is not None:
            return have
        tmp = self.root / ".incoming" / f"{ref.map_id}@{ref.version}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        limit = download_deadline_s(sum(f.size for f in ref.files))
        deadline = time.monotonic() + limit
        try:
            for f in ref.files:
                h, n = hashlib.sha256(), 0
                with open(tmp / f.name, "wb") as fh:
                    fails = 0
                    while True:
                        try:
                            chunks = (self._fetch(ref.map_id, ref.version, f.name) if n == 0 else
                                      self._fetch(ref.map_id, ref.version, f.name, offset=n))
                            for chunk in chunks:
                                n += len(chunk)
                                if n > f.size:
                                    raise MapInstallError(f"{f.name} 比清单里说的大")
                                if time.monotonic() > deadline:
                                    raise MapInstallError(f"下载超过 {limit:g} s 没下完")
                                h.update(chunk)
                                fh.write(chunk)
                                fails = 0
                            if n < f.size:
                                raise ConnectionError(f"连接断了,收到 {n}/{f.size} 字节")
                            break
                        except MapInstallError:
                            raise
                        except FetchRefused as exc:
                            raise MapInstallError(f"{f.name} 下载失败: {exc}") from exc
                        except Exception as exc:
                            fails += 1
                            why = f"{f.name} 下载失败: {type(exc).__name__}: {exc}"
                            if fails >= RESUME_TRIES or time.monotonic() > deadline:
                                raise MapInstallError(f"{why}(连续 {fails} 次)") from exc
                            log.warning("%s;%d 字节处接着下", why, n)
                            self._sleep(min(2.0 ** (fails - 1), 30.0))
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


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def https_fetch(base_url: str, ssl_context: Any, *, timeout_s: float = 60.0) -> Fetch:
    """从站点的狗专用口拉地图文件(mTLS,跟上传同一套证书)。``offset`` 给了就带 ``Range`` 续传;站点
    不认 ``Range``(回 200 整个文件)就把前面那段读掉扔了。站点回 4xx:``FetchRefused``。"""
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    def fetch(map_id: str, version: str, name: str, offset: int = 0) -> Iterable[bytes]:
        url = f"{base_url.rstrip('/')}/maps/{quote(map_id)}/{quote(version)}/{quote(name)}"
        req = urllib.request.Request(url)
        if offset:
            req.add_header("Range", f"bytes={offset}-")
        try:
            r = urllib.request.urlopen(req, context=ssl_context, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise FetchRefused(f"站点回 {exc.code}") from exc
            raise
        with r:
            skip = offset if offset and r.status != 206 else 0
            while skip:
                got = r.read(min(skip, 1 << 20))
                if not got:
                    return
                skip -= len(got)
            while chunk := r.read(1 << 20):
                yield chunk
    return fetch
