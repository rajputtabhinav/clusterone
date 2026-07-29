-- ClusterOne initial schema (v1)

CREATE TABLE IF NOT EXISTS credentials (
    id          INTEGER PRIMARY KEY,
    label       TEXT,
    username    TEXT NOT NULL,
    secret_enc  BLOB NOT NULL,           -- DPAPI ciphertext only; never plaintext
    scope       TEXT,                    -- 'default' | 'host:<ip>' | 'oem:<key>'
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS servers (
    id            INTEGER PRIMARY KEY,
    ip            TEXT NOT NULL UNIQUE,
    hostname      TEXT,
    manufacturer  TEXT,
    oem           TEXT,
    model         TEXT,
    serial        TEXT,
    bmc_version   TEXT,
    bios_version  TEXT,
    power_state   TEXT,
    status        TEXT NOT NULL DEFAULT 'unknown',
    protocol      TEXT,
    redfish_root  TEXT,
    tls_fpr       TEXT,
    cred_id       INTEGER REFERENCES credentials(id),
    last_seen     TEXT,
    discovered_at TEXT,
    updated_at    TEXT,
    tags          TEXT
);
CREATE INDEX IF NOT EXISTS idx_servers_status ON servers(status);
CREATE INDEX IF NOT EXISTS idx_servers_oem    ON servers(oem);

CREATE TABLE IF NOT EXISTS firmware (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,           -- BMC | BIOS | HGX
    oem         TEXT NOT NULL,
    model       TEXT,
    version     TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,
    size_bytes  INTEGER,
    signature   TEXT,
    uploaded_at TEXT,
    uploaded_by TEXT,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fw_type_oem ON firmware(type, oem);

CREATE TABLE IF NOT EXISTS update_jobs (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT,
    created_by  TEXT,
    fw_type     TEXT,
    concurrency INTEGER,
    apply_time  TEXT,                    -- Immediate | OnReset
    dry_run     INTEGER DEFAULT 0,
    state       TEXT,
    total       INTEGER,
    succeeded   INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS update_tasks (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES update_jobs(id) ON DELETE CASCADE,
    server_id      INTEGER NOT NULL REFERENCES servers(id),
    firmware_id    INTEGER NOT NULL REFERENCES firmware(id),
    state          TEXT NOT NULL DEFAULT 'queued',
    progress       INTEGER DEFAULT 0,
    version_before TEXT,
    version_after  TEXT,
    redfish_task   TEXT,                 -- TaskMonitor URI (enables resume)
    message        TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    retries        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_job   ON update_tasks(job_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON update_tasks(state);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id          INTEGER PRIMARY KEY,
    range_spec  TEXT,
    methods     TEXT,
    started_at  TEXT,
    finished_at TEXT,
    scanned     INTEGER,
    found       INTEGER,
    state       TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    level     TEXT,
    category  TEXT,                      -- discovery/update/firmware/system/auth
    actor     TEXT,
    server_id INTEGER,
    job_id    INTEGER,
    message   TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts  ON activity_log(ts);
CREATE INDEX IF NOT EXISTS idx_activity_cat ON activity_log(category);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Single-row version table. PRIMARY KEY + INSERT OR REPLACE in migrate()
-- guarantees exactly one row even across torn writes / crash-mid-migration.
-- Old (versions < 1) builds created this without a PRIMARY KEY; the
-- application code now uses INSERT OR REPLACE so legacy DBs still work
-- but new DBs get the proper constraint.
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY NOT NULL
);
