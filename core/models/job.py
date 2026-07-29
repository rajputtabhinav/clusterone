from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskState(str, Enum):
    QUEUED = "queued"
    PRECHECK = "precheck"
    UPLOADING = "uploading"
    FLASHING = "flashing"
    REBOOTING = "rebooting"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class UpdateTask:
    server_id: int
    firmware_id: int
    state: TaskState = TaskState.QUEUED
    progress: int = 0
    version_before: str | None = None
    version_after: str | None = None
    message: str = ""


@dataclass
class UpdateJob:
    fw_type: str                       # BMC | BIOS | HGX
    concurrency: int = 8
    apply_time: str = "OnReset"        # Immediate | OnReset
    tasks: list[UpdateTask] = field(default_factory=list)
