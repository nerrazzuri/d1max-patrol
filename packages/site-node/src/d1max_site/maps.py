"""站点的地图目录(W00c5d 第二部分,决策 8:站点是地图的唯一权威)。

- 一张图落在 ``<站点目录>/maps/<地图号>/<版本>/``,文件旁边一份 ``map.json``(文件名、大小、sha256),
  登记进 ``maps`` 表。**同一个地图号 + 版本只登记一次,不覆盖**:狗上可能正用着它,换了内容就对不上了。
- 来路两条:狗建完图传上来(接收口把文件收进 ``maps-in/<狗>/<地图号>/<版本>/``,收齐 ``map.json`` 且
  每个文件的大小、哈希都对得上,才搬进目录登记);管理员在站点主机上导入一个目录(CLI)。
- 录包(建图的原料)传上来落 ``bags/<狗>/<包名>/``,登记进 ``bags`` 表。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.maps import MANIFEST, MapFile, MapRef, check_name
from d1max_site.evidence import ChunkWriter, PathRefused, Stored, safe_join

log = logging.getLogger(__name__)


class MapError(ValueError):
    """这张图登记不了 / 找不到。"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class MapCatalog:
    def __init__(self, home: Path, db, *, now_ms: Callable[[], int]) -> None:
        self.root = Path(home) / "maps"
        self.incoming = Path(home) / "maps-in"
        self.bags_root = Path(home) / "bags"
        for d in (self.root, self.incoming, self.bags_root):
            d.mkdir(parents=True, exist_ok=True)
        self.db = db
        self._now = now_ms
        self._lock = threading.Lock()
        self.writer = ChunkWriter()

    # ------------------------------------------------------------ 收(接收口调)

    def put_map_chunk(self, robot_id: str, run: str, rel: str, *, offset: int, data: bytes,
                      total: int) -> Stored:
        """狗传上来的图:``run`` = ``<地图号>/<版本>``,``rel`` = 文件名(或 ``map.json``)。"""
        try:
            map_id, version = run.split("/")
            check_name(map_id, "地图号")
            check_name(version, "版本")
            check_name(rel, "文件名")
        except (ValueError, ContractError) as exc:
            raise PathRefused(f"地图的路径不对:{run!r}/{rel!r}") from exc
        if not self._build_issued(robot_id, map_id, version):
            # 只收站点让它建的那张(W00c5d 内部评审:不然随便哪台登记过的狗都能塞一张图进目录)。
            raise PathRefused(f"站点没让 {robot_id} 建 {map_id}:{version}")
        got = self.writer.write(safe_join(self.incoming, robot_id, map_id, version, rel),
                                offset=offset, data=data, total=total)
        if got.size >= total:
            try:
                self.take_uploaded(robot_id, map_id, version)
            except MapError as exc:
                # 收不下(版本撞了、清单跟文件对不上):**告诉狗永远不收**(它隔离、留着那份、站点出
                # upload_refused 告警),而不是回 200 让它删掉 —— 那样这张图就悄悄没了。
                raise PathRefused(str(exc)) from exc
        return got

    def _build_issued(self, robot_id: str, map_id: str, version: str) -> bool:
        rows = self.db.query("SELECT payload FROM commands WHERE robot_id=? AND kind='map_build'",
                             (robot_id,))
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(p, dict) and (p.get("map_id"), p.get("version")) == (map_id, version):
                return True
        return False

    def build_in_flight(self, map_id: str, version: str) -> bool:
        """站点已经让某台狗建这个版本了(还没收齐登记):再派一台建同一个版本要挡。
        狗回了 ``map_build_failed``(或者回执拒了)的那一次不算 —— 不然这个版本号就永远用不了了。"""
        for r in self.db.query("SELECT robot_id, payload, ack_result FROM commands "
                               "WHERE kind='map_build'"):
            try:
                p = json.loads(r["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(p, dict) or (p.get("map_id"), p.get("version")) != (map_id,
                                                                                  version):
                continue
            if r["ack_result"] not in (None, "", "accepted"):
                continue                          # 狗没接
            if not self._build_failed(r["robot_id"], map_id, version):
                return True
        return False

    def _build_failed(self, robot_id: str, map_id: str, version: str) -> bool:
        for e in self.db.query("SELECT data FROM events WHERE robot_id=? "
                               "AND kind='map_build_failed'", (robot_id,)):
            try:
                d = json.loads(e["data"])
            except (TypeError, ValueError):
                continue
            if isinstance(d, dict) and (d.get("map_id"), d.get("version")) == (map_id, version):
                return True
        return False

    def put_bag_chunk(self, robot_id: str, run: str, rel: str, *, offset: int, data: bytes,
                      total: int) -> Stored:
        """狗传上来的录包:``run`` = 包名,``rel`` = 包里的文件名。"""
        try:
            check_name(run, "包名")
            check_name(rel, "文件名")
        except ContractError as exc:
            raise PathRefused(f"录包的路径不对:{run!r}/{rel!r}") from exc
        path = safe_join(self.bags_root, robot_id, run, rel)
        got = self.writer.write(path, offset=offset, data=data, total=total)
        if got.size >= total:
            self.bag_done(robot_id, run,
                          sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file()))
        return got

    # ------------------------------------------------------------ 查

    def list(self) -> list[dict[str, Any]]:
        return [_row(r) for r in self.db.query(
            "SELECT * FROM maps ORDER BY map_id, created_ms DESC")]

    def get(self, map_id: str, version: str) -> MapRef:
        rows = self.db.query("SELECT files FROM maps WHERE map_id=? AND version=?",
                             (map_id, version))
        if not rows:
            raise MapError(f"没有这张图:{map_id}:{version}")
        return MapRef.from_wire({"map_id": map_id, "version": version,
                                 "files": json.loads(rows[0]["files"])})

    def file_path(self, map_id: str, version: str, name: str) -> Path:
        """狗下载用。名字按契约查过,而且必须在登记的清单里。"""
        try:
            ref = self.get(check_name(map_id, "地图号"), check_name(version, "版本"))
            check_name(name, "文件名")
        except ContractError as exc:
            raise MapError(str(exc)) from exc
        if name not in {f.name for f in ref.files}:
            raise MapError(f"这张图里没有 {name}")
        return self.root / map_id / version / name

    # ------------------------------------------------------------ 登记

    def import_dir(self, src: Path, *, map_id: str, version: str, source: str = "import",
                   note: str = "") -> MapRef:
        """把一个目录里的文件(不含子目录与 ``map.json``)登记成一张图。"""
        try:
            check_name(map_id, "地图号")
            check_name(version, "版本")
            names = sorted(p.name for p in Path(src).iterdir()
                           if p.is_file() and p.name != MANIFEST)
            for n in names:
                check_name(n, "文件名")
        except (ContractError, OSError) as exc:
            raise MapError(str(exc)) from exc
        if not names:
            raise MapError(f"{src} 里没有文件")
        with self._lock:
            dst = self._fresh_dir(map_id, version)
            tmp = dst.with_name(dst.name + ".importing")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            files = []
            for n in names:
                shutil.copy2(Path(src) / n, tmp / n)
                files.append(MapFile(name=n, size=(tmp / n).stat().st_size,
                                     sha256=_sha256(tmp / n)))
            ref = MapRef(map_id=map_id, version=version, files=tuple(files))
            (tmp / MANIFEST).write_text(json.dumps(ref.to_wire(), ensure_ascii=False, indent=2),
                                        encoding="utf-8")
            os.replace(tmp, dst)
            self._register(ref, source=source, note=note)
        return ref

    def take_uploaded(self, robot_id: str, map_id: str, version: str) -> MapRef | None:
        """狗传上来的一张图:``map.json`` 到了、清单里每个文件都在且大小与哈希对得上 → 搬进目录登记。
        还没收齐返回 None(下一个文件收完再来);清单本身不对抛 :class:`MapError`。"""
        src = self.incoming / robot_id / map_id / version
        try:
            ref = MapRef.from_wire(json.loads((src / MANIFEST).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (ValueError, ContractError, OSError) as exc:
            raise MapError(f"{robot_id} 传来的 {map_id}:{version} 清单读不懂:{exc}") from exc
        if (ref.map_id, ref.version) != (map_id, version):
            raise MapError(f"清单里写的是 {ref.map_id}:{ref.version},目录是 {map_id}:{version}")
        for f in ref.files:
            p = src / f.name
            if not p.is_file() or p.stat().st_size < f.size:
                return None                        # 还没收齐(还在传)
            if p.stat().st_size != f.size or _sha256(p) != f.sha256:
                raise MapError(f"{robot_id} 传来的 {map_id}:{version} 里 {f.name} 跟清单对不上")
        with self._lock:
            dst = self._fresh_dir(map_id, version)
            tmp = dst.with_name(dst.name + ".taking")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            for f in ref.files:
                shutil.copy2(src / f.name, tmp / f.name)
            shutil.copy2(src / MANIFEST, tmp / MANIFEST)
            os.replace(tmp, dst)
            self._register(ref, source=robot_id, note="")
        shutil.rmtree(src, ignore_errors=True)     # 收进目录了:收件那份不留
        log.info("%s 建的图收齐了:%s:%s", robot_id, map_id, version)
        return ref

    def _fresh_dir(self, map_id: str, version: str) -> Path:
        dst = self.root / map_id / version
        if dst.exists() or self.db.query("SELECT 1 FROM maps WHERE map_id=? AND version=?",
                                         (map_id, version)):
            raise MapError(f"{map_id}:{version} 已经有了;换一个版本号(狗上可能正用着它)")
        dst.parent.mkdir(parents=True, exist_ok=True)
        return dst

    def _register(self, ref: MapRef, *, source: str, note: str) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO maps(map_id, version, source, created_ms, files, note) "
                      "VALUES (?,?,?,?,?,?)",
                      (ref.map_id, ref.version, source, self._now(),
                       json.dumps([f.to_wire() for f in ref.files]), note[:200]))

    # ------------------------------------------------------------ 录包

    def bag_done(self, robot_id: str, name: str, size: int) -> None:
        """登记(或刷新)一个录包。狗那头的包名带录的时刻,同名不会是两个包。"""
        with self.db.tx() as c:
            c.execute("INSERT INTO bags(robot_id, name, first_ms, last_ms, bytes) "
                      "VALUES (?,?,?,?,?) ON CONFLICT(robot_id, name) DO UPDATE SET "
                      "last_ms=excluded.last_ms, bytes=excluded.bytes",
                      (robot_id, name, self._now(), self._now(), size))

    def bags(self, robot_id: str | None = None) -> list[dict[str, Any]]:
        if robot_id is None:
            return [dict(r) for r in self.db.query("SELECT * FROM bags ORDER BY first_ms DESC")]
        return [dict(r) for r in self.db.query(
            "SELECT * FROM bags WHERE robot_id=? ORDER BY first_ms DESC", (robot_id,))]


def _row(r) -> dict[str, Any]:
    d = dict(r)
    try:
        d["files"] = json.loads(d.get("files") or "[]")
    except ValueError:
        d["files"] = []
    return d
