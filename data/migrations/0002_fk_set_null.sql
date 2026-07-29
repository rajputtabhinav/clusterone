-- 0002: ON DELETE SET NULL for cross-row references so an operator can
-- remove a server, firmware, or credential after it has been part of a
-- previous update job.
--
-- Before this migration, `update_tasks.server_id`, `update_tasks.firmware_id`,
-- and `servers.cred_id` were declared `REFERENCES …` with no `ON DELETE`
-- action. SQLite's default is NO ACTION, which rejects the DELETE with
-- `FOREIGN KEY constraint failed`; the QML ✕ button then silently no-ops
-- and the operator concludes the app is broken.
--
-- After: deleting the referenced row sets the dependent column to NULL.
-- Audit / job history is preserved; the LEFT JOIN in
-- `jobs_repo.list_tasks_with_server` already renders the missing pieces as
-- blanks instead of crashing.
--
-- SQLite cannot ALTER a foreign-key constraint in place, so each affected
-- table is rebuilt via the table-recreate pattern (RENAME old, CREATE new,
-- INSERT … SELECT, DROP old).
--
-- ``legacy_alter_table=ON`` is critical: in modern SQLite, ALTER TABLE …
-- RENAME auto-rewrites FK references *in other tables* to point at the
-- renamed table. Without legacy mode, renaming `servers` would silently
-- repoint `update_tasks.server_id` at `servers__migrate_0002`, and the
-- next DROP would leave the FK dangling. Legacy mode keeps references
-- as plain text, so our newly-created `servers` table is found by name.

PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;

-- ---- update_tasks: server_id + firmware_id → nullable, SET NULL on delete

ALTER TABLE update_tasks RENAME TO update_tasks__migrate_0002;

CREATE TABLE update_tasks (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES update_jobs(id) ON DELETE CASCADE,
    server_id      INTEGER REFERENCES servers(id)   ON DELETE SET NULL,
    firmware_id    INTEGER REFERENCES firmware(id)  ON DELETE SET NULL,
    state          TEXT NOT NULL DEFAULT 'queued',
    progress       INTEGER DEFAULT 0,
    version_before TEXT,
    version_after  TEXT,
    redfish_task   TEXT,
    message        TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    retries        INTEGER DEFAULT 0
);

INSERT INTO update_tasks (
    id, job_id, server_id, firmware_id, state, progress,
    version_before, version_after, redfish_task, message,
    started_at, finished_at, retries
)
SELECT
    id, job_id, server_id, firmware_id, state, progress,
    version_before, version_after, redfish_task, message,
    started_at, finished_at, retries
FROM update_tasks__migrate_0002;

DROP TABLE update_tasks__migrate_0002;

CREATE INDEX IF NOT EXISTS idx_tasks_job   ON update_tasks(job_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON update_tasks(state);

-- ---- servers: cred_id → SET NULL on delete

ALTER TABLE servers RENAME TO servers__migrate_0002;

CREATE TABLE servers (
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
    cred_id       INTEGER REFERENCES credentials(id) ON DELETE SET NULL,
    last_seen     TEXT,
    discovered_at TEXT,
    updated_at    TEXT,
    tags          TEXT
);

INSERT INTO servers (
    id, ip, hostname, manufacturer, oem, model, serial,
    bmc_version, bios_version, power_state, status, protocol,
    redfish_root, tls_fpr, cred_id, last_seen, discovered_at,
    updated_at, tags
)
SELECT
    id, ip, hostname, manufacturer, oem, model, serial,
    bmc_version, bios_version, power_state, status, protocol,
    redfish_root, tls_fpr, cred_id, last_seen, discovered_at,
    updated_at, tags
FROM servers__migrate_0002;

DROP TABLE servers__migrate_0002;

CREATE INDEX IF NOT EXISTS idx_servers_status ON servers(status);
CREATE INDEX IF NOT EXISTS idx_servers_oem    ON servers(oem);

PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
