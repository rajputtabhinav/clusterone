-- 0003: Bulk OS provisioning schema (ClusterOne v1.1).
--
-- Adds the data model for ISO Library, OS Profiles, Disk Selection Profiles,
-- and the per-server provisioning job / task tracking. Mirrors the firmware
-- update tables in shape so the existing infrastructure (jobs page, audit
-- log, reports) extends naturally.
--
-- Also extends `servers` with a cached disk inventory column so the disk
-- resolver doesn't need to re-poll Redfish at every preview render.

-- ---- ISO Library: uploaded install media -----------------------------------

CREATE TABLE IF NOT EXISTS iso_library (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    os_family   TEXT NOT NULL,         -- 'rhel'|'rocky'|'alma'|'ubuntu'|'debian'|'esxi'|'proxmox'|'windows'
    os_version  TEXT NOT NULL,         -- '9.4', '24.04', '8.0u3', '2022', ...
    arch        TEXT NOT NULL DEFAULT 'x86_64',
    file_path   TEXT NOT NULL,         -- absolute path under config.iso_dir()
    sha256      TEXT NOT NULL UNIQUE,  -- content-addressable; dedupe across uploads
    size_bytes  INTEGER,
    source_url  TEXT,                  -- optional: where it was downloaded from
    uploaded_at TEXT,
    uploaded_by TEXT,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_iso_family ON iso_library(os_family, os_version);

-- ---- OS Profiles: reusable install templates -------------------------------

CREATE TABLE IF NOT EXISTS os_profiles (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,         -- 'web-tier', 'db-tier', 'lab-default'
    os_family     TEXT NOT NULL,
    template_kind TEXT NOT NULL,         -- 'kickstart' | 'cloud-init' | 'autounattend' | 'esxi-ks'
    template_body TEXT NOT NULL,         -- the literal template (supports {{ var }} substitution)
    network_mode  TEXT NOT NULL DEFAULT 'dhcp',  -- 'dhcp' | 'static'
    network_cfg   TEXT,                  -- JSON: { 'cidr_pool', 'gateway', 'dns', 'vlan' }
    hostname_ptrn TEXT,                  -- 'web-{n:03}' | 'db-{ip_last_octet}' | '' (use server.hostname)
    ssh_keys      TEXT,                  -- newline-separated authorized_keys
    root_pw_enc   BLOB,                  -- DPAPI-encrypted root password (vault contract)
    created_at    TEXT,
    created_by    TEXT,
    notes         TEXT
);

-- ---- Disk Selection Profiles: intent-based drive picking -------------------

CREATE TABLE IF NOT EXISTS disk_selection_profiles (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    strategy        TEXT NOT NULL,         -- see core/services/disk_resolver.py for grammar
    allow_nonempty  INTEGER NOT NULL DEFAULT 0,   -- 1 = OK to wipe a disk with existing signatures
    on_no_match     TEXT NOT NULL DEFAULT 'fail', -- 'fail' | 'skip' | 'prompt'
    created_at      TEXT,
    notes           TEXT
);

-- ---- Provisioning jobs + tasks (mirrors update_jobs / update_tasks) -------

CREATE TABLE IF NOT EXISTS provisioning_jobs (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT,
    created_by      TEXT,
    iso_id          INTEGER REFERENCES iso_library(id)             ON DELETE SET NULL,
    os_profile_id   INTEGER REFERENCES os_profiles(id)             ON DELETE SET NULL,
    disk_profile_id INTEGER REFERENCES disk_selection_profiles(id) ON DELETE SET NULL,
    concurrency     INTEGER NOT NULL DEFAULT 8,    -- parallel power-cycle batch size
    state           TEXT NOT NULL DEFAULT 'pending',
    total           INTEGER,
    succeeded       INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    confirm_token   TEXT                            -- the 'WIPE 48 SERVERS' the operator typed
);

CREATE TABLE IF NOT EXISTS provisioning_tasks (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES provisioning_jobs(id) ON DELETE CASCADE,
    server_id       INTEGER REFERENCES servers(id) ON DELETE SET NULL,
    -- resolved_disk: JSON {name, model, serial, capacity_bytes, ...} captured
    -- at job kickoff so audit history survives even if disks change later.
    resolved_disk   TEXT,
    rendered_cfg_sha TEXT,                          -- cache hash for the per-host autoinstall config
    state           TEXT NOT NULL DEFAULT 'queued',
    progress        INTEGER DEFAULT 0,
    message         TEXT,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_prov_tasks_job   ON provisioning_tasks(job_id);
CREATE INDEX IF NOT EXISTS idx_prov_tasks_state ON provisioning_tasks(state);

-- ---- Cache disk inventory on the servers row ------------------------------

ALTER TABLE servers ADD COLUMN disks_json TEXT;
ALTER TABLE servers ADD COLUMN disks_at   TEXT;
