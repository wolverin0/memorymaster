from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

try:
    from scripts import scheduled_ingest
except ImportError:
    import scheduled_ingest  # type: ignore[no-redef]


def _to_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _write_config(path: Path, *, codex_home: Path, max_files: int) -> Path:
    payload = {
        "sessions_root": str(codex_home / "sessions"),
        "pattern": "rollout-*.jsonl",
        "max_files": max(1, int(max_files)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run background Codex session ingestion into MemoryMaster.")
    parser.add_argument("--db", default="memorymaster.db", help="SQLite path or Postgres DSN")
    parser.add_argument("--workspace", default=".", help="Workspace root for MemoryService artifacts")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"), help="Codex home directory")
    parser.add_argument(
        "--config-json",
        default="artifacts/connectors/codex_live.json",
        help="Generated codex_live connector config path",
    )
    parser.add_argument(
        "--turns-output",
        default="artifacts/connectors/codex_live_turns.jsonl",
        help="Normalized turns output path",
    )
    parser.add_argument(
        "--state-json",
        default="artifacts/connectors/codex_live_state.json",
        help="Connector state checkpoint path",
    )
    parser.add_argument("--interval-seconds", type=float, default=60.0, help="Sleep interval between runs")
    parser.add_argument("--max-runs", type=int, default=None, help="Stop after N runs")
    parser.add_argument("--max-files", type=int, default=400, help="Scan only the most recent N session files")
    parser.add_argument("--once", action="store_true", help="Run exactly once")
    parser.add_argument(
        "--sensitivity-mode",
        choices=["allow", "redact", "drop"],
        default="redact",
        help="Sensitive content handling before ingest",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    codex_home = Path(args.codex_home).expanduser()
    config_path = _write_config(Path(args.config_json), codex_home=codex_home, max_files=args.max_files)
    scheduled_args: list[str] = [
        "--db",
        args.db,
        "--workspace",
        args.workspace,
        "--connector",
        "codex_live",
        "--input",
        str(config_path),
        "--turns-output",
        args.turns_output,
        "--state-json",
        args.state_json,
        "--interval-seconds",
        _to_str(args.interval_seconds),
        "--sensitivity-mode",
        args.sensitivity_mode,
    ]
    if args.max_runs is not None:
        scheduled_args.extend(["--max-runs", _to_str(args.max_runs)])
    if args.once:
        scheduled_args.append("--once")
    return scheduled_ingest.main(scheduled_args)


if __name__ == "__main__":
    raise SystemExit(main())
