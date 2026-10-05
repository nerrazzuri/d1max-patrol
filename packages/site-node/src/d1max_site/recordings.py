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

from d1max_contract.intake import VIDEO_FILE, split_video
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

    # ------------------------------------------------------------ 收

    def put(self, robot_id: str, run: str, rel: str, *, offset: int, data: bytes,
            total: int) -> Stored:
        """收件口调:``run`` = ``<相机>/<时刻>``,``rel`` = ``video.mp4``。收齐了登记。"""
        got = split_video(run)
        if got is None or rel != VIDEO_FILE:
            raise PathRefused(f"录像的路径不对:{run!r}/{rel!r}")
        camera, stamp = got
        path = safe_join(self.root, robot_id, camera, stamp[:8], f"{stamp}.mp4")
        stored = self.writer.write(path, offset=offset, data=data, total=total)
        if stored.size >= total:
            start = stamp_ms(stamp)
            with self.db.tx() as c:
                c.execute(
                    "INSERT INTO recordings(robot_id, camera, stamp, start_ms, bytes, sha256, "
                    "received_ms) VALUES (?,?,?,?,?,?,?) ON CONFLICT(robot_id, camera, stamp) "
                    # 重传不刷新 received_ms:留存从第一次收齐算(W18 外审)
                    "DO UPDATE SET bytes=excluded.bytes, sha256=excluded.sha256",
                    (robot_id, camera, stamp, start, stored.size, stored.sha256, self._now()))
        return stored

    # ------------------------------------------------------------ 查

    def list(self, *, robot_id: str | None = None, camera: str | None = None,
             since_ms: int | None = None, until_ms: int | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        """按开头时刻,最新的在前。``since``/``until`` 按「这一段盖住的时间」算:开头在 ``since``
        之前一段
        以内的也算(盖住了 ``since`` 那一刻)。"""
        q, args = "SELECT * FROM recordings WHERE 1=1", []
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
        rows = self.db.query("SELECT * FROM recordings WHERE id=?", (rid,))
        return dict(rows[0]) if rows else None

    def path(self, row: dict[str, Any]) -> Path:
        return safe_join(self.root, row["robot_id"], row["camera"], row["stamp"][:8],
                         f"{row['stamp']}.mp4")

    def set_keep(self, rid: int, keep: bool) -> dict[str, Any] | None:
        with self.db.tx() as c:
            c.execute("UPDATE recordings SET keep=? WHERE id=?", (int(bool(keep)), rid))
        return self.get(rid)

    # ------------------------------------------------------------ 删

    def _delete(self, row: dict[str, Any]) -> bool:
        """删一段:**文件真没了才删登记**(W18 外审:删不掉也删登记的话,录像在界面上消失、以后再也
        清不到,盘没腾出来却报「腾了」)。回删成了没有。"""
        try:
            self.path(row).unlink(missing_ok=True)
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
