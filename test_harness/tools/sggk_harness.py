#!/usr/bin/env python3
"""Compatibility launcher for the user-facing SGGK Harness workflow."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_harness.orchestration.__main__ import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
