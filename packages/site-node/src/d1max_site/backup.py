"""站点自己的备份(W00c5d,决策 8:站点成了唯一权威,就得有备份)。

每小时一次:
- 库:sqlite 在线备份出一份 ``<备份目录>/db/site-<时刻>.db``,留最近 ``KEEP_DB`` 份;
- 证据库:增量镜像到 ``<备份目录>/evidence/``(没有的、大小或修改时间对不上的才拷;站点上删了的,
  备份里**不跟着删** —— 备份是用来找回东西的)。

备份目录在 ``site.json`` 的 ``backup_dir`` 里配(站点主机上的外接盘,挂到
``/var/lib/d1max-site-backup`` —— 站点服务单元只许写这个位置)。没配就是没备份:值守汇总里明说。
上次成功超过 ``STALE_MS`` 出一条 ``backup_stale`` 告警。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_site.alert_sources import SITE

log = logging.getLogger(__name__)

#: 多久备份一次(秒)。
EVERY_S = 3600
#: 库留几份。
KEEP_DB = 7
#: 上次成功超过这么久算备份过期(毫秒)。
STALE_MS = 25 * 3600 * 1000


class SiteBackup:
    def __init__(self, db, evidence_root: Path, dest: Path | None, *,
                 now_ms: Callable[[], int], alerts: Any = None) -> None:
        self.db = db
        self.evidence_root = Path(evidence_root)
        self.dest = Path(dest) if dest is not None else None
        self._now = now_ms
        self.alerts = alerts
        self.started_ms = now_ms()
        self.last_ok_ms: int | None = None
        self.last_error = ""
        self._next_ms = 0
        self._stale_told = False

    def status(self) -> dict[str, Any]:
        return {"configured": self.dest is not None,
                "dest": str(self.dest) if self.dest is not None else "",
                "last_ok_ms": self.last_ok_ms, "error": self.last_error,
                "stale": self.stale()}

    def stale(self) -> bool:
        if self.dest is None:
            return False
        since = self.last_ok_ms if self.last_ok_ms is not None else self.started_ms
        return self._now() - since > STALE_MS

    def step(self) -> None:
        """后台线程每分钟调一次:到点就备份;过期了报一次告警。"""
        now = self._now()
        if self.dest is not None and now >= self._next_ms:
            self._next_ms = now + EVERY_S * 1000
            self.run_once()
        stale = self.stale()
        if stale and not self._stale_told and self.alerts is not None:
            self.alerts.raise_alert(kind="backup_stale", robot=SITE, title="站点备份过期了",
                                    detail=self.last_error or "超过 25 小时没有备份成功")
        self._stale_told = stale

    def run_once(self) -> bool:
        if self.dest is None:
            return False
        try:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._now() / 1000))
            dbdir = self.dest / "db"
            self.db.backup_to(dbdir / f"site-{stamp}.db")
            for old in sorted(dbdir.glob("site-*.db"))[:-KEEP_DB]:
                old.unlink(missing_ok=True)
            self._mirror()
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("站点备份没成: %s", self.last_error)
            return False
        self.last_ok_ms = self._now()
        self.last_error = ""
        return True

    def _mirror(self) -> None:
        assert self.dest is not None
        if not self.evidence_root.is_dir():
            return
        out = self.dest / "evidence"
        for src in self.evidence_root.rglob("*"):
            if not src.is_file() or src.name.endswith(".tmp"):
                continue
            dst = out / src.relative_to(self.evidence_root)
            st = src.stat()
            try:
                ds = dst.stat()
                if ds.st_size == st.st_size and int(ds.st_mtime) == int(st.st_mtime):
                    continue
            except FileNotFoundError:
                pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
