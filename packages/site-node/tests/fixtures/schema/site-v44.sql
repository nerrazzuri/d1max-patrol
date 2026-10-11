-- 站点库结构 44 的空库(提交 1d31433 的 db.py 建出来的;测升级用,别手改)
BEGIN TRANSACTION;
CREATE TABLE accounts (
    name       TEXT PRIMARY KEY,
    role       TEXT NOT NULL,
    salt       BLOB NOT NULL,
    pw_hash    BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
, display_name TEXT NOT NULL DEFAULT '');
CREATE TABLE alerts (
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
    context      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    status  INTEGER NOT NULL DEFAULT 0,
    detail  TEXT NOT NULL DEFAULT '{}',
    remote  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE bags (
    robot_id TEXT NOT NULL,
    name     TEXT NOT NULL,
    first_ms INTEGER NOT NULL,
    last_ms  INTEGER NOT NULL,
    bytes    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (robot_id, name)
);
CREATE TABLE bundles (
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
CREATE TABLE cameras (
    name       TEXT PRIMARY KEY,
    onvif_url  TEXT NOT NULL,
    username   TEXT NOT NULL DEFAULT '',
    password   TEXT NOT NULL DEFAULT '',
    zone       TEXT NOT NULL,
    rtsp_url   TEXT NOT NULL DEFAULT '',
    motion     INTEGER NOT NULL DEFAULT 0,
    added_ms   INTEGER NOT NULL
);
CREATE TABLE charge_cycles (
    robot_id   TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    task_id    TEXT NOT NULL DEFAULT '',
    sent_ms    INTEGER NOT NULL DEFAULT 0,
    started_ms INTEGER NOT NULL,
    until_ms   INTEGER
);
CREATE TABLE charge_overlaps (
    robot_id   TEXT PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
CREATE TABLE chargers (
    robot_id    TEXT PRIMARY KEY,
    map_id      TEXT NOT NULL,
    map_version TEXT NOT NULL,
    x           REAL NOT NULL,
    y           REAL NOT NULL,
    yaw         REAL NOT NULL,
    set_ms      INTEGER NOT NULL,
    set_by      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE commands (
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
CREATE TABLE deter_sessions (
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
, ending TEXT NOT NULL DEFAULT '', ending_back INTEGER NOT NULL DEFAULT 0, person_seen INTEGER NOT NULL DEFAULT 0, person_alerted INTEGER NOT NULL DEFAULT 0, cornered INTEGER NOT NULL DEFAULT 0, standoff_center TEXT NOT NULL DEFAULT '', withdraw TEXT NOT NULL DEFAULT '');
CREATE TABLE dock_hazards (
    robot_id   TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE events (
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
CREATE TABLE evidence_plain (
    robot_id   TEXT PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
CREATE TABLE evidence_unsealed (
    robot_id   TEXT PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
CREATE TABLE export_purges (
    run_id     INTEGER PRIMARY KEY,
    created_ms INTEGER NOT NULL
);
CREATE TABLE force_episodes (
    robot_id   TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    siren      INTEGER NOT NULL DEFAULT 0,
    siren_ms   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE homes (
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
CREATE TABLE incident_sources (
    name       TEXT PRIMARY KEY,
    secret     TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE incidents (
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
CREATE TABLE intercepts (
    name   TEXT PRIMARY KEY,
    map_id TEXT NOT NULL,
    map_version TEXT NOT NULL,
    x      REAL NOT NULL,
    y      REAL NOT NULL,
    yaw    REAL NOT NULL
, reach TEXT NOT NULL DEFAULT '', reach_note TEXT NOT NULL DEFAULT '', reach_key TEXT NOT NULL DEFAULT '');
CREATE TABLE maps (
    map_id     TEXT NOT NULL,
    version    TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    files      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (map_id, version)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "meta" VALUES('schema','44');
CREATE TABLE missions (
    bundle_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    mission_id TEXT NOT NULL,
    definition TEXT NOT NULL,
    PRIMARY KEY (bundle_id, version, mission_id)
);
CREATE TABLE nav_zones (
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
CREATE TABLE pending_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    robot      TEXT NOT NULL,
    title      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    context    TEXT NOT NULL DEFAULT '{}',
    created_ms INTEGER NOT NULL
);
CREATE TABLE person_sightings (
    robot_id   TEXT PRIMARY KEY,
    started_ms INTEGER NOT NULL
);
CREATE TABLE purged (
    kind      TEXT NOT NULL,
    robot_id  TEXT NOT NULL,
    key       TEXT NOT NULL,
    purged_ms INTEGER NOT NULL,
    PRIMARY KEY (kind, robot_id, key)
);
CREATE TABLE recordings (
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
CREATE TABLE releases (
    name       TEXT PRIMARY KEY,
    version    TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    requires_mission_schema INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE robot_holds (
    robot_id TEXT PRIMARY KEY,
    by       TEXT NOT NULL,
    at_ms    INTEGER NOT NULL,
    reason   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE robot_service (
    robot_id TEXT PRIMARY KEY,
    by       TEXT NOT NULL,
    at_ms    INTEGER NOT NULL,
    note     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE robot_state (
    robot_id     TEXT PRIMARY KEY,
    status       TEXT,
    capabilities TEXT,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE robots (
    robot_id      TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    issued_at     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0,
    control_epoch INTEGER NOT NULL DEFAULT 1,
    enrolled_at   INTEGER NOT NULL
);
CREATE TABLE run_photos (
    run_id  INTEGER NOT NULL,
    name    TEXT NOT NULL,
    done_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE runs (
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
    reviewed   INTEGER NOT NULL DEFAULT 0, judge_tries INTEGER NOT NULL DEFAULT 0, keep INTEGER NOT NULL DEFAULT 0,
    UNIQUE (robot_id, mission, stamp)
);
CREATE TABLE schedule_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT NOT NULL,
    scheduled_ms INTEGER NOT NULL,
    outcome      TEXT NOT NULL,
    robot_id     TEXT,
    task_id      TEXT,
    result       TEXT,
    note         TEXT NOT NULL DEFAULT '',
    decided_at   INTEGER NOT NULL,
    told_ms      INTEGER DEFAULT 0, standby_name TEXT NOT NULL DEFAULT '',
    UNIQUE (entry_id, scheduled_ms, outcome)
);
CREATE TABLE schedule_state (
    entry_id        TEXT PRIMARY KEY,
    last_started_ms INTEGER NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    name       TEXT NOT NULL REFERENCES accounts(name),
    created_at INTEGER NOT NULL,
    last_used  INTEGER NOT NULL,
    scope      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE site_mode (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    mode          TEXT NOT NULL,
    prev_mode     TEXT NOT NULL DEFAULT '',
    visitor_zones TEXT NOT NULL DEFAULT '[]',
    until_ms      INTEGER,
    set_by        TEXT NOT NULL DEFAULT '',
    set_ms        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE standby_points (
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
CREATE TABLE teleop_leases (
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
CREATE TABLE weather (
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
CREATE TABLE weather_returns (
    robot_id   TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE zone_arming (
    zone       TEXT PRIMARY KEY,
    home_armed INTEGER NOT NULL
);
CREATE TABLE zones (
    zone      TEXT PRIMARY KEY,
    intercept TEXT NOT NULL
);
CREATE INDEX commands_robot ON commands(robot_id, issued_at);
CREATE INDEX schedule_runs_task ON schedule_runs(task_id);
CREATE INDEX incidents_task ON incidents(task_id);
CREATE INDEX recordings_start ON recordings(start_ms);
CREATE INDEX alerts_last ON alerts(last_ms);
CREATE INDEX events_kind ON events(kind, robot_id, received_at);
CREATE INDEX runs_by_robot ON runs (robot_id, stamp);
COMMIT;
