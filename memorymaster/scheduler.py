from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.service import MemoryService


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_git_head(workspace_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def run_daemon(
    service: MemoryService,
    *,
    interval_seconds: int = 3600,
    max_cycles: int | None = None,
    compact_every: int = 0,
    min_citations: int = 1,
    min_score: float = 0.58,
    policy_mode: str = "legacy",
    policy_limit: int = 200,
    git_trigger: bool = False,
    git_check_seconds: int = 10,
) -> dict[str, int]:
    cycles = 0
    last_head = get_git_head(service.workspace_root) if git_trigger else None
    next_due = time.monotonic()
    next_git_check = time.monotonic()
    warned_git_unavailable = False

    while True:
        if max_cycles is not None and cycles >= max_cycles:
            return {"cycles": cycles}

        now = time.monotonic()
        due = now >= next_due
        commit_triggered = False

        if git_trigger and now >= next_git_check:
            head = get_git_head(service.workspace_root)
            next_git_check = now + max(1, git_check_seconds)
            if head is None and not warned_git_unavailable:
                print(json.dumps({"ts": utc_now(), "event": "git_unavailable"}))
                warned_git_unavailable = True
            if head and last_head and head != last_head:
                commit_triggered = True
            if head:
                last_head = head

        if due or commit_triggered:
            cycles += 1
            run_compactor = compact_every > 0 and (cycles % compact_every == 0)
            result = service.run_cycle(
                run_compactor=run_compactor,
                min_citations=min_citations,
                min_score=min_score,
                policy_mode=policy_mode,
                policy_limit=policy_limit,
            )
            print(
                json.dumps(
                    {
                        "ts": utc_now(),
                        "cycle": cycles,
                        "trigger": "commit" if commit_triggered and not due else "timer",
                        "run_compactor": run_compactor,
                        "policy_mode": policy_mode,
                        "result": result,
                    }
                )
            )
            next_due = time.monotonic() + max(1, interval_seconds)

        time.sleep(0.5)
