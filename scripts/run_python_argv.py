"""Run a Python helper with an argv vector loaded from JSON.

This avoids native-shell quote loss for structured JSON arguments on Windows.
The arguments file must contain a JSON array of strings; no shell is invoked.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def load_arguments(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError("arguments JSON must be an array of strings")
    return payload


def run_script(script: Path, arguments: list[str]) -> int:
    completed = subprocess.run([sys.executable, str(script), *arguments], check=False)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--arguments", type=Path, required=True)
    args = parser.parse_args()
    if not args.script.is_file():
        parser.error(f"script does not exist: {args.script}")
    return run_script(args.script, load_arguments(args.arguments))


if __name__ == "__main__":
    raise SystemExit(main())
