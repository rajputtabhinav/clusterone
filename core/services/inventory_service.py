from __future__ import annotations

from data.repositories.servers_repo import ServersRepo


class InventoryService:
    def __init__(self, repo: ServersRepo) -> None:
        self._repo = repo

    def list(self) -> list[dict]:
        return self._repo.list()

    def status_counts(self) -> dict[str, int]:
        return self._repo.status_counts()

    def upsert(self, server: dict) -> int:
        return self._repo.upsert(server)

    def get_by_ip(self, ip: str) -> dict | None:
        return self._repo.get_by_ip(ip)

    def set_status(self, server_id: int, status: str) -> None:
        self._repo.set_status(server_id, status)

    def set_oem_override(self, server_id: int, oem: str | None) -> bool:
        """Operator-pinned OEM for plugin routing; ``None`` restores auto."""
        return self._repo.set_oem_override(server_id, oem)

    def set_tls_fingerprint(self, ip: str, fpr: str) -> dict | None:
        return self._repo.set_tls_fpr_by_ip(ip, fpr)

    def set_disks(self, ip: str, disks_json: str) -> bool:
        """Persist the JSON-serialized drive inventory for the host."""
        return self._repo.set_disks_by_ip(ip, disks_json)

    def delete(self, server_id: int) -> None:
        self._repo.delete(server_id)
