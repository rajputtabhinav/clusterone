"""Credential vault.

On Windows with pywin32 available, secrets are encrypted with **DPAPI** —
ciphertext is bound to the Windows account, no master password to manage.

If pywin32 is missing, the vault REFUSES to encrypt by default. Dev / CI
hosts that need the plaintext (base64) fallback must explicitly opt in by
setting the ``CLUSTERONE_ALLOW_PLAINTEXT_VAULT=1`` environment variable.
This prevents a non-Windows build from accidentally writing real BMC
passwords to disk unencrypted.

Only ciphertext (``BLOB``) ever touches the ``credentials`` table.
"""
from __future__ import annotations

import base64
import logging
import os
import sys

log = logging.getLogger(__name__)

_HAS_DPAPI = False
if sys.platform == "win32":  # pragma: no cover - exercised on Windows
    try:
        import win32crypt  # type: ignore
        _HAS_DPAPI = True
    except ImportError:
        log.warning(
            "pywin32 not installed — credential vault is in DEV FALLBACK mode "
            "(secrets stored as base64, NOT encrypted). Install pywin32 for production."
        )

_ALLOW_PLAINTEXT = os.environ.get("CLUSTERONE_ALLOW_PLAINTEXT_VAULT") == "1"

_PLAIN_PREFIX = b"PLAIN:"
_DPAPI_DESCRIPTION = "ClusterOne credential"


class VaultUnavailable(RuntimeError):
    """Raised when encrypt is attempted without DPAPI and without explicit opt-in."""


class CredentialVault:
    def encrypt(self, plaintext: str) -> bytes:
        data = (plaintext or "").encode("utf-8")
        if _HAS_DPAPI:
            return win32crypt.CryptProtectData(
                data, _DPAPI_DESCRIPTION, None, None, None, 0,
            )
        if not _ALLOW_PLAINTEXT:
            raise VaultUnavailable(
                "Refusing to encrypt: pywin32 (DPAPI) is not installed and the "
                "plaintext fallback is not enabled. For dev/CI, set "
                "CLUSTERONE_ALLOW_PLAINTEXT_VAULT=1."
            )
        return _PLAIN_PREFIX + base64.b64encode(data)

    def decrypt(self, ciphertext: bytes) -> str:
        if not ciphertext:
            return ""
        if ciphertext.startswith(_PLAIN_PREFIX):
            return base64.b64decode(ciphertext[len(_PLAIN_PREFIX):]).decode("utf-8")
        if _HAS_DPAPI:
            _, data = win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)
            return data.decode("utf-8")
        raise VaultUnavailable(
            "Encrypted credential found but pywin32 is not installed on this host"
        )

    @property
    def is_secure(self) -> bool:
        """True iff DPAPI is in use (i.e. real encryption, not dev fallback)."""
        return _HAS_DPAPI
