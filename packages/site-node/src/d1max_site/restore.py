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


#: 恢复出一个能起的站点必须有的(A2 外审 R1):站点配置、MQTT 的配置和 ACL、站点身份。
REQUIRED = ("config/site.json", "broker/mosquitto.conf", "broker/acl", "ca/ca.key", "ca/ca.crt",
            "ca/server/server.crt", "ca/server/server.key")


def _stamp(p: Path, prefix: str) -> str:
    return p.name.split(".")[0].removeprefix(prefix)


def _complete(dest: Path) -> tuple[Path | None, Path | None]:
    """最新一次**做完了**的备份:(库快照, 清单)。快照有、清单没有的是做到一半断了的,不算(R1)。
    一份清单都没有(老版本做的备份):回 (最新快照, None),调用方算没过。"""
    db = dest / "db"
    snaps = {_stamp(p, "site-"): p for p in db.glob("site-*.db*")
             if p.name.endswith((".db", ".db" + SEALED))}
    mans = {_stamp(p, "manifest-"): p for p in db.glob("manifest-*.json*")
            if p.name.endswith((".json", ".json" + SEALED))}
    both = sorted(set(snaps) & set(mans))
    if both:
        return snaps[both[-1]], mans[both[-1]]
    return (snaps[sorted(snaps)[-1]] if snaps else None), None


def _manifest_problem(dest: Path, box: SealBox | None, man: Path | None, tmp: Path) -> str:
    """选定的那次备份按清单完整吗:回空串 = 完整;否则回原因。校验、恢复、演练共用(A2 复查)。"""
    if man is None:
        return "没有做完的备份(没有清单):要么做到一半断了,要么是老版本做的 —— 等下一次备份"
    try:
        listed = json.loads(_read(box, man, tmp / "m.json").read_bytes())["files"]
    except (SealError, OSError, ValueError, KeyError, TypeError) as exc:
        return f"清单 {man.name} 读不了:{exc}"
    lost = [f for f in listed if not (dest / f).is_file()]
    if lost:
        return f"清单上的 {len(lost)} 个文件不见了(比如 {lost[0]})"
    return ""


def _read(box: SealBox | None, src: Path, tmp: Path) -> Path:
    """加密的解到 ``tmp``,明文的原样。"""
    if src.name.endswith(SEALED):
        if box is None:
            raise SealError(f"{src.name} 是加密的,没给备份密钥")
        box.open_file(src, tmp)
        return tmp
    return src


def _facts(db_path: Path) -> tuple[dict[str, int], list[tuple[str, ...]], list[tuple[str, ...]],
                                   str]:
    """(各表行数, 登记的每一趟, 登记的每张照片 ``(狗, 任务, 时刻, 名字)``, 完整性检查结果)。"""
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
        photos = [tuple(r) for r in con.execute(
            "SELECT r.robot_id, r.mission, r.stamp, p.name FROM run_photos p "
            "JOIN runs r ON r.id = p.run_id")]
        return counts, runs, photos, check
    finally:
        con.close()


def _counts(db_path: Path) -> tuple[dict[str, int], list[tuple[str, ...]], str]:
    counts, runs, _photos, check = _facts(db_path)
    return counts, runs, check


def _check_tree(rep: Report, root: Path, suffix: str, runs: list[tuple[str, ...]],
                photos: list[tuple[str, ...]], where: str) -> None:
    """必须有的组件、登记的每一趟、每张照片都在(R1:只核剩下的文件,整份删掉的发现不了)。"""
    for need in REQUIRED:
        rel = need if not need.startswith("config/") or where == "备份里" else need[7:]
        if not (root / (rel + suffix)).is_file():
            rep.bad(f"{where}没有 {rel}:恢复出来的站点起不来,或者狗、手机要全部重配")
    missing = [r for r in runs if not (root / "evidence" / r[0] / r[1] / r[2]).is_dir()]
    if missing:
        rep.bad(f"{where}登记的 {len(missing)} 趟没有目录(比如 {'/'.join(missing[0])})")
    gone = [p for p in photos
            if not (root / "evidence" / p[0] / p[1] / p[2] / "photos" / (p[3] + suffix)).is_file()]
    if gone:
        rep.bad(f"{where}登记的 {len(gone)} 张照片没有文件(比如 {'/'.join(gone[0])})")


