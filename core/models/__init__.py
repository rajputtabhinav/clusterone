"""Domain models (plain dataclasses, no Qt dependency)."""
from core.models.firmware import Firmware
from core.models.job import TaskState, UpdateJob, UpdateTask
from core.models.server import Credentials, ServerInfo

__all__ = [
    "Firmware",
    "TaskState",
    "UpdateJob",
    "UpdateTask",
    "Credentials",
    "ServerInfo",
]
