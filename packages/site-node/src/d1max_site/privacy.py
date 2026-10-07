"""个人数据(W30,决策 43:马来西亚 PDPA 的基础;第一版不做人脸)。

站点上跟人有关的是**证据**(巡检照片、人员截图、事件现场照,都在运行记录里)和**录像**(W18)。

- **留存期**:运行记录默认留 :data:`KEEP_DAYS`(90)天,按站点最后收到它的时刻算,过了就删(目录、登记、
  备份里的那一份);录像按 W18 的 30 天。**标了「留着」的不删**(要留作证据的,值班的人标)。
- **按时间段删**(当事人要求删除时;只能按时间、地点 —— 第一版不认人脸,认不出哪张是谁):
  ``d1max-site privacy-purge``,删这段时间里的运行记录和录像(连备份),标了「留着」的不删、单独列出来;
  记审计。
- **删证据连导出一起删**(到期删、按时间段删都是;W30 外审 3):里面有被删的那几趟的导出包删掉;
  正在打的,打完核一遍,里面有被删的就作废。
- **谁看过、导出过**:看照片、看一趟的记录、放录像、看实时画面、下载导出包,都记审计(同一个人看同一样
  东西 10 分钟内只记一次),见 ``api``。
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEEP_DAYS = 90
DAY_MS = 86_400_000


class PrivacyDesk:
    def __init__(self, db: Any, evidence: Any, *, now_ms: Callable[[], int],
                 recordings: Any = None, backup_dest: Path | None = None,
                 keep_days: int = KEEP_DAYS, exports: Any = None) -> None:
        self.db = db
        #: 导出(``runs.RunDesk``):删证据时连里面有这几趟的导出一起删(W30 外审 3)。
        self.exports = exports
        self.evidence = evidence
        self.recordings = recordings
        self.backup_dest = Path(backup_dest) if backup_dest else None
        self.keep_days = keep_days
        self._now = now_ms

    def _delete_run(self, run: dict[str, Any]) -> bool:
        """删一趟:站点上的目录、备份里的那一份、登记。目录删不掉就不删登记(下次再删)。"""
        d = self.evidence.dir_of(run)
        try:
            if d.exists():
                shutil.rmtree(d)
            if self.backup_dest is not None:
                b = self.backup_dest / "evidence" / d.relative_to(self.evidence.root)
                if b.exists():
                    shutil.rmtree(b)
        except OSError as exc:
            log.warning("运行记录 %s 删不掉:%s(下次再删)", run["id"], exc)
            return False
        with self.db.tx() as c:
            c.execute("DELETE FROM run_photos WHERE run_id=?", (run["id"],))
            c.execute("DELETE FROM runs WHERE id=?", (run["id"],))
            # 「这一趟的导出要删」跟删登记同一个事务落库(W30 复查):导出删成了才清
            c.execute("INSERT OR IGNORE INTO export_purges(run_id, created_ms) VALUES (?,?)",
                      (run["id"], self._now()))
        return True

    def prune(self) -> int:
        """过了留存期、没标留着的运行记录删掉。站点杂事循环定时调。回删了几趟。"""
        cutoff = self._now() - self.keep_days * DAY_MS
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM runs WHERE keep=0 AND last_ms<? ORDER BY last_ms LIMIT 500", (cutoff,))]
        done = {r["id"] for r in rows if self._delete_run(r)}
        self._drop_exports()                            # 连以前没删成的导出一起接着删
        n = len(done)
        if n:
            log.info("运行记录过了 %d 天留存期:删了 %d 趟", self.keep_days, n)
        return n

    def purge(self, *, since_ms: int, until_ms: int, robot_id: str | None = None
              ) -> dict[str, Any]:
        """删一段时间里的运行记录(跟这段时间有交叠的)和录像(开始时刻在这段里的)。标了留着的不删,
        列出来。回 ``{runs, recordings, held_runs, held_recordings, failed}``。"""
        if until_ms <= since_ms:
            raise ValueError("结束要晚于开始")
        q = "SELECT * FROM runs WHERE first_ms<? AND last_ms>=?"
        args: list[Any] = [until_ms, since_ms]
        if robot_id:
            q += " AND robot_id=?"
            args.append(robot_id)
        out: dict[str, Any] = {"runs": 0, "recordings": 0, "held_runs": [],
                               "held_recordings": [], "failed": 0}
        done: set[int] = set()
        for r in [dict(x) for x in self.db.query(q, tuple(args))]:
            if r["keep"]:
                out["held_runs"].append(r["id"])
            elif self._delete_run(r):
                out["runs"] += 1
                done.add(r["id"])
            else:
                out["failed"] += 1
        out["exports"] = self._drop_exports()
        if self.recordings is not None:
            q = "SELECT * FROM recordings WHERE start_ms>=? AND start_ms<?"
            args = [since_ms, until_ms]
            if robot_id:
                q += " AND robot_id=?"
                args.append(robot_id)
            for r in [dict(x) for x in self.db.query(q, tuple(args))]:
                if r["keep"]:
                    out["held_recordings"].append(r["id"])
                elif self.recordings._delete(r):
                    out["recordings"] += 1
                else:
                    out["failed"] += 1
        return out

    def _drop_exports(self) -> int:
        """把 ``export_purges`` 里记着的那几趟的导出删掉,删成了才清记录;删不掉(权限、盘)留着,
        下一次删除、定时清理、站点重启接着删(W30 复查)。回删了几份。"""
        if self.exports is None:
            return 0
        ids = {r["run_id"] for r in self.db.query("SELECT run_id FROM export_purges")}
        if not ids:
            return 0
        try:
            n = self.exports.drop_exports(ids)
        except OSError as exc:
            log.warning("导出删不掉(%s):记着,下次接着删", exc)
            return 0
        with self.db.tx() as c:
            c.executemany("DELETE FROM export_purges WHERE run_id=?", [(i,) for i in ids])
        return n

    def set_keep(self, run_id: int, keep: bool) -> dict[str, Any]:
        with self.db.tx() as c:
            if c.execute("UPDATE runs SET keep=? WHERE id=?", (int(keep), run_id)).rowcount == 0:
                raise KeyError(run_id)
        return {"id": run_id, "keep": keep}
