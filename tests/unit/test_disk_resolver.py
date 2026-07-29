"""Disk Resolver — strategy → drive selection across heterogeneous fleets.

Coverage is critical here: a wrong-disk match in a 50-server bulk install
wipes 50 servers. Every strategy needs a happy path, a no-match path, and
edge cases (size tolerance, model globs, ambiguous matches).
"""
from __future__ import annotations

import pytest

from core.models.drive import Drive
from core.services.disk_resolver import resolve


def _nvme(name="nvme0n1", gb=1920, model="Samsung MZ1L21T9", **kw):
    return Drive(
        name=name, protocol="NVME", media_type="SSD",
        capacity_bytes=int(gb * 1_000_000_000), model=model, **kw,
    )


def _sata_ssd(name="sda", gb=480, model="INTEL SSDSC2KB480G7", **kw):
    return Drive(
        name=name, protocol="SATA", media_type="SSD",
        capacity_bytes=int(gb * 1_000_000_000), model=model, **kw,
    )


def _sata_hdd(name="sdb", gb=2000, model="WDC WD2000FYYZ", **kw):
    return Drive(
        name=name, protocol="SATA", media_type="HDD",
        capacity_bytes=int(gb * 1_000_000_000), model=model, **kw,
    )


# ---- empty / no-strategy paths ---------------------------------------------


def test_empty_inventory_fails_cleanly():
    r = resolve("first_nvme", [])
    assert r.status == "unmatched"
    assert "no drives" in r.reason.lower()


def test_blank_strategy_fails():
    r = resolve("", [_nvme()])
    assert r.status == "unmatched"
    assert r.drive is None


def test_unknown_strategy_fails():
    r = resolve("by_quantum_entanglement", [_nvme()])
    assert r.status == "unmatched"
    assert "unknown strategy" in r.reason.lower()


# ---- first_nvme -------------------------------------------------------------


def test_first_nvme_picks_only_nvme():
    inv = [_sata_ssd(), _nvme(), _sata_hdd()]
    r = resolve("first_nvme", inv)
    assert r.ok is True
    assert r.drive.is_nvme is True


def test_first_nvme_no_nvme_present():
    inv = [_sata_ssd(), _sata_hdd()]
    r = resolve("first_nvme", inv)
    assert r.status == "unmatched"
    assert "no nvme" in r.reason.lower()


def test_first_nvme_picks_first_when_multiple():
    a = _nvme(name="nvme0n1", gb=480)
    b = _nvme(name="nvme1n1", gb=3840)
    r = resolve("first_nvme", [a, b])
    assert r.drive.name == "nvme0n1"


# ---- first_m2 ---------------------------------------------------------------


def test_first_m2_matches_model():
    drives = [
        _nvme(name="nvme0n1", model="Samsung M.2 NVMe 980 PRO", gb=240),
        _nvme(name="nvme1n1", model="Samsung MZ1L21T9", gb=1920),
    ]
    r = resolve("first_m2", drives)
    assert r.ok
    assert "M.2" in r.drive.model


def test_first_m2_matches_slot_label():
    drives = [
        _nvme(name="nvme0n1", model="Generic", slot="M.2_1", gb=480),
        _nvme(name="nvme1n1", model="Generic", gb=3840),
    ]
    r = resolve("first_m2", drives)
    assert r.ok
    assert r.drive.slot == "M.2_1"


def test_first_m2_fallback_to_small_nvme_ssd():
    drives = [_nvme(name="nvme0n1", model="Generic NVMe", gb=240)]
    r = resolve("first_m2", drives)
    assert r.ok
    assert r.drive.name == "nvme0n1"


def test_first_m2_no_match():
    r = resolve("first_m2", [_nvme(gb=3840), _sata_hdd()])
    # 3840GB is > 1TB threshold so the fallback also rejects it
    assert r.status == "unmatched"


# ---- smallest_ssd -----------------------------------------------------------


