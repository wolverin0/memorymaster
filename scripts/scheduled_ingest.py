from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

try:
    from scripts import (
        conversation_importer,
        email_live_to_turns,
        github_live_to_turns,
        jira_live_to_turns,
        git_to_turns,
        messages_to_turns,
        slack_live_to_turns,
        tickets_to_turns,
        webhook_to_turns,
    )
except ImportError:
    import conversation_importer  # type: ignore[no-redef]
    import email_live_to_turns  # type: ignore[no-redef]
    import github_live_to_turns  # type: ignore[no-redef]
    import jira_live_to_turns  # type: ignore[no-redef]
    import git_to_turns  # type: ignore[no-redef]
    import messages_to_turns  # type: ignore[no-redef]
    import slack_live_to_turns  # type: ignore[no-redef]
    import tickets_to_turns  # type: ignore[no-redef]
    import webhook_to_turns  # type: ignore[no-redef]


# Scheduler behavior:
# - Reads a local export file with a selected connector parser.
# - Converts exports to normalized operator turn JSONL.
# - Ingests each normalized turn into MemoryService.
# - Uses deterministic idempotency keys, so repeated runs are retry-safe.

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[-. ]?)?(?:\(?\d{2,4}\)?[-. ]?)\d{3,4}[-. ]?\d{3,4}\b")
_SECRET_ASSIGN_RE = re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\s*[:=]\s*([^\s,;]+)")


@dataclass(frozen=True)
class ConnectorLoad:
    rows: list[dict[str, Any]]
    input_rows: int
    next_cursor: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ConnectorOps:
    load_rows: Callable[[Path, dict[str, Any]], ConnectorLoad]
    convert_rows: Callable[[ConnectorLoad, str, str], list[dict[str, Any]]]
    write_jsonl: Callable[[Path, list[dict[str, Any]]], None]
    default_session_id: str


