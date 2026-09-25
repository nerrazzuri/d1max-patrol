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
import shutil
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract.intake import dog_may_upload, split_run

log = logging.getLogger(__name__)

#: 一个路径段最多多少字节(Linux 的文件名上限)。
_MAX_SEG_BYTES = 255


class PathRefused(ValueError):
    """路径不合规。拒绝就是拒绝,不清洗了接着用。"""


def safe_join(root: Path, *parts: str) -> Path:
    """把网络上来的路径片段接到 ``root`` 底下。两道闸:逐段查(空段、``.``、``..``、控制字符、
    孤立代理、超过 255 字节),接完 ``resolve()`` 再查一次还在 ``root`` 底下。

    按**站点主机(Linux)**的规矩查:中文、冒号、问号这些都是合法文件名(任务名「夜巡 22:00」),
    不因为 Windows 不认就拒 —— 拒了狗那头就永远传不完(W00c5d 内部评审)。"""
    root = root.resolve()
    cur = root
    for part in parts:
        for seg in part.split("/"):
            if not seg or not seg.strip() or seg in {".", ".."}:
                raise PathRefused(f"路径段不合规:{part!r}")
            if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in seg):
                raise PathRefused(f"路径段里有控制字符或孤立代理:{seg!r}")
            if len(seg.encode("utf-8")) > _MAX_SEG_BYTES:
                raise PathRefused(f"路径段过长:{part!r}")
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


#: 一个文件最大多少字节(录包最大;照片、事件远小于它)。超了永远不收。
MAX_FILE_BYTES = 8 * 1024 ** 3
#: 站点盘剩这么多就不再收(先让狗等着 —— 库也在这块盘上,写满了整个站点都停)。
MIN_FREE_BYTES = 2 * 1024 ** 3
MIN_FREE_RATIO = 0.02
#: 增量哈希最多记多少个文件(追加型的 ``.jsonl`` 收完了也留着,下次追加不用整个重算)。
HASH_CACHE = 256


class DiskFull(OSError):
    """站点盘快满了:这一块先不收,狗过一会儿再来。"""


class ChunkWriter:
    """按块落盘(三种偏移规矩见模块说明),返回站点对自己存下的字节算的哈希。证据库、地图、录包共用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: 按路径分 64 把锁(同一个文件的两块不许交错写;锁的个数有上限,不随文件数涨)。
        self._stripes = [threading.Lock() for _ in range(64)]
        #: 增量哈希:路径 → (已算到的长度, 哈希对象)。有上限,最久没用的先扔。
        self._hashes: OrderedDict[Path, tuple[int, Any]] = OrderedDict()

    def _remember(self, path: Path, n: int, h: Any) -> None:
        with self._lock:
            self._hashes[path] = (n, h)
            self._hashes.move_to_end(path)
            while len(self._hashes) > HASH_CACHE:
                self._hashes.popitem(last=False)

    def write(self, path: Path, *, offset: int, data: bytes, total: int) -> Stored:
        if offset < 0 or total < 0 or offset + len(data) > total:
            raise ValueError(f"偏移或总长不对:offset={offset} len={len(data)} total={total}")
        if total > MAX_FILE_BYTES:
            raise PathRefused(f"文件太大({total} 字节,上限 {MAX_FILE_BYTES})")
        here = path.parent
        while not here.exists() and here != here.parent:
            here = here.parent
        usage = shutil.disk_usage(here)
        if usage.free < max(MIN_FREE_BYTES, usage.total * MIN_FREE_RATIO):
            raise DiskFull(f"站点盘只剩 {usage.free // 2**20} MB")
        with self._stripes[hash(path) % len(self._stripes)]:
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
                self._remember(path, size, h)
                digest = h.copy().hexdigest()
            else:
                digest = self._full_hash(path)
            if size >= total and not path.name.endswith(".jsonl"):
                with self._lock:
                    self._hashes.pop(path, None)       # 照片这种收完就不会再变
        return Stored(size=size, sha256=digest)

    def _full_hash(self, path: Path) -> str:
        h = hashlib.sha256()
        n = 0
        if path.exists():
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
                    n += len(chunk)
        self._remember(path, n, h)
        return h.copy().hexdigest()


class EvidenceStore:
    def __init__(self, root: Path | str, db, *, now_ms: Callable[[], int]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self._now = now_ms
        self.writer = ChunkWriter()
        #: 收完一趟里一个文件之后调(判读排队用)。
        self.on_file: list[Callable[[int, str], None]] = []

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
        mission, stamp = parts
        path = safe_join(self.root, robot_id, mission, stamp, rel)
        got = self.writer.write(path, offset=offset, data=data, total=total)
        if got.size >= total:
            self._index(robot_id, mission, stamp, rel)
        return got

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
        size = sum(p.stat().st_size for p in run.rglob("*") if p.is_file())
        with self.db.tx() as c:
            c.execute("INSERT INTO runs(robot_id, mission, stamp, first_ms, last_ms) "
                      "VALUES (?,?,?,?,?) ON CONFLICT(robot_id, mission, stamp) DO NOTHING",
                      (robot_id, mission, stamp, now, now))
            row = c.execute("SELECT id FROM runs WHERE robot_id=? AND mission=? AND stamp=?",
                            (robot_id, mission, stamp)).fetchone()
            if rel.startswith("photos/"):
                # 收齐的照片才登记(还在传的那张不判读、更不当基线)。
                c.execute("INSERT INTO run_photos(run_id, name, done_ms) VALUES (?,?,?) "
                          "ON CONFLICT(run_id, name) DO UPDATE SET done_ms=excluded.done_ms",
                          (row["id"], rel.split("/", 1)[1], now))
            photos = c.execute("SELECT COUNT(*) AS n FROM run_photos WHERE run_id=?",
                               (row["id"],)).fetchone()["n"]
            c.execute("UPDATE runs SET last_ms=?, photos=?, bytes=?, "
                      "finished=COALESCE(?, finished), result=COALESCE(?, result) WHERE id=?",
                      (now, photos, size, finished, result, row["id"]))
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

    def runs_in(self, *, robot_id: str | None, since_stamp: str, until_stamp: str
                ) -> list[dict[str, Any]]:
        """按**开跑时刻**(目录名 ``<UTC 时刻>[-后缀]``,字典序就是时间序)取一段,不设上限。"""
        q = "SELECT * FROM runs WHERE stamp>=? AND stamp<?"
        args: list[Any] = [since_stamp, until_stamp]
        if robot_id is not None:
            q += " AND robot_id=?"
            args.append(robot_id)
        return [_row(r) for r in self.db.query(q + " ORDER BY stamp, id", tuple(args))]

    def run(self, run_id: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM runs WHERE id=?", (run_id,))
        return _row(rows[0]) if rows else None

    def dir_of(self, run: dict[str, Any]) -> Path:
        return self.run_dir(run["robot_id"], run["mission"], run["stamp"])

    def done_photos(self, run_id: int) -> dict[str, int]:
        """收齐了的照片:名字 → 收齐的时刻。"""
        return {r["name"]: r["done_ms"] for r in self.db.query(
            "SELECT name, done_ms FROM run_photos WHERE run_id=?", (run_id,))}

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
