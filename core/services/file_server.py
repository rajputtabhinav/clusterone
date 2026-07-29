"""Embedded HTTP server for BMC-side ISO + autoinstall config fetches.

Runs on the existing AsyncEngine loop alongside the discovery and update
work. Two routes:

* ``/iso/<sha>``              — streams the ISO file straight off disk
                                 with HTTP range support (BMCs request
                                 byte ranges as they read the virtual CD)
* ``/autoinstall/<token>``    — returns the per-host autoinstall config
                                 rendered by ``autoinstall_renderer``

Both endpoints require a signed token in the URL (HMAC-SHA256 of the
resource id + expiry, signed with a per-run secret). 15-minute default
expiry; sufficient for a BMC to fetch the ISO once and re-fetch on retry.

Security posture:
* Bind to an operator-chosen interface (defaults to ``0.0.0.0`` but the
  ``Settings`` page exposes ``bmc_http_bind`` to restrict to the mgmt CIDR).
* Signed URLs prevent any non-orchestrator party from learning where ISOs
  live or forging fetches.
* All fetches are audited.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import socket
import time
from pathlib import Path

from aiohttp import web

log = logging.getLogger(__name__)


def _new_secret() -> str:
    return secrets.token_hex(32)


def detect_advertise_host(probe_target: str = "8.8.8.8") -> str | None:
    """Find the local IPv4 address the OS would pick to reach the network.

    BMCs need a reachable URL to fetch ISOs. ``0.0.0.0`` is a bind address,
    NOT a reachable address — sending that to a BMC guarantees the fetch
    fails. We detect the actual outbound interface by opening a UDP socket
    to a public IP (no packets sent — connect() on UDP just resolves the
    route) then reading ``getsockname()``.

    Returns the IPv4 string on success, ``None`` if every strategy fails.
    Caller logs the result so the operator can override via the
    ``bmc_http_advertise_host`` setting if the detected NIC is wrong.

    Strategy A — UDP-connect to ``probe_target``; the OS picks the
    outbound interface and ``getsockname()`` reveals it. Works even when
    the public IP is unreachable (no packets actually sent on UDP connect).

    Strategy B — enumerate ``socket.gethostbyname_ex(hostname)`` and pick
    the first non-loopback RFC1918 address. Useful in air-gapped labs
    where Strategy A can't establish a default route.
    """
    # Strategy A: outbound-interface probe (UDP, no traffic)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect((probe_target, 1))
            addr = s.getsockname()[0]
            if addr and not addr.startswith("0."):
                return addr
    except OSError:
        pass

    # Strategy B: enumerate local addresses, pick first private IPv4
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        for addr in addrs:
            if addr.startswith("127."):
                continue
            try:
                ip = ipaddress.IPv4Address(addr)
            except ValueError:
                continue
            if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                return str(ip)
    except (socket.gaierror, OSError):
        pass

    return None


class SignedUrlSigner:
    """HMAC-SHA256 signer + verifier for resource fetch tokens.

    URL shape: ``/<resource>/<id>?exp=<unix_ts>&sig=<hex>``.
    """

    def __init__(self, secret: str | None = None) -> None:
        self._secret = (secret or _new_secret()).encode("ascii")

    def sign(self, resource: str, identifier: str, ttl_seconds: int = 900) -> dict:
        """Return ``{exp, sig}`` for the given resource/id pair."""
        exp = int(time.time()) + int(ttl_seconds)
        sig = self._compute(resource, identifier, exp)
        return {"exp": exp, "sig": sig}

    def verify(self, resource: str, identifier: str, exp: str, sig: str) -> bool:
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            return False
        if exp_i < int(time.time()):
            return False
        expected = self._compute(resource, identifier, exp_i)
        return hmac.compare_digest(expected, sig or "")

    def _compute(self, resource: str, identifier: str, exp: int) -> str:
        msg = f"{resource}:{identifier}:{exp}".encode("ascii")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()


class FileServer:
    """aiohttp app that serves ISOs (by SHA) + autoinstall configs (by token).

    The autoinstall payload provider is passed at construction so this
    module stays decoupled from the orchestrator. Real usage:

        srv = FileServer(
            iso_dir=config.iso_dir(),
            autoinstall_provider=lambda token: orchestrator.config_for(token),
        )
        await srv.start(host="0.0.0.0", port=8443)
        url = srv.signed_iso_url(sha256, host_for_url="10.10.10.5")
        # ... pass url to plugin.mount_iso ...
        await srv.stop()
    """

    def __init__(
        self,
        iso_dir: Path,
        autoinstall_provider=None,
        signer: SignedUrlSigner | None = None,
        public_base_url: str | None = None,
    ) -> None:
        self._iso_dir = Path(iso_dir)
        self._provider = autoinstall_provider
        self._signer = signer or SignedUrlSigner()
        self._public_base = public_base_url  # e.g. "http://10.10.10.5:8443"
        self._app = web.Application()
        self._app.router.add_get("/iso/{sha}", self._handle_iso)
        self._app.router.add_get("/autoinstall/{token}", self._handle_autoinstall)
        self._app.router.add_get("/healthz", self._handle_health)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # ---- lifecycle ---------------------------------------------------------

    async def start(self, host: str = "0.0.0.0", port: int = 8443,
                    public_base_url: str | None = None) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, host=host, port=port)
            await self._site.start()
        except Exception:
            self._site = None
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            raise
        # Precedence: explicit advertise URL > constructor arg > bind address.
        # If we'd otherwise advertise 0.0.0.0 (which BMCs can't reach),
        # auto-detect the management IP via the outbound-interface probe.
        # Operators can override via the ``bmc_http_advertise_host`` setting.
        resolved = (
            public_base_url
            or self._public_base
            or self._resolve_advertise_url(host, port)
        )
        self._public_base = resolved
        if "0.0.0.0" in (self._public_base or ""):
            log.warning(
                "File server is advertising 0.0.0.0:%s to BMCs — they cannot "
                "fetch from that address. Set bmc_http_advertise_host in "
                "Settings to your management-NIC IP.", port,
            )
        else:
            log.info("File server listening on %s:%s (advertising %s)",
                     host, port, self._public_base)

    @staticmethod
    def _resolve_advertise_url(host: str, port: int) -> str:
        """Pick a URL BMCs can actually reach. Falls back to the bind
        address only when every detection strategy fails — at which point
        ``start()`` emits a warning so the operator sees they need to
        configure ``bmc_http_advertise_host`` explicitly."""
        if host and host not in ("0.0.0.0", "::"):
            return f"http://{host}:{port}"
        detected = detect_advertise_host()
        if detected:
            log.info("Auto-detected management host for BMC fetches: %s", detected)
            return f"http://{detected}:{port}"
        return f"http://{host}:{port}"

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- URL signing -------------------------------------------------------

    # TTLs (seconds). ISO needs to survive slow BMC pulls of multi-GB
    # images (Rocky 9 = 11 GB / ~100 Mbps mgmt link = ~15 min just for
    # transfer; the BMC may also re-request after a stall). Autoinstall
    # is plaintext root password material — keep that window minimal.
    _ISO_TTL_S = 2 * 60 * 60            # 2 hours
    _AUTOINSTALL_TTL_S = 5 * 60         # 5 minutes (was 15)

    def signed_iso_url(self, sha256: str) -> str:
        if self._public_base is None:
            raise RuntimeError(
                "File server has not been started — call start() before "
                "generating signed URLs."
            )
        sig = self._signer.sign("iso", sha256, ttl_seconds=self._ISO_TTL_S)
        return f"{self._public_base}/iso/{sha256}?exp={sig['exp']}&sig={sig['sig']}"

    def signed_autoinstall_url(self, token: str) -> str:
        if self._public_base is None:
            raise RuntimeError(
                "File server has not been started — call start() before "
                "generating signed URLs."
            )
        sig = self._signer.sign("autoinstall", token, ttl_seconds=self._AUTOINSTALL_TTL_S)
        return (f"{self._public_base}/autoinstall/{token}"
                f"?exp={sig['exp']}&sig={sig['sig']}")

    # ---- handlers ----------------------------------------------------------

    async def _handle_iso(self, request: web.Request) -> web.StreamResponse:
        sha = request.match_info["sha"]
        if not self._signer.verify("iso", sha,
                                   request.query.get("exp", ""),
                                   request.query.get("sig", "")):
            return web.Response(status=403, text="bad signature")
        path = self._iso_dir / f"{sha}.iso"
        if not path.is_file():
            return web.Response(status=404, text="not found")
        log.info("ISO fetch sha=%s from %s", sha[:12], request.remote)
        # FileResponse honors Range headers automatically (sendfile under
        # the hood on Linux/Windows). Perfect for BMC streaming.
        return web.FileResponse(path)

    async def _handle_autoinstall(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        if not self._signer.verify("autoinstall", token,
                                   request.query.get("exp", ""),
                                   request.query.get("sig", "")):
            return web.Response(status=403, text="bad signature")
        if self._provider is None:
            return web.Response(status=503, text="no provider")
        body = self._provider(token)
        if body is None:
            return web.Response(status=404, text="unknown token")
        log.info("Autoinstall fetch token=%s from %s", token[:12], request.remote)
        return web.Response(body=body, content_type="text/plain", charset="utf-8")

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "pid": os.getpid()})
