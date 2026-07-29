from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_FIELDS = [
    "hostname", "manufacturer", "oem", "model", "serial",
    "bmc_version", "bios_version", "power_state", "status",
    "protocol", "redfish_root", "tls_fpr",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ServersRepo:
    def __init__(self, conn: sqlite3.Connection, write_lock=None) -> None:
        self.conn = conn
        # Shared cross-thread write lock (Database.write_lock). Tests pass None.
        self._lock = write_lock if write_lock is not None else contextlib.nullcontext()

    def list(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get(self, server_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        return dict(row) if row else None

    def get_by_ip(self, ip: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM servers WHERE ip=?", (ip,)).fetchone()
        return dict(row) if row else None

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM servers GROUP BY status"
        ).fetchall()
        d = {r["status"]: r["c"] for r in rows}
        return {
            "total": sum(d.values()),
            "online": d.get("online", 0),
            "needs_update": d.get("needs_update", 0),
            "failed": d.get("failed", 0),
            "offline": d.get("offline", 0),
        }

    def upsert(self, s: dict) -> int:
        """Insert or update a server keyed by IP. Returns the row id.

        IPs are DYNAMIC: the same address can be reassigned to different
        physical hardware (DHCP, re-cabling). When the incoming serial proves
        the hardware changed, the per-server state that was bound to the OLD
        machine — the manual OEM selection and the cached disk inventory —
        is cleared in the same UPDATE, so a stale pick can never flash the
        wrong vendor recipe or preview the wrong disks on the new box.
        """
        now = _now()
        with self._lock:
            existing = self.conn.execute(
                "SELECT id, serial FROM servers WHERE ip=?", (s["ip"],)
            ).fetchone()
            params = {f: s.get(f) for f in _FIELDS}
            params["ip"] = s["ip"]
            params["now"] = now
            if existing:
                # Conservative rule: only treat it as a hardware swap when BOTH
                # serials are known and differ (a BMC that omits the serial
                # proves nothing either way).
                old_serial = (existing["serial"] or "").strip()
                new_serial = (s.get("serial") or "").strip()
                hw_changed = bool(old_serial and new_serial and old_serial != new_serial)
                extra = ""
                if hw_changed:
                    extra = ", oem_override=NULL, disks_json=NULL, disks_at=NULL"
                    log.warning(
                        "%s: different hardware at this IP (serial %s -> %s) — "
                        "cleared OEM selection + cached disk inventory",
                        s["ip"], old_serial, new_serial,
                    )
                sets = ", ".join(f"{f}=:{f}" for f in _FIELDS)
                self.conn.execute(
                    f"UPDATE servers SET {sets}, last_seen=:now, updated_at=:now{extra} WHERE ip=:ip",
                    params,
                )
                sid = existing["id"]
            else:
                cols = ["ip", *_FIELDS, "discovered_at", "last_seen", "updated_at"]
                params["discovered_at"] = now
                placeholders = ", ".join(f":{c}" for c in cols)
                cur = self.conn.execute(
                    f"INSERT INTO servers ({', '.join(cols)}) VALUES ({placeholders})",
                    {**params, "last_seen": now, "updated_at": now},
                )
                sid = int(cur.lastrowid)
            self.conn.commit()
            return sid

    def set_oem_override(self, server_id: int, oem: str | None) -> bool:
        """Pin (or clear, with ``None``) the operator's manual OEM choice.

        Kept out of ``_FIELDS`` on purpose — discovery upserts must never
        overwrite a manual override (see migration 0005).
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE servers SET oem_override=?, updated_at=? WHERE id=?",
                (oem, _now(), server_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_status(self, server_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE servers SET status=?, updated_at=? WHERE id=?",
                (status, _now(), server_id),
            )
            self.conn.commit()

    def set_disks_by_ip(self, ip: str, disks_json: str) -> bool:
        """Cache the JSON-serialized disk inventory on the server row.

        Called after each Redfish Storage discovery. The provisioning
        wizard reads ``disks_json`` (without re-polling) to render its
        per-host preview screen.
        """
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM servers WHERE ip=?", (ip,)
            ).fetchone()
            if not existing:
                return False
            self.conn.execute(
                "UPDATE servers SET disks_json=?, disks_at=?, updated_at=? WHERE id=?",
                (disks_json, _now(), _now(), existing["id"]),
            )
            self.conn.commit()
            return True

    def set_tls_fpr_by_ip(self, ip: str, fpr: str) -> dict | None:
        """Record the BMC's TLS fingerprint (TOFU). Returns change-info dict or None."""
        with self._lock:
            existing = self.conn.execute(
                "SELECT id, tls_fpr FROM servers WHERE ip=?", (ip,)
            ).fetchone()
            if not existing:
                return None
            old = existing["tls_fpr"]
            self.conn.execute(
                "UPDATE servers SET tls_fpr=?, updated_at=? WHERE id=?",
                (fpr, _now(), existing["id"]),
            )
            self.conn.commit()
            return {"id": existing["id"], "old": old, "new": fpr,
                    "changed": bool(old and old != fpr)}

    def delete(self, server_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
            self.conn.commit()

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM servers").fetchone()["c"])
