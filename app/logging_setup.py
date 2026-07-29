"""Logging configuration: rotating app log + console + a separate audit trail."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app import config

_AUDIT_LOGGER = "clusterone.audit"


class _SanitizingFilter(logging.Filter):
    """Strip ``\\r``/``\\n`` from log messages so a BMC error containing
    a newline can't forge an entire fake log line. Without this an
    attacker-controlled response field (or a buggy plugin) could write:

        [BMC] Update failed
        2026-06-04 12:00:00 INFO clusterone : Admin login succeeded

    and the audit trail would silently grow a fabricated row. Replace
    control chars with explicit escape sequences so the actual content
    is preserved and visible to a human reviewer.
    """

    _ESCAPES = {"\n": r"\n", "\r": r"\r", "\x00": r"\x00", "\x1b": r"\x1b"}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(ch in msg for ch in self._ESCAPES):
            for ch, rep in self._ESCAPES.items():
                msg = msg.replace(ch, rep)
            # Replace args so the formatter prints the sanitized string.
            record.msg = msg
            record.args = None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    config.ensure_dirs()
    logs = config.logs_dir()

    root = logging.getLogger()
    if root.handlers:
        return  # already configured
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s : %(message)s")
    sanitizer = _SanitizingFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(sanitizer)
    root.addHandler(console)

    # ``delay=True`` so the file isn't opened until the first record
    # writes — friendlier under PyInstaller bundles and avoids holding a
    # handle that blocks log rotation when another process already has it.
    app_file = RotatingFileHandler(
        logs / "clusterone.log", maxBytes=5_000_000, backupCount=5,
        encoding="utf-8", delay=True,
    )
    app_file.setFormatter(fmt)
    app_file.addFilter(sanitizer)
    root.addHandler(app_file)

    # Separate, longer-retained audit trail (firmware/auth/update actions).
    audit = logging.getLogger(_AUDIT_LOGGER)
    audit.setLevel(logging.INFO)
    audit.propagate = False
    audit_file = RotatingFileHandler(
        logs / "audit.log", maxBytes=5_000_000, backupCount=20,
        encoding="utf-8", delay=True,
    )
    audit_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit_file.addFilter(sanitizer)
    audit.addHandler(audit_file)


def audit_logger() -> logging.Logger:
    return logging.getLogger(_AUDIT_LOGGER)
