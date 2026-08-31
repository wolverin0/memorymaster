#!/usr/bin/env python3
"""Installed provider-neutral wrapper for the Workflow Intelligence receipt hook."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path("__MEMORYMASTER_PROJECT_ROOT__")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memorymaster.workflow_intelligence.hook import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
