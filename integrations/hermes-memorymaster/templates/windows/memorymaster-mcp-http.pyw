"""Consoleless Windows launcher for the authenticated MemoryMaster MCP service."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from memorymaster.surfaces.mcp_http import main


def _log_stream():
    configured = os.environ.get("MEMORYMASTER_LOG_DIR", "").strip()
    base = Path(configured) if configured else Path(os.environ["LOCALAPPDATA"]) / "MemoryMaster" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "mcp-http.log").open("a", encoding="utf-8", buffering=1)


if __name__ == "__main__":
    stream = _log_stream()
    sys.stdout = stream
    sys.stderr = stream
    raise SystemExit(main())
