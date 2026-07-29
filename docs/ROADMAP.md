# ClusterOne — Roadmap

Each phase ends with something runnable. **Dev target: simulator-first** — a
fake Redfish/BMC server (`tests/sim/`) lets the full pipeline run with zero risk
to hardware; real lab BMCs wire in at Phase 4 validation milestones.

| Phase | Outcome | Status |
|-------|---------|--------|
| **0 — Shell** | Scaffold, theming (Light/Dark/System, Inter, 14px radii, 200ms anims), nav rail, 6 pages. App launches. | ✅ done |
| **1 — Data plane** | SQLite + migrations + repositories; Inventory & Firmware Library with real CRUD on seeded data. | next |
| **2 — Discovery** | AsyncEngine + scanner + `GenericRedfishPlugin.discover`; live-streaming inventory. Validated vs the simulator. | |
| **3 — Updates** | UpdateOrchestrator + state machine + generic Redfish `SimpleUpdate`; live progress; Activity timeline. | |
| **4 — OEM plugins** | Real plugins layered on the generic base; priority driven by lab hardware. CredentialVault + TLS pinning. | |
| **5 — Hardening** | Retries/resume, error/empty states, settings wired, structured logging. | |
| **6 — Packaging** | PyInstaller one-dir, qrc-compiled QML, Inno Setup installer, code signing → `ClusterOne.exe`. | |
| **V2** | RBAC, scheduling/maintenance windows, quorum-aware rollout, reporting/export, telemetry. | |

## Implementation order (detailed)

1. ✅ Project config, logging, data-dir resolution.
2. ✅ Qt bootstrap + DI container + bridge controllers.
3. ✅ QML themed shell (nav + 6 pages + live theme switch).
4. ✅ Domain models, plugin ABC, SQLite layer + schema, generic plugin skeleton.
5. Repositories + models wired to Inventory/Firmware (replace seeded data).
6. `tests/sim/` fake Redfish server → scanner → `discover()` → live Inventory.
7. PluginRegistry (manifest + entry-point loading) + match routing.
8. UpdateOrchestrator + job runner state machine + Updates wizard live progress.
9. First real OEM plugin(s) against lab hardware; vault + TLS pinning.
10. Settings wired; polish.
11. PyInstaller spec + Inno Setup + signing scripts.
