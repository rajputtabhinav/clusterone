# ClusterOne — Database

SQLite in **WAL mode** (concurrent UI reader + engine writer). Schema lives in
`data/migrations/NNNN_*.sql`, applied in order by the runner in
`data/database.py`. Current version: **0001**.

## Tables

| Table | Purpose |
|-------|---------|
| `servers` | Discovered inventory + live status + pinned TLS fingerprint |
| `credentials` | DPAPI **ciphertext** only (`secret_enc BLOB`), scoped default/host/oem |
| `firmware` | Local repository; deduped by `sha256` |
| `update_jobs` | One bulk request (fw type, concurrency, apply-time, dry-run) |
| `update_tasks` | State-machine unit: one server × one firmware; holds `redfish_task` for resume |
| `discovery_runs` | Scan history |
| `activity_log` | Append-only audit trail (`actor` column ready for V2 RBAC) |
| `settings` | Key/value app settings |
| `schema_version` | Migration bookkeeping |

## Key design points

- **`update_tasks.redfish_task`** stores the BMC TaskMonitor URI, so a flash
  interrupted by app restart **resumes monitoring** instead of being lost.
- **`activity_log`** has no hard FK to `servers` (keeps it append-only even if a
  server row is deleted) and carries an `actor` column now so RBAC drops in later.
- **`firmware.sha256` UNIQUE** dedupes uploads and is re-verified before every flash.
- Indexes on `servers(status)`, `servers(oem)`, `firmware(type,oem)`,
  `update_tasks(job_id|state)`, `activity_log(ts|category)`.

State machine values for `update_tasks.state`:
`queued → precheck → uploading → flashing → rebooting → verifying → completed | failed`.
