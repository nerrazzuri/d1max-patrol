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


#: 站点的证据私钥(W30b,决策 51;``site.json`` 的 ``evidence_key`` 可以改)。安装脚本生成,不进备份。
DEFAULT_EVIDENCE_KEY = Path("/etc/d1max-site/evidence.key")


def open_sealed(path: Path, dst: Path, key: bytes | None) -> str:
    """狗封好的证据(W30b)解开成 ``dst``。回空串;解不开回原因(以后配好私钥用 ``d1max-site
    evidence-open`` 补解)。

    **封好的那份不在这里删**(W30b 外审 2):调用方登记成了才删。先删后登记的话,登记一失败,站点上
    只剩一个没登记的明文,补解再也找不到它。"""
    from d1max_contract.evseal import SealError, open_file
    if key is None:
        return "站点没配证据私钥(/etc/d1max-site/evidence.key)"
    try:
        open_file(path, dst, key)
    except (SealError, OSError) as exc:
        return str(exc)[:200]
    return ""


class EvidenceStore:
    def __init__(self, root: Path | str, db, *, now_ms: Callable[[], int]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self._now = now_ms
        self.writer = ChunkWriter()
        #: 收完一趟里一个文件之后调(判读排队用)。
        self.on_file: list[Callable[[int, str], None]] = []
        #: 证据私钥(W30b):狗封好的照片收齐了用它解开。没配是 ``None``(封好的留着、报 P2)。
        self.evidence_key: bytes | None = None
        #: 封好的照片解不开:``(狗, 一趟, 文件, 原因)``。站点主程序接到告警上。
        self.on_unopened: Callable[[str, str, str, str], None] | None = None

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
            from d1max_contract.evseal import plain_name
            plain = plain_name(rel)
            if plain is not None:
                # 封好的照片(W30b):解开成原名再登记;解不开留着封好的、报一声(回执照回:狗那份删了,
                # 站点这份是唯一的,私钥配好了能补解)
                why = open_sealed(path, path.with_name(plain.rsplit("/", 1)[-1]), self.evidence_key)
                if why:
                    log.warning("%s 传来的 %s/%s 解不开:%s", robot_id, run, rel, why)
                    # 这一趟照样登记(不登记照片):留存期、删除请求按趟删目录,封好的跟着删
                    self._index(robot_id, mission, stamp, "", notify=False)
                    if self.on_unopened is not None:
                        self.on_unopened(robot_id, run, rel, why)
                    return got
                self._index(robot_id, mission, stamp, plain)
                path.unlink(missing_ok=True)             # 登记成了才删封好的(外审 2)
                return got
            self._index(robot_id, mission, stamp, rel)
        return got

    def open_leftovers(self) -> tuple[int, int]:
        """封好、还没解开的照片补解(``evidence-open``)。回(解开了几个, 还解不开几个)。"""
        from d1max_contract.evseal import SUFFIX
        ok = bad = 0
        for p in sorted(self.root.rglob("*" + SUFFIX)):
            try:
                robot, mission, stamp, *rest = p.relative_to(self.root).parts
            except ValueError:
                continue
            rel = "/".join(rest)
            plain = rel[: -len(SUFFIX)]
            if open_sealed(p, p.with_name(plain.rsplit("/", 1)[-1]), self.evidence_key):
                bad += 1
                continue
            self._index(robot, mission, stamp, plain)
            p.unlink(missing_ok=True)                    # 登记成了才删封好的(外审 2)
            ok += 1
        return ok, bad

    # ------------------------------------------------------------ 登记

    def _index(self, robot_id: str, mission: str, stamp: str, rel: str, *,
               notify: bool = True) -> None:
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
        for cb in list(self.on_file) if notify else ():
            try:
                cb(row["id"], rel)
            except Exception:
                log.exception("收完文件的回调炸了")

    # ------------------------------------------------------------ 查

    def runs(self, *, robot_id: str | None = None, since_ms: int | None = None,
             until_ms: int | None = None, limit: int = 200,
             mission: str | None = None) -> list[dict[str, Any]]:
        """``mission``:只要这个任务的(W17:告警带的 ``task_id``,goto 一趟的任务名就是它)。"""
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if robot_id is not None:
            q += " AND robot_id=?"
            args.append(robot_id)
        if mission is not None:
            q += " AND mission=?"
            args.append(mission)
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

    def photo_complete(self, path: Path) -> bool:
        """``<证据库>/<狗>/<任务>/<时刻>/photos/<名字>`` 这张照片收齐了没有
        (判读拿历史照片比对用)。"""
        try:
            robot, mission, stamp, photos, name = path.resolve().relative_to(
                self.root.resolve()).parts
        except ValueError:
            return False
        if photos != "photos":
            return False
        return bool(self.db.query(
            "SELECT 1 FROM run_photos p JOIN runs r ON r.id = p.run_id WHERE r.robot_id=? "
            "AND r.mission=? AND r.stamp=? AND p.name=?", (robot, mission, stamp, name)))

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


def load_evidence_key(cfg: dict) -> bytes | None:
    """站点配的证据私钥(W30b);没有回 ``None``(封好的证据收齐了留着、报 P2)。"""
    from d1max_contract.evseal import SealError, load_private
    path = Path(cfg.get("evidence_key") or DEFAULT_EVIDENCE_KEY)
    if not path.is_file():
        log.warning("没有证据私钥 %s:狗封好的照片、录像收齐了解不开(装机脚本会生成)", path)
        return None
    try:
        return load_private(path)
    except (SealError, OSError) as exc:
        log.error("证据私钥用不了:%s", exc)
        return None


class EvidencePlainWatch:
    """狗报证据没封(能力 ``evidence.sealed`` 是假:没装站点公钥)→ 报 P2 ``evidence_plain``,一台一次
    (``evidence_plain`` 表,跟待报告警同一个事务),装好了(报真)就清。W30b,决策 51。"""

    def __init__(self, db, dispatcher: Any, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self.alerts: Any = None

    def tick(self) -> None:
        from d1max_site.pending_alerts import flush, queue
        seen = {r["robot_id"] for r in self.db.query("SELECT robot_id FROM evidence_plain")}
        fresh = getattr(self.dispatcher, "_fresh", None)
        now = self._now()
        for rid, c in list(self.dispatcher.clients.items()):
            if c.capabilities is None or (callable(fresh) and not fresh(c)):
                continue
            ev = c.capabilities.tasks.get("evidence")
            if not isinstance(ev, dict) or not isinstance(ev.get("sealed"), bool):
                continue                                  # 老代理不报:不说
            if ev["sealed"] is False and rid not in seen:
                with self.db.tx() as tx:
                    tx.execute("INSERT INTO evidence_plain(robot_id, created_ms) VALUES (?,?)",
                               (rid, now))
                    queue(tx, kind="evidence_plain", robot=rid,
                          title="狗上的照片、录像没加密",
                          detail="狗上没有站点的证据公钥(/etc/d1max/evidence-pub.key):照片、截图、"
                                 "录像明文存在狗上。从证书包拷过去、重启代理",
                          context={}, now_ms=now)
            elif ev["sealed"] is True and rid in seen:
                with self.db.tx() as tx:
                    tx.execute("DELETE FROM evidence_plain WHERE robot_id=?", (rid,))
        flush(self.db, self.alerts)
