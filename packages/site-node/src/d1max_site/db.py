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
from typing import Any

SCHEMA_VERSION = 50                      # B1:告警搁置(alerts.shelved_*)

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
    last_used  INTEGER NOT NULL,
    scope      TEXT NOT NULL DEFAULT ''
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
    schema         INTEGER NOT NULL DEFAULT 1,
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
    told_ms      INTEGER DEFAULT 0,
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
-- 原点(W13a,决策 16):安全返航、回充的语义,每台狗每张图的每个版本一个;待命点(上面那张表)是运营调度
-- 的语义,可以多个。站点下发地图时把原点发给狗(``map_activate.home``)。
CREATE TABLE IF NOT EXISTS homes (
    robot_id     TEXT NOT NULL,
    map_id       TEXT NOT NULL,
    map_version  TEXT NOT NULL,
    name         TEXT NOT NULL,
    x            REAL NOT NULL,
    y            REAL NOT NULL,
    yaw          REAL NOT NULL,
    marked_at_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (robot_id, map_id, map_version)
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
    told_ms      INTEGER DEFAULT 0,
    UNIQUE (source, event_id)
);
CREATE INDEX IF NOT EXISTS incidents_task ON incidents(task_id);
CREATE TABLE IF NOT EXISTS cameras (
    name       TEXT PRIMARY KEY,
    onvif_url  TEXT NOT NULL,
    username   TEXT NOT NULL DEFAULT '',
    password   TEXT NOT NULL DEFAULT '',
    zone       TEXT NOT NULL,
    rtsp_url   TEXT NOT NULL DEFAULT '',
    motion     INTEGER NOT NULL DEFAULT 0,
    added_ms   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id    TEXT NOT NULL,
    camera      TEXT NOT NULL,
    stamp       TEXT NOT NULL,
    start_ms    INTEGER NOT NULL,
    bytes       INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    received_ms INTEGER NOT NULL,
    keep        INTEGER NOT NULL DEFAULT 0,
    sealed      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (robot_id, camera, stamp)
);
CREATE INDEX IF NOT EXISTS recordings_start ON recordings(start_ms);
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
    escalated    INTEGER NOT NULL,
    context      TEXT NOT NULL DEFAULT '{}',
    shelved_by   TEXT NOT NULL DEFAULT '',
    shelved_ms   INTEGER,
    shelved_until_ms INTEGER,
    shelved_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS alerts_last ON alerts(last_ms);
