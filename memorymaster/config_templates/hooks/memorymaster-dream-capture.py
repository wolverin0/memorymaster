"""Quiet Codex/Claude transcript capture for native MemoryMaster Dreaming."""

from __future__ import annotations

import argparse
import json
import os
import sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
sys.path.insert(0, PROJECT_ROOT)


def _enabled() -> bool:
    value = os.environ.get("MEMORYMASTER_DREAM_ENABLED")
    return value is None or value.strip().lower() not in {"0", "false", "no", "off", ""}


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    args = parser.parse_args()
    if not _enabled():
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
        from memorymaster.core.capture_control import capture_state_path
        from memorymaster.dreaming.capture import capture_hook_payload
        from memorymaster.dreaming.ledger import DreamLedger

        capture_hook_payload(payload, provider=args.provider, ledger=DreamLedger(capture_state_path()))
    except Exception as exc:
        try:
            from memorymaster.core.capture_control import capture_state_path
            from memorymaster.dreaming.ledger import DreamLedger

            DreamLedger(capture_state_path()).record_hook_error(args.provider, type(exc).__name__)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
