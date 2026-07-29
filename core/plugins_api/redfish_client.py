"""Shared async Redfish HTTP primitives used by every OEM plugin.

BMC TLS handling: if a pinned SHA-256 fingerprint is supplied
(``tls_fingerprint`` kwarg) the connector uses ``aiohttp.Fingerprint`` to
enforce it — any cert change raises ``ServerFingerprintMismatch``. If no pin
is known (first-contact discovery) we fall back to permissive TLS
(``CERT_NONE``) so the operator can capture the fingerprint; subsequent
connects will pin.

Auth strategy: prefer Redfish session auth (POST /SessionService/Sessions
returns ``X-Auth-Token``) over HTTP Basic. iDRAC9 counts every Basic-auth
request as a fresh login attempt, so three bad/garbled requests in a row
lock the account; session auth costs one login and reuses the token for
every subsequent call. ``login()`` is lazy — the first request that needs
auth triggers it. If ``/SessionService/Sessions`` is unavailable (older
BMCs / simulators), we transparently fall back to Basic and remember not
to retry session login on this client. On a 401 from any later request we
attempt exactly one re-login + retry; a second 401 surfaces as a real
authentication failure to the caller.
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import ssl
import time
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)


class RedfishError(Exception):
    """Base for every Redfish-layer failure. Callers should usually catch
    one of the more specific subclasses below; ``RedfishError`` itself is
    kept for backward compatibility (existing call sites still use it)."""


class RedfishConnectionError(RedfishError):
    """Transient transport-layer failure — socket reset, TLS handshake
    timeout, BMC self-resetting mid-flash, etc. Caller SHOULD retry with
    backoff. Mirrors Sushy's ``ConnectionError`` / Ironic's
    ``RedfishConnectionError`` semantics."""


class RedfishAccessError(RedfishError):
    """Authentication / authorization failure — HTTP 401 / 403. Caller
    MUST NOT retry blindly; surface to operator. Re-login is handled
    internally by ``RedfishClient`` once before this is raised."""


class RedfishHTTPError(RedfishError):
    """Non-transient HTTP failure (4xx other than 401/403, 5xx). Carries
    the BMC's response text so the operator sees the actual rejection
    reason (RED014 "job already present", RED097 "target mismatch", …)."""
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# Retry-After parsing — DMTF spec + RFC 7231. The header may be either an
# integer (seconds) or an HTTP-date. Returns seconds (float) clamped to a
# sensible range so a misbehaving BMC can't park us forever.
def parse_retry_after(value: str | None, default: float = 0.0,
                      cap: float = 600.0) -> float:
    if not value:
        return default
    s = value.strip()
    if not s:
        return default
    try:
        seconds = float(s)
        # Reject inf / nan / negative — any of these would either freeze
        # the poll loop or crash asyncio.sleep() with ValueError.
        if not math.isfinite(seconds) or seconds < 0:
            return default
        return max(0.0, min(seconds, cap))
    except ValueError:
        pass
    try:
        # HTTP-date — e.g. "Wed, 21 Oct 2015 07:28:00 GMT"
        dt = email.utils.parsedate_to_datetime(s)
        if dt is None:
            return default
        wait = dt.timestamp() - time.time()
        return max(0.0, min(wait, cap))
    except (TypeError, ValueError):
        return default


# Default headers sent on every request. ``OData-Version: 4.0`` is required
# by some HPE iLO5 firmware (returns HTTP 415 without it); ``Accept`` keeps
# stricter BMCs from defaulting to XML.
_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "OData-Version": "4.0",
}

# Endpoint we POST to to mint a session token. Spec-canonical path; any BMC
# advertising SessionService at an alternate URI will still resolve this via
# its redirect (we observed iDRAC9, iLO5, AMI MegaRAC, Supermicro all serve
# the canonical path).
_SESSION_PATH = "/redfish/v1/SessionService/Sessions"


class RedfishClient:
    def __init__(
        self,
        ip: str,
        port: int = 443,
        scheme: str = "https",
        username: str = "",
        password: str = "",
        timeout: float = 10.0,
        tls_fingerprint: str | None = None,
    ) -> None:
        self._ip = ip
        self._port = port
        self._scheme = scheme
        self._username = username
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._tls_fpr = tls_fingerprint
        self._captured_fpr: str | None = None
        # One persistent session per client lifetime — reuse the TLS handshake
        # across all the GETs done during a discovery or flash poll loop.
        self._session: aiohttp.ClientSession | None = None
        # Redfish session-auth state. ``_token`` is set after a successful
        # login(); ``_session_uri`` is the BMC-assigned resource we DELETE on
        # close to free the slot. ``_session_login_unavailable`` latches True
        # if the BMC doesn't expose SessionService so we don't pound it.
        self._token: str | None = None
        self._session_uri: str | None = None
        self._session_login_unavailable: bool = False
        # Serialises login attempts so two concurrent requests racing into
        # the same client don't mint two sessions on the BMC.
        self._login_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._ip}:{self._port}"

    @property
    def captured_fingerprint(self) -> str | None:
        return self._captured_fpr

    def _url(self, path: str) -> str:
        """Absolute request URL for a Redfish *path*.

        Accepts a leading-slash path, a slashless path, OR an already-absolute
        URL. Some BMCs return an absolute ``Location`` (TaskMonitor URI) in
        their 202 response; concatenating that onto ``base_url`` would yield a
        malformed ``https://host:443http://host/...``. Absolute URLs pass
        through unchanged.
        """
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def _connector(self) -> aiohttp.BaseConnector:
        if self._scheme != "https":
            return aiohttp.TCPConnector()
        # If we know the peer cert's SHA-256 (TOFU captured at discovery),
        # enforce it. aiohttp.Fingerprint raises ServerFingerprintMismatch
        # on any change — surfacing a possible MITM or BMC cert re-issue.
        if self._tls_fpr:
            try:
                digest = bytes.fromhex(self._tls_fpr)
                return aiohttp.TCPConnector(ssl=aiohttp.Fingerprint(digest))
            except ValueError:
                log.warning("Invalid TLS fingerprint hex for %s — pin ignored", self._ip)
        # First contact (or unpinned): accept any cert. Discovery captures
        # the fingerprint immediately so subsequent connects pin.
        # SECURITY: if we ALREADY captured a fingerprint on this client
        # (e.g. during discovery), enforce it on every subsequent connect
        # for the same instance — even if the caller forgot to plumb
        # tls_fingerprint through. Prevents silent first-contact MITM
        # after enrollment.
        if self._captured_fpr:
            try:
                digest = bytes.fromhex(self._captured_fpr)
                return aiohttp.TCPConnector(ssl=aiohttp.Fingerprint(digest))
            except ValueError:
                pass
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return aiohttp.TCPConnector(ssl=ctx)

    def _auth(self) -> aiohttp.BasicAuth | None:
        return aiohttp.BasicAuth(self._username, self._password) if self._username else None

    async def _session_for(self) -> aiohttp.ClientSession:
        """Return the aiohttp session, lazily constructed.

        IMPORTANT: we deliberately do NOT pin BasicAuth at the session level
        any more. Doing so makes every request unconditionally send the
        ``Authorization: Basic ...`` header, which (a) counts as a login
        attempt against iDRAC9 and triggers the 3-strike lockout and
        (b) would shadow the ``X-Auth-Token`` once we've minted one. Auth
        is decided per-request by :meth:`_request_kwargs` instead.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=self._connector(),
                timeout=self._timeout,
            )
        return self._session

    def _request_kwargs(self, extra_headers: dict[str, str] | None = None) -> dict:
        """Build the ``aiohttp`` request kwargs for one request.

        When we already hold a session token we send it via ``X-Auth-Token``
        and omit Basic auth entirely — that's the whole point of moving to
        session auth. Otherwise we fall back to per-request ``BasicAuth``
        so the very first request can authenticate (e.g. to ``login()``
        itself, or to discovery against a simulator that lacks
        SessionService).
        """
        headers = dict(_DEFAULT_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        kwargs: dict = {"headers": headers}
        if self._token:
            headers["X-Auth-Token"] = self._token
        else:
            auth = self._auth()
            if auth is not None:
                kwargs["auth"] = auth
        return kwargs

    async def login(self) -> bool:
        """Mint a Redfish session, capture ``X-Auth-Token`` + ``Location``.

        Returns True on success, False if the BMC does not expose
        SessionService (in which case the client transparently keeps using
        Basic auth for the rest of its lifetime). Idempotent: a second call
        with a token already held is a no-op.

        Concurrency: a single ``_login_lock`` serialises racing callers so
        a discovery walk doing five GETs in parallel doesn't try to mint
        five sessions on the BMC.
        """
        if self._token:
            return True
        async with self._login_lock:
            # Re-check under the lock — another coroutine may have logged
            # in while we were waiting.
            if self._token:
                return True
            if self._session_login_unavailable:
                return False
            if not self._username:
                # No credentials configured — nothing to log in with.
                return False
            sess = await self._session_for()
            url = self.base_url + _SESSION_PATH
            body = {"UserName": self._username, "Password": self._password}
            # Send BasicAuth on the login itself — that's how Redfish lets
            # you bootstrap the session. ``_request_kwargs`` does this for
            # us because ``_token`` is still None at this point.
            kwargs = self._request_kwargs()
            try:
                async with sess.post(url, json=body, **kwargs) as resp:
                    if resp.status == 404:
                        # No SessionService — latch the flag so every
                        # subsequent request goes straight to Basic auth.
                        self._session_login_unavailable = True
                        log.debug(
                            "SessionService not exposed by %s — using Basic auth",
                            self._ip,
                        )
                        return False
                    if resp.status >= 400:
                        # Some BMCs echo the request body in their 4xx
                        # response (we've seen iDRAC and AMI MegaRAC do
                        # this) — so the password we just sent would
                        # land in the exception text and the log file.
                        # Don't include the body for session-login errors.
                        raise RedfishError(
                            f"Session login {url}: HTTP {resp.status} "
                            "(body suppressed — may contain credentials)"
                        )
                    token = resp.headers.get("X-Auth-Token")
                    location = resp.headers.get("Location", "")
                    if not token:
                        # No token — treat as unavailable so we keep
                        # working over Basic. Real iDRAC/iLO/AMI always
                        # send X-Auth-Token on 201.
                        self._session_login_unavailable = True
                        log.warning(
                            "Session login to %s returned no X-Auth-Token — "
                            "falling back to Basic", self._ip,
                        )
                        return False
                    self._token = token
                    # Strip scheme+host from absolute Location values so
                    # we can DELETE via relative path on close. Spec
                    # allows either form.
                    if location.startswith(self.base_url):
                        location = location[len(self.base_url):]
                    self._session_uri = location or None
                    log.debug(
                        "Redfish session minted on %s (uri=%s)",
                        self._ip, self._session_uri,
                    )
                    return True
            except aiohttp.ClientError as exc:
                # Network-level failure — surface so the caller can decide
                # whether it's fatal. Do NOT latch the unavailable flag:
                # a transient TLS hiccup shouldn't permanently force the
                # client into Basic mode.
                raise RedfishError(f"Session login {url} failed: {exc}") from exc

    async def logout(self) -> None:
        """DELETE the BMC-side session resource and clear local state.

        BMCs have a finite session table; iDRAC9 caps at ~16. Leaving
        sessions hanging across many discovery passes will eventually
        exhaust that table and start refusing logins. We always DELETE on
        close. Safe to call when no session exists — short-circuits.
        """
        uri = self._session_uri
        token = self._token
        # Clear local state up-front so a failure mid-DELETE can't leave
        # us thinking we still hold a valid token.
        self._token = None
        self._session_uri = None
        if not uri or not token:
            return
        if self._session is None or self._session.closed:
            return
        try:
            url = self.base_url + uri if uri.startswith("/") else uri
            # The DELETE must carry the X-Auth-Token of the session it is
            # deleting, otherwise the BMC returns 401. We bypass
            # _request_kwargs because we've already cleared self._token.
            headers = dict(_DEFAULT_HEADERS)
            headers["X-Auth-Token"] = token
            async with self._session.delete(url, headers=headers) as resp:
                # 200, 204, 404 are all fine — the slot is gone either way.
                if resp.status >= 400 and resp.status != 404:
                    log.debug(
                        "Session DELETE %s returned HTTP %s — slot may persist",
                        url, resp.status,
                    )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("Session DELETE for %s suppressed: %s", self._ip, exc)

    async def close(self) -> None:
        """Release the underlying TCP/TLS session. Idempotent.

        Sends a best-effort logout to the BMC first so we don't leak a
        session-table slot on the remote side.
        """
        try:
            await self.logout()
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("Logout during close suppressed: %s", exc)
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "RedfishClient":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.close()

    async def _ensure_logged_in(self) -> None:
        """Lazy login. Best-effort — falls back to Basic on 404/missing.

        Concurrency: the lock-free fast-path read of ``_token`` and
        ``_session_login_unavailable`` is intentional — both flags are
        monotonic-after-first-set (token only clears via ``logout()`` /
        ``_force_relogin()`` which take the lock; ``unavailable`` is a
        one-way latch). The lock-free read avoids contention on the hot
        request path. ``login()`` itself takes ``_login_lock`` and
        re-checks under the lock, so a concurrent first-request never
        mints two sessions on the BMC.
        """
        # Snapshot once — avoid TOCTOU on the second flag if logout()
        # is racing with us. Either way the worst outcome is a single
        # redundant login attempt; the lock inside login() prevents
        # two real session-table inserts on the BMC.
        token = self._token
        unavail = self._session_login_unavailable
        if token or unavail:
            return
        if not self._username:
            return
        try:
            await self.login()
        except RedfishError as exc:
            # Log and keep going on Basic. A truly broken BMC will fail
            # the actual request a moment later with a clearer error.
            log.debug("Lazy login on %s failed (%s) — using Basic", self._ip, exc)

    async def get_json(self, path: str) -> dict:
        body, _ = await self.get_json_and_headers(path)
        return body

    async def get_json_and_headers(self, path: str) -> tuple[dict, dict]:
        """Same as ``get_json`` but returns ``(body, headers)``.

        Used by poll loops that need to honor ``Retry-After`` from the
        Task response — the BMC tells us "wait N seconds before asking
        again" and we should obey instead of hammering a fixed interval.
        """
        url = self._url(path)
        await self._ensure_logged_in()
        sess = await self._session_for()
        async with sess.get(url, **self._request_kwargs()) as resp:
            if resp.status == 401 and self._can_retry_after_relogin():
                await self._force_relogin()
                async with sess.get(url, **self._request_kwargs()) as retry:
                    # Accept any 2xx — Redfish lawfully returns 204 No
                    # Content for empty collections, 206 for partial
                    # content, etc. Old code rejected those as errors.
                    if not (200 <= retry.status < 300):
                        raise RedfishHTTPError(f"GET {url}: HTTP {retry.status}",
                                               status=retry.status)
                    return (await retry.json(content_type=None),
                            dict(retry.headers))
            if not (200 <= resp.status < 300):
                raise RedfishHTTPError(f"GET {url}: HTTP {resp.status}",
                                       status=resp.status)
            return await resp.json(content_type=None), dict(resp.headers)

    async def post_json(self, path: str, body: dict) -> dict:
        """POST a JSON body. Returns ``{'status', 'location', 'body'}``.

        Redfish async actions return 202 + ``Location`` (TaskMonitor URI);
        sync ones return 200/204 with the body.
        """
        url = self._url(path)
        await self._ensure_logged_in()
        sess = await self._session_for()
        async with sess.post(url, json=body, **self._request_kwargs()) as resp:
            if resp.status == 401 and self._can_retry_after_relogin():
                await self._force_relogin()
                async with sess.post(url, json=body, **self._request_kwargs()) as retry:
                    return await self._unpack_post(url, retry)
            return await self._unpack_post(url, resp)

    @staticmethod
    async def _unpack_post(url: str, resp: aiohttp.ClientResponse) -> dict:
        """Common 4xx-aware unpacking for the POST path. Kept private."""
        if resp.status >= 400:
            text = ""
            try:
                text = await resp.text()
            except Exception:
                pass
            raise RedfishHTTPError(
                f"POST {url}: HTTP {resp.status} {text[:200]}", status=resp.status)
        location = resp.headers.get("Location", "")
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = {}
        return {"status": resp.status, "location": location, "body": data}

    async def patch_json(self, path: str, body: dict) -> dict:
        """PATCH a JSON body (e.g. a Boot override). Returns the same
        ``{'status', 'location', 'body'}`` shape as :meth:`post_json`.

        Goes through ``_request_kwargs`` (X-Auth-Token / Basic auth + the
        required OData headers) and the one-shot 401 re-login retry, exactly
        like the GET/POST paths. A raw ``session.patch`` omits auth and 401s
        on every real (non-simulator) BMC.
        """
        url = self._url(path)
        await self._ensure_logged_in()
        sess = await self._session_for()
        async with sess.patch(url, json=body, **self._request_kwargs()) as resp:
            if resp.status == 401 and self._can_retry_after_relogin():
                await self._force_relogin()
                async with sess.patch(url, json=body, **self._request_kwargs()) as retry:
                    return await self._unpack_post(url, retry)
            return await self._unpack_post(url, resp)

    def _can_retry_after_relogin(self) -> bool:
        """True iff a session re-login could plausibly fix the 401.

        We only retry when (a) we have credentials and (b) the BMC has
        actually offered us a session at some point (or we haven't tried
        yet). A 401 on Basic-only paths is a real auth failure — retrying
        would just deepen the iDRAC lockout we're trying to avoid.
        """
        if not self._username:
            return False
        if self._session_login_unavailable and not self._token:
            return False
        return True

    async def _force_relogin(self) -> None:
        """Drop the current token and mint a fresh one. Best-effort.

        Must take ``_login_lock`` for the clear+reset so a concurrent
        caller doesn't observe a torn state (token cleared, unavailable
        flag flipped, but no new session yet) and stampede the BMC's
        SessionService.
        """
        async with self._login_lock:
            self._token = None
            self._session_uri = None
            # If a previous attempt latched ``unavailable`` but we got
            # here because we DO hold a token (now cleared), clear that
            # flag too so the retry actually attempts SessionService.
            self._session_login_unavailable = False
        try:
            await self.login()
        except RedfishError as exc:
            log.debug("Re-login on 401 for %s failed: %s", self._ip, exc)

    async def post_multipart(
        self,
        path: str,
        params: dict,
        file_path: str,
        filename: str | None = None,
        oem_params: dict | None = None,
        timeout: float = 1800.0,
    ) -> dict:
        """Push a firmware/image blob via DMTF multipart HTTP push.

        Builds a ``multipart/form-data`` body with two or three parts:

        * ``UpdateParameters`` — a JSON document (Targets, ApplyTime, …)
        * ``UpdateFile``       — the raw firmware bytes
        * ``OemParameters``    — optional OEM JSON (e.g. Supermicro/AMI wants
                                 ``{"ImageType": "BIOS"|"BMC"}``)

        This is how ``MultipartHttpPushUri`` works: ClusterOne pushes the
        bytes straight to the BMC, so the BMC never has to reach back to us
        over the network (no HTTP server, no firewall holes). Returns the
        same ``{'status', 'location', 'body'}`` shape as :meth:`post_json`.

        ``timeout`` is generous (30 min default) because a multi-hundred-MB
        image upload + the BMC's initial validation can take minutes.
        """
        url = self._url(path)
        p = Path(file_path)
        if not p.is_file():
            raise RedfishError(f"firmware blob missing: {file_path}")

        # CRITICAL: do NOT read the whole blob into memory. A 300 MB BIOS
        # × N concurrent flashes OOMs. aiohttp streams from a file handle
        # in 64KB chunks, so the resident-set stays flat regardless of
        # image size. We open a fresh handle on every send (including 401
        # retries) so the position is always reset to 0.
        fname = filename or p.name

        def _build_form() -> aiohttp.FormData:
            form = aiohttp.FormData()
            # Order matters for some BMCs: parameters first, then OEM, then file.
            form.add_field(
                "UpdateParameters",
                json.dumps(params),
                content_type="application/json",
            )
            if oem_params is not None:
                form.add_field(
                    "OemParameters",
                    json.dumps(oem_params),
                    content_type="application/json",
                )
            # Stream from disk — aiohttp will chunk-encode and read on demand.
            # The file handle is owned by the FormData and closed when the
            # request completes (success or failure).
            form.add_field(
                "UpdateFile",
                open(p, "rb"),
                filename=fname,
                content_type="application/octet-stream",
            )
            return form

        await self._ensure_logged_in()
        sess = await self._session_for()
        kwargs = self._request_kwargs()
        kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        async with sess.post(url, data=_build_form(), **kwargs) as resp:
            if resp.status == 401 and self._can_retry_after_relogin():
                await self._force_relogin()
                retry_kwargs = self._request_kwargs()
                retry_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
                async with sess.post(url, data=_build_form(), **retry_kwargs) as retry:
                    return await self._unpack_multipart(url, retry)
            return await self._unpack_multipart(url, resp)

    @staticmethod
    async def _unpack_multipart(url: str, resp: aiohttp.ClientResponse) -> dict:
        if resp.status >= 400:
            text = ""
            try:
                text = await resp.text()
            except Exception:
                pass
            raise RedfishHTTPError(
                f"Multipart push {url}: HTTP {resp.status} {text[:300]}",
                status=resp.status,
            )
        location = resp.headers.get("Location", "")
        try:
            body = await resp.json(content_type=None)
        except Exception:
            body = {}
        return {"status": resp.status, "location": location, "body": body}

    async def tcp_alive(self, timeout: float = 2.0) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._ip, self._port), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False
