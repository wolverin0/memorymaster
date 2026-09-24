"""Bounded child-process supervisor for the existing read-only operational review."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

from memorymaster.operations.operational_review import REVIEW_CHECKS
from memorymaster.operations.review_attempt import atomic_json, read_json, utc_now

TIMEOUT_SECONDS = 24 * 60


def _command(config: dict) -> list[str]:
    command = [str(config["python"]), "-m", "memorymaster.operations.operational_review",
               "--db", str(config["db"]), "--lookback-hours", str(config.get("lookback_hours", 8)), "--json"]
    for name in ("expected_version", "canary_query", "canary_human_id"):
        if config.get(name):
            command.extend(["--" + name.replace("_", "-"), str(config[name])])
    for canary in config.get("canaries") or []:
        if isinstance(canary, dict) and canary.get("query") and canary.get("human_id"):
            command.extend(["--canary", str(canary["query"]), str(canary["human_id"])])
    return command


def _run_child(config: dict, root: Path, attempt: dict, *, timeout_seconds: float, popen) -> tuple[int, str]:
    environment = {**os.environ, "MEMORYMASTER_REVIEW_ATTEMPT_FILE": str(root / "attempt.json"),
                   "MEMORYMASTER_REVIEW_ATTEMPT_ID": attempt["attempt_id"]}
    with (root / f"{attempt['attempt_id']}.stdout.json").open("w", encoding="utf-8") as stdout, (
        root / f"{attempt['attempt_id']}.stderr.log"
    ).open("w", encoding="utf-8") as stderr:
        child = popen(_command(config), stdout=stdout, stderr=stderr, env=environment,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            return child.wait(timeout=timeout_seconds), "completed"
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            return 124, "TIMEOUT"
        except BaseException:
            child.kill()
            child.wait()
            raise


def _completed_payload(root: Path, attempt_id: str, code: int) -> dict:
    payload = read_json(root / f"{attempt_id}.stdout.json")
    if (payload.get("schema") != "memorymaster.operational-review.v1"
            or payload.get("attempt_id") != attempt_id or payload.get("review_performed") is not True
            or payload.get("exit_code") != code or payload.get("verdict") != {0: "PASS", 1: "FAIL", 3: "WARN"}.get(code)
            or not isinstance(payload.get("checks"), list) or len(payload["checks"]) != len(REVIEW_CHECKS)):
        return {}
    return payload


def supervise(config: dict, *, timeout_seconds: float = TIMEOUT_SECONDS, popen=subprocess.Popen, now=utc_now) -> int:
    root = Path(config["output_root"])
    started = now()
    attempt = {"schema": "memorymaster.operational-review-attempt.v1", "attempt_id": "review-" + uuid.uuid4().hex,
               "started_at": started.isoformat(), "deadline_at": (started + timedelta(seconds=timeout_seconds)).isoformat(),
               "interval_seconds": int(config.get("every_hours", 6)) * 3600,
               "phase": "starting", "phase_seconds": {}, "completed_at": None, "outcome": "INCOMPLETE"}
    atomic_json(root / "attempt.json", attempt)
    code, outcome, performed = 9, "INCOMPLETE", False
    try:
        code, outcome = _run_child(config, root, attempt, timeout_seconds=timeout_seconds, popen=popen)
        payload = _completed_payload(root, attempt["attempt_id"], code) if outcome == "completed" else {}
        if payload:
            atomic_json(root / "latest.json", payload)
            outcome, performed = payload["verdict"], True
        elif outcome == "completed":
            code, outcome = 9, "INCOMPLETE"
    except (Exception, KeyboardInterrupt):
        code, outcome = 9, "INCOMPLETE"
    finally:
        progress = read_json(root / "attempt.json")
        finished = {**attempt, **progress, "completed_at": now().isoformat(), "outcome": outcome,
                    "exit_code": code, "review_performed": performed}
        atomic_json(root / "attempt.json", finished)
        with (root / "history.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(finished, sort_keys=True) + "\n")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    return supervise(json.loads(Path(args.config).read_text(encoding="utf-8-sig")))


if __name__ == "__main__":
    raise SystemExit(main())
