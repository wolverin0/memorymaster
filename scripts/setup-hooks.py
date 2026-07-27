"""Shim for backward compat — calls memorymaster.surfaces.setup_hooks.main().

New users should run `memorymaster-setup` after `pip install memorymaster`.
This shim exists so that `python scripts/setup-hooks.py` still works inside
a cloned repo checkout.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memorymaster.surfaces.setup_hooks import main  # noqa: E402

if __name__ == "__main__":
    main()
