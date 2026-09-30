"""狗上正在用的那一张图(W00c5d 第二部分,决策 8:站点是地图的唯一权威,狗上只有工作副本)。

- 站点下发 ``map_activate``(地图号、版本、每个文件的大小与 sha256)→ 从站点的狗专用口下载
  (mTLS),**每个文件边下边算哈希,大小或哈希对不上整张不装**;装好了交给适配器载入
  (适配器没有载入这一步的,这台狗就不报这项能力,站点也不会发)。
- 狗上只留正在用的这一张:``<store>/maps/<地图号>/<版本>/``,旁边 ``active.json`` 记着是哪张;
  别的一律删。
  重启时按 ``active.json`` 接着用 —— **先完整校验**(:meth:`MapKeeper.verify_active`,W09g):清单跟
  声明一样、每个文件是普通文件、大小与 sha256 都对;校验不过不载、不退回 ``--map``(见代理)。
  :meth:`MapKeeper.active` 只读声明,不碰文件,状态查询里随便调。
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
import stat
import threading
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
#: 收养来的版本目录里的标记(内容是收养的时刻,纳秒):换图时最近收养的那一份留着。
ADOPTED = ".adopted"
#: 站点回这几个才算「不给」(没有这个、Range 不对);别的 4xx(比如证书一时认不出的 403)照样重试。
REFUSED_HTTP = (404, 410, 416)
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


class MapIntegrityError(RuntimeError):
    """正在用的那张图校验不过(W09g):声明坏了、清单对不上、文件缺了、不是普通文件、大小或哈希不对。
    消息说是哪个文件、哪一项,给站点看。"""


class MapKeeper:
    def __init__(self, root: Path | str, *, fetch: Fetch,
                 sleep: Callable[[float], Any] = time.sleep) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._fetch = fetch
        self._sleep = sleep
        #: 收养(建图收尾,线程里)、装(下载,线程里)、提交与扔掉(事件循环里)改同一个目录:一把锁串着
        #: (内审应修 5)。
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 现在是哪张

    def active(self) -> MapRef | None:
        """``active.json`` 声明的那张(**只读声明**,不碰图的文件:状态查询、周期里随便调)。没有、
        解析不了都回 None。文件坏没坏归 :meth:`verify_active`。"""
        try:
            return self.declared()
        except MapIntegrityError as exc:
            log.warning("%s", exc)
            return None

    def declared(self) -> MapRef | None:
        """同 :meth:`active`,但分得清「没有」(None)和「有、坏了」(抛
        :class:`MapIntegrityError`)。"""
        path = self.root / "active.json"
        try:
            text = path.read_text("utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MapIntegrityError(f"active.json 读不了: {exc}") from exc
        try:
            return MapRef.from_wire(json.loads(text))
        except (ValueError, ContractError) as exc:
            raise MapIntegrityError(f"active.json 坏了: {exc}") from exc

    def verify_active(self) -> MapRef | None:
        """正在用的那张完整校验(W09g,**阻塞、要算整张图的 sha256**:只在代理起来、激活时调)。没有
        ``active.json`` 回 None;校验过了回它;不过抛 :class:`MapIntegrityError`。"""
        ref = self.declared()
        if ref is None:
            return None
        problem = self._problem(ref)
        if problem:
            raise MapIntegrityError(f"正在用的图 {ref.map_id}:{ref.version} {problem}")
        return ref

    def _problem(self, ref: MapRef) -> str:
        """``ref`` 在本地库里那份哪儿不对(空串 = 都对):版本目录在、不是符号链接;清单 ``map.json`` 跟
        ``ref`` 一样;每个文件是普通文件(不跟符号链接)、大小、sha256 都对。"""
        d = self.dir_of(ref)
        try:
            st = os.lstat(d)
        except OSError:
            return "的目录不在"
        if not stat.S_ISDIR(st.st_mode):              # lstat:符号链接不算目录
            return "的目录是符号链接或不是目录"
        try:
            have = MapRef.from_wire(json.loads((d / MANIFEST).read_text("utf-8")))
        except (OSError, ValueError, ContractError) as exc:
            return f"的清单 {MANIFEST} 读不了或坏了: {exc}"[:200]
        if have != ref:
            return f"的清单 {MANIFEST} 跟声明的不一样"
        for f in ref.files:
            p = d / f.name                          # 名字契约里限死了(不许 / 与点开头):落在目录里
            try:
                fst = os.lstat(p)
            except OSError:
                return f"缺了 {f.name}"
            if not stat.S_ISREG(fst.st_mode):
                return f"的 {f.name} 不是普通文件(符号链接、目录、设备都不认)"
            if fst.st_size != f.size:
                return f"的 {f.name} 大小不对({fst.st_size},应为 {f.size})"
            if _sha256(p) != f.sha256:
                return f"的 {f.name} sha256 不对(内容坏了)"
        return ""

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
        """本地库里已经有这个版本、清单一样、每个文件是普通文件且大小和 sha256 都核得上:回目录;不然
        None(**阻塞**,算哈希)。"""
        problem = self._problem(ref)
        if problem:
            log.info("本地库里的 %s:%s %s:要下", ref.map_id, ref.version, problem)
            return None
        return self.dir_of(ref)

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
            (tmp / ADOPTED).write_text(str(time.time_ns()), encoding="ascii")
            with self._lock:
                cur = self.active()
                if cur is not None and (cur.map_id, cur.version) == (ref.map_id, ref.version):
                    return self.dir_of(ref)                 # 这当中被激活了:不动它
                dst = self.dir_of(ref)
                shutil.rmtree(dst, ignore_errors=True)
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, dst)
            return dst
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def install(self, ref: MapRef) -> Path:
        """下载、逐个核对、装好(**阻塞,在线程里调**)。返回图的目录。不改 ``active.json``。
        本地库里已经有、逐个核得上(正在用的这一版、这只狗自己建的):不下。**正在用的这一版也逐个核**
        (W09g:原来清单一样就直接回,内容坏了站点重新下发也修不了);核不上就下到 ``.incoming``、下完
        核过了才换进去 —— 下载失败原来那份还在。"""
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
            with self._lock:
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
        # 正在用的已经记下了:之后的清理出什么错都只记一笔(不然调用方当成「记不下」,狗报的跟盘上的
        # 不一致 —— 内审应修 5)。点开头的(收养、下载的临时目录)不碰;最近收养的那一份留着。
        try:
            with self._lock:
                keep = {self.dir_of(ref).resolve()}
                adopted = self._adopted()
                if adopted:
                    keep.add(adopted[-1].resolve())
                for m in self.root.iterdir():
                    if not m.is_dir() or m.name.startswith("."):
                        continue
                    for v in m.iterdir():
                        if v.is_dir() and not v.name.startswith(".") and v.resolve() not in keep:
                            shutil.rmtree(v, ignore_errors=True)
                    if m.name != ref.map_id and not any(m.iterdir()):
                        m.rmdir()
        except OSError:
            log.warning("换图之后清理旧图没清干净", exc_info=True)

    def _adopted(self) -> list[Path]:
        """收养来的版本目录,按收养先后排。"""
        got = []
        for mark in self.root.glob(f"*/*/{ADOPTED}"):
            if mark.parent.parent.name.startswith("."):
                continue
            try:
                got.append((int(mark.read_text("ascii")), mark.parent))
            except (OSError, ValueError):
                continue
        return [d for _, d in sorted(got)]

    def discard(self, ref: MapRef) -> None:
        """装好了但没载进去:删掉,狗上照旧用原来那张。收养来的不删(建图的狗自己那份,删了要重下)。"""
        with self._lock:
            cur = self.active()
            d = self.dir_of(ref)
            if (cur is None or (cur.map_id, cur.version) != (ref.map_id, ref.version)) \
                    and not (d / ADOPTED).exists():
                shutil.rmtree(d, ignore_errors=True)


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
            if exc.code in REFUSED_HTTP:
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
