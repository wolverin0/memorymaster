"""Managed agy adapter. All failures are silent and never block a turn."""

import sys

PROJECT_ROOT = __MEMORYMASTER_PROJECT_ROOT_REPR__  # noqa: F821 - installer substitutes a literal
DB_PATH = __MEMORYMASTER_DB_PATH_REPR__  # noqa: F821 - installer substitutes a literal

if __name__ == "__main__":
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from memorymaster.surfaces.agy_hooks import main

        raise SystemExit(main(sys.argv[1], db_path=DB_PATH))
    except Exception:
        raise SystemExit(0)
