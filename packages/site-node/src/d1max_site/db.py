"""站点状态的唯一落盘处:一个 SQLite 文件(W00c 设计 §4)。

一个连接 + 一把锁:站点 API 是多线程的(``ThreadingHTTPServer``),派遣器在事件循环线程里写;
SQLite 自己的线程检查关掉,串行化由这把锁负责。WAL 模式:读不挡写。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

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
    created_at INTEGER NOT NULL
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
CREATE TABLE IF NOT EXISTS robot_state (
    robot_id     TEXT PRIMARY KEY,
    status       TEXT,
    capabilities TEXT,
    updated_at   INTEGER NOT NULL
);
"""


class SiteDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_DDL)
            self._conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)",
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
