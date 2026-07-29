# ClusterOne — Production Completion Plan

Status as of this writing: Phase 0 (shell) + Phase 1 (real data + persistence)
are complete. The shell launches, theme + window state persist, and Inventory /
Firmware / Activity / Dashboard / status ribbon are bound to real SQLite data
with working search, sort, add, and delete.

This document is the bar and the sequencing to take ClusterOne to a shippable
`ClusterOne.exe` v1.0.

## Definition of "production-ready" v1.0

A datacenter engineer can:

1. Install `ClusterOne_Setup.exe` on a fresh Windows box (signed, no SmartScreen).
2. Point discovery at their lab subnet and see real servers stream into Inventory.
3. Select hosts, pick firmware, push to Redfish-capable BMCs with **dry-run + pre-flight + confirm + resume** safety gates.
4. Export a PASS/FAIL report (DOCX/PDF in Netweb/Tyrone house style).
5. Trust that an app crash mid-flash resumes from the persisted `redfish_task` URI.
6. Read a user guide and a plugin-author guide.

Plus: code-signed binaries, headless CI, simulator-backed e2e tests, no
plaintext secrets on disk, deliberate BMC TLS trust (TOFU pinning).

## Milestones (sequenced; effort = engineering-days, single dev)

| # | Milestone | Effort | Blocks |
|---|---|---|---|
| **M1** | Discovery + simulator + async engine + plugin registry | 5 | M2, M4 |
| **M2** | Update engine + state machine + safety gates | 6 | M4, M6 |
| **M3** | Security: DPAPI vault + TLS TOFU pinning | 3 | shipping |
| **M4** | Real OEM plugins (Supermicro + Dell first) | 2 (+1/each more) | shipping |
| **M5** | UX: Ctrl+K palette, detail drawer, empty/loading states, transitions, bundled fonts | 4 | shipping |
| **M6** | Report export (DOCX/PDF, house style) | 3 | — |
| **M7** | Tests + CI (pytest matrix, headless QML smoke) | 3 | shipping |
| **M8** | Packaging: PyInstaller + Inno Setup + EV signing | 3 | shipping |
| **M9** | Documentation: user guide, plugin SDK, release runbook | 2 | shipping |
| | **Total** | **~31 d** | ~6–7 calendar weeks |

Critical path: M1 → M2 → M3 → M7 → M8. M5/M6/M9 are parallelizable.

## Per-milestone definition of done

### M1 — Discovery
- Type `127.0.0.1:8000-8009` (simulator) or a real subnet → live row insertion in Inventory; offline hosts skipped cleanly.
- AsyncEngine: dedicated asyncio thread; results crossed to Qt via queued signals.
- Generic Redfish plugin's `discover()` reads `Systems` + `Managers` correctly.
- Plugin registry loads from `plugins/*/manifest.json` AND `clusterone.plugins` entry points; confidence-scored `match()`.
- Activity log + status ribbon reflect the run.
- No blocking on the GUI thread.

### M2 — Updates
- Per-task state machine: `queued → precheck → uploading → flashing → rebooting → verifying → completed | failed`.
- Concurrency cap; resume from `update_tasks.redfish_task` after restart.
- Pre-flight gates: SHA-256 re-verify, power state, model match, no silent downgrade.
- Dry-run mode is default for first job against a fleet.
- Confirmation dialog with "type the host count" for bulk.
- Live wizard reflects every transition; pause/cancel.

### M3 — Security
- Credentials encrypted at rest via Windows DPAPI; only ciphertext in `credentials.secret_enc`.
- Scope resolution: `host:<ip>` → `oem:<key>` → `default`.
- BMC TLS pinning (TOFU): record fingerprint on first contact, warn on change.
- No global `verify=False`. Optional corporate CA import.
- Every credential read/write writes to audit log.

