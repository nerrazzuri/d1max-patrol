"""备份校验、恢复、灾备演练(商业化 A2,决策 53)。

备份(:mod:`d1max_site.backup`)每小时做一次,可「做了」不等于「能恢复」。这里三件事:

- :func:`verify`:备份目录能不能用 ——
  - 最新的库快照解得开、SQLite 完整性检查过;
  - 每个加密文件 MAC 对得上(只核不解,不落明文;``sample`` 给了就只抽这么多个,站点每周自动跑的是抽查);
  - 站点身份齐:``site.json``、CA(私钥、站点服务证书)—— 没有它们,新主机上狗要全部重登记、手机要
    重加站点;
  - 库里登记的每一趟,备份里都有它的目录。
- :func:`restore`:把备份恢复成一个站点目录(要一个空目录)。恢复完照样跑一遍 :func:`verify_home`。
- :func:`drill`:演练 —— 恢复到临时目录、跟正在跑的站点对一下数、删掉临时目录,记一笔。

密钥(备份密钥、口令密钥、证据私钥)**不在备份里**:装机时离线另存,恢复时放回 ``/etc/d1max-site/``。
"""

from __future__ import annotations

import json
import random
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d1max_site.backup import SEALED
from d1max_site.sealbox import SealBox, SealError

#: 备份里这几块恢复到站点目录的哪儿(备份里的名字 → 站点目录下的名字)。
LAYOUT = {"evidence": "evidence", "maps": "maps", "bags": "bags", "releases": "releases",
          "ca": "ca", "broker": "broker"}
#: 跟正在跑的站点对数的表。
COUNTED = ("robots", "accounts", "runs", "bundles")


@dataclass
class Report:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    checked_files: int = 0
    snapshot: str = ""

    def bad(self, why: str) -> None:
        self.ok = False
        self.problems.append(why)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": self.problems[:50], "counts": self.counts,
                "checked_files": self.checked_files, "snapshot": self.snapshot}


def _box(key: Path | None) -> SealBox | None:
    if key is None:
        return None
    if not Path(key).is_file():
        raise SealError(f"没有备份密钥 {key}(装机时离线另存的那一份)")
    raw = Path(key).read_bytes()
    return SealBox(raw)


def _latest_snapshot(dest: Path) -> Path | None:
    snaps = sorted(p for p in (dest / "db").glob("site-*.db*")
                   if p.name.endswith((".db", ".db" + SEALED)))
    return snaps[-1] if snaps else None


