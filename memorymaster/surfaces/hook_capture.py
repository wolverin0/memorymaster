"""Quiet native OpenCode session handoff to the existing Dreaming queue."""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", choices=("opencode",), required=True)
    args = parser.parse_args(argv)
    enabled = os.environ.get("MEMORYMASTER_DREAM_ENABLED")
    if enabled is not None and enabled.strip().lower() in {"0", "false", "no", "off", ""}:
        return 0
    try:
        payload = json.loads(sys.stdin.read(65_536) or "{}")
        if not isinstance(payload, dict):
            return 0
        from memorymaster.core.capture_control import capture_state_path
        from memorymaster.dreaming.capture import capture_hook_payload
        from memorymaster.dreaming.ledger import DreamLedger

        capture_hook_payload(payload, provider=args.cli, ledger=DreamLedger(capture_state_path()))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
