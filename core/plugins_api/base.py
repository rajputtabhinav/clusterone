"""The OEM plugin contract.

Every vendor plugin implements :class:`OEMPlugin`. The shipped
``GenericRedfishPlugin`` provides the full DMTF-standard implementation; OEM
plugins subclass it and override only their vendor-specific deltas.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.models.drive import Drive
from core.models.firmware import Firmware
from core.models.server import Credentials, ServerInfo

# Re-exported for convenience.
__all__ = [
    "FirmwareType",
    "Capability",
    "RedfishProbe",
    "CompatResult",
    "Result",
    "ProgressCallback",
    "Credentials",
    "Drive",
    "BootTarget",
    "OEMPlugin",
]


class FirmwareType(str, Enum):
    BMC = "BMC"
    BIOS = "BIOS"
    HGX = "HGX"


class BootTarget(str, Enum):
    """Redfish BootSourceOverrideTarget values used by the provisioning flow."""
    CD = "Cd"
    PXE = "Pxe"
    HDD = "Hdd"
    NONE = "None"


class Capability(str, Enum):
    DISCOVER = "discover"
    VALIDATE = "validate"
    UPDATE_BMC = "update_bmc"
    UPDATE_BIOS = "update_bios"
    UPDATE_HGX = "update_hgx"
    GET_STORAGE = "get_storage"
    MOUNT_ISO = "mount_iso"
    POWER_CONTROL = "power_control"


@dataclass
class RedfishProbe:
    """Lightweight identity captured during discovery, used for plugin matching."""
    ip: str
    manufacturer: str | None = None
    model: str | None = None
    redfish_root: str | None = None
    oem_blocks: tuple[str, ...] = ()


@dataclass
class CompatResult:
    compatible: bool
    reason: str = ""
    is_downgrade: bool = False


@dataclass
class Result:
    ok: bool
    message: str = ""
    version_after: str | None = None


# Called by plugins to report progress: (state, percent 0..100).
ProgressCallback = Callable[[str, int], None]


class OEMPlugin(ABC):
    key: str = "generic"
    display_name: str = "Generic"
    supported_models: tuple[str, ...] = ()

    @abstractmethod
    def match(self, probe: RedfishProbe) -> float:
        """Return a 0.0–1.0 confidence that this plugin should handle the host."""

    @abstractmethod
    async def discover(self, base_url: str, creds: Credentials) -> ServerInfo:
        """Read identity + firmware versions for a single host.

        ``base_url`` is the fully-qualified scheme/host/port (e.g.
        ``https://10.10.10.5:443``); the plugin appends ``/redfish/v1/...``.
        """

    @abstractmethod
    async def validate(self, server: ServerInfo, firmware: Firmware) -> CompatResult:
        """Check model match, version direction, and signature before flashing."""

    @abstractmethod
    async def update_bmc(
        self, server: ServerInfo, firmware: Firmware,
        on_progress: ProgressCallback, creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        """Push a BMC firmware image. ``creds`` authenticates the BMC
        Redfish session — plugins MUST pass them to the underlying
        RedfishClient or the POST to ``UpdateService.SimpleUpdate`` will
        401 against any production iDRAC/iLO/XCC.

        ``apply_now`` controls activation timing. BMC firmware self-applies
        with a BMC reset regardless, so this is mostly a no-op for BMC."""

    @abstractmethod
    async def update_bios(
        self, server: ServerInfo, firmware: Firmware,
        on_progress: ProgressCallback, creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        """Push a host BIOS image. ``creds`` semantics match ``update_bmc``.

        BIOS images stage and apply on the *next host reboot*. When
        ``apply_now`` is True the plugin issues a host reset after staging
        so the new BIOS takes effect immediately; when False the operator
        reboots the host on their own schedule (the default — safer)."""

    async def update_hgx(
        self, server: ServerInfo, firmware: Firmware,
        on_progress: ProgressCallback, creds: Credentials,
        apply_now: bool = False,
    ) -> Result:
        """Push an HGX/GPU firmware image. ``creds`` semantics match
        ``update_bmc``.

        HGX/GPU components (NVIDIA HGX baseboards: GPU dies, ERoT security
        ICs, InfoROM, on-board HMC, etc.) appear as separate
        ``FirmwareInventory`` members and accept the same DMTF multipart
        push as BIOS/BMC — only the ``Targets`` URI changes.

        Default implementation returns a clean ``Result(ok=False, ...)`` so
        legacy OEM plugins that don't yet implement HGX compile cleanly and
        the orchestrator surfaces an actionable message instead of crashing.
        Subclasses override to perform the actual flash."""
        return Result(
            ok=False,
            message=f"{self.key} does not support HGX/GPU firmware updates",
        )

    # ---- v1.1 OS provisioning surface --------------------------------------
    #
    # Default no-op implementations on the base class let existing OEM
    # plugins continue to satisfy the contract while we roll the new
    # capabilities out vendor-by-vendor. GenericRedfishPlugin overrides all
    # four with DMTF-standard implementations; vendor subclasses override
    # only where their BMC deviates (Dell slot name, HPE eject-before-insert,
    # etc.).

    async def get_storage_inventory(
        self, base_url: str, creds: Credentials,
    ) -> list[Drive]:
        """Walk Redfish Storage subsystem and return every present Drive."""
        return []

    async def mount_iso(
        self, server: ServerInfo, image_url: str, creds: Credentials,
    ) -> Result:
        """Insert ``image_url`` into the BMC's Virtual Media slot."""
        return Result(ok=False, message=f"{self.key} does not support mount_iso")

    async def eject_iso(
        self, server: ServerInfo, creds: Credentials,
    ) -> Result:
        """Eject any currently-mounted Virtual Media (idempotent)."""
        return Result(ok=False, message=f"{self.key} does not support eject_iso")

    async def set_boot_once(
        self, server: ServerInfo, target: BootTarget, creds: Credentials,
    ) -> Result:
        """Set BootSourceOverride for the next boot only (cleared after one boot)."""
        return Result(ok=False, message=f"{self.key} does not support set_boot_once")

    async def power_cycle(
        self, server: ServerInfo, creds: Credentials,
    ) -> Result:
        """Issue ComputerSystem.Reset (ForceRestart). Idempotent — safe to retry."""
        return Result(ok=False, message=f"{self.key} does not support power_cycle")

    async def power_action(
        self, server: ServerInfo, action: str, creds: Credentials,
    ) -> Result:
        """Apply a power action by logical name: ``on`` | ``off`` |
        ``force_off`` | ``restart`` | ``graceful_restart`` | ``cycle``.

        Maps the logical action to a Redfish ``ResetType`` the BMC actually
        advertises (ComputerSystem.Reset AllowableValues). Default no-op so
        third-party plugins keep compiling; GenericRedfishPlugin implements it
        (and every bundled OEM plugin inherits that)."""
        return Result(ok=False, message=f"{self.key} does not support power_action")

    async def get_power_state(
        self, server: ServerInfo, creds: Credentials,
    ) -> str | None:
        """Return the host's current Redfish ``PowerState`` (``On``/``Off``/…),
        or ``None`` if it can't be read."""
        return None

    def capabilities(self) -> set[Capability]:
        return {
            Capability.DISCOVER,
            Capability.VALIDATE,
            Capability.UPDATE_BMC,
            Capability.UPDATE_BIOS,
        }
