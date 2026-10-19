#!/usr/bin/env python
"""Checkout shim so `python ddc.py ...` keeps working from a git checkout (not part of the wheel: installs get a `ddc` command)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clouddeck.controller import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