### M4 — OEM plugins
- Supermicro + Dell plugins implemented as subclasses of `GenericRedfishPlugin`, overriding only deltas (Dell `InstallUpon` / iDRAC job queue; Supermicro X11/H12 quirks).
- Each ships a `manifest.json` and a per-OEM simulator fixture.
- Each handles one real lab host end-to-end (discover + dry-run flash; real BIOS flash on a non-production target).

### M5 — UX polish that ships
- **Ctrl+K command palette** routes to every primary action.
- **Server detail drawer** with version history + per-server actions (power on/off, force re-discover, edit cred scope).
- Real empty/loading/error states with action CTAs on every page.
- Page transitions (200ms cross-fade); visible focus rings (Tab navigation everywhere).
- Inter + IBM Plex Mono TTFs bundled in `assets/fonts/`.
- Result toasts + a reusable confirmation dialog.

### M6 — Report export
- Two report types: **Discovery snapshot** (inventory + compliance) and **Update run** (job summary + per-task PASS/FAIL).
- DOCX via `python-docx`; PDF via `docx2pdf` (Word) or `reportlab` fallback.
- Templates match the Netweb/Tyrone house style (header/footer, table styling, signature blocks).
- Triggered from the Dashboard and from Activity job rows.

### M7 — Tests + CI
- Unit tests for repos, services, state machine, proxy filter/sort, credential vault.
- Integration tests against the simulator: full discovery, full update with each failure mode, resume-after-restart.
- Headless QML smoke test promoted into CI.
- GitHub Actions (or equivalent) matrix: Windows + Linux (offscreen Qt platform).
- Coverage ≥ 75% on `core/` and `data/`. UI not coverage-targeted.

### M8 — Packaging & signing
- `packaging/ClusterOne.spec` — PyInstaller one-dir, QML compiled to `.qrc`/`rcc`, fonts + plugin manifests bundled.
- `packaging/installer.iss` — Inno Setup per-machine install; creates `%PROGRAMDATA%\ClusterOne` with correct ACLs.
- Authenticode (EV) sign `ClusterOne.exe` and the installer.
- Single-source version in `app/config.py` → exe metadata → installer.

### M9 — Documentation
- `docs/USER_GUIDE.md` with screenshots.
- `docs/PLUGIN_SDK.md` — write & ship a third-party OEM plugin via entry points.
- `docs/RELEASE.md` — build/sign/publish runbook.
- `docs/SECURITY.md` extended with operator runbook (rotate creds, cert-pinning workflow, audit-log export).

## Cross-cutting (woven through all milestones)

- **Error handling**: every async boundary wraps + logs + surfaces to UI; no silent failures.
- **Reliability**: exponential backoff on transient Redfish errors; hard-stop on signature/compat failures; idempotent task transitions.
- **Performance**: validate against a 1000-host simulator subnet.
- **Observability**: structured (JSON) logs; "Export diagnostics" action bundling logs + DB schema + version (no secrets).
- **Accessibility**: Tab nav, focus rings, Qt Accessible names, AAA contrast spot-check.
- **Settings persisted**: theme + window geometry already done. Also persist: last-used IP range, default credential scope, update concurrency, log retention.
- **Quorum-aware rollout**: cap "max % of selection rebooting at once" in the Updates wizard (default 25%).

## Risks

1. **Real-hardware variance** — Redfish standard until it isn't. Per-OEM bug round after first contact with real BMCs.
2. **EV code-signing cert** lead time + cost. Start procurement now if not in motion.
3. **Frameless window edge cases** — DPI changes, multi-monitor, snap layouts.
4. **DPAPI scope** — user vs machine; confirm operational model before M3.
5. **First flash on a real production server is irreversible** — dry-run + confirmation are defaults, not opt-in.

## Currently in flight

**M1 — Discovery engine** (this commit kicks it off): async engine, scanner with
IP-range parser, shared Redfish client, plugin registry, real
`GenericRedfishPlugin.discover()`, and a multi-host Redfish simulator under
`tests/sim/`.
