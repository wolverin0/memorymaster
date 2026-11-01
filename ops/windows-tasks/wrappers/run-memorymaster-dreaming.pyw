"""Consoleless activated Dreaming launcher for graph observations and profiles."""

from __future__ import annotations

import os

from memorymaster.surfaces.scheduled_task import main


if __name__ == "__main__":
    os.environ["MEMORYMASTER_GRAPH_OBSERVATIONS"] = "1"
    os.environ["MEMORYMASTER_COMPILED_PROFILE"] = "1"
    raise SystemExit(main())
