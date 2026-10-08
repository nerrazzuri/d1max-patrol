"""连续录像在站点(W18,决策 31、32)。

狗一分钟切一段,从录像发件箱传上来(狗专用口 ``/video/api/intake/put``,mTLS,分块续传,站点按自己存下
的字节回哈希)。收齐一段就登记进 ``recordings`` 表:哪只狗、哪路相机、开头时刻(**狗的钟**,UTC)。

- **留 30 天**(决策 32):从站点**第一次收齐**算(``received_ms``,不按狗的钟),过了就删(文件真删掉了才删
  登记);标了「留着」的不删(``keep``)。
- **盘先让给证据**:站点盘剩不到 ``LOW_FREE`` 就从最旧的(没标留着的)删起,删到 ``OK_FREE`` 为止,报一次
  ``recording_trimmed``。30 天两路连续录像要 2–3 TB,站点盘未必有;不先删录像的话,盘到了收件口的底线
  (``MIN_FREE``,见 ``evidence``)所有上传都停 —— 巡检的照片、记录也传不上来了。
- 文件:``<站点目录>/recordings/<狗>/<相机>/<日期>/<时刻>.mp4``(分片 mp4,手机、桌面直接放)。

线程:收件在收件口的请求线程里;查、删在 API 线程、站点循环里。登记靠库的锁;同一个文件的两块不交错
(``ChunkWriter`` 分路径的锁)。
"""

from __future__ import annotations

import calendar
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract.intake import VIDEO_FILE, VIDEO_SEALED, split_video
from d1max_site.db import SiteDB
from d1max_site.evidence import ChunkWriter, PathRefused, Stored, safe_join

log = logging.getLogger(__name__)

#: 留多久(决策 32)。
KEEP_DAYS = 30
#: 站点盘剩这么多以下就先删录像(比例),删到 ``OK_FREE``。
LOW_FREE = 0.10
OK_FREE = 0.15
#: 一段当多长(秒):狗报的 ``segment_s``;没报按这个。只用来算「哪几段盖住这一刻」。
SEGMENT_S = 60


def stamp_ms(stamp: str) -> int:
    """``20261004T013000Z`` → 毫秒(UTC)。"""
    return calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")) * 1000