-- W21 复查:站点起来时按种类取每台狗最近一条事件(上装故障的全集),不扫整张事件表。
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, robot_id, received_at);
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
CREATE TABLE IF NOT EXISTS robot_holds (
    robot_id TEXT PRIMARY KEY,
    by       TEXT NOT NULL,
    at_ms    INTEGER NOT NULL,
    reason   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS releases (
    name       TEXT PRIMARY KEY,
    version    TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    requires_mission_schema INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bags (
    robot_id TEXT NOT NULL,
    name     TEXT NOT NULL,
    first_ms INTEGER NOT NULL,
    last_ms  INTEGER NOT NULL,
    bytes    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (robot_id, name)
);
-- W10:禁行区、限速区,挂在几何版本上;修订号每改 +1;发布前人工确认(确认的是哪一版修订)。
CREATE TABLE IF NOT EXISTS nav_zones (
    map_id        TEXT NOT NULL,
    map_version   TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    body          TEXT NOT NULL,
    updated_by    TEXT NOT NULL,
    updated_ms    INTEGER NOT NULL,
    confirmed_rev INTEGER,
    confirmed_by  TEXT,
    confirmed_ms  INTEGER,
    PRIMARY KEY (map_id, map_version)
);
-- W20:全站一个当前模式(一行);在家时撤防的防区(没有这一行的防区一律布防)。
CREATE TABLE IF NOT EXISTS site_mode (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    mode          TEXT NOT NULL,
    prev_mode     TEXT NOT NULL DEFAULT '',
    visitor_zones TEXT NOT NULL DEFAULT '[]',
    until_ms      INTEGER,
    set_by        TEXT NOT NULL DEFAULT '',
    set_ms        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS zone_arming (
    zone       TEXT PRIMARY KEY,
    home_armed INTEGER NOT NULL
);
-- W22:正在进行的驱离(一台狗一场)。站点重启接着管;结束就删。
CREATE TABLE IF NOT EXISTS deter_sessions (
    robot_id    TEXT PRIMARY KEY,
    incident_id INTEGER NOT NULL,
    zone        TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    level       INTEGER NOT NULL,
    started_ms  INTEGER NOT NULL,
    level_ms    INTEGER NOT NULL,
    human_ms    INTEGER NOT NULL DEFAULT 0,
    auto        INTEGER NOT NULL DEFAULT 1,
    by          TEXT NOT NULL DEFAULT ''
);
-- W29:天气(一行)。手动切的(带到点时刻)、联网查的(带查的时刻、原因)。
CREATE TABLE IF NOT EXISTS weather (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    manual          TEXT NOT NULL DEFAULT '',
    manual_until_ms INTEGER,
    manual_by       TEXT NOT NULL DEFAULT '',
    manual_ms       INTEGER,
    auto            TEXT NOT NULL DEFAULT '',
    auto_ms         INTEGER,
    auto_detail     TEXT NOT NULL DEFAULT '',
    auto_error      TEXT NOT NULL DEFAULT ''
);
-- W29 外审:雷暴撤了巡检、要派回待命点的狗(派成了才删;狗在跑别的了就作废)。
CREATE TABLE IF NOT EXISTS weather_returns (
    robot_id   TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
-- W13:每台狗的桩前对准点(桩前约 1.5 m、正对桩)。
CREATE TABLE IF NOT EXISTS chargers (
    robot_id    TEXT PRIMARY KEY,
    map_id      TEXT NOT NULL,
    map_version TEXT NOT NULL,
    x           REAL NOT NULL,
    y           REAL NOT NULL,
    yaw         REAL NOT NULL,
    set_ms      INTEGER NOT NULL,
    set_by      TEXT NOT NULL DEFAULT ''
);
-- W13:每台狗这一轮回充走到哪一步了(goto / dock / resume 被打断 / cooldown 失败或人中止之后歇着)。
CREATE TABLE IF NOT EXISTS charge_cycles (
    robot_id   TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    task_id    TEXT NOT NULL DEFAULT '',
    sent_ms    INTEGER NOT NULL DEFAULT 0,
    started_ms INTEGER NOT NULL,
    until_ms   INTEGER
);
-- W13 外审:狗报「桩上危险」(运动锁住)报过 P1 的(一次挂一次;狗上摘了就删)。
-- W30b(决策 51):狗报证据没封(没装站点公钥)报过 P2 的(装好了删)。
CREATE TABLE IF NOT EXISTS evidence_plain (
    robot_id   TEXT PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
-- W30b 复查 Minor:这一回「狗上证据封不上」已经报过的狗(人手动解决了、站点重启了都不重报;
-- 狗报件数回到 0 才删)。
CREATE TABLE IF NOT EXISTS evidence_unsealed (
    robot_id   TEXT PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
-- W26(决策 52):狗翻倒、被抱起来的这一回(报过 P1 了;狗报回 ok 删)。siren:1 = 布防中被抱起来、
-- 警笛警灯还没开成(下一拍再发),2 = 开成了(45 秒内这几路归它,驱离不许关),0 = 不响、过点了。
CREATE TABLE IF NOT EXISTS force_episodes (
    robot_id   TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    siren      INTEGER NOT NULL DEFAULT 0,
    siren_ms   INTEGER NOT NULL DEFAULT 0
);
-- 系统审查 S07:删过的证据(kind:run 一趟 <任务>/<时刻>、rec 一段录像 <相机>/<时刻>)。跟删除同一个
-- 事务落库;再传上来照收照回执、收齐就扔,不登记(回执丢了、断网攒着的、跟删除交错的都不复活)。
CREATE TABLE IF NOT EXISTS purged (
    kind      TEXT NOT NULL,
    robot_id  TEXT NOT NULL,
    key       TEXT NOT NULL,
    purged_ms INTEGER NOT NULL,
    PRIMARY KEY (kind, robot_id, key)
);
-- 商业化 A6(决策 53):收推送的手机(极光的推送号)、要推的 P1(告警写库的同一个事务里排上;同一条
-- 同一档只排一次)。
CREATE TABLE IF NOT EXISTS push_devices (
    reg_id     TEXT PRIMARY KEY,
    account    TEXT NOT NULL,
    platform   TEXT NOT NULL,
    updated_ms INTEGER NOT NULL,
    session    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS push_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key  TEXT NOT NULL,
    tier       INTEGER NOT NULL,
    robot      TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    next_ms    INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_ms    INTEGER,
    dropped    TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    UNIQUE (alert_key, tier)
);
-- 商业化 A1:指标历史(每 5 分钟一笔,留 30 天):盘、狗在线、开着的告警、狗上待传的、事件循环延迟。
CREATE TABLE IF NOT EXISTS metrics (
    ts    INTEGER NOT NULL,
    key   TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS metrics_ts ON metrics(ts);
-- 商业化 A4:狗的开通码(一只狗一个;只记令牌的哈希;领过一次就作废)。
CREATE TABLE IF NOT EXISTS enroll_claims (
    robot_id   TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    expires_ms INTEGER NOT NULL,
    used_ms    INTEGER,
    -- A 阶段外审 I1:出码时这只狗证书的指纹(这一代)。领的时候登记表和读到的证书都得是这一代。
    cert_fp    TEXT NOT NULL DEFAULT ''
);
-- W33(决策 48):布防中狗没在驱离时看见人,这一回报过 P1 了(狗确认人走了删,再看见再报)。
CREATE TABLE IF NOT EXISTS person_sightings (
    robot_id   TEXT PRIMARY KEY,
    started_ms INTEGER NOT NULL
);
-- W33(C40221 真机):只许手动派的狗。站点自己发起的动作(排程、入侵派遣、回充、任务完了自动回待命点)
-- 一律不派;人手动派的照常。新登记的狗默认在这里(管理员确认后才接自动派遣);没有这一行 = 接。
CREATE TABLE IF NOT EXISTS robot_service (
    robot_id TEXT PRIMARY KEY,
    by       TEXT NOT NULL,
    at_ms    INTEGER NOT NULL,
    note     TEXT NOT NULL DEFAULT ''
);
-- W28 外审:这一回低电已经报过「两台同时在充」的(重派、重启不再报;电量回到 30% 以上删)。
CREATE TABLE IF NOT EXISTS charge_overlaps (
    robot_id   TEXT PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dock_hazards (
    robot_id   TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
-- W30 复查:删了运行记录、导出还没删成的(删导出成了才清;下一次删除、定时清理、重启都接着删)。
CREATE TABLE IF NOT EXISTS export_purges (
    run_id     INTEGER PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
-- W24 复查:要报、还没报成的告警(连内容一起)。报成才删;人走了、驱离结束都不动它。
CREATE TABLE IF NOT EXISTS pending_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    robot      TEXT NOT NULL,
    title      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    context    TEXT NOT NULL DEFAULT '{}',
    created_ms INTEGER NOT NULL
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
    # W00c6d 升级前检查:新版要的任务包 schema、每个任务包自己的 schema(老的都是 1)。
    ("releases", "requires_mission_schema", "INTEGER NOT NULL DEFAULT 1"),
    ("bundles", "schema", "INTEGER NOT NULL DEFAULT 1"),
    # W00c6c 内审:这一轮的「没跑」说过没有。老库里的行当说过了(0),新记的行写 NULL。
    ("schedule_runs", "told_ms", "INTEGER DEFAULT 0"),
    # W14 外审:这一趟跑完回哪个待命点,派单时定下(空 = 默认的);老行没有,回默认的。
    ("schedule_runs", "standby_name", "TEXT NOT NULL DEFAULT ''"),
    # W16 外审:这条入侵的告警报成了没有。老库里的行当说过了(0,升级不把历史入侵全报一遍),新记的行写
    # NULL。
    ("incidents", "told_ms", "INTEGER DEFAULT 0"),
    # W17:告警带现场(狗在哪、哪一趟、拦截点),JSON。老行没有。
    ("alerts", "context", "TEXT NOT NULL DEFAULT '{}'"),
    # W17:会话的用途,'' 普通、'watch' 值守令牌(只能看告警、30 天)。老行都是普通会话。
    ("sessions", "scope", "TEXT NOT NULL DEFAULT ''"),
    # W20:操作人实名。账号名是登录用的,显示名是给人看的(「张三」);老账号空着,显示账号名。
    ("accounts", "display_name", "TEXT NOT NULL DEFAULT ''"),
    # W22 复查:这一场要收尾了(原因);空 = 没在收尾。站点重启读到就只接着收尾,不再开任何东西。
    ("deter_sessions", "ending", "TEXT NOT NULL DEFAULT ''"),
    ("deter_sessions", "ending_back", "INTEGER NOT NULL DEFAULT 0"),
    # W23:拦截点走不走得到。reach 非空 = 走不到(派单不派);reach_note = 查不了的那几样;reach_key =
    # 查的时候的指纹(点、区域修订、待命点、图),变了站点重查。老行空着:下一拍对账时查。
    ("intercepts", "reach", "TEXT NOT NULL DEFAULT ''"),
    ("intercepts", "reach_note", "TEXT NOT NULL DEFAULT ''"),
    ("intercepts", "reach_key", "TEXT NOT NULL DEFAULT ''"),
    # W24 外审:这一场看到过人没有、人员告警报成了没有(站点重启后接着对账、接着报)
    ("deter_sessions", "person_seen", "INTEGER NOT NULL DEFAULT 0"),
    ("deter_sessions", "person_alerted", "INTEGER NOT NULL DEFAULT 0"),
    # W25:这一场无路可退过没有(报过 P1 意图)
    ("deter_sessions", "cornered", "INTEGER NOT NULL DEFAULT 0"),
    # W25 外审:这一场的拦截点(保持距离的拴绳中心,补派、重启都按它)
    ("deter_sessions", "standoff_center", "TEXT NOT NULL DEFAULT ''"),
    # W33 外审:就地驱离开场时狗在跑的那一趟,还没确认撤掉(每拍再撤,结束了才清;重启接着撤)
    ("deter_sessions", "withdraw", "TEXT NOT NULL DEFAULT ''"),
    # W30:运行记录标了「留着」(要留作证据):过了留存期也不删,按时间段删时也不删
    ("runs", "keep", "INTEGER NOT NULL DEFAULT 0"),
    # W30b 外审 3:这一段还封着(解不开):照样登记,留存、腾盘、删除请求都管得到;列表、回放不给
    ("recordings", "sealed", "INTEGER NOT NULL DEFAULT 0"),
    # PR #87 复查 R4:受力警报上次发「开」的时刻(定时续:狗重启、上装全关了也补回来)
    ("force_episodes", "siren_ms", "INTEGER NOT NULL DEFAULT 0"),
    # A6 外审 F3:登记推送号的那次登录(令牌哈希);那次登录退出、过期了就不推
    ("push_devices", "session", "TEXT NOT NULL DEFAULT ''"),
    ("enroll_claims", "cert_fp", "TEXT NOT NULL DEFAULT ''"),          # A 阶段外审 I1
    ("alerts", "shelved_by", "TEXT NOT NULL DEFAULT ''"),               # B1 搁置
    ("alerts", "shelved_ms", "INTEGER"),
    ("alerts", "shelved_until_ms", "INTEGER"),
    ("alerts", "shelved_reason", "TEXT NOT NULL DEFAULT ''"),
)



class SchemaTooNew(RuntimeError):
    """站点库是更新的站点程序写的(商业化 A7):老程序不开它。"""

class SiteDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        #: 口令加密(W30,``sealbox.SealBox``):站点接上;``None`` = 口令明文(开发、老站点)。
        self.sealbox: Any = None
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
            # 商业化 A7:库比程序新(站点回滚到老版本、拿新库给老程序用),不开 —— 开了会把版本号
            # 改回去,新版加的表和列老程序不认,下次再升就对不上了。
            has_meta = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone() \
                if has_meta else None
            if row is not None and str(row[0]).isdigit() and int(row[0]) > SCHEMA_VERSION:
                self._conn.close()
                raise SchemaTooNew(f"站点库是第 {row[0]} 版结构,这个站点程序只认到第 "
                                   f"{SCHEMA_VERSION} 版:库是新版程序写的。装回新版程序,或者"
                                   "用老版本时候的备份恢复(docs/版本兼容与升级顺序.md)")
            had_homes = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='homes'").fetchone()
            self._conn.executescript(_DDL)
            # 老库补列(CREATE TABLE IF NOT EXISTS 不会给已有的表加列)。
            for table, col, decl in _ADDED_COLUMNS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            if not had_homes:
                # W13a 迁移:以前没有原点表,下发地图时拿「这张图上的待命点(默认的优先、再按名字)
                # 」当原点。
                # 照同样的挑法把每台狗每张图每个版本的那一个抄成原点;
                # 待命点原样留着(默认待命点还是它)。
                self._conn.execute(
                    "INSERT OR IGNORE INTO homes(robot_id, map_id, map_version, name, x, y, yaw) "
                    "SELECT robot_id, map_id, map_version, name, x, y, yaw FROM standby_points "
                    "WHERE map_version != '' ORDER BY is_default DESC, name")
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