def verify(dest: Path, key: Path | None, *, sample: int | None = None,
           rand: random.Random | None = None) -> Report:
    """校验备份目录 ``dest``(``key`` = 备份密钥;明文备份给 ``None``)。

    要都过才算「能恢复成完整的站点」(A2 外审 R1):最新一次**做完了**的备份(有清单)、清单上的每个
    文件都在、库快照完整、必须的组件(站点配置、MQTT 配置和 ACL、站点身份)齐、登记的每一趟和每张
    照片都在、文件 MAC 对得上。明文备份不带站点身份 —— 只能算数据备份,不过。"""
    dest = Path(dest)
    rep = Report()
    try:
        box = _box(key)
    except SealError as exc:
        rep.bad(str(exc))
        return rep
    suffix = SEALED if box is not None else ""
    snap, man = _complete(dest)
    runs: list[tuple[str, ...]] = []
    photos: list[tuple[str, ...]] = []
    with tempfile.TemporaryDirectory(prefix="d1max-verify-") as tmp:
        if snap is None:
            rep.bad("备份里没有库快照(db/site-*.db)")
        else:
            rep.snapshot = snap.name
            try:
                rep.counts, runs, photos, check = _facts(_read(box, snap, Path(tmp) / "site.db"))
                if check != "ok":
                    rep.bad(f"库快照 {snap.name} 完整性检查没过:{check[:200]}")
            except (SealError, OSError, sqlite3.Error) as exc:
                rep.bad(f"库快照 {snap.name} 打不开:{exc}")
        why = _manifest_problem(dest, box, man, Path(tmp))
        if why:
            rep.bad(why)
    _check_tree(rep, dest, suffix, runs, photos, "备份里")
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
    """把备份 ``dest`` 恢复成站点目录 ``home``(要不存在或是空的)。回恢复后的校验结果。

    **整个恢复在 umask 077 下做**(A2 外审 R3):目录、文件从建出来那一刻就只有站点用户能读写 ——
    CA 私钥、签发过的狗私钥、库都是;中途断了也不留宽权限的明文。用最新一次做完了的备份。"""
    import os
    dest, home = Path(dest), Path(home)
    if home.exists() and any(home.iterdir()):
        raise SealError(f"{home} 不是空的:恢复只往空目录里放(不盖掉现有的站点)")
    box = _box(key)
    snap, man = _complete(dest)
    if snap is None:
        raise SealError("备份里没有库快照(db/site-*.db)")
    # A2 复查:**写之前**先按清单核这次备份完整(没清单、缺文件就不恢复 —— 恢复出来缺东西却报成)
    with tempfile.TemporaryDirectory(prefix="d1max-restore-") as tmp:
        why = _manifest_problem(dest, box, man, Path(tmp))
    if why:
        raise SealError(f"不恢复:{why}")
    old = os.umask(0o077)
    try:
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)

        def put(src: Path, dst: Path) -> None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.name.endswith(SEALED):
                if box is None:
                    raise SealError(f"{src.name} 是加密的,没给备份密钥")
                box.open_file(src, dst.with_name(dst.name[:-len(SEALED)]))
            else:
                shutil.copyfile(src, dst)                 # 不抄源文件的权限:按 umask 077 建
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
        (home / "broker" / "data").mkdir(parents=True, exist_ok=True)   # MQTT 持久化目录
    finally:
        os.umask(old)
    return verify_home(home)


def verify_home(home: Path) -> Report:
    """恢复出来的站点目录能不能起:库完整、必须的组件齐、登记的每一趟和每张照片都在。"""
    home = Path(home)
    rep = Report()
    runs: list[tuple[str, ...]] = []
    photos: list[tuple[str, ...]] = []
    try:
        rep.counts, runs, photos, check = _facts(home / "site.db")
        if check != "ok":
            rep.bad(f"site.db 完整性检查没过:{check[:200]}")
    except (OSError, sqlite3.Error) as exc:
        rep.bad(f"site.db 打不开:{exc}")
    try:
        json.loads((home / "site.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rep.bad(f"site.json 读不了:{exc}")
    _check_tree(rep, home, "", runs, photos, "站点目录里")
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
