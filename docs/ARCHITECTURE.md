# ClusterOne — Architecture

Clean, layered architecture. The dependency rule points inward: QML knows
nothing of services; services know nothing of Qt. The **Bridge** is the only
place Qt and domain logic meet — keeping the engine testable headless (CI, the
simulator) and the UI swappable.

```
┌─────────────────────────────────────────────────────────────────────┐
│ PRESENTATION  — QML / Qt Quick Controls (GPU-accelerated, animated)   │
│   Main.qml · NavRail · pages/* · components/* · Theme (context obj)   │
└──────────────▲──────────────────────────────────────┬────────────────┘
      signals   │ (Qt::QueuedConnection — thread-safe)  │ bindings / slots
┌──────────────┴──────────────────────────────────────▼────────────────┐
│ BRIDGE  — PyQt6 QObjects exposed to QML (the ONLY Qt-aware layer)     │
│   AppController · ThemeController · QAbstractListModel subclasses      │
└──────────────▲──────────────────────────────────────┬────────────────┘
      results   │                                       │ commands
┌──────────────┴──────────────────────────────────────▼────────────────┐
│ SERVICE LAYER  — pure Python, Qt-agnostic, unit-testable               │
│   DiscoveryService · UpdateOrchestrator · FirmwareService              │
│   ActivityService · SettingsService · CredentialVault                  │
│      │                     │                      │                    │
│      ▼                     ▼                      ▼                    │
│  AsyncEngine        PluginRegistry         Repository layer            │
│  (asyncio loop      (OEM plugins,          (SQLite + WAL)              │
│   in 1 thread)       match + load)                                     │
└──────┼───────────────────┼────────────────────────┼───────────────────┘
  aiohttp/asyncssh   implements OEMPlugin          SQL
       ▼                   ▼                         ▼
  Server BMCs        plugins/<oem>/             SQLite DB + firmware
  (Redfish/IPMI/SSH)  generic_redfish base       blob store + logs
                      + dell/hpe/supermicro…      (%LOCALAPPDATA%)
```

## Concurrency model

A single dedicated **asyncio event loop runs in its own thread** (`AsyncEngine`).
Discovery (254 hosts) and updates (parallel flashes) are I/O-bound, so async
handles thousands of concurrent sockets without a thread-per-host. The GUI
thread never blocks. Results cross back via `loop.call_soon_threadsafe` → a Qt
signal emitted with a queued connection.

- **Redfish**: `aiohttp`
- **SSH**: `asyncssh`
- **IPMI**: `ipmitool` subprocess invoked with an **argument list** (never a
  shell string)
- **SQLite**: writes serialized through the engine's single connection; UI reads
  via its own read-only WAL connection.

## Process / module layout

| Layer | Package |
|-------|---------|
| Presentation | `ui/qml` |
| Bridge | `ui/bridge` (`ThemeController`, `AppController`, models) |
| Services | `core/services` |
| Engine | `core/engine` (async loop, scanner, job runner) |
| Plugin API | `core/plugins_api` (ABC, registry, redfish client) |
| Plugins | `plugins/*` |
| Persistence | `data` (database, migrations, repositories) |
| Composition root (DI) | `app/container.py` |

## Component hierarchy (QML)

```
Main.qml (frameless ApplicationWindow)
├─ NavRail (6 items + brand + status)
└─ ColumnLayout
   ├─ TopBar (title · search · ThemeToggle · window controls; drag region)
   └─ StackLayout → Dashboard · Inventory · Updates · FirmwareLibrary · Activity · Settings

components/ : Card · StatTile · StatusPill · NavButton · PrimaryButton ·
             SearchField · ThemeToggle · WindowControls
```

Data reaches the UI through `QAbstractListModel` subclasses (server/firmware/
activity/job), updated incrementally as discovery streams results. `Theme` is a
live `QObject` whose color properties re-emit on mode change, so Light ↔ Dark ↔
System reskins instantly with a 200 ms animation.
