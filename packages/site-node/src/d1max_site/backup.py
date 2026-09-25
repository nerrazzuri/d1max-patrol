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
import sqlite3
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
                 now_ms: Callable[[], int], alerts: Any = None,
                 more: dict[str, Path] | None = None) -> None:
        self.db = db
        self.evidence_root = Path(evidence_root)
        #: 还要镜像的目录(W00c5d 内部评审:地图、录包也只在站点上有一份):``{备份里的名字: 目录}``。
        self.more = {k: Path(v) for k, v in (more or {}).items()}
        self.dest = Path(dest) if dest is not None else None
        self._now = now_ms
        self.alerts = alerts
        # 上次成功、从什么时候起配了备份,都记在库里:重启不能把「25 小时没成」的钟清零(内部评审)。
        self.last_ok_ms: int | None = self._meta_int("backup_last_ok_ms")
        since = self._meta_int("backup_since_ms")
        if since is None and self.dest is not None:
            since = now_ms()
            self._meta_set("backup_since_ms", since)
        self.started_ms = since if since is not None else now_ms()
        self.last_error = ""
        self._next_ms = 0
        self._stale_told = False

    def _meta_int(self, key: str) -> int | None:
        rows = self.db.query("SELECT value FROM meta WHERE key=?", (key,))
        try:
            return int(rows[0]["value"]) if rows else None
        except (TypeError, ValueError):
            return None

    def _meta_set(self, key: str, value: int) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

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
        local = Path(self.db.path).with_name(".backup-snapshot.db")
        try:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._now() / 1000))
            dbdir = self.dest / "db"
            dbdir.mkdir(parents=True, exist_ok=True)
            # 先在本机盘上拍一个一致的快照(拿着库锁,但本机盘很快),再慢慢拷到外接盘(不拿锁):
            # 直接往外接盘上备份会一直拿着库锁,事件循环和接收口全卡住(内部评审)。
            local.unlink(missing_ok=True)
            self.db.backup_to(local)
            tmp = dbdir / f".site-{stamp}.db.tmp"
            shutil.copy2(local, tmp)
            os.replace(tmp, dbdir / f"site-{stamp}.db")
            for old in sorted(dbdir.glob("site-*.db"))[:-KEEP_DB]:
                old.unlink(missing_ok=True)
            self._mirror()
        except (OSError, sqlite3.Error) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("站点备份没成: %s", self.last_error)
            return False
        finally:
            local.unlink(missing_ok=True)
        self.last_ok_ms = self._now()
        self._meta_set("backup_last_ok_ms", self.last_ok_ms)
        self.last_error = ""
        return True

    def _mirror(self) -> None:
        assert self.dest is not None
        for name, root in {"evidence": self.evidence_root, **self.more}.items():
            self._mirror_one(root, self.dest / name)

    def _mirror_one(self, root: Path, out: Path) -> None:
        if not root.is_dir():
            return
        for src in root.rglob("*"):
            rel = src.relative_to(root)
            # 搬到一半的目录(``*.taking``、``*.importing``、``*.staging``)不拷:搬完那份下一轮拷。
            if any(p.endswith((".taking", ".importing", ".staging")) for p in rel.parts[:-1]):
                continue
            if not src.is_file() or src.name.endswith(".tmp"):
                continue
            dst = out / rel
            try:
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
            except FileNotFoundError:
                continue                          # 拷的时候源没了(搬走、删掉):这一个跳过,别的照拷
