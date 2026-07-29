# ClusterOne

**One Platform. Every Server.**

A native Windows desktop application for enterprise **firmware lifecycle
management** AND **bulk OS provisioning** across servers and AI clusters.
PyQt6 / QML on a Qt-agnostic Python service core, with a plugin-based
multi-OEM layer, SQLite at rest, a DPAPI credential vault, and an embedded
HTTP server that streams ISOs to BMCs via Redfish Virtual Media.

## Features

- **Discovery** — IP range / CIDR / port-range scanner, parallel Redfish
  probe, per-host TLS pinning (TOFU). Includes a bundled simulator so the
  full flow works without real hardware.
- **Firmware updates** — bulk BMC / BIOS / HGX flashes via DMTF Redfish
  `UpdateService.SimpleUpdate`; pre-flight SHA-256 verify; per-task state
  machine; live progress UI; resume-on-restart.
- **Bulk OS provisioning (v1.1)** — ISO Library, embedded HTTP server with
  HMAC-signed URLs, autoinstall renderer (Rocky 9 kickstart + Ubuntu 24
  cloud-init bundled; operator-overridable templates), 10-strategy disk
  resolver with per-host overrides + live drive preview, multi-stage WIPE
  confirmation, audit-trail capture of the resolved disk at job time.
- **Plugin-based OEM support** — Dell, HPE, Lenovo, Supermicro, ASRock
  Rack, ASUS, Gigabyte, MSI on top of a generic DMTF Redfish base. Vendor
  plugins override only their deltas (e.g. Dell's `RemovableDisk`
  Virtual Media slot, HPE iLO's eject-before-insert).
- **Premium desktop UX** — frameless rounded window, sticky save bars,
  toasts, server detail drawer, type-to-confirm on destructive actions,
  click-outside-clears-focus on every text input.

## Install (end users)

Download the latest signed `ClusterOne_Setup_<version>.exe` from the
releases page and run it. See [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md).

## Run from source (dev)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m app                    # launches the GUI
```

For end-to-end discovery/update testing without real hardware, run the
in-process simulator in a second terminal:

```powershell
.venv\Scripts\python -m tests.sim.redfish_sim  # 8 fake hosts on :8000-8007
```

then in the app's Discovery dialog point at `http://127.0.0.1:8000-8007`.

## Build the installer

```powershell
.venv\Scripts\pip install pyinstaller
.\packaging\build.ps1                          # PyInstaller + Inno Setup
```

See [`docs/RELEASE.md`](docs/RELEASE.md) for the full signing + publish
runbook.

## Tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\pip install pytest pytest-asyncio
.venv\Scripts\pytest tests\unit -v
.venv\Scripts\python tests\smoke_qml.py
.venv\Scripts\pytest tests\integration -v
```

CI runs the same matrix on Windows + Linux for Python 3.12 / 3.13
(see `.github/workflows/ci.yml`).

## Layout

| Path | Purpose |
|------|---------|
| `app/` | Entrypoint, bootstrap, DI container, config, logging, focus filter |
| `core/` | Domain models, services (discovery, update + provisioning orchestrators, file server, autoinstall renderer, disk resolver), async engine, plugin API, security, reports |
| `plugins/` | OEM plugins — `generic_redfish` (DMTF base) + `supermicro`, `dell`, `hpe`, `lenovo`, `gigabyte`, `asrock`, `asus`, `msi` |
| `data/` | SQLite repositories + migrations (`0001` schema, `0002` FK cascade, `0003` provisioning), seeds, bundled autoinstall templates |
| `ui/` | QML views (`ui/qml`) + PyQt bridge controllers (`ui/bridge`) |
| `tests/` | Unit + integration tests + `sim/` Redfish/BMC simulator |
| `packaging/` | PyInstaller spec, Inno Setup installer, signing scripts |
| `docs/` | Architecture, plugin SDK, security, user guide, release runbook, production plan |

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design and
[`docs/PRODUCTION_PLAN.md`](docs/PRODUCTION_PLAN.md) for the roadmap.

## Runtime data

Database, firmware blobs, **ISO library**, and logs live **outside** the
install directory under `%LOCALAPPDATA%\ClusterOne` (override with
`CLUSTERONE_DATA_DIR`). Generated reports land in
`%USERPROFILE%\Documents\ClusterOne\`.

The embedded HTTP server for BMC-side ISO/autoinstall fetches binds to
`0.0.0.0:8443` by default (configurable via the `bmc_http_bind` /
`bmc_http_port` settings keys). Lock it to your management subnet in
production. URLs are short-lived (15-minute expiry, HMAC-SHA256-signed).

## Security

- BMC credentials are encrypted at rest via Windows DPAPI (only ciphertext
  ever touches SQLite).
- BMC TLS is TOFU-pinned (Trust On First Use); subsequent connections warn
  on fingerprint change. No global `verify=False`.
- Firmware images are SHA-256-deduped and re-verified before every flash.
- Append-only audit log captures every discovery, firmware add/delete, and
  update transition.

## License

TBD.
