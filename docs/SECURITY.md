# ClusterOne — Security

Designed to be used by enterprise datacenters and server manufacturers, so
security is a first-class concern, not an afterthought.

## Credentials
- **Never stored in plaintext.** Encrypt with **Windows DPAPI**
  (`CryptProtectData`) — secrets are bound to the Windows account; no master
  password to manage. Only `credentials.secret_enc` (ciphertext) touches SQLite.
- Scoped resolution: `host:<ip>` → `oem:<key>` → `default`.

## BMC TLS
- BMCs ship self-signed certs. Use **TOFU pinning**: record the cert SHA-256 on
  first successful contact (`servers.tls_fpr`), warn on change.
- **Never** a global `verify=False`. Allow importing a corporate CA for properly
  signed BMCs.

## Firmware integrity
- SHA-256 computed on import and **re-verified before every flash**.
- Vendor signature checked where the OEM exposes it. Reject on mismatch.
- No silent downgrades unless explicitly forced.

## Injection / input safety
- External tools (`ipmitool`) invoked with **argument lists**, never shell strings.
- IP ranges and file paths validated; firmware stored by hash → no path traversal.

## Audit & least privilege
- `activity_log` is append-only with an `actor` column (RBAC-ready for V2),
  exportable for compliance.
- Runtime data under `%LOCALAPPDATA%\ClusterOne`, **not** the signed install dir.

## Distribution
- **Authenticode-sign** `ClusterOne.exe` and the installer (EV cert preferred to
  avoid SmartScreen/AV quarantine).

## Rollout safety (operational)
- Dry-run mode (precheck + upload, skip flash) — default for new fleets.
- Pre-flight gates: power state, no in-progress BMC task, model match, checksum.
- **Quorum-aware staggered rollout**: cap how much of a cluster reboots at once
  so AI-cluster control planes keep quorum.
