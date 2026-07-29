"""Disk resolution: turn an operator-authored *strategy* string into a
concrete ``Drive`` per server.

The strategy grammar is deliberately tiny and human-readable. The same
profile applies cleanly to a heterogeneous fleet: ``first_nvme`` picks
the right disk on a Samsung-flash Supermicro AND the right disk on an
all-Intel Dell, because each host's inventory is resolved separately.

Grammar (case-insensitive):

    first_nvme              First drive with Protocol=NVMe
    first_m2                First drive with /m\\.?2/ in model or slot
    smallest_ssd            Smallest drive with MediaType=SSD (boot card convention)
    largest_disk            Largest drive of any kind (single-disk hosts)
    smallest_disk           Smallest drive of any kind
    by_size:480GB           Any drive within ±10% of 480 GB
    by_size:480GB±5%        Custom tolerance
    by_model:INTEL_SSDSC*   Model glob (fnmatch-style, _ → space)
    by_slot:0               Match drive whose `slot` ends with the given token
    by_serial:SN12345       Exact serial match (single-host pin)
    manual_per_host         Force operator to pick in the preview screen

Resolution returns a 4-tuple ``(drive, reason, status)``:

* ``drive`` — the matched Drive (or ``None`` if nothing matched)
* ``reason`` — short user-facing string ("matched first NVMe", "no NVMe present")
* ``status`` — one of ``matched`` | ``unmatched`` | ``ambiguous`` |
  ``requires_override``

The provisioning wizard renders ``reason`` and ``status`` in the per-host
preview screen, allowing the operator to override outliers.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from core.models.drive import Drive


@dataclass
class Resolution:
    drive: Drive | None
    reason: str
    status: str        # 'matched' | 'unmatched' | 'ambiguous' | 'requires_override'

    @property
    def ok(self) -> bool:
        return self.status == "matched" and self.drive is not None


_GB = 1_000_000_000   # decimal GB (matches manufacturer specs)


def _parse_size_with_tolerance(spec: str) -> tuple[int, float]:
    """`480GB`, `1.92TB`, `480GB±5%`. Returns (bytes, tolerance_fraction)."""
    s = spec.strip().replace(" ", "")
    tol = 0.10
    if "±" in s:
        s, tol_part = s.split("±", 1)
        if tol_part.endswith("%"):
            tol_part = tol_part[:-1]
        tol = float(tol_part) / 100.0
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(GB|TB|MB)$", s, re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse size '{spec}'")
    val, unit = float(m.group(1)), m.group(2).upper()
    bytes_ = int(val * {"MB": 1_000_000, "GB": _GB, "TB": 1_000 * _GB}[unit])
    return bytes_, tol


def _within_tolerance(actual: int, target: int, tol: float) -> bool:
    if target == 0:
        return False
    return abs(actual - target) / target <= tol


def resolve(strategy: str, drives: list[Drive]) -> Resolution:
    """Apply ``strategy`` to a server's drive inventory."""
    s = (strategy or "").strip().lower()
    if not s:
        return Resolution(None, "no strategy supplied", "unmatched")
    if not drives:
        return Resolution(None, "host reports no drives", "unmatched")

    if s == "manual_per_host":
        return Resolution(None, "operator override required", "requires_override")

    if s == "first_nvme":
        nvmes = [d for d in drives if d.is_nvme]
        if not nvmes:
            return Resolution(None, "no NVMe present", "unmatched")
        return Resolution(nvmes[0], "matched first NVMe", "matched")

    if s == "first_m2":
        m2_pat = re.compile(r"m\.?2", re.IGNORECASE)
        cands = [d for d in drives
                 if m2_pat.search(d.model or "") or m2_pat.search(d.slot or "")]
        if not cands:
            # Fallback: any NVMe SSD < 1TB is *probably* an M.2 boot drive.
            cands = [d for d in drives if d.is_nvme and d.is_ssd
                     and d.capacity_bytes < 1_000 * _GB]
        if not cands:
            return Resolution(None, "no M.2 device detected", "unmatched")
        return Resolution(cands[0], "matched first M.2", "matched")

    if s == "smallest_ssd":
        ssds = [d for d in drives if d.is_ssd and d.capacity_bytes > 0]
        if not ssds:
            return Resolution(None, "no SSD present", "unmatched")
        pick = min(ssds, key=lambda d: d.capacity_bytes)
        return Resolution(pick, f"matched smallest SSD ({pick.capacity_gb:.0f} GB)", "matched")

    if s == "largest_disk":
        sized = [d for d in drives if d.capacity_bytes > 0]
        if not sized:
            return Resolution(None, "no drives with reported capacity", "unmatched")
        pick = max(sized, key=lambda d: d.capacity_bytes)
        return Resolution(pick, f"matched largest disk ({pick.capacity_gb:.0f} GB)", "matched")

    if s == "smallest_disk":
        sized = [d for d in drives if d.capacity_bytes > 0]
        if not sized:
            return Resolution(None, "no drives with reported capacity", "unmatched")
        pick = min(sized, key=lambda d: d.capacity_bytes)
        return Resolution(pick, f"matched smallest disk ({pick.capacity_gb:.0f} GB)", "matched")

    if s.startswith("by_size:"):
        try:
            target, tol = _parse_size_with_tolerance(s.split(":", 1)[1])
        except ValueError as exc:
            return Resolution(None, str(exc), "unmatched")
        cands = [d for d in drives if _within_tolerance(d.capacity_bytes, target, tol)]
        if not cands:
            return Resolution(None,
                              f"no drive within ±{int(tol*100)}% of {target / _GB:.0f}GB",
                              "unmatched")
        if len(cands) > 1:
            # Ambiguous → tell the operator; they can override per-host.
            return Resolution(cands[0],
                              f"{len(cands)} drives matched size — overriding to first; review",
                              "ambiguous")
        return Resolution(cands[0], f"matched by size ({cands[0].capacity_gb:.0f} GB)", "matched")

    if s.startswith("by_model:"):
        # Underscores in the rule stand in for spaces (avoids quoting in UI).
        pat = s.split(":", 1)[1].replace("_", " ")
        cands = [d for d in drives if fnmatch.fnmatch((d.model or "").lower(), pat)]
        if not cands:
            return Resolution(None, f"no drive matches model glob '{pat}'", "unmatched")
        if len(cands) > 1:
            return Resolution(cands[0],
                              f"{len(cands)} drives matched model — overriding to first; review",
                              "ambiguous")
        return Resolution(cands[0], f"matched model '{cands[0].model}'", "matched")

    if s.startswith("by_slot:"):
        tok = s.split(":", 1)[1].strip()
        cands = [d for d in drives
                 if (d.slot or "").lower().endswith(tok.lower())]
        if not cands:
            return Resolution(None, f"no drive in slot ending '{tok}'", "unmatched")
        return Resolution(cands[0], f"matched slot '{cands[0].slot}'", "matched")

    if s.startswith("by_serial:"):
        sn = s.split(":", 1)[1].strip()
        cands = [d for d in drives if (d.serial or "").lower() == sn.lower()]
        if not cands:
            return Resolution(None, f"no drive with serial '{sn}'", "unmatched")
        return Resolution(cands[0], f"matched serial '{cands[0].serial}'", "matched")

    return Resolution(None, f"unknown strategy '{strategy}'", "unmatched")
