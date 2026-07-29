"""Redfish Storage / Drives JSON parser.

Verifies that the disk-discovery code in `plugins.generic_redfish.plugin`
correctly normalizes drive payloads across the variations real BMCs emit.
"""
from __future__ import annotations

from core.models.drive import Drive
from plugins.generic_redfish.plugin import _drive_from_redfish_json


def test_nvme_drive_basic():
    """A typical Supermicro/ASRock NVMe entry."""
    payload = {
        "@odata.id": "/redfish/v1/Systems/1/Storage/0/Drives/0",
        "Id": "nvme0n1",
        "Name": "Disk 0",
        "Model": "Samsung MZ1L21T9",
        "SerialNumber": "S5JNNG0R900000",
        "Protocol": "NVMe",
        "MediaType": "SSD",
        "CapacityBytes": 1_920_000_000_000,
        "PhysicalLocation": {
            "PartLocation": {
                "LocationType": "Slot",
                "ServiceLabel": "Bay 0",
            }
        },
    }
    d = _drive_from_redfish_json(payload)
    assert d.name == "nvme0n1"
    assert d.protocol == "NVME"
    assert d.media_type == "SSD"
    assert d.is_nvme is True
    assert d.is_ssd is True
    assert d.capacity_bytes == 1_920_000_000_000
    assert d.model == "Samsung MZ1L21T9"
    assert d.serial == "S5JNNG0R900000"
    assert d.slot == "Bay 0"
    assert d.capacity_gb == 1920.0


def test_sata_hdd_legacy_naming():
    """Older Dell / HPE may report Id as the numeric index only."""
    payload = {
        "@odata.id": "/redfish/v1/Systems/1/Storage/0/Drives/3",
        "Id": "sdc",
        "Model": "WDC WD20EFRX",
        "SerialNumber": "WD-12345",
        "Protocol": "SATA",
        "MediaType": "HDD",
        "CapacityBytes": 2_000_000_000_000,
        "PhysicalLocation": {"Info": "Front Bay 3"},
    }
    d = _drive_from_redfish_json(payload)
    assert d.name == "sdc"
    assert d.protocol == "SATA"
    assert d.is_ssd is False
    assert d.is_nvme is False
    assert d.slot == "Front Bay 3"


def test_unknown_protocol_falls_back_to_uri_tail():
    """If the BMC omits a Linux-style name, derive one from the @odata.id."""
    payload = {
        "@odata.id": "/redfish/v1/Systems/1/Storage/0/Drives/7",
        "Id": "",
        "Protocol": "SAS",
        "MediaType": "HDD",
        "CapacityBytes": 600_000_000_000,
        "Model": "HGST",
    }
    d = _drive_from_redfish_json(payload)
    assert d.name == "7"
    assert d.protocol == "SAS"


def test_empty_bay_is_skipped():
    """A near-empty record (no CapacityBytes) is an empty/ghost bay — it must
    be SKIPPED (None), never returned as a zero-capacity drive that could
    resolve and get the wrong disk wiped."""
    payload = {"@odata.id": "/redfish/v1/Systems/1/Storage/0/Drives/x"}
    assert _drive_from_redfish_json(payload) is None


def test_absent_state_is_skipped():
    """Lenovo XCC / HPE publish empty slots as Drives with State=Absent."""
    payload = {"@odata.id": "/x/Drives/1", "Id": "Bay.1",
               "CapacityBytes": 960_000_000_000, "Status": {"State": "Absent"}}
    assert _drive_from_redfish_json(payload) is None


def test_non_device_name_is_not_fabricated_as_nvme():
    """A BMC reporting a non-Linux Id must NOT be turned into a fabricated
    'nvmeXn1' (which would wipe a different physical disk) — keep it opaque."""
    payload = {"@odata.id": "/x/Drives/2", "Id": "Disk.Bay.2", "Protocol": "NVMe",
               "CapacityBytes": 960_000_000_000, "SerialNumber": "SER123"}
    d = _drive_from_redfish_json(payload)
    assert d is not None
    assert not d.name.startswith("nvme")   # opaque label, not a guessed device
    assert d.serial == "SER123"            # serial is the safe targeting key


def test_round_trip_through_dict():
    """to_dict / from_dict roundtrip preserves all fields — needed for
    on-disk caching to servers.disks_json."""
    original = Drive(
        name="nvme0n1", protocol="NVME", media_type="SSD",
        capacity_bytes=1_920_000_000_000, model="Samsung",
        serial="S123", slot="Bay 0", redfish_uri="/x/y/z",
    )
    rt = Drive.from_dict(original.to_dict())
    assert rt == original