class RecordingStore:
    def __init__(self, db: SiteDB, root: Path, *, now_ms: Callable[[], int],
                 keep_days: int = KEEP_DAYS,
                 disk_usage: Callable[[Path], tuple[int, int, int]] = shutil.disk_usage) -> None:
        self.db = db
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._now = now_ms
        self.keep_days = keep_days
        self._disk_usage = disk_usage
        self.writer = ChunkWriter()
        #: 为了腾盘删了录像(``(删了几段, 最早收齐的那段的收齐时刻)``):站点主程序接到告警源上。
        self.on_trimmed: Callable[[int, int], None] | None = None
        #: 盘紧又删不掉(``(删不掉几段, 最后一个错误)``):不处理的话盘到底线,巡检证据也传不上来了。
        self.on_stuck: Callable[[int, str], None] | None = None
        self.last_delete_error = ""
        #: 证据私钥(W30b):狗封好的录像段收齐了用它解开;解不开的回调同 :class:`EvidenceStore`。
        self.evidence_key: bytes | None = None
        self.on_unopened: Callable[[str, str, str, str], None] | None = None

    # ------------------------------------------------------------ 收

    def put(self, robot_id: str, run: str, rel: str, *, offset: int, data: bytes,
            total: int) -> Stored:
        """收件口调:``run`` = ``<相机>/<时刻>``,``rel`` = ``video.mp4``(封好的是
        ``video.mp4.d1e``,W30b:收齐了解开再登记)。收齐了登记。"""
        got = split_video(run)
        if got is None or rel not in (VIDEO_FILE, VIDEO_SEALED):
            raise PathRefused(f"录像的路径不对:{run!r}/{rel!r}")
        camera, stamp = got
        sealed = rel == VIDEO_SEALED
        plain_path = safe_join(self.root, robot_id, camera, stamp[:8], f"{stamp}.mp4")
        path = plain_path.with_name(plain_path.name + ".d1e") if sealed else plain_path
        stored = self.writer.write(path, offset=offset, data=data, total=total)
        if stored.size >= total:
            return self._done(robot_id, camera, stamp, path, plain_path, stored)
        return stored

    def _done(self, robot_id: str, camera: str, stamp: str, path: Path, plain_path: Path,
              stored: Stored) -> Stored:
        sealed = 0
        if path != plain_path:
            from d1max_site.evidence import open_sealed, sha256_file
            why = open_sealed(path, plain_path, self.evidence_key)
            if why:
                # 解不开也登记(外审 3):记成「还封着」,留存期、腾盘、删除请求照样删得到它
                log.warning("%s 的录像 %s/%s 解不开:%s", robot_id, camera, stamp, why)
                sealed = 1
                plain = Stored(size=path.stat().st_size, sha256=stored.sha256 or sha256_file(path))
            else:
                plain = Stored(size=plain_path.stat().st_size, sha256=sha256_file(plain_path))
        else:
            plain = stored
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO recordings(robot_id, camera, stamp, start_ms, bytes, sha256, "
                "received_ms, sealed) VALUES (?,?,?,?,?,?,?,?) "
                # 重传不刷新 received_ms:留存从第一次收齐算(W18 外审)
                "ON CONFLICT(robot_id, camera, stamp) DO UPDATE SET bytes=excluded.bytes, "
                "sha256=excluded.sha256, sealed=excluded.sealed",
                (robot_id, camera, stamp, stamp_ms(stamp), plain.size, plain.sha256, self._now(),
                 sealed))
        if sealed:
            if self.on_unopened is not None:
                self.on_unopened(robot_id, f"{camera}/{stamp}", VIDEO_SEALED, why)
        elif path != plain_path:
            path.unlink(missing_ok=True)                 # 登记成了才删封好的(外审 2)
        return stored

    def open_leftovers(self) -> tuple[int, int]:
        """封好、还没解开的录像段补解(``evidence-open``)。回(解开了几个, 还解不开几个)。"""
        ok = bad = 0
        for p in sorted(self.root.rglob("*.mp4.d1e")):
            try:
                robot, camera, _day, name = p.relative_to(self.root).parts
            except ValueError:
                continue
            stamp = name[: -len(".mp4.d1e")]
            plain_path = p.with_name(f"{stamp}.mp4")
            try:
                self._done(robot, camera, stamp, p, plain_path,
                           Stored(size=p.stat().st_size, sha256=""))
            except Exception:
                log.exception("录像 %s 补解、登记没成(封好的留着,下次再来)", p.name)
            if p.exists():
                bad += 1                                 # 还是解不开(_done 报过了)
            else:
                ok += 1
        return ok, bad

    # ------------------------------------------------------------ 查

    def list(self, *, robot_id: str | None = None, camera: str | None = None,
             since_ms: int | None = None, until_ms: int | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        """按开头时刻,最新的在前。``since``/``until`` 按「这一段盖住的时间」算:开头在 ``since``
        之前一段
        以内的也算(盖住了 ``since`` 那一刻)。"""
        q, args = "SELECT * FROM recordings WHERE sealed=0", []   # 还封着的放不了,不列
        if robot_id is not None:
            q += " AND robot_id=?"
            args.append(robot_id)
        if camera is not None:
            q += " AND camera=?"
            args.append(camera)
        if since_ms is not None:
            q += " AND start_ms>?"
            args.append(since_ms - SEGMENT_S * 1000)
        if until_ms is not None:
            q += " AND start_ms<=?"
            args.append(until_ms)
        q += " ORDER BY start_ms DESC, id DESC LIMIT ?"
        args.append(max(1, min(limit, 5000)))
        return [dict(r) for r in self.db.query(q, tuple(args))]

    def get(self, rid: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM recordings WHERE id=? AND sealed=0", (rid,))
        return dict(rows[0]) if rows else None

    def path(self, row: dict[str, Any]) -> Path:
        """这一段在盘上的文件(还封着的是 ``.mp4.d1e``)。"""
        name = f"{row['stamp']}.mp4" + (".d1e" if row.get("sealed") else "")
        return safe_join(self.root, row["robot_id"], row["camera"], row["stamp"][:8], name)

    def set_keep(self, rid: int, keep: bool) -> dict[str, Any] | None:
        with self.db.tx() as c:
            c.execute("UPDATE recordings SET keep=? WHERE id=?", (int(bool(keep)), rid))
        return self.get(rid)

    # ------------------------------------------------------------ 删

    def _delete(self, row: dict[str, Any]) -> bool:
        """删一段:**文件真没了才删登记**(W18 外审:删不掉也删登记的话,录像在界面上消失、以后再也
        清不到,盘没腾出来却报「腾了」)。回删成了没有。"""
        try:
            p = self.path(row)
            p.unlink(missing_ok=True)
            # 封着、解开的两份都删(补解到一半停了的话两份都在)
            p.with_name(f"{row['stamp']}.mp4").unlink(missing_ok=True)
            p.with_name(f"{row['stamp']}.mp4.d1e").unlink(missing_ok=True)
        except (OSError, PathRefused) as exc:
            log.warning("录像删不掉(%s):%s", exc, row.get("id"))
            self.last_delete_error = str(exc)
            return False
        with self.db.tx() as c:
            c.execute("DELETE FROM recordings WHERE id=?", (row["id"],))
        return True

    def _free_ratio(self) -> float:
        total, _used, free = self._disk_usage(self.root)
        return free / total if total else 0.0

    def prune(self) -> tuple[int, int]:
        """过了留存期的删掉;盘紧了再从最旧的删。回 ``(按期删了几段, 为腾盘删了几段)``,只算真删掉的。
        站点循环定时调。

        **留存与「最旧」都按站点第一次收齐的时刻**(``received_ms``),不按片段名里狗的钟(W18 外审:狗的
        钟慢半年时刚收到的就被当成过期删了,快半年时留远超 30 天;几只狗钟差不同时,盘紧先删钟最慢
        那只的新录像)。狗的钟只用来排时间线、查某一刻前后。"""
        cutoff = self._now() - self.keep_days * 86400_000
        old = [dict(r) for r in self.db.query(
            "SELECT * FROM recordings WHERE keep=0 AND received_ms<? ORDER BY received_ms, id",
            (cutoff,))]
        aged = sum(self._delete(r) for r in old)
        trimmed = 0
        oldest = 0
        failed: set[int] = set()
        if self._free_ratio() < LOW_FREE:
            # 按收齐先后往后走(游标),每段只试一次:删掉的没了,删不掉的跳过、接着试后面的 —— 前面一批
            # 删不掉不许把后面能删的饿死(W18 外审复查)。到 OK_FREE 或者没有可试的了才停。
            cursor = (-1, -1)
            while self._free_ratio() < OK_FREE:
                rows = [dict(r) for r in self.db.query(
                    "SELECT * FROM recordings WHERE keep=0 AND (received_ms>? OR "
                    "(received_ms=? AND id>?)) ORDER BY received_ms, id LIMIT 50",
                    (cursor[0], cursor[0], cursor[1]))]
                if not rows:
                    break
                for r in rows:
                    cursor = (r["received_ms"], r["id"])
                    if not self._delete(r):
                        failed.add(r["id"])
                        continue
                    if not trimmed:
                        oldest = r["received_ms"]
                    trimmed += 1
                    if self._free_ratio() >= OK_FREE:
                        break
            if trimmed:
                log.warning("站点盘紧:删了最旧的 %d 段录像", trimmed)
                if self.on_trimmed is not None:
                    try:
                        self.on_trimmed(trimmed, oldest)
                    except Exception:
                        log.exception("报「为腾盘删了录像」失败")
            if failed:                                   # 腾出来了也报:删不掉本身就是毛病
                log.error("站点盘紧,%d 段录像删不掉", len(failed))
                if self.on_stuck is not None:
                    try:
                        self.on_stuck(len(failed), self.last_delete_error)
                    except Exception:
                        log.exception("报「录像删不掉」失败")
        return aged, trimmed
