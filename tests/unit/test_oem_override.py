"""Manual per-server OEM override: registry routing, persistence, end-to-end.

The operator can pin an OEM in the UI when a BMC misreports its manufacturer
and auto-detection routes to the wrong plugin (or the generic fallback).
The override must (a) win over confidence scoring, (b) survive re-discovery
upserts, and (c) actually drive plugin selection in the service layer.
"""
from __future__ import annotations

from core.plugins_api.base import RedfishProbe, Result
from core.plugins_api.registry import PluginRegistry
from core.services.power_service import PowerService
from data.repositories.servers_repo import ServersRepo


class _StubPlugin:
    def __init__(self, key: str, score: float) -> None:
        self.key = key
        self.display_name = key.title()
        self._score = score
        self.power_calls: list[str] = []

    def match(self, probe) -> float:
        return self._score

    async def power_action(self, server, action, creds) -> Result:
        self.power_calls.append(action)
        return Result(ok=True, message=f"{action} via {self.key}")

    async def get_power_state(self, server, creds):
        return "On"


def _registry(*plugins) -> PluginRegistry:
    reg = PluginRegistry()
    reg._plugins.extend(plugins)
    return reg


PROBE = RedfishProbe(ip="10.0.0.1", manufacturer="WeirdBrand Inc.",
                     redfish_root="https://10.0.0.1/redfish/v1/")


# ---- registry.resolve ----

def test_resolve_returns_selected_plugin_regardless_of_scores():
    high_scorer = _StubPlugin("supermicro", 0.9)
    pinned = _StubPlugin("dell", 0.0)        # would NEVER win on score
    reg = _registry(high_scorer, pinned)
    assert reg.resolve(PROBE, override_key="dell") is pinned


def test_resolve_is_manual_only_no_selection_means_none():
    """No auto-matching for operations: without a manual pick, resolve()
    returns None even when a plugin scores high on the probe."""
    a = _StubPlugin("supermicro", 0.9)
    reg = _registry(a)
    assert reg.resolve(PROBE) is None
    assert reg.resolve(PROBE, override_key=None) is None
    assert reg.resolve(PROBE, override_key="") is None


def test_resolve_unknown_selection_returns_none():
    """A stale key (plugin uninstalled) must NOT silently fall back to a
    different vendor's recipe — operations fail with a clear message."""
    a = _StubPlugin("supermicro", 0.9)
    reg = _registry(a)
    assert reg.resolve(PROBE, override_key="acme") is None


# ---- persistence ----

def test_override_survives_rediscovery_upsert(db_conn):
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.5", "status": "online",
                       "manufacturer": "WeirdBrand Inc.", "oem": "weirdbrand inc."})
    assert repo.set_oem_override(sid, "supermicro") is True
    assert repo.get(sid)["oem_override"] == "supermicro"

    # Discovery re-upserts the row WITHOUT an oem_override key — the manual
    # pin must survive (it's deliberately not part of upsert's _FIELDS).
    repo.upsert({"ip": "10.0.0.5", "status": "online",
                 "manufacturer": "WeirdBrand Inc.", "oem": "weirdbrand inc.",
                 "bmc_version": "2.0"})
    row = repo.get(sid)
    assert row["oem_override"] == "supermicro"
    assert row["bmc_version"] == "2.0"      # upsert still updated the rest

    # Clearing restores auto.
    repo.set_oem_override(sid, None)
    assert repo.get(sid)["oem_override"] is None


def test_clear_override_restores_required_state(db_conn):
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.6", "status": "online", "serial": "SN-1"})
    repo.set_oem_override(sid, "dell")
    assert repo.get(sid)["oem_override"] == "dell"
    repo.set_oem_override(sid, None)            # "✕ Clear selection" in the UI
    assert repo.get(sid)["oem_override"] is None


def test_ip_reassigned_to_different_hardware_clears_stale_pin(db_conn):
    """IPs are dynamic: when discovery proves the hardware at an IP changed
    (different serial), the old machine's OEM pick and cached disk inventory
    must NOT carry over to the new machine."""
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.7", "status": "online", "serial": "SN-A"})
    repo.set_oem_override(sid, "dell")
    repo.set_disks_by_ip("10.0.0.7", '[{"name":"sda"}]')

    # Same hardware re-discovered → pick + disk cache survive.
    repo.upsert({"ip": "10.0.0.7", "status": "online", "serial": "SN-A"})
    row = repo.get(sid)
    assert row["oem_override"] == "dell"
    assert row["disks_json"] == '[{"name":"sda"}]'

    # DIFFERENT hardware shows up at the same IP → both cleared.
    repo.upsert({"ip": "10.0.0.7", "status": "online", "serial": "SN-B"})
    row = repo.get(sid)
    assert row["oem_override"] is None
    assert row["disks_json"] is None
    assert row["serial"] == "SN-B"


def test_missing_serial_is_conservative_keeps_pin(db_conn):
    """A BMC that omits the serial proves nothing — never clear on guesswork."""
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.8", "status": "online", "serial": "SN-X"})
    repo.set_oem_override(sid, "hpe")
    repo.upsert({"ip": "10.0.0.8", "status": "online"})   # no serial reported
    assert repo.get(sid)["oem_override"] == "hpe"


# ---- end-to-end through a service ----

class _StubActivity:
    def log(self, *a, **k) -> None:
        pass


class _NoEngine:
    def submit(self, coro):  # pragma: no cover
        raise AssertionError("tests drive _run directly")


async def test_power_service_routes_via_override(db_conn):
    auto_winner = _StubPlugin("generic_redfish", 0.1)   # what auto would pick
    pinned = _StubPlugin("supermicro", 0.0)             # operator's choice
    reg = _registry(auto_winner, pinned)
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.9", "status": "online",
                       "manufacturer": "WeirdBrand Inc.",
                       "redfish_root": "https://10.0.0.9:443/redfish/v1/"})
    repo.set_oem_override(sid, "supermicro")

    svc = PowerService(_NoEngine(), reg, repo, _StubActivity())
    results: list[tuple] = []
    await svc._run([sid], "on", "", "", 4,
                   on_result=lambda *a: results.append(a), on_done=None)

    assert results[0][2] is True
    assert pinned.power_calls == ["on"]          # override plugin did the work
    assert auto_winner.power_calls == []         # auto winner was bypassed


async def test_power_service_refuses_without_oem_selection(db_conn):
    """Manual-only: a server with NO OEM picked must fail with an actionable
    message, never silently auto-route."""
    plugin = _StubPlugin("supermicro", 0.9)
    reg = _registry(plugin)
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.11", "status": "online",
                       "redfish_root": "https://10.0.0.11:443/redfish/v1/"})
    svc = PowerService(_NoEngine(), reg, repo, _StubActivity())
    results: list[tuple] = []
    await svc._run([sid], "on", "", "", 4,
                   on_result=lambda *a: results.append(a), on_done=None)
    assert results[0][2] is False
    assert "no OEM selected" in results[0][3]
    assert plugin.power_calls == []
