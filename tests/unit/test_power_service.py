"""PowerService: issues plugin power actions, reads PowerState back, persists it.

asyncio_mode=auto (see pyproject) runs the async tests without an explicit
marker. We drive the service's async ``_run`` directly so no Qt engine is
involved.
"""
from __future__ import annotations

from core.plugins_api.base import Result
from core.services.power_service import PowerService
from data.repositories.servers_repo import ServersRepo


class _FakePlugin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.state = "Off"

    async def power_action(self, server, action, creds) -> Result:
        self.calls.append((action, server.ip))
        if action == "on":
            self.state = "On"
        elif action in ("off", "force_off"):
            self.state = "Off"
        return Result(ok=True, message=f"{action} issued")

    async def get_power_state(self, server, creds):
        return self.state


class _FakeRegistry:
    def __init__(self, plugin) -> None:
        self._p = plugin

    def match(self, probe):
        return self._p

    def resolve(self, probe, override_key=None):
        return self._p


class _StubActivity:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def log(self, level, category, message, **kw) -> None:
        self.events.append((level, category, message))


class _NoEngine:
    def submit(self, coro):  # pragma: no cover - tests call _run directly
        raise AssertionError("tests should drive _run directly, not the engine")


def _make(db_conn, plugin, *, redfish="https://10.0.0.9:443/redfish/v1/"):
    repo = ServersRepo(db_conn)
    sid = repo.upsert({"ip": "10.0.0.9", "status": "online", "oem": "supermicro",
                       "redfish_root": redfish, "power_state": "Off"})
    # Manual-only routing: every operation requires an operator-picked OEM.
    repo.set_oem_override(sid, "supermicro")
    svc = PowerService(_NoEngine(), _FakeRegistry(plugin), repo, _StubActivity())
    return svc, repo, sid


async def test_power_on_persists_state(db_conn):
    plugin = _FakePlugin()
    svc, repo, sid = _make(db_conn, plugin)
    results: list[tuple] = []
    await svc._run([sid], "on", "", "", 4,
                   on_result=lambda *a: results.append(a), on_done=None)
    assert plugin.calls == [("on", "10.0.0.9")]
    assert results[0][2] is True          # ok
    assert results[0][4] == "On"          # power_state read back
    assert repo.get(sid)["power_state"] == "On"   # persisted


async def test_force_off_persists_off(db_conn):
    plugin = _FakePlugin()
    plugin.state = "On"
    svc, repo, sid = _make(db_conn, plugin)
    done: list[dict] = []
    await svc._run([sid], "force_off", "", "", 4,
                   on_result=lambda *a: None, on_done=lambda s: done.append(s))
    assert plugin.calls == [("force_off", "10.0.0.9")]
    assert repo.get(sid)["power_state"] == "Off"
    assert done == [{"action": "force_off", "ok": 1, "failed": 0, "total": 1}]


async def test_unknown_action_is_rejected_without_engine(db_conn):
    plugin = _FakePlugin()
    svc, _repo, sid = _make(db_conn, plugin)
    assert svc.apply([sid], "explode") is None    # unknown -> no submit
    assert plugin.calls == []


async def test_missing_redfish_endpoint_fails_cleanly(db_conn):
    plugin = _FakePlugin()
    svc, _repo, sid = _make(db_conn, plugin, redfish="")
    results: list[tuple] = []
    await svc._run([sid], "on", "", "", 4,
                   on_result=lambda *a: results.append(a), on_done=None)
    assert results[0][2] is False
    assert "no Redfish endpoint" in results[0][3]
    assert plugin.calls == []                      # never reached the plugin
