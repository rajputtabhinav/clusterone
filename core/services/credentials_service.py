"""High-level credential management built on the vault + repo.

Scope precedence: ``host:<ip>`` → ``oem:<key>`` → ``default``. The first match
wins; if nothing is stored the caller falls back to user-supplied creds.
"""
from __future__ import annotations

import logging

from core.models.server import Credentials
from core.security.credential_vault import CredentialVault
from data.repositories.credentials_repo import CredentialsRepo

log = logging.getLogger(__name__)


class CredentialsService:
    def __init__(self, repo: CredentialsRepo, vault: CredentialVault) -> None:
        self._repo = repo
        self._vault = vault

    def list(self) -> list[dict]:
        # NB: never returns secrets — only metadata (label/username/scope).
        return self._repo.list()

    def add(self, *, label: str, username: str, password: str,
            scope: str = "default") -> int:
        ciphertext = self._vault.encrypt(password)
        return self._repo.add(
            label=label, username=username, secret_enc=ciphertext, scope=scope,
        )

    def delete(self, cred_id: int) -> None:
        self._repo.delete(cred_id)

    def count(self) -> int:
        return self._repo.count()

    def resolve(self, *, ip: str | None = None, oem: str | None = None) -> Credentials | None:
        """Most-specific credential for a target. Returns ``None`` if nothing stored.

        Precedence: ``host:<ip>`` → ``oem:<oem>`` → ``default``. If a
        more-specific scope EXISTS but fails decryption (e.g. vault key
        rotated, secret payload corrupt), we STOP rather than silently
        falling through to the next scope — falling through would hand
        the operator a less-specific credential they didn't intend to
        use, which on a flash means the wrong account gets BMC access.
        The exception is surfaced via a loud warning so the operator
        knows to re-enter that credential.
        """
        for scope in (f"host:{ip}" if ip else None, f"oem:{oem}" if oem else None, "default"):
            if scope is None:
                continue
            row = self._repo.find_for_scope(scope)
            if not row:
                continue
            try:
                password = self._vault.decrypt(row["secret_enc"])
            except Exception as exc:
                log.error(
                    "Decryption failed for credential id=%s scope=%s: %s — "
                    "refusing to fall through to a less-specific scope. "
                    "Re-enter this credential in Settings.",
                    row["id"], scope, exc,
                )
                # Return None so caller surfaces "no credential" instead of
                # silently using a different (potentially wrong) account.
                return None
            return Credentials(username=row["username"], password=password)
        return None

    @property
    def vault_is_secure(self) -> bool:
        return self._vault.is_secure
