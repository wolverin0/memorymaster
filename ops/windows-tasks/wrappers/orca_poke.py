"""Deliver a checkpoint prompt to the live MemoryMaster agent terminal in Orca.

Replaces the WezTerm-only poke that failed 6 of 7 days after panes moved to Orca
(review 2026-09-23 F-08). Terminal handles change per incarnation, so the target
is resolved at send time: connected + writable terminal whose worktree is the
MemoryMaster checkout, preferring Claude over Codex, most recent output first.
Exit codes: 0 delivered, 2 bad input, 3 Orca unavailable, 4 no target, 5 send failed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ORCA = Path(r"{LOCALAPPDATA}\Programs\orca\resources\bin\orca.exe")
WORKSPACE = "{REPO_FWD}"
PREFERENCE = ("claude", "codex")
LOG = Path.home() / ".memorymaster" / "checkpoints" / "feature-checkpoint.log"
# 2026-10-01 failed once on a 40 s `terminal list` timeout and 09-30 on a moment with
# no connected terminal: both are transient, so resolve the target a few times.
TARGET_ATTEMPTS = 3
TARGET_RETRY_SECONDS = 60


def _log(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} orca-poke {message}\n")


def _orca(*args: str, timeout: int = 40) -> dict:
    completed = subprocess.run(
        [str(ORCA), *args, "--json"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"orca {args[0]} {args[1]} exit={completed.returncode}")
    return json.loads(completed.stdout)


def _target() -> dict | None:
    terminals = _orca("terminal", "list").get("result", {}).get("terminals", [])
    candidates = [
        t for t in terminals
        if (t.get("worktreePath") or "").replace("\\", "/").rstrip("/").lower() == WORKSPACE.lower()
        and t.get("connected") and t.get("writable") and not t.get("orphaned")
        and t.get("agentIdentity") in PREFERENCE
    ]
    candidates.sort(key=lambda t: (PREFERENCE.index(t["agentIdentity"]), -(t.get("lastOutputAt") or 0)))
    return candidates[0] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("message_file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = Path(args.message_file)
    if not path.is_file():
        _log(f"FAIL(2): missing message file {path.name}")
        return 2
    text = path.read_text(encoding="utf-8").strip()
    if not ORCA.is_file():
        _log("FAIL(3): orca.exe not found")
        return 3
    target, failure = None, None
    for attempt in range(1, TARGET_ATTEMPTS + 1):
        try:
            target = _target()
            failure = None if target else (4, "no connected MemoryMaster claude/codex terminal in Orca")
        except Exception as exc:  # noqa: BLE001 - scheduled task must log, not crash
            failure = (3, f"orca unavailable: {exc}")
        if target is not None:
            break
        if attempt < TARGET_ATTEMPTS:
            _log(f"RETRY({failure[0]}) {attempt}/{TARGET_ATTEMPTS}: {failure[1]}")
            time.sleep(TARGET_RETRY_SECONDS)
    if target is None:
        _log(f"FAIL({failure[0]}): {failure[1]}")
        return failure[0]
    label = f"{target['agentIdentity']} {target['handle'][:18]}"
    if args.dry_run:
        _log(f"DRY-RUN target={label} chars={len(text)}")
        print(json.dumps({"target": target["handle"], "agent": target["agentIdentity"], "chars": len(text)}))
        return 0
    try:
        result = _orca("terminal", "send", "--terminal", target["handle"], "--text", text,
                       "--enter", "--wait-submit", "15", timeout=60)
    except Exception as exc:  # noqa: BLE001
        _log(f"FAIL(5): send to {label} failed: {exc}")
        return 5
    ok = bool(result.get("ok"))
    _log(f"{'OK' if ok else 'FAIL(5)'}: {len(text)} chars -> {label}")
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
