"""Redfish session-based auth in :class:`RedfishClient`.

Background — why session auth matters:

iDRAC9 counts every HTTP Basic request as a fresh login attempt against
its lockout policy. Three Basic-auth failures in a row lock the
``root`` account for 30 minutes (or until a sysadmin clears the
counter). Discovery alone touches 8+ endpoints per host, so a single
typo in stored credentials used to take down the whole BMC. The fix is
to mint ONE Redfish session via POST ``/SessionService/Sessions``, then
carry the returned ``X-Auth-Token`` on every subsequent request — that
counts as one login no matter how many GETs we run on top of it.

These tests pin the contract:

1. ``login()`` captures the X-Auth-Token and Location headers.
2. Once we hold a token, requests carry ``X-Auth-Token`` and DROP the
   ``Authorization: Basic`` header entirely (otherwise the BMC still
   counts the Basic side and we gain nothing).
3. A 401 on any later request triggers exactly one transparent re-login
   + retry. A 500 response on the retry would surface normally.
4. ``close()`` DELETEs the session resource so we don't exhaust the
   BMC's session table over a long discovery loop.

Stub BMCs are tiny aiohttp apps on a random localhost port (same pattern
as :mod:`tests.unit.test_firmware_update_transport`). Nothing here touches
real hardware.
"""
from __future__ import annotations

import pytest
from aiohttp import web

from core.plugins_api.redfish_client import RedfishClient


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Canonical session URI the stubs hand out — keep one constant so the tests
# can assert against it without coupling to any specific BMC.
_SESSION_LOC = "/redfish/v1/SessionService/Sessions/42"
_TOKEN = "abc123"


class _MockBMC:
    """Minimal Redfish BMC stub with configurable auth behaviour.

    Each test instantiates one with knobs flipped to exercise a particular
    code path (e.g. "first GET returns 401 then succeeds"). Recorded
    request metadata lets the tests assert on the actual headers we sent.
    """

    def __init__(
        self,
        *,
        fail_first_get: bool = False,
        session_returns_no_location: bool = False,
    ):
        # Replayable knobs the tests reach in to configure.
        self.fail_first_get = fail_first_get
        self.session_returns_no_location = session_returns_no_location

        # Observed state — what the tests assert against.
        self.login_count = 0
        self.update_service_hits: list[dict[str, str]] = []
        self.deleted_session_uris: list[str] = []
        # Counter used to flip the 401-then-200 behaviour without making
        # the tests depend on internal request ordering. Once we've served
        # a 401, the next hit goes 200.
        self._update_service_fail_remaining = 1 if fail_first_get else 0

        self.app = web.Application()
        r = self.app.router
        r.add_post("/redfish/v1/SessionService/Sessions", self._post_session)
        r.add_delete(
            "/redfish/v1/SessionService/Sessions/{sid}",
            self._delete_session,
        )
        r.add_get("/redfish/v1/UpdateService", self._update_service)

    async def _post_session(self, req: web.Request) -> web.Response:
        body = await req.json()
        # Sanity check — the test will fail loudly if we ever stop sending
        # the canonical Redfish keys.
        assert body.get("UserName") == "root"
        assert body.get("Password") == "calvin"
        self.login_count += 1
        headers = {"X-Auth-Token": _TOKEN}
        if not self.session_returns_no_location:
            headers["Location"] = _SESSION_LOC
        return web.Response(status=201, headers=headers)

    async def _delete_session(self, req: web.Request) -> web.Response:
        # Record the path so the test can assert it matches Location.
        self.deleted_session_uris.append(req.path)
        return web.Response(status=204)

    async def _update_service(self, req: web.Request) -> web.Response:
        # Snapshot the auth-relevant headers exactly as the BMC would see
        # them. ``Authorization`` is present on Basic; ``X-Auth-Token`` on
        # session.
        self.update_service_hits.append(
            {
                "Authorization": req.headers.get("Authorization", ""),
                "X-Auth-Token": req.headers.get("X-Auth-Token", ""),
                "OData-Version": req.headers.get("OData-Version", ""),
                "Accept": req.headers.get("Accept", ""),
            }
        )
        if self._update_service_fail_remaining > 0:
            self._update_service_fail_remaining -= 1
            return web.Response(status=401, text="stale token")
        return web.json_response({"ServiceEnabled": True, "Id": "UpdateService"})


