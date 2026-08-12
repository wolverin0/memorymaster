"""Run a Codex observation gate with deterministic success/failure markers."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


FAILURE_MARKER_EXIT = 21
MISSING_SUCCESS_EXIT = 22
TIMEOUT_EXIT = 124


def _require_fresh_markers(success_marker: Path, failure_marker: Path) -> None:
    existing = [path for path in (success_marker, failure_marker) if path.exists()]
    if existing:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"gate marker paths must be fresh: {names}")


def _resolve_exit_code(
    child_exit_code: int, *, success_exists: bool, failure_exists: bool
) -> int:
    if child_exit_code != 0:
        return child_exit_code
    if failure_exists:
        return FAILURE_MARKER_EXIT
    if not success_exists:
        return MISSING_SUCCESS_EXIT
    return 0


def _write_result(
    result_path: Path,
    *,
    started_at: str,
    child_exit_code: int,
    gate_exit_code: int,
) -> None:
    payload = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "child_exit_code": child_exit_code,
        "gate_exit_code": gate_exit_code,
        "status": "passed" if gate_exit_code == 0 else "failed",
    }
    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_gate(
    *,
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    result_path: Path,
    success_marker: Path,
    failure_marker: Path,
    timeout_seconds: int,
) -> int:
    _require_fresh_markers(success_marker, failure_marker)
    for path in (log_path, result_path, success_marker, failure_marker):
        path.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    child_exit_code = TIMEOUT_EXIT
    with log_path.open("w", encoding="utf-8") as stream:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                input=prompt,
                text=True,
                encoding="utf-8",
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
            child_exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            stream.write("\nObservation gate child timed out.\n")
    gate_exit_code = _resolve_exit_code(
        child_exit_code,
        success_exists=success_marker.exists(),
        failure_exists=failure_marker.exists(),
    )
    _write_result(
        result_path,
        started_at=started_at,
        child_exit_code=child_exit_code,
        gate_exit_code=gate_exit_code,
    )
    return gate_exit_code


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-path", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--success-marker", type=Path, required=True)
    parser.add_argument("--failure-marker", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=10_800)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a child command is required after --")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return run_gate(
        command=args.command,
        prompt=args.prompt_path.read_text(encoding="utf-8"),
        cwd=args.cwd,
        log_path=args.log_path,
        result_path=args.result_path,
        success_marker=args.success_marker,
        failure_marker=args.failure_marker,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
