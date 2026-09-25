"""站点状态的唯一落盘处:一个 SQLite 文件(W00c 设计 §4)。

一个连接 + 一把锁:站点 API 是多线程的(``ThreadingHTTPServer``),派遣器在事件循环线程里写;
SQLite 自己的线程检查关掉,串行化由这把锁负责。WAL 模式:读不挡写。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 11

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS robots (
    robot_id      TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    issued_at     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0,
    control_epoch INTEGER NOT NULL DEFAULT 1,
    enrolled_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    name       TEXT PRIMARY KEY,
    role       TEXT NOT NULL,
    salt       BLOB NOT NULL,
    pw_hash    BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    status  INTEGER NOT NULL DEFAULT 0,
    detail  TEXT NOT NULL DEFAULT '{}',
    remote  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    name       TEXT NOT NULL REFERENCES accounts(name),
    created_at INTEGER NOT NULL,
    last_used  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    command_id  TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    robot_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    issued_by   TEXT NOT NULL,
    issued_at   INTEGER NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    ack_result  TEXT,
    ack_reason  TEXT
);
CREATE INDEX IF NOT EXISTS commands_robot ON commands(robot_id, issued_at);
CREATE TABLE IF NOT EXISTS events (
    robot_id    TEXT NOT NULL,
    boot_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    data        TEXT NOT NULL,
    stamp       INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (robot_id, boot_id, seq)
);
CREATE TABLE IF NOT EXISTS bundles (
    bundle_id      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    timezone       TEXT NOT NULL,
    schedule       TEXT NOT NULL,
    imported_at    INTEGER NOT NULL,
    imported_by    TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bundle_id, version)
);
CREATE TABLE IF NOT EXISTS missions (
    bundle_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    mission_id TEXT NOT NULL,
    definition TEXT NOT NULL,
    PRIMARY KEY (bundle_id, version, mission_id)
);
CREATE TABLE IF NOT EXISTS schedule_state (
    entry_id        TEXT PRIMARY KEY,
    last_started_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT NOT NULL,
    scheduled_ms INTEGER NOT NULL,
    outcome      TEXT NOT NULL,
    robot_id     TEXT,
    task_id      TEXT,
    result       TEXT,
    note         TEXT NOT NULL DEFAULT '',
    decided_at   INTEGER NOT NULL,
    UNIQUE (entry_id, scheduled_ms, outcome)
);
CREATE INDEX IF NOT EXISTS schedule_runs_task ON schedule_runs(task_id);
CREATE TABLE IF NOT EXISTS standby_points (
    robot_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    map_id     TEXT NOT NULL,
    map_version TEXT NOT NULL DEFAULT '',
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    yaw        REAL NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (robot_id, name)
);
CREATE TABLE IF NOT EXISTS incident_sources (
    name       TEXT PRIMARY KEY,
    secret     TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS intercepts (
    name   TEXT PRIMARY KEY,
    map_id TEXT NOT NULL,
    map_version TEXT NOT NULL,
    x      REAL NOT NULL,
    y      REAL NOT NULL,
    yaw    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS zones (
    zone      TEXT PRIMARY KEY,
    intercept TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    zone         TEXT NOT NULL,
    intercept    TEXT,
    received_at  INTEGER NOT NULL,
    occurred_at  INTEGER,
    outcome      TEXT NOT NULL,
    robot_id     TEXT,
    task_id      TEXT,
    result       TEXT,
    merged_into  INTEGER,
    note         TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, event_id)
);
CREATE INDEX IF NOT EXISTS incidents_task ON incidents(task_id);
CREATE TABLE IF NOT EXISTS alerts (
    key          TEXT PRIMARY KEY,
    level        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    robot        TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL,
    first_ms     INTEGER NOT NULL,
    last_ms      INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    acked_by     TEXT NOT NULL,
    acked_ms     INTEGER,
    resolved_by  TEXT NOT NULL,
    resolved_ms  INTEGER,
    escalated    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_last ON alerts(last_ms);
CREATE TABLE IF NOT EXISTS teleop_leases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id        TEXT NOT NULL,
    epoch           INTEGER NOT NULL,
    operator        TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    end_reason      TEXT,
    takeover_by     TEXT,
    takeover_reason TEXT,
    UNIQUE (robot_id, epoch)
);
-- W00c5d(决策 8):狗传上来的运行记录。文件在 <站点目录>/evidence/<狗>/<任务>/<时刻>/ 下。
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id   TEXT NOT NULL,
    mission    TEXT NOT NULL,
    stamp      TEXT NOT NULL,
    first_ms   INTEGER NOT NULL,
    last_ms    INTEGER NOT NULL,
    finished   INTEGER NOT NULL DEFAULT 0,
    result     TEXT NOT NULL DEFAULT '',
    photos     INTEGER NOT NULL DEFAULT 0,
    bytes      INTEGER NOT NULL DEFAULT 0,
    judged_ms  INTEGER,
    verdicts   TEXT NOT NULL DEFAULT '{}',
    reviewed   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (robot_id, mission, stamp)
);
CREATE INDEX IF NOT EXISTS runs_by_robot ON runs (robot_id, stamp);
-- W00c5d 内部评审:收齐了的照片(还在传的不算,不判读、不当基线)。
CREATE TABLE IF NOT EXISTS run_photos (
    run_id  INTEGER NOT NULL,
    name    TEXT NOT NULL,
    done_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, name)
);
-- W00c5d 第二部分:站点的地图目录与建图录包。文件在 <站点目录>/maps/、bags/ 下。
CREATE TABLE IF NOT EXISTS maps (
    map_id     TEXT NOT NULL,
    version    TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    files      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (map_id, version)
);
-- W00c5d 第三部分:站点的发布目录。包在 <站点目录>/releases/<版本名>.tar.gz。
CREATE TABLE IF NOT EXISTS releases (
    name       TEXT PRIMARY KEY,
    version    TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bags (
    robot_id TEXT NOT NULL,
    name     TEXT NOT NULL,
    first_ms INTEGER NOT NULL,
    last_ms  INTEGER NOT NULL,
    bytes    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (robot_id, name)
);
CREATE TABLE IF NOT EXISTS robot_state (
    robot_id     TEXT PRIMARY KEY,
    status       TEXT,
    capabilities TEXT,
    updated_at   INTEGER NOT NULL
);
"""


