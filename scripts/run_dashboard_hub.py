#!/usr/bin/env python3
"""Run the local trading dashboard hub."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dashboard_hub import main


if __name__ == "__main__":
    raise SystemExit(main())
