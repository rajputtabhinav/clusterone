"""Public plugin API: the OEMPlugin contract, registry, and shared Redfish client."""
from core.plugins_api.base import (
    Capability,
    CompatResult,
    Credentials,
    FirmwareType,
    OEMPlugin,
    ProgressCallback,
    RedfishProbe,
    Result,
)

__all__ = [
    "Capability",
    "CompatResult",
    "Credentials",
    "FirmwareType",
    "OEMPlugin",
    "ProgressCallback",
    "RedfishProbe",
    "Result",
]
