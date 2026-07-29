# ClusterOne — Plugin Author SDK

Third-party OEM plugins extend ClusterOne with vendor-specific discovery and
firmware-update behavior. Plugins are pip-installable Python packages that
register through the `clusterone.plugins` entry-point group, OR they can ship
inside the install directory under `plugins/<vendor>/`.

The shipped `GenericRedfishPlugin` is the always-available DMTF-standard
implementation. **Every OEM plugin should subclass it** and override only its
deltas (vendor URIs, model globs, error semantics, the SUM-tool fallback for
older Supermicro boards, Dell's iDRAC job queue, etc.).

## The contract

`core/plugins_api/base.py::OEMPlugin`:

```python
class OEMPlugin(ABC):
    key: str                                  # short stable id ("supermicro")
    display_name: str
    supported_models: tuple[str, ...]         # globs

    # ---- Discovery + firmware updates ----
    def match(self, probe: RedfishProbe) -> float:
        """Return 0.0–1.0 confidence this plugin should handle the host."""

    async def discover(self, base_url: str, creds: Credentials) -> ServerInfo: ...
    async def validate(self, server, fw) -> CompatResult: ...
    async def update_bmc(self, server, fw, on_progress, creds: Credentials) -> Result: ...
    async def update_bios(self, server, fw, on_progress, creds: Credentials) -> Result: ...
    async def update_hgx(self, server, fw, on_progress, creds: Credentials) -> Result: ...

    # ---- v1.1 OS provisioning surface (default no-ops in the base class) ----
    async def get_storage_inventory(
        self, base_url: str, creds: Credentials,
    ) -> list[Drive]:
        """Walk Redfish Storage and return every present Drive — used by
        the disk resolver to pick which drive the OS installer should
        target."""

    async def mount_iso(
        self, server, image_url: str, creds: Credentials,
    ) -> Result:
        """POST `VirtualMedia.InsertMedia { Image: image_url }` on the
        appropriate slot. Operator-supplied creds MUST be forwarded."""

    async def eject_iso(self, server, creds: Credentials) -> Result:
        """Idempotent. Should treat 'no media mounted' as success."""

    async def set_boot_once(
        self, server, target: BootTarget, creds: Credentials,
    ) -> Result:
        """PATCH the System with BootSourceOverrideEnabled=Once."""

    async def power_cycle(self, server, creds: Credentials) -> Result:
        """ComputerSystem.Reset with ResetType=ForceRestart."""

    def capabilities(self) -> set[Capability]: ...
```

`on_progress(state: str, percent: int)` is how your plugin drives the
update orchestrator's state machine — emit `"uploading"`, `"flashing"`,
`"rebooting"`, `"verifying"` with monotonically increasing percent so
the UI animates and each transition is logged.

**Credentials are passed to every action method** — the orchestrator
resolves them via the precedence chain (UI input → vault host scope →
vault OEM scope → vault default → empty). Forward them to your
`RedfishClient` so the BMC accepts the POST.

### v1.1 OS provisioning state machine (informational)

When your plugin's `mount_iso` / `set_boot_once` / `power_cycle` are
exercised by the **ProvisioningOrchestrator**, each task walks:

```
queued → resolving_disk → rendering_config → publishing
       → mounting_iso → setting_boot → power_cycling
       → installing → verifying → completed | failed
```

You don't need to call `on_progress` for provisioning — the orchestrator
drives state transitions itself based on which method it just invoked.
Just return a clean `Result(ok=True)` or `Result(ok=False, message=…)`
with a user-readable error.

## Minimal plugin

A pure subclass that only sharpens routing:

```python
# my_oem/plugin.py
from core.plugins_api.base import RedfishProbe
from plugins.generic_redfish.plugin import GenericRedfishPlugin

class MyOemPlugin(GenericRedfishPlugin):
    key = "my_oem"
    display_name = "My OEM"
    supported_models = ("Foo1*", "Foo2*")

    def match(self, probe: RedfishProbe) -> float:
        manuf = (probe.manufacturer or "").lower()
        return 0.9 if "my-oem" in manuf else 0.0

PLUGIN = MyOemPlugin()
```

## Bundled vs pip-installed

### Bundled (ships with the app)

Place under `plugins/<key>/`:

```
plugins/my_oem/
├── __init__.py       # from .plugin import PLUGIN
├── manifest.json     # see schema below
└── plugin.py
```

`manifest.json`:

```json
{
  "key": "my_oem",
  "display_name": "My OEM",
  "version": "0.1.0",
  "supported_models": ["Foo1*", "Foo2*"],
  "capabilities": ["discover", "validate", "update_bmc", "update_bios"],
  "entry": "plugin.py",
  "instance": "PLUGIN"
}
```

`PluginRegistry.load_bundled()` scans `plugins/*/manifest.json` at startup
and imports `plugins.<key>.plugin`, taking the attribute named by `instance`.

### Pip-installable (third-party)

Ship a Python package that exposes an entry point in the
`clusterone.plugins` group:

```toml
# pyproject.toml
[project.entry-points."clusterone.plugins"]
my_oem = "my_oem_pkg:PLUGIN"
```

`PluginRegistry.load_entry_points()` discovers and loads it. Users install
with `pip install my-oem-clusterone-plugin` against the bundled Python.

## Routing — how a host finds your plugin

On every discovered host, every plugin's `match(probe)` is called. The highest
score wins. The generic plugin returns `0.1` as the floor. Use confident
matches (`0.9`+) when you're certain (vendor string, OEM block); use
`0.5`–`0.8` for hints; return `0.0` if it isn't your hardware.

## Reporting progress correctly

```python
async def update_bmc(self, server, fw, on_progress):
    on_progress("uploading", 10)
    task_uri = await self._post_simple_update(...)
    on_progress("flashing", 30)

    last = 0
    for _ in range(120):
        task = await self._client.get_json(task_uri)
        pct = int(task["PercentComplete"])
        if pct != last:
            on_progress("flashing", 30 + int(pct * 0.5))
            last = pct
        if task["TaskState"] in ("Completed", "Exception", "Killed"):
            break
        await asyncio.sleep(0.5)

    on_progress("rebooting", 85)
    await self._wait_for_bmc_back(...)

    on_progress("verifying", 95)
    actual = await self._read_current_version(...)
    if actual != fw.version:
        return Result(ok=False, message=f"verify mismatch — got {actual}")
    return Result(ok=True, version_after=actual)
```

## Safety expectations

Your plugin's `validate()` and `update_*()` must:

- **Never** disable TLS verification globally. Reuse `RedfishClient`, which
  honors the pinning store (M3).
- **Re-verify** SHA-256 on the firmware blob before sending it to the BMC.
- **Refuse** version downgrades unless the caller explicitly asks for it
  (`CompatResult(compatible=False, is_downgrade=True, ...)`).
- **Always** return a structured `Result`. Don't `raise` for expected failure
  modes — the orchestrator records `Result.message` on the task.

## Testing your plugin

Use the in-process simulator (`tests/sim/redfish_sim.py`) to exercise the
discovery + update pipeline without real hardware. Add per-OEM fixtures to
the fleet list if your plugin reads vendor-specific OEM blocks.

```python
# my_oem_pkg/tests/test_plugin.py
import pytest
from aiohttp import web
from tests.sim.redfish_sim import make_app, DEFAULT_FLEET
from my_oem_pkg import PLUGIN

@pytest.mark.asyncio
async def test_match_my_oem():
    # supply a fake probe matching your vendor string
    from core.plugins_api.base import RedfishProbe
    probe = RedfishProbe(ip="1.2.3.4", manufacturer="My-OEM Corp")
    assert PLUGIN.match(probe) > 0.8
```

## When to override `_do_update`

If your OEM departs from standard Redfish `SimpleUpdate` (e.g. Dell iDRAC's
job queue, Supermicro X11 SUM fallback, HPE iLO flash signaling), override
the generic plugin's `_do_update`. The orchestrator only cares that you call
`on_progress` and return a `Result`.

## When to override Virtual Media methods

Each vendor's Redfish Virtual Media subsystem has gotchas. The base
`GenericRedfishPlugin` posts `InsertMedia` / `EjectMedia` against
`/redfish/v1/Managers/{id}/VirtualMedia/CD`. Override on subclass only
for vendor differences. Real examples shipped:

**Dell iDRAC** — different slot name:

```python
class DellPlugin(GenericRedfishPlugin):
    key = "dell"
    # iDRAC9/10 publishes the OS-install slot as RemovableDisk, not CD.
    # Setting this class field is the only override needed; every
    # InsertMedia / EjectMedia call from the base class uses it.
    _VM_SLOT = "RemovableDisk"
```

**HPE iLO** — eject-before-insert idempotency:

```python
class HpePlugin(GenericRedfishPlugin):
    key = "hpe"

    async def mount_iso(self, server, image_url, creds):
        # iLO 4 / iLO 5 reject InsertMedia if anything is already mounted.
        # An explicit eject first makes the mount idempotent.
        await self.eject_iso(server, creds)
        return await super().mount_iso(server, image_url, creds)
```

## Disk resolver (v1.1)

The ProvisioningOrchestrator picks the *target drive* per host using a
strategy string like `first_nvme`, `smallest_ssd`, `by_size:480GB±10%`,
`by_model:INTEL_SSDSC*`, etc. The strategy runs against the `Drive` list
your plugin returned from `get_storage_inventory`. If you report
plausible `Drive.protocol` (`NVMe`/`SATA`/`SAS`), `Drive.media_type`
(`SSD`/`HDD`), and `Drive.capacity_bytes`, the bundled strategies
"just work" against your hardware. See
`core/services/disk_resolver.py` for the full grammar.
