"""BMC TLS trust — Trust-On-First-Use (TOFU) fingerprint pinning.

BMCs ship self-signed certs. Rather than disabling TLS verification globally
(the lazy + insecure default), we capture the peer cert's SHA-256 fingerprint
on the first successful contact and pin it. Subsequent connections verify the
fingerprint and warn on change.
"""
from __future__ import annotations

import hashlib
import logging
import socket
import ssl

log = logging.getLogger(__name__)


def fetch_fingerprint(host: str, port: int = 443, timeout: float = 5.0) -> str | None:
    """Open a TLS socket to ``host:port`` and return the peer cert's SHA-256.

    Returns the fingerprint as lowercase hex with no separators, or ``None`` if
    the host is unreachable / not TLS.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
                if not der:
                    return None
                return hashlib.sha256(der).hexdigest()
    except (OSError, ssl.SSLError) as exc:
        log.debug("TLS fingerprint probe failed for %s:%s — %s", host, port, exc)
        return None


def verify_pin(expected: str | None, actual: str | None) -> tuple[bool, str]:
    """Compare a captured fingerprint to the pinned one.

    Returns ``(ok, message)``. ``ok`` is True when the fingerprints match OR
    when there's nothing pinned yet (first-use capture).
    """
    if not expected:
        return True, "first-use; capturing fingerprint"
    if not actual:
        return False, "could not retrieve peer fingerprint"
    if expected.lower() == actual.lower():
        return True, "fingerprint matches"
    return False, f"FINGERPRINT MISMATCH — expected {expected[:16]}…, got {actual[:16]}…"
