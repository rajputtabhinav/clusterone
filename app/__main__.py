"""Entrypoint: ``python -m app``."""
from __future__ import annotations

import sys

from app.application import main

if __name__ == "__main__":
    sys.exit(main())
