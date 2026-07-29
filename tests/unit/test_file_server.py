"""Signed URL signer + verifier for the embedded HTTP file server."""
from __future__ import annotations

import time
from unittest.mock import patch

from core.services.file_server import (
    FileServer,
    SignedUrlSigner,
    detect_advertise_host,
)


def test_sign_then_verify_roundtrip():
    s = SignedUrlSigner(secret="00" * 32)
    bundle = s.sign("iso", "abcdef", ttl_seconds=60)
    assert s.verify("iso", "abcdef", str(bundle["exp"]), bundle["sig"]) is True


def test_verify_rejects_wrong_resource():
    s = SignedUrlSigner(secret="00" * 32)
    b = s.sign("iso", "abc", ttl_seconds=60)
    assert s.verify("autoinstall", "abc", str(b["exp"]), b["sig"]) is False


def test_verify_rejects_wrong_id():
    s = SignedUrlSigner(secret="00" * 32)
    b = s.sign("iso", "abc", ttl_seconds=60)
    assert s.verify("iso", "abd", str(b["exp"]), b["sig"]) is False


def test_verify_rejects_tampered_signature():
    s = SignedUrlSigner(secret="00" * 32)
    b = s.sign("iso", "abc", ttl_seconds=60)
    assert s.verify("iso", "abc", str(b["exp"]), "deadbeef" * 8) is False


def test_verify_rejects_expired_token():
    s = SignedUrlSigner(secret="00" * 32)
    expired = int(time.time()) - 10
    sig = s._compute("iso", "abc", expired)
    assert s.verify("iso", "abc", str(expired), sig) is False


def test_verify_rejects_malformed_exp():
    s = SignedUrlSigner(secret="00" * 32)
    b = s.sign("iso", "abc", ttl_seconds=60)
    assert s.verify("iso", "abc", "not-an-int", b["sig"]) is False
    assert s.verify("iso", "abc", "", b["sig"]) is False


def test_different_secrets_produce_incompatible_signatures():
    a = SignedUrlSigner(secret="AA" * 32)
    b = SignedUrlSigner(secret="BB" * 32)
    sig_a = a.sign("iso", "abc", ttl_seconds=60)
    assert b.verify("iso", "abc", str(sig_a["exp"]), sig_a["sig"]) is False


def test_default_secret_is_random_per_instance():
    """Each FileServer/SignedUrlSigner constructed without a secret gets
    a fresh random one — two instances should not accept each other's signatures."""
    a = SignedUrlSigner()
    b = SignedUrlSigner()
    sig_a = a.sign("iso", "abc", ttl_seconds=60)
    assert b.verify("iso", "abc", str(sig_a["exp"]), sig_a["sig"]) is False


# ---- BMC-reachable URL resolution (no more http://0.0.0.0/) -------------

def test_detect_advertise_host_returns_non_loopback():
    """Detector should pick a routable local IP — not 0.0.0.0 or 127.0.0.1.
    Skipped if the test box has no IP (offline laptop with WiFi disabled)."""
    detected = detect_advertise_host()
    if detected is None:
        return   # offline runner — acceptable
    assert not detected.startswith("0.")
    assert not detected.startswith("127.")


def test_resolve_advertise_url_uses_explicit_host_unchanged():
    """If the operator binds explicitly to a non-wildcard address, that wins."""
    url = FileServer._resolve_advertise_url("10.10.10.5", 8443)
    assert url == "http://10.10.10.5:8443"


def test_resolve_advertise_url_uses_loopback_when_127_bound():
    """Dev mode (bind to 127.0.0.1) should NOT trigger auto-detection."""
    url = FileServer._resolve_advertise_url("127.0.0.1", 8443)
    assert url == "http://127.0.0.1:8443"


def test_resolve_advertise_url_auto_detects_when_bound_to_wildcard():
    """When bound to 0.0.0.0, the resolver should auto-detect a usable IP.
    Patched here so the test doesn't depend on the runner's network."""
    with patch("core.services.file_server.detect_advertise_host",
               return_value="10.10.10.5"):
        url = FileServer._resolve_advertise_url("0.0.0.0", 8443)
    assert url == "http://10.10.10.5:8443"


def test_resolve_advertise_url_falls_back_to_wildcard_when_detection_fails():
    """If detection returns None, we surface 0.0.0.0 — start() then logs
    a loud WARNING so the operator knows they must set bmc_http_advertise_host."""
    with patch("core.services.file_server.detect_advertise_host",
               return_value=None):
        url = FileServer._resolve_advertise_url("0.0.0.0", 8443)
    assert url == "http://0.0.0.0:8443"
