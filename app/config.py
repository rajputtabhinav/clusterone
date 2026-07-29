"""Application metadata and filesystem path resolution.

Runtime data (DB, firmware, logs) is deliberately kept out of the install
directory, which is read-only for standard users once packaged.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ClusterOne"
APP_TAGLINE = "One Platform. Every Server."
APP_VERSION = "0.1.0"
ORG_NAME = "ClusterOne"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> Path:
    """Resolve a bundled resource for both the dev tree and a PyInstaller build."""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", project_root()))
    else:
        base = project_root()
    return base.joinpath(*parts)


def data_dir() -> Path:
    """Per-user writable data directory (never the install dir)."""
    override = os.environ.get("CLUSTERONE_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_NAME


def db_path() -> Path:
    return data_dir() / "db" / "clusterone.db"


def firmware_dir() -> Path:
    return data_dir() / "firmware"


def iso_dir() -> Path:
    """Per-user ISO library. Content-addressable: `<sha256>.iso`."""
    return data_dir() / "iso"


def logs_dir() -> Path:
    return data_dir() / "logs"


def ensure_dirs() -> None:
    for path in (db_path().parent, firmware_dir(), iso_dir(), logs_dir()):
        path.mkdir(parents=True, exist_ok=True)
