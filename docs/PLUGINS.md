# ClusterOne — Plugin System

## Contract

Every OEM plugin implements `core/plugins_api/base.py::OEMPlugin`:

```python
class OEMPlugin(ABC):
    key: str; display_name: str; supported_models: tuple[str, ...]
    def match(self, probe: RedfishProbe) -> float           # 0.0–1.0 confidence
    async def discover(self, ip, creds) -> ServerInfo
    async def validate(self, server, fw) -> CompatResult
    async def update_bmc(self, server, fw, on_progress) -> Result
    async def update_bios(self, server, fw, on_progress) -> Result
    async def update_hgx(self, server, fw, on_progress) -> Result   # optional
    def capabilities(self) -> set[Capability]
```

## Layering — the key real-world design

Redfish is standardized (DMTF), and all eight supported OEMs implement the
`UpdateService`. So the standard path is written **once**:

```
GenericRedfishPlugin            full DMTF-standard discovery + flash
   ├─ DellPlugin                DellUpdateService, InstallUpon, iDRAC job queue
   ├─ HpePlugin                 iLO UpdateService specifics, version parsing
   ├─ SupermicroPlugin          X11/H12 quirks, SUM-tool fallback for old BMCs
   ├─ LenovoPlugin              XCC semantics
   └─ AsrockRack/Gigabyte/Asus/Msi   mostly standard, minor overrides
```

Each OEM plugin **subclasses** the generic base and overrides only its deltas.
This is what keeps multi-OEM support from drowning in per-vendor rewrites.

## Discovery & matching

`PluginRegistry`:
1. Loads bundled plugins from `plugins/*/manifest.json` via `importlib`.
2. Loads third-party plugins from the `clusterone.plugins` **entry-point** group
   (pip-installable, no core changes).
3. For each discovered host, calls `match(probe)` on all plugins and routes to
   the **highest-confidence** one. `GenericRedfishPlugin` is the always-available
   floor (score `0.1`).

## Manifest (`plugins/<oem>/manifest.json`)

```json
{
  "key": "supermicro",
  "display_name": "Supermicro",
  "version": "0.1.0",
  "supported_models": ["H13*", "X13*"],
  "capabilities": ["discover", "validate", "update_bmc", "update_bios"],
  "entry": "plugin.py",
  "instance": "PLUGIN"
}
```
