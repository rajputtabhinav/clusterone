"""Credential vault round-trip (DPAPI on Windows, dev-fallback elsewhere)."""
from __future__ import annotations

from core.security.credential_vault import CredentialVault


def test_round_trip():
    vault = CredentialVault()
    ciphertext = vault.encrypt("hunter2")
    assert isinstance(ciphertext, (bytes, bytearray))
    assert b"hunter2" not in ciphertext  # never plaintext
    assert vault.decrypt(ciphertext) == "hunter2"


def test_empty_string():
    vault = CredentialVault()
    assert vault.decrypt(vault.encrypt("")) == ""
    assert vault.decrypt(b"") == ""
