"""CLI entry point for the aggregate-only legacy sensitivity inventory."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from memorymaster.govern.jobs.sensitivity_inventory import run_inventory


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid inventory arguments")


def _invalid_arguments() -> dict[str, str]:
    return {
        "classification": "LEGACY-SENSITIVITY-INVENTORY",
        "mode": "dry_run",
        "reason": "invalid_arguments",
        "recommendation": "REVIEW_ONLY",
        "status": "BLOCKED",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _SafeArgumentParser(add_help=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--artifact-root", action="append", default=[])
    parser.add_argument("--spool-root", action="append", default=[])
    parser.add_argument("--chunk-size", type=int, default=65536)
    parser.add_argument("--max-file-bytes", type=int, default=1048576)
    parser.add_argument("--max-entries", type=int, default=100000)
    try:
        args = parser.parse_args(argv)
    except ValueError:
        print(json.dumps(_invalid_arguments(), sort_keys=True, separators=(",", ":")))
        return 3
    result = run_inventory(
        args.db,
        artifact_roots=args.artifact_root,
        spool_roots=args.spool_root,
        chunk_size=args.chunk_size,
        max_file_bytes=args.max_file_bytes,
        max_entries=args.max_entries,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return {"COMPLETED": 0, "BLOCKED": 3, "BLOCKED-EXTERNAL": 4}[str(result["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