async def _serve(mock: _MockBMC) -> tuple[web.AppRunner, int]:
    """Bind the mock to a random localhost port. Returns (runner, port).

    The test must ``await runner.cleanup()`` in its ``finally`` block to
    free the socket — the harness does not own a lifecycle for it.
    """
    runner = web.AppRunner(mock.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = list(runner.addresses)[0][1]
    return runner, port


def _client(port: int) -> RedfishClient:
    """Build a RedfishClient pointed at the stub on ``port``."""
    return RedfishClient(
        ip="127.0.0.1",
        port=port,
        scheme="http",          # bypass TLS — the stubs are plain HTTP
        username="root",
        password="calvin",
        timeout=5.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_captures_token_and_session_uri():
    """Spec: ``login()`` populates ``_token`` from ``X-Auth-Token`` and
    ``_session_uri`` from ``Location``.

    This is the bare minimum for everything else to work. Without these
    two values we have nothing to send on follow-up requests and nothing
    to DELETE on close — leaking sessions on the BMC.
    """
    mock = _MockBMC()
    runner, port = await _serve(mock)
    try:
        client = _client(port)
        try:
            ok = await client.login()
            assert ok is True, "login() should return True on 201 response"
            assert client._token == _TOKEN
            assert client._session_uri is not None
            assert client._session_uri.endswith("/42")
            assert mock.login_count == 1
        finally:
            await client.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_subsequent_requests_use_token_not_basic():
    """Spec: once a token is held, requests carry ``X-Auth-Token`` and
    omit ``Authorization: Basic``.

    Sending Basic alongside the token would still count as a login on
    iDRAC9 and defeat the entire purpose of the change. The two MUST be
    mutually exclusive on a per-request basis. We also assert the
    ``OData-Version`` and ``Accept`` headers are present — some HPE iLO5
    firmware returns 415 without them.
    """
    mock = _MockBMC()
    runner, port = await _serve(mock)
    try:
        client = _client(port)
        try:
            await client.login()
            await client.get_json("/redfish/v1/UpdateService")
            assert len(mock.update_service_hits) == 1
            hit = mock.update_service_hits[0]
            assert hit["X-Auth-Token"] == _TOKEN
            # Crucially: NO Basic header on the wire.
            assert hit["Authorization"] == ""
            # Per-request defaults that some BMCs require.
            assert hit["OData-Version"] == "4.0"
            assert hit["Accept"] == "application/json"
        finally:
            await client.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_401_triggers_relogin_and_retry():
    """Spec: a 401 from a routine request triggers ONE silent re-login +
    retry.

    The BMC might reboot and drop its session table mid-poll, or the
    token might TTL out. Our caller should never see the 401 — we mint a
    fresh token and replay the request. We also assert that the body
    finally returned is the one from the 200 retry, not lost data from
    the 401.
    """
    # ``fail_first_get`` causes the stub's first /UpdateService GET to
    # 401 (simulating an expired token) and the second to succeed.
    mock = _MockBMC(fail_first_get=True)
    runner, port = await _serve(mock)
    try:
        client = _client(port)
        try:
            # Prime the client with a token from a successful login.
            await client.login()
            assert mock.login_count == 1
            body = await client.get_json("/redfish/v1/UpdateService")
            # Two hits on /UpdateService: the original 401 and the retry.
            assert len(mock.update_service_hits) == 2
            # Exactly one extra login happened after the 401. The first
            # login was the priming one above; this is the retry login.
            assert mock.login_count == 2
            # The caller received the body from the SUCCESSFUL retry.
            assert body.get("Id") == "UpdateService"
        finally:
            await client.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_close_deletes_session():
    """Spec: ``close()`` DELETEs the session resource so BMCs don't fill
    up their session table over long polling loops.

    iDRAC9 caps the session table at ~16. Ten back-to-back discovery
    passes that each leak one session = lockout. We DELETE in the
    teardown path, idempotently — repeated close() is a no-op.
    """
    mock = _MockBMC()
    runner, port = await _serve(mock)
    try:
        client = _client(port)
        await client.login()
        # Capture the URI BEFORE close() clears the field, so we can
        # assert that this is exactly what the BMC saw deleted.
        session_uri = client._session_uri
        assert session_uri is not None

        await client.close()

        assert len(mock.deleted_session_uris) == 1
        assert mock.deleted_session_uris[0] == _SESSION_LOC
        # Local state cleared so a follow-up request would mint a fresh
        # session instead of trying to reuse a token the BMC has dropped.
        assert client._token is None
        assert client._session_uri is None
        # Idempotent — a second close() does nothing.
        await client.close()
        assert len(mock.deleted_session_uris) == 1
    finally:
        await runner.cleanup()