def _counts(db_path: Path) -> tuple[dict[str, int], list[tuple[str, str, str]], str]:
    """(各表行数, 登记的每一趟, 完整性检查结果)。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for t in COUNTED:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                counts[t] = -1
        runs = [tuple(r) for r in con.execute("SELECT robot_id, mission, stamp FROM runs")]
        return counts, runs, check
    finally:
        con.close()


def verify(dest: Path, key: Path | None, *, sample: int | None = None,
           rand: random.Random | None = None) -> Report:
    """校验备份目录 ``dest``(``key`` = 备份密钥;明文备份给 ``None``)。"""
    dest = Path(dest)
    rep = Report()
    try:
        box = _box(key)
    except SealError as exc:
        rep.bad(str(exc))
        return rep
    snap = _latest_snapshot(dest)
    runs: list[tuple[str, str, str]] = []
    if snap is None:
        rep.bad("备份里没有库快照(db/site-*.db)")
    else:
        rep.snapshot = snap.name
        with tempfile.TemporaryDirectory(prefix="d1max-verify-") as tmp:
            plain = Path(tmp) / "site.db"
            try:
                if snap.name.endswith(SEALED):
                    if box is None:
                        raise SealError("库快照是加密的,没给备份密钥")
                    box.open_file(snap, plain)
                else:
                    shutil.copy2(snap, plain)
                rep.counts, runs, check = _counts(plain)
                if check != "ok":
                    rep.bad(f"库快照 {snap.name} 完整性检查没过:{check[:200]}")
            except (SealError, OSError, sqlite3.Error) as exc:
                rep.bad(f"库快照 {snap.name} 打不开:{exc}")
    # 站点身份
    cfg = dest / "config" / ("site.json" + (SEALED if box is not None else ""))
    if not cfg.is_file():
        rep.bad("备份里没有 site.json(站点身份)")
    if box is not None:
        for need in ("ca/ca.key", "ca/ca.crt", "ca/server/server.crt", "ca/server/server.key"):
            if not (dest / (need + SEALED)).is_file():
                rep.bad(f"备份里没有 {need}:新主机上狗要全部重登记、手机要重加站点")
    # 每一趟都在
    missing = [r for r in runs if not (dest / "evidence" / r[0] / r[1] / r[2]).is_dir()]
    if missing:
        rep.bad(f"库里登记的 {len(missing)} 趟在备份里没有目录(比如 {'/'.join(missing[0])})")
    # 文件:加密的核 MAC(只核不解)
    files = [p for p in dest.rglob("*") if p.is_file() and p.parent.name != "db"
             and not p.name.startswith(".")]
    if sample is not None and len(files) > sample:
        files = (rand or random.Random()).sample(files, sample)
    for p in files:
        try:
            if p.name.endswith(SEALED):
                if box is None:
                    raise SealError("是加密的,没给备份密钥")
                box.verify_file(p)
            else:
                with open(p, "rb") as f:
                    while f.read(1 << 20):
                        pass
        except (SealError, OSError) as exc:
            rep.bad(f"{p.relative_to(dest)}:{exc}")
        rep.checked_files += 1
    return rep


def restore(dest: Path, key: Path | None, home: Path) -> Report:
    """把备份 ``dest`` 恢复成站点目录 ``home``(要不存在或是空的)。回恢复后的校验结果。"""
    dest, home = Path(dest), Path(home)
    if home.exists() and any(home.iterdir()):
        raise SealError(f"{home} 不是空的:恢复只往空目录里放(不盖掉现有的站点)")
    box = _box(key)
    snap = _latest_snapshot(dest)
    if snap is None:
        raise SealError("备份里没有库快照(db/site-*.db)")
    home.mkdir(parents=True, exist_ok=True)

    def put(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.name.endswith(SEALED):
            if box is None:
                raise SealError(f"{src.name} 是加密的,没给备份密钥")
            box.open_file(src, dst.with_name(dst.name[:-len(SEALED)]))
        else:
            shutil.copy2(src, dst)
    put(snap, home / ("site.db" + (SEALED if snap.name.endswith(SEALED) else "")))
    for f in (dest / "config").glob("*") if (dest / "config").is_dir() else ():
        if f.is_file():
            put(f, home / f.name)
    for name, target in LAYOUT.items():
        root = dest / name
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                put(f, home / target / f.relative_to(root))
    key_file = home / "ca" / "ca.key"
    if key_file.exists():
        key_file.chmod(0o600)
    for p in (home / "ca" / "server").glob("*.key") if (home / "ca" / "server").is_dir() else ():
        p.chmod(0o600)
    return verify_home(home)


def verify_home(home: Path) -> Report:
    """恢复出来的站点目录能不能起:库完整、身份齐、登记的每一趟都有目录。"""
    home = Path(home)
    rep = Report()
    db = home / "site.db"
    runs: list[tuple[str, str, str]] = []
    try:
        rep.counts, runs, check = _counts(db)
        if check != "ok":
            rep.bad(f"site.db 完整性检查没过:{check[:200]}")
    except (OSError, sqlite3.Error) as exc:
        rep.bad(f"site.db 打不开:{exc}")
    try:
        json.loads((home / "site.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rep.bad(f"site.json 读不了:{exc}")
    for need in ("ca/ca.key", "ca/ca.crt", "ca/server/server.crt", "ca/server/server.key"):
        if not (home / need).is_file():
            rep.bad(f"没有 {need}")
    missing = [r for r in runs if not (home / "evidence" / r[0] / r[1] / r[2]).is_dir()]
    if missing:
        rep.bad(f"登记的 {len(missing)} 趟没有目录(比如 {'/'.join(missing[0])})")
    return rep


def drill(live_db: Path, dest: Path, key: Path | None, *,
          now_ms: Callable[[], int]) -> dict[str, Any]:
    """演练:最新备份恢复到临时目录、校验、跟正在跑的站点对数,删掉临时目录。回结果(调用方记库)。"""
    with tempfile.TemporaryDirectory(prefix="d1max-drill-") as tmp:
        home = Path(tmp) / "site"
        try:
            rep = restore(dest, key, home)
        except (SealError, OSError) as exc:
            return {"ok": False, "at_ms": now_ms(), "problems": [str(exc)], "diff": {}}
        live, _runs, _check = _counts(Path(live_db))
        # 对数只是给人看的:库快照最多晚一个小时,这期间新收的、删掉的都会让两边不一样,不算失败
        return {"ok": rep.ok, "at_ms": now_ms(), "problems": rep.problems[:20],
                "counts": rep.counts, "live": live}