#: 建表之后才加的列:(表, 列, 声明)。老库打开时补上。
_ADDED_COLUMNS = (
    ("commands", "priority", "INTEGER NOT NULL DEFAULT 0"),          # W00c2b
    ("standby_points", "map_version", "TEXT NOT NULL DEFAULT ''"),   # W00c2b 内部评审
    ("intercepts", "map_version", "TEXT NOT NULL DEFAULT ''"),       # W00c2c
    ("accounts", "disabled", "INTEGER NOT NULL DEFAULT 0"),          # W00c3
    ("runs", "judge_tries", "INTEGER NOT NULL DEFAULT 0"),           # W00c5d 内部评审
)


class SiteDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        fresh = not self.path.exists()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     isolation_level=None)
        # 库里有事件源的共享密钥与会话令牌的哈希:只许站点用户自己读。
        if fresh or self.path.stat().st_mode & 0o077:
            os.chmod(self.path, 0o600)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_DDL)
            # 老库补列(CREATE TABLE IF NOT EXISTS 不会给已有的表加列)。
            for table, col, decl in _ADDED_COLUMNS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                               (str(SCHEMA_VERSION),))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """一次事务:锁住、BEGIN、出错回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args))

    def backup_to(self, path: Path) -> None:
        """整库在线备份到 ``path``(sqlite 的 backup 接口,拿着库锁做:备份出来的是一个一致的快照)。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(path))
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