def test_smallest_ssd_picks_smallest():
    inv = [_nvme(gb=1920), _sata_ssd(gb=240, name="sda"), _sata_ssd(gb=480, name="sdb")]
    r = resolve("smallest_ssd", inv)
    assert r.ok
    assert r.drive.capacity_gb == 240.0


def test_smallest_ssd_no_ssd():
    r = resolve("smallest_ssd", [_sata_hdd(), _sata_hdd(name="sdc")])
    assert r.status == "unmatched"


# ---- largest / smallest disk ------------------------------------------------


def test_largest_disk_picks_largest():
    r = resolve("largest_disk", [_nvme(gb=480), _sata_hdd(gb=4000)])
    assert r.ok
    assert r.drive.capacity_gb == 4000.0


def test_smallest_disk_picks_smallest():
    r = resolve("smallest_disk", [_nvme(gb=480), _sata_hdd(gb=4000)])
    assert r.ok
    assert r.drive.capacity_gb == 480.0


# ---- by_size with tolerance -------------------------------------------------


def test_by_size_default_tolerance_10pct():
    inv = [_sata_ssd(gb=480), _nvme(gb=1920), _sata_hdd(gb=2000)]
    r = resolve("by_size:480GB", inv)
    assert r.ok
    assert r.drive.capacity_gb == 480.0


def test_by_size_custom_tolerance():
    inv = [_sata_ssd(gb=460)]   # 4% below 480
    assert resolve("by_size:480GB±3%", inv).status == "unmatched"
    assert resolve("by_size:480GB±5%", inv).ok is True


def test_by_size_tb_unit():
    inv = [_nvme(gb=1920)]
    r = resolve("by_size:1.92TB", inv)
    assert r.ok


def test_by_size_ambiguous():
    inv = [_sata_ssd(gb=480, name="sda"), _sata_ssd(gb=460, name="sdb")]
    r = resolve("by_size:470GB", inv)
    assert r.status == "ambiguous"
    assert "2 drives" in r.reason


def test_by_size_invalid_spec():
    r = resolve("by_size:LOTS", [_nvme()])
    assert r.status == "unmatched"
    assert "Cannot parse" in r.reason or "cannot parse" in r.reason.lower()


# ---- by_model ---------------------------------------------------------------


def test_by_model_exact_glob():
    inv = [
        _sata_ssd(model="INTEL SSDSC2KB480G7", name="sda"),
        _nvme(model="Samsung MZ1L21T9", name="nvme0n1"),
    ]
    r = resolve("by_model:intel_ssdsc*", inv)
    assert r.ok
    assert "INTEL" in r.drive.model


def test_by_model_no_match():
    r = resolve("by_model:micron*", [_sata_ssd()])
    assert r.status == "unmatched"


# ---- by_slot / by_serial ----------------------------------------------------


def test_by_slot_endswith():
    inv = [
        _nvme(name="nvme0n1", slot="Bay 0", gb=1920),
        _nvme(name="nvme1n1", slot="Bay 1", gb=1920),
    ]
    r = resolve("by_slot:0", inv)
    assert r.ok
    assert r.drive.slot == "Bay 0"


def test_by_serial_exact():
    inv = [
        _nvme(serial="S5JNNG0R900001", name="nvme0n1"),
        _nvme(serial="S5JNNG0R900002", name="nvme1n1"),
    ]
    r = resolve("by_serial:s5jnng0r900002", inv)
    assert r.ok
    assert r.drive.serial == "S5JNNG0R900002"


# ---- manual_per_host --------------------------------------------------------


def test_manual_per_host_requires_override():
    r = resolve("manual_per_host", [_nvme()])
    assert r.status == "requires_override"
    assert r.drive is None


# ---- case insensitivity -----------------------------------------------------


def test_strategy_is_case_insensitive():
    r = resolve("FIRST_NVME", [_nvme()])
    assert r.ok
