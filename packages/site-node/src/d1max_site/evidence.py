"""证据库(W00c5d,决策 8):狗传上来的运行记录落在站点,站点是唯一权威。

- 落盘:``<站点目录>/evidence/<狗>/<任务>/<时刻>/<文件>``。路径段逐段查、接完再查一次还在根底下
  (网络上来的名字一律不信,拒绝就是拒绝,不清洗了接着用)。
- 收一块:三种偏移 —— 正好接上就追加;比盘上小就是重传,截到那里再写;比盘上大就是中间缺了一段,
  不写,把盘上真实的大小报回去(狗看见比自己以为的小就从头来)。**哈希是站点对自己存下的字节算的**,
  不是回显狗说的。接着上一块往下追加时增量算(每块整文件重算,录包那种大文件会是平方级),别的
  情况整文件重算。
- 登记:每收完一个文件,更新 ``runs`` 表(这一趟的起止、清单里的汇总、照片数、字节数)。
- 判读结论(``findings.json``)、人工复核(``review.json``)是站点自己写的,狗传不上来
  (白名单见 :mod:`d1max_contract.intake`)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract.intake import dog_may_upload, split_run

log = logging.getLogger(__name__)

_BAD_CHARS = frozenset("\\:\x00*?<>|\"")
_MAX_SEG = 255


class PathRefused(ValueError):
    """路径不合规。拒绝就是拒绝,不清洗了接着用。"""


def safe_join(root: Path, *parts: str) -> Path:
    """把网络上来的路径片段接到 ``root`` 底下。两道闸:逐段查(``..``、空段、控制字符、超长、尾随
    空格或点、怪字符),接完 ``resolve()`` 再查一次还在 ``root`` 底下。"""
    root = root.resolve()
    cur = root
    for part in parts:
        for seg in part.split("/"):
            if not seg or not seg.strip() or seg in {".", ".."}:
                raise PathRefused(f"路径段不合规:{part!r}")
            if len(seg.encode("utf-16-le", "surrogatepass")) // 2 > _MAX_SEG:
                raise PathRefused(f"路径段过长:{part!r}")
            if seg != seg.rstrip(" ."):
                raise PathRefused(f"路径段有尾随空格或点:{seg!r}")
            if _BAD_CHARS & set(seg) or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF
                                            for c in seg):
                raise PathRefused(f"路径段里有不许出现的字符:{seg!r}")
            cur = cur / seg
    out = cur.resolve()
    if out != root and root not in out.parents:
        raise PathRefused(f"接出来的路径跑到 {root} 外面去了")
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Stored:
    """落盘之后的实况。``sha256`` 是站点对自己存下的字节算的。"""

    size: int
    sha256: str


class EvidenceStore:
    def __init__(self, root: Path | str, db, *, now_ms: Callable[[], int]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self._now = now_ms
        self._lock = threading.Lock()
        #: 按路径分 64 把锁(同一个文件的两块不许交错写;锁的个数有上限,不随文件数涨)。
        self._stripes = [threading.Lock() for _ in range(64)]
        #: 增量哈希:路径 → (已算到的长度, 哈希对象)。文件收完就扔掉。
        self._hashes: dict[Path, tuple[int, Any]] = {}
        #: 收完一趟里一个文件之后调(判读排队用)。
        self.on_file: list[Callable[[int, str], None]] = []

    def _path_lock(self, path: Path) -> threading.Lock:
        return self._stripes[hash(path) % len(self._stripes)]

    def run_dir(self, robot_id: str, mission: str, stamp: str) -> Path:
        return safe_join(self.root, robot_id, mission, stamp)

    # ------------------------------------------------------------ 收

    def put(self, robot_id: str, run: str, rel: str, *, offset: int, data: bytes,
            total: int) -> Stored:
        parts = split_run(run)
        if parts is None:
            raise PathRefused(f"一趟的目录名要是 <任务>/<时刻>:{run!r}")
        if not dog_may_upload(rel):
            raise PathRefused(f"站点不收这种文件:{rel!r}")
        if offset < 0 or total < 0 or offset + len(data) > total:
            raise ValueError(f"偏移或总长不对:offset={offset} len={len(data)} total={total}")
        mission, stamp = parts
        path = safe_join(self.root, robot_id, mission, stamp, rel)
        with self._path_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            have = path.stat().st_size if path.exists() else 0
            if offset > have:
                return Stored(size=have, sha256=self._full_hash(path))
            with open(path, "r+b" if path.exists() else "w+b") as fh:
                fh.seek(offset)
                fh.write(data)
                fh.truncate(offset + len(data))
                fh.flush()
                os.fsync(fh.fileno())
            size = offset + len(data)
            cached = self._hashes.get(path)
            if cached is not None and cached[0] == offset == have:
                h = cached[1]
                h.update(data)
                self._hashes[path] = (size, h)
                digest = h.copy().hexdigest()
            else:
                digest = self._full_hash(path)
            if size >= total:
                with self._lock:
                    self._hashes.pop(path, None)
        if size >= total:
            self._index(robot_id, mission, stamp, rel)
        return Stored(size=size, sha256=digest)

    def _full_hash(self, path: Path) -> str:
        h = hashlib.sha256()
        n = 0
        if path.exists():
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
                    n += len(chunk)
        with self._lock:
            self._hashes[path] = (n, h)
        return h.copy().hexdigest()

    # ------------------------------------------------------------ 登记

    def _index(self, robot_id: str, mission: str, stamp: str, rel: str) -> None:
        run = self.run_dir(robot_id, mission, stamp)
        now = self._now()
        finished, result = None, None
        if rel == "manifest.json":
            try:
                m = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
                summary = m.get("summary") if isinstance(m, dict) else None
                if isinstance(summary, dict) and summary:
                    finished, result = 1, str(summary.get("result", ""))[:64]
            except (OSError, ValueError):
                log.warning("%s/%s/%s 的清单读不懂", robot_id, mission, stamp)
        photos = len([p for p in (run / "photos").glob("*") if p.is_file()]) \
            if (run / "photos").is_dir() else 0
        size = sum(p.stat().st_size for p in run.rglob("*") if p.is_file())
        with self.db.tx() as c:
            c.execute("INSERT INTO runs(robot_id, mission, stamp, first_ms, last_ms) "
                      "VALUES (?,?,?,?,?) ON CONFLICT(robot_id, mission, stamp) DO NOTHING",
                      (robot_id, mission, stamp, now, now))
            c.execute("UPDATE runs SET last_ms=?, photos=?, bytes=?, "
                      "finished=COALESCE(?, finished), result=COALESCE(?, result) "
                      "WHERE robot_id=? AND mission=? AND stamp=?",
                      (now, photos, size, finished, result, robot_id, mission, stamp))
            row = c.execute("SELECT id FROM runs WHERE robot_id=? AND mission=? AND stamp=?",
                            (robot_id, mission, stamp)).fetchone()
        for cb in list(self.on_file):
            try:
                cb(row["id"], rel)
            except Exception:
                log.exception("收完文件的回调炸了")

    # ------------------------------------------------------------ 查

    def runs(self, *, robot_id: str | None = None, since_ms: int | None = None,
             until_ms: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if robot_id is not None:
            q += " AND robot_id=?"
            args.append(robot_id)
        if since_ms is not None:
            q += " AND first_ms>=?"
            args.append(since_ms)
        if until_ms is not None:
            q += " AND first_ms<?"
            args.append(until_ms)
        q += " ORDER BY stamp DESC, id DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        return [_row(r) for r in self.db.query(q, tuple(args))]

    def run(self, run_id: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM runs WHERE id=?", (run_id,))
        return _row(rows[0]) if rows else None

    def dir_of(self, run: dict[str, Any]) -> Path:
        return self.run_dir(run["robot_id"], run["mission"], run["stamp"])

    def photo_path(self, run: dict[str, Any], name: str) -> Path:
        if "/" in name or not name:
            raise PathRefused(f"照片名不合规:{name!r}")
        return safe_join(self.dir_of(run), "photos", name)


def _row(r) -> dict[str, Any]:
    d = dict(r)
    try:
        d["verdicts"] = json.loads(d.get("verdicts") or "{}")
    except ValueError:
        d["verdicts"] = {}
    d["finished"] = bool(d.get("finished"))
    return d