def _normalize_cursor(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _load_static_rows(
    loader: Callable[[Path], tuple[list[dict[str, Any]], int]],
    path: Path,
    cursor: dict[str, Any],
) -> ConnectorLoad:
    rows, input_rows = loader(path)
    return ConnectorLoad(rows=rows, input_rows=input_rows, next_cursor=_normalize_cursor(cursor), metadata={})


def _load_incremental_rows(
    loader: Callable[[Path, dict[str, Any] | None], tuple[list[dict[str, Any]], int, dict[str, Any]]],
    path: Path,
    cursor: dict[str, Any],
) -> ConnectorLoad:
    rows, input_rows, next_cursor = loader(path, cursor=_normalize_cursor(cursor))
    return ConnectorLoad(
        rows=rows,
        input_rows=input_rows,
        next_cursor=_normalize_cursor(next_cursor),
        metadata={},
    )


def _load_conversation_rows(path: Path, cursor: dict[str, Any]) -> ConnectorLoad:
    preferred_format = _to_str(cursor.get("parser")).strip().lower() or "auto"
    if preferred_format not in {"auto", "openai", "claude", "gemini", "conversation"}:
        preferred_format = "auto"
    rows, input_rows, input_messages, parser_name = conversation_importer.load_rows(path, format_hint=preferred_format)
    next_cursor = {
        "version": 1,
        "parser": parser_name,
        "last_input_rows": input_rows,
        "last_input_messages": input_messages,
    }
    return ConnectorLoad(
        rows=rows,
        input_rows=input_rows,
        next_cursor=next_cursor,
        metadata={"parser": parser_name},
    )


def _connector_table() -> dict[str, ConnectorOps]:
    return {
        "conversation": ConnectorOps(
            load_rows=_load_conversation_rows,
            convert_rows=lambda loaded, session_id, thread_id: conversation_importer.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
                parser_name=_to_str(loaded.metadata.get("parser")).strip() or "conversation",
            ),
            write_jsonl=conversation_importer.write_jsonl,
            default_session_id="import",
        ),
        "git": ConnectorOps(
            load_rows=lambda path, cursor: _load_static_rows(git_to_turns.load_rows, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: git_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=git_to_turns.write_jsonl,
            default_session_id="git",
        ),
        "tickets": ConnectorOps(
            load_rows=lambda path, cursor: _load_incremental_rows(tickets_to_turns.load_rows_incremental, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: tickets_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=tickets_to_turns.write_jsonl,
            default_session_id="tickets",
        ),
        "messages": ConnectorOps(
            load_rows=lambda path, cursor: _load_incremental_rows(messages_to_turns.load_rows_incremental, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: messages_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=messages_to_turns.write_jsonl,
            default_session_id="messages",
        ),
        "github_live": ConnectorOps(
            load_rows=lambda path, cursor: _load_incremental_rows(github_live_to_turns.load_rows, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: github_live_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=github_live_to_turns.write_jsonl,
            default_session_id="github_live",
        ),
        "jira_live": ConnectorOps(
            load_rows=lambda path, cursor: _load_incremental_rows(jira_live_to_turns.load_rows, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: jira_live_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=jira_live_to_turns.write_jsonl,
            default_session_id="jira_live",
        ),
        "slack_live": ConnectorOps(
            load_rows=lambda path, cursor: _load_incremental_rows(slack_live_to_turns.load_rows, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: slack_live_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=slack_live_to_turns.write_jsonl,
            default_session_id="slack_live",
        ),
        "email_live": ConnectorOps(
            load_rows=lambda path, cursor: _load_incremental_rows(email_live_to_turns.load_rows, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: email_live_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=email_live_to_turns.write_jsonl,
            default_session_id="email_live",
        ),
        "webhook": ConnectorOps(
            load_rows=lambda path, cursor: _load_incremental_rows(webhook_to_turns.load_rows, path, cursor),
            convert_rows=lambda loaded, session_id, thread_id: webhook_to_turns.convert_rows(
                loaded.rows,
                default_session_id=session_id,
                default_thread_id=thread_id,
            ),
            write_jsonl=webhook_to_turns.write_jsonl,
            default_session_id="webhook",
        ),
    }


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_observations(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = _to_str(item).strip()
            if text:
                out.append(text)
        return out
    text = _to_str(value).strip()
    return [text] if text else []


def make_idempotency_key(connector: str, turn: dict[str, Any]) -> str:
    canonical = {
        "connector": connector.strip().lower(),
        "session_id": _to_str(turn.get("session_id")).strip(),
        "thread_id": _to_str(turn.get("thread_id")).strip(),
        "turn_id": _to_str(turn.get("turn_id")).strip(),
        "user_text": _to_str(turn.get("user_text")).strip(),
        "assistant_text": _to_str(turn.get("assistant_text")).strip(),
        "observations": _normalize_observations(turn.get("observations")),
    }
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"ingest-{connector.strip().lower()}-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _turn_signature(connector: str, turn: dict[str, Any]) -> str:
    canonical = {
        "connector": connector.strip().lower(),
        "session_id": _to_str(turn.get("session_id")).strip(),
        "thread_id": _to_str(turn.get("thread_id")).strip(),
        "turn_id": _to_str(turn.get("turn_id")).strip(),
        "timestamp": _to_str(turn.get("timestamp")).strip(),
    }
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _turns_digest(turns: list[dict[str, Any]], connector: str) -> str:
    signatures = sorted(_turn_signature(connector, turn) for turn in turns)
    encoded = json.dumps(signatures, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _turn_to_claim_text(turn: dict[str, Any]) -> str:
    parts: list[str] = []
    user_text = _to_str(turn.get("user_text")).strip()
    assistant_text = _to_str(turn.get("assistant_text")).strip()
    observations = _normalize_observations(turn.get("observations"))

    if user_text:
        parts.append(f"user: {user_text}")
    if assistant_text:
        parts.append(f"assistant: {assistant_text}")
    for observation in observations:
        parts.append(f"observation: {observation}")

    if parts:
        return "\n".join(parts)
    return json.dumps(turn, ensure_ascii=True, sort_keys=True)


def _redact_sensitive_text(text: str) -> tuple[str, bool]:
    redacted = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    redacted = _EMAIL_RE.sub("[REDACTED:email]", redacted)
    redacted = _PHONE_RE.sub("[REDACTED:phone]", redacted)
    return redacted, redacted != text


def _sanitize_turn_for_sensitivity(turn: dict[str, Any], *, mode: str) -> tuple[dict[str, Any] | None, bool]:
    normalized_mode = mode.strip().lower()
    if normalized_mode == "allow":
        return dict(turn), False

    sanitized = dict(turn)
    user_text = _to_str(sanitized.get("user_text")).strip()
    assistant_text = _to_str(sanitized.get("assistant_text")).strip()
    observations = _normalize_observations(sanitized.get("observations"))

    changed = False
    red_user, changed_user = _redact_sensitive_text(user_text)
    red_assistant, changed_assistant = _redact_sensitive_text(assistant_text)
    red_observations: list[str] = []
    changed_observations = False
    for item in observations:
        red, was_changed = _redact_sensitive_text(item)
        red_observations.append(red)
        changed_observations = changed_observations or was_changed

    changed = changed_user or changed_assistant or changed_observations
    if normalized_mode == "drop" and changed:
        return None, True

    sanitized["user_text"] = red_user
    sanitized["assistant_text"] = red_assistant
    sanitized["observations"] = red_observations
    return sanitized, changed


def _run_import(
    *,
    connector: str,
    input_path: Path,
    output_path: Path,
    session_id: str,
    thread_id: str,
) -> tuple[int, list[dict[str, Any]], dict[str, Any] | None]:
    return _run_import_with_cursor(
        connector=connector,
        input_path=input_path,
        output_path=output_path,
        session_id=session_id,
        thread_id=thread_id,
        connector_cursor={},
    )


def _run_import_with_cursor(
    *,
    connector: str,
    input_path: Path,
    output_path: Path,
    session_id: str,
    thread_id: str,
    connector_cursor: dict[str, Any] | None,
) -> tuple[int, list[dict[str, Any]], dict[str, Any] | None]:
    connectors = _connector_table()
    if connector not in connectors:
        raise ValueError(f"Unsupported connector: {connector}")

    ops = connectors[connector]
    loaded = ops.load_rows(input_path, _normalize_cursor(connector_cursor))
    turns = ops.convert_rows(loaded, session_id, thread_id)
    ops.write_jsonl(output_path, turns)
    return loaded.input_rows, turns, loaded.next_cursor


def _ingest_turns(
    service: MemoryService,
    connector: str,
    turns: list[dict[str, Any]],
    *,
    sensitivity_mode: str,
) -> dict[str, int]:
    ingested = 0
    skipped_sensitive = 0
    redacted_sensitive = 0
    for turn in turns:
        prepared_turn, changed = _sanitize_turn_for_sensitivity(turn, mode=sensitivity_mode)
        if prepared_turn is None:
            skipped_sensitive += 1
            continue
        if changed:
            redacted_sensitive += 1
        turn = prepared_turn
        session_id = _to_str(turn.get("session_id")).strip()
        thread_id = _to_str(turn.get("thread_id")).strip()
        turn_id = _to_str(turn.get("turn_id")).strip()
        claim_text = _turn_to_claim_text(turn)
        citation = CitationInput(
            source=f"local://connector/{connector}",
            locator=f"session={session_id};thread={thread_id};turn={turn_id}",
            excerpt=claim_text[:500],
        )
        service.ingest(
            text=claim_text,
            citations=[citation],
            idempotency_key=make_idempotency_key(connector, turn),
            claim_type=f"{connector}_turn",
            subject=(session_id or connector),
            predicate="turn_id",
            object_value=(turn_id or None),
            scope="project",
            volatility="medium",
            confidence=0.5,
        )
        ingested += 1
    return {
        "ingested": ingested,
        "skipped_sensitive": skipped_sensitive,
        "redacted_sensitive": redacted_sensitive,
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_seen_signatures(state: dict[str, Any], *, limit: int) -> tuple[list[str], set[str]]:
    raw = state.get("seen_signatures")
    if not isinstance(raw, list):
        return [], set()
    out: list[str] = []
    for item in raw[-limit:]:
        text = _to_str(item).strip()
        if text:
            out.append(text)
    return out, set(out)


def _append_seen_signatures(existing: list[str], new_values: list[str], *, limit: int) -> list[str]:
    if not new_values:
        return existing[-limit:]
    merged = [*existing, *new_values]
    return merged[-limit:]


def _load_connector_cursors(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    raw_map = state.get("connector_cursors")
    if isinstance(raw_map, dict):
        for raw_name, raw_cursor in raw_map.items():
            name = _to_str(raw_name).strip().lower()
            if not name or not isinstance(raw_cursor, dict):
                continue
            out[name] = dict(raw_cursor)

    legacy_connector = _to_str(state.get("connector")).strip().lower()
    legacy_cursor = state.get("connector_cursor")
    if legacy_connector and isinstance(legacy_cursor, dict) and legacy_connector not in out:
        out[legacy_connector] = dict(legacy_cursor)
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repeatedly import local connector exports and ingest them with deterministic idempotency keys."
    )
    parser.add_argument("--db", default="memorymaster.db", help="SQLite path or Postgres DSN")
    parser.add_argument(
        "--connector",
        choices=sorted(_connector_table().keys()),
        required=True,
        help="Connector type",
    )
    parser.add_argument("--input", required=True, help="Path to local export file")
    parser.add_argument(
        "--turns-output",
        default=None,
        help="Output path for normalized turns JSONL (defaults to artifacts/connectors/<connector>_turns.jsonl)",
    )
    parser.add_argument("--session-id", default=None, help="Override output session_id")
    parser.add_argument("--thread-id", default=None, help="Override output thread_id")
    parser.add_argument(
        "--conversation-format",
        choices=["auto", "openai", "claude", "gemini", "conversation"],
        default="auto",
        help="Parser hint for connector=conversation (stored in connector cursor)",
    )
    parser.add_argument(
        "--sensitivity-mode",
        choices=["allow", "redact", "drop"],
        default="allow",
        help="Connector-side handling for sensitive payloads during ingest",
    )
    parser.add_argument("--interval-seconds", type=float, default=60.0, help="Sleep interval between runs")
    parser.add_argument("--max-runs", type=int, default=None, help="Stop after N runs (default: run forever)")
    parser.add_argument("--state-json", default="artifacts/connectors/scheduled_ingest_state.json", help="State file path")
    parser.add_argument(
        "--cursor-limit",
        type=int,
        default=10000,
        help="Max recent turn signatures stored for incremental checkpointing",
    )
    parser.add_argument("--once", action="store_true", help="Run exactly once")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    connector = args.connector.strip().lower()
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_path = (
        Path(args.turns_output)
        if args.turns_output
        else Path("artifacts") / "connectors" / f"{connector}_turns.jsonl"
    )
    state_path = Path(args.state_json)

    session_id = _to_str(args.session_id).strip() or connector
    thread_id = _to_str(args.thread_id).strip() or input_path.stem
    max_runs = 1 if args.once else args.max_runs

    service = MemoryService(args.db)
    service.init_db()

    prior_state = _read_state(state_path)
    runs = int(_to_str(prior_state.get("runs") or "0") or "0")
    cursor_limit = max(1000, int(args.cursor_limit))
    seen_signatures, seen_set = _load_seen_signatures(prior_state, limit=cursor_limit)
    connector_cursors = _load_connector_cursors(prior_state)
    connector_cursor = dict(connector_cursors.get(connector, {}))
    if connector == "conversation":
        preferred_format = _to_str(args.conversation_format).strip().lower() or "auto"
        if preferred_format != "auto" and not _to_str(connector_cursor.get("parser")).strip():
            connector_cursor["parser"] = preferred_format
    started_at = time.time()

    while True:
        cycle_started = time.time()
        input_rows, turns, connector_cursor = _run_import_with_cursor(
            connector=connector,
            input_path=input_path,
            output_path=output_path,
            session_id=session_id,
            thread_id=thread_id,
            connector_cursor=connector_cursor,
        )
        connector_cursors[connector] = _normalize_cursor(connector_cursor)
        all_digest = _turns_digest(turns, connector)
        new_turns: list[dict[str, Any]] = []
        new_signatures: list[str] = []
        for turn in turns:
            signature = _turn_signature(connector, turn)
            if signature in seen_set:
                continue
            new_turns.append(turn)
            new_signatures.append(signature)
            seen_set.add(signature)
        ingest_stats = _ingest_turns(
            service,
            connector,
            new_turns,
            sensitivity_mode=args.sensitivity_mode,
        )
        ingested = int(ingest_stats.get("ingested", 0))
        if new_signatures:
            seen_signatures = _append_seen_signatures(seen_signatures, new_signatures, limit=cursor_limit)
            seen_set = set(seen_signatures)
        runs += 1
        state_payload = {
            "cursor_version": 1,
            "connector": connector,
            "input": str(input_path),
            "turns_output": str(output_path),
            "runs": runs,
            "last_input_rows": input_rows,
            "last_output_turns": len(turns),
            "last_turns_digest": all_digest,
            "last_new_turns": len(new_turns),
            "last_ingested": ingested,
            "last_run_epoch": int(cycle_started),
            "seen_signatures": seen_signatures,
            "connector_cursor": connector_cursor,
            "connector_cursors": connector_cursors,
            "sensitivity_mode": args.sensitivity_mode,
            "last_redacted_sensitive": int(ingest_stats.get("redacted_sensitive", 0)),
            "last_skipped_sensitive": int(ingest_stats.get("skipped_sensitive", 0)),
        }
        _write_state(state_path, state_payload)
        print(json.dumps(state_payload, ensure_ascii=True, sort_keys=True))

        if max_runs is not None and runs >= max_runs:
            break
        sleep_for = max(0.05, float(args.interval_seconds))
        time.sleep(sleep_for)

    elapsed = round(time.time() - started_at, 3)
    print(json.dumps({"runs": runs, "elapsed_seconds": elapsed, "state_json": str(state_path)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
