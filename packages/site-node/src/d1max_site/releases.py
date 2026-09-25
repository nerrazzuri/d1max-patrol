"""站点的发布目录(W00c5d 第三部分,决策 8:版本也由站点管)。

管理员在站点主机上 ``d1max-site release-add <发布包目录>``:核对包里 ``release.json`` 写的指纹跟
整棵树算出来的一致、名字跟目录名一致,再打成 ``<站点目录>/releases/<版本名>.tar.gz``(算大小与
sha256),登记进 ``releases`` 表。狗从狗专用口下载(mTLS),自己再核一遍。
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract.digest import tree_sha256
from d1max_contract.errors import ContractError
from d1max_contract.releases import ReleaseRef, check_release_name

MANIFEST = "release.json"


class ReleaseCatalogError(ValueError):
    """这个包登记不了 / 找不到。"""


class ReleaseCatalog:
    def __init__(self, home: Path, db, *, now_ms: Callable[[], int]) -> None:
        self.root = Path(home) / "releases"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self._now = now_ms

    def add(self, pkg: Path, *, note: str = "") -> dict[str, Any]:
        pkg = Path(pkg)
        try:
            raw = json.loads((pkg / MANIFEST).read_text(encoding="utf-8"))
            name = check_release_name(raw.get("name"))
        except (OSError, ValueError, ContractError, AttributeError) as exc:
            raise ReleaseCatalogError(f"{pkg} 不像一个发布包:{exc}") from exc
        if name != pkg.name:
            raise ReleaseCatalogError(f"{MANIFEST} 写的是 {name},目录名却是 {pkg.name}")
        for p in pkg.rglob("*"):
            if p.is_symlink() or not (p.is_file() or p.is_dir()) or \
                    (p.is_file() and p.stat().st_nlink > 1):
                # 狗那头只收普通文件与目录;带着链接(软的、硬的)登记,打出来的包里是链接条目,
                # 狗解开之后指纹对不上,永远装不上。
                raise ReleaseCatalogError(f"包里有链接或特殊文件:{p.relative_to(pkg)}")
        got = tree_sha256(pkg, skip=MANIFEST)
        if got != raw.get("content_sha256"):
            raise ReleaseCatalogError(f"包的指纹对不上:自述 {raw.get('content_sha256')},算出 {got}")
        if self.db.query("SELECT 1 FROM releases WHERE name=?", (name,)):
            raise ReleaseCatalogError(f"{name} 已经登记过了")
        out = self.root / f"{name}.tar.gz"
        tmp = out.with_name(out.name + ".tmp")
        with tarfile.open(tmp, "w:gz") as tf:
            tf.add(pkg, arcname=name, filter=_plain)
        h = hashlib.sha256()
        with open(tmp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        os.replace(tmp, out)
        row = {"name": name, "version": str(raw.get("version", "")), "sha256": h.hexdigest(),
               "size": out.stat().st_size, "created_ms": self._now(), "note": note[:200]}
        with self.db.tx() as c:
            c.execute("INSERT INTO releases(name, version, sha256, size, created_ms, note) "
                      "VALUES (:name, :version, :sha256, :size, :created_ms, :note)", row)
        return row

    def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM releases ORDER BY name DESC")]

    def get(self, name: str) -> ReleaseRef:
        rows = self.db.query("SELECT name, sha256, size FROM releases WHERE name=?", (name,))
        if not rows:
            raise ReleaseCatalogError(f"没有这一版:{name}")
        return ReleaseRef(name=rows[0]["name"], sha256=rows[0]["sha256"], size=rows[0]["size"])

    def file_path(self, name: str) -> Path:
        try:
            check_release_name(name)
        except ContractError as exc:
            raise ReleaseCatalogError(str(exc)) from exc
        self.get(name)
        return self.root / f"{name}.tar.gz"


def _plain(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """只打普通文件与目录(链接、设备一律不带,狗那头也不收);属主清掉。"""
    if not (ti.isfile() or ti.isdir()):
        return None
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti
