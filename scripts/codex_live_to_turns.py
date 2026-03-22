from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _content_to_text(item).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "message", "content", "body", "value"):
            text = _content_to_text(value.get(key)).strip()
            if text:
                return text
        return ""
    return _to_str(value)


def _normalize_text_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _to_str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Codex live connector config must be a JSON object")
    return payload


def _normalize_path(value: Any) -> str:
    raw = _to_str(value).strip()
    if not raw:
        return ""
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return str(Path(raw).expanduser())


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in (_normalize_path(part) for part in value) if item]
    text = _normalize_path(value)
    return [text] if text else []


def _read_config(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    sessions_root = _normalize_path(payload.get("sessions_root") or Path.home() / ".codex" / "sessions")
    if not sessions_root:
        raise ValueError("Codex live connector requires sessions_root")
    max_files_raw = payload.get("max_files", 400)
    try:
        max_files = max(1, int(max_files_raw))
    except Exception as exc:
        raise ValueError("max_files must be an integer") from exc
    return {
        "sessions_root": sessions_root,
        "pattern": _to_str(payload.get("pattern") or "rollout-*.jsonl").strip() or "rollout-*.jsonl",
        "max_files": max_files,
        "include_cwds": _normalize_string_list(payload.get("include_cwds")),
        "exclude_cwds": _normalize_string_list(payload.get("exclude_cwds")),
    }


def _candidate_session_files(config: dict[str, Any]) -> tuple[Path, list[Path]]:
    sessions_root = Path(config["sessions_root"]).expanduser()
    if not sessions_root.exists():
        raise FileNotFoundError(f"Codex sessions root not found: {sessions_root}")
    pattern = _to_str(config.get("pattern")).strip() or "rollout-*.jsonl"
    files = [path for path in sessions_root.rglob(pattern) if path.is_file()]
    files.sort(key=lambda value: (value.stat().st_mtime, value.as_posix().lower()))
    max_files = max(1, int(config.get("max_files") or 400))
    if len(files) > max_files:
        files = files[-max_files:]
    files.sort(key=lambda value: value.as_posix().lower())
    return sessions_root, files


def _cursor_files(cursor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    raw = cursor.get("files")
    if not isinstance(raw, dict):
        return out
    for rel, item in raw.items():
        rel_text = _to_str(rel).strip()
        if not rel_text or not isinstance(item, dict):
            continue
        out[rel_text] = {
            "line": max(0, int(item.get("line") or 0)),
            "session_id": _to_str(item.get("session_id")).strip(),
            "cwd": _to_str(item.get("cwd")).strip(),
            "current_turn_id": _to_str(item.get("current_turn_id")).strip(),
        }
    return out


def _cursor_pending(cursor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    raw = cursor.get("pending_turns")
    if not isinstance(raw, dict):
        return out
    for key, item in raw.items():
        key_text = _to_str(key).strip()
        if not key_text or not isinstance(item, dict):
            continue
        out[key_text] = dict(item)
    return out


def _pending_key(session_id: str, turn_id: str) -> str:
    return f"{session_id}::{turn_id}"


def _session_id_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("rollout-"):
        return stem[len("rollout-") :]
    return stem


def _ensure_pending(
    pending_turns: dict[str, dict[str, Any]],
    *,
    session_id: str,
    turn_id: str,
    workspace: str,
    session_file: str,
) -> dict[str, Any]:
    key = _pending_key(session_id, turn_id)
    existing = pending_turns.get(key)
    if existing is None:
        existing = {
            "session_id": session_id,
            "thread_id": session_id,
            "turn_id": turn_id,
            "user_text": "",
            "assistant_text": "",
            "timestamp": "",
            "workspace": workspace,
            "session_file": session_file,
            "observations": [],
        }
        pending_turns[key] = existing
    else:
        existing["session_id"] = _to_str(existing.get("session_id")).strip() or session_id
        existing["thread_id"] = _to_str(existing.get("thread_id")).strip() or session_id
        existing["turn_id"] = _to_str(existing.get("turn_id")).strip() or turn_id
        if workspace and not _to_str(existing.get("workspace")).strip():
            existing["workspace"] = workspace
        if session_file and not _to_str(existing.get("session_file")).strip():
            existing["session_file"] = session_file
    return existing


def _observations_for_turn(turn: dict[str, Any]) -> list[str]:
    observations = ["source=codex_live"]
    workspace = _to_str(turn.get("workspace")).strip()
    session_file = _to_str(turn.get("session_file")).strip()
    if workspace:
        observations.append(f"workspace={workspace}")
    if session_file:
        observations.append(f"session_file={session_file}")
    raw = turn.get("observations")
    if isinstance(raw, list):
        observations.extend(_to_str(item).strip() for item in raw if _to_str(item).strip())
    return _normalize_text_list(observations)


def _finalize_turn(turn: dict[str, Any]) -> dict[str, Any] | None:
    session_id = _to_str(turn.get("session_id")).strip()
    turn_id = _to_str(turn.get("turn_id")).strip()
    if not session_id or not turn_id:
        return None
    user_text = _to_str(turn.get("user_text")).strip()
    assistant_text = _to_str(turn.get("assistant_text")).strip()
    if not user_text and not assistant_text:
        return None
    return {
        "session_id": session_id,
        "thread_id": _to_str(turn.get("thread_id")).strip() or session_id,
        "turn_id": turn_id,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "observations": _observations_for_turn(turn),
        "timestamp": _to_str(turn.get("timestamp")).strip(),
        "workspace": _to_str(turn.get("workspace")).strip(),
    }


def _cwd_allowed(cwd: str, include_cwds: list[str], exclude_cwds: list[str]) -> bool:
    if not cwd:
        return not include_cwds
    normalized = cwd.lower()
    if include_cwds and not any(normalized.startswith(prefix.lower()) for prefix in include_cwds):
        return False
    if exclude_cwds and any(normalized.startswith(prefix.lower()) for prefix in exclude_cwds):
        return False
    return True


def load_rows(
    path: Path,
    *,
    cursor: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    config = _read_config(path)
    sessions_root, files = _candidate_session_files(config)
    file_states = _cursor_files(cursor or {})
    pending_turns = _cursor_pending(cursor or {})
    include_cwds = list(config["include_cwds"])
    exclude_cwds = list(config["exclude_cwds"])
    rows: list[dict[str, Any]] = []
    input_rows = 0
    next_file_states: dict[str, dict[str, Any]] = {}

    for file in files:
        rel = file.relative_to(sessions_root).as_posix()
        prior = file_states.get(rel, {})
        start_line = max(0, int(prior.get("line") or 0))
        session_id = _to_str(prior.get("session_id")).strip() or _session_id_from_path(file)
        cwd = _to_str(prior.get("cwd")).strip()
        current_turn_id = _to_str(prior.get("current_turn_id")).strip()
        total_lines = start_line
        for index, line in enumerate(file.read_text(encoding="utf-8-sig").splitlines(), start=1):
            total_lines = index
            if index <= start_line:
                continue
            payload_text = line.strip()
            if not payload_text:
                continue
            item = json.loads(payload_text)
            if not isinstance(item, dict):
                raise ValueError(f"JSONL line {index} in {file} must be an object")
            input_rows += 1
            item_type = _to_str(item.get("type")).strip()
            payload = item.get("payload")
            payload_dict = payload if isinstance(payload, dict) else {}
            timestamp = _to_str(item.get("timestamp")).strip()

            if item_type == "session_meta":
                session_id = _to_str(payload_dict.get("id")).strip() or session_id
                cwd = _to_str(payload_dict.get("cwd")).strip() or cwd
                continue

            if item_type == "turn_context":
                turn_id = _to_str(payload_dict.get("turn_id")).strip()
                turn_cwd = _to_str(payload_dict.get("cwd")).strip() or cwd
                if turn_id:
                    pending = _ensure_pending(
                        pending_turns,
                        session_id=session_id,
                        turn_id=turn_id,
                        workspace=turn_cwd,
                        session_file=rel,
                    )
                    if turn_cwd:
                        pending["workspace"] = turn_cwd
                    if timestamp and not _to_str(pending.get("timestamp")).strip():
                        pending["timestamp"] = timestamp
                continue

            if item_type != "event_msg":
                continue

            event_type = _to_str(payload_dict.get("type")).strip()
            if event_type == "task_started":
                current_turn_id = _to_str(payload_dict.get("turn_id")).strip()
                if current_turn_id:
                    pending = _ensure_pending(
                        pending_turns,
                        session_id=session_id,
                        turn_id=current_turn_id,
                        workspace=cwd,
                        session_file=rel,
                    )
                    if timestamp and not _to_str(pending.get("timestamp")).strip():
                        pending["timestamp"] = timestamp
                continue

            if event_type == "user_message":
                turn_id = _to_str(payload_dict.get("turn_id")).strip() or current_turn_id
                if not turn_id:
                    continue
                pending = _ensure_pending(
                    pending_turns,
                    session_id=session_id,
                    turn_id=turn_id,
                    workspace=cwd,
                    session_file=rel,
                )
                message = _content_to_text(payload_dict.get("message")).strip()
                if message:
                    pending["user_text"] = message
                if timestamp and not _to_str(pending.get("timestamp")).strip():
                    pending["timestamp"] = timestamp
                continue

            if event_type == "task_complete":
                turn_id = _to_str(payload_dict.get("turn_id")).strip() or current_turn_id
                if not turn_id:
                    continue
                pending = _ensure_pending(
                    pending_turns,
                    session_id=session_id,
                    turn_id=turn_id,
                    workspace=cwd,
                    session_file=rel,
                )
                assistant_text = _content_to_text(payload_dict.get("last_agent_message")).strip()
                if assistant_text:
                    pending["assistant_text"] = assistant_text
                if not _to_str(pending.get("timestamp")).strip():
                    pending["timestamp"] = timestamp
                workspace = _to_str(pending.get("workspace")).strip() or cwd
                if _cwd_allowed(workspace, include_cwds, exclude_cwds):
                    finalized = _finalize_turn(pending)
                    if finalized is not None:
                        rows.append(finalized)
                pending_turns.pop(_pending_key(session_id, turn_id), None)
                if current_turn_id == turn_id:
                    current_turn_id = ""
                continue

            if event_type == "turn_aborted":
                turn_id = _to_str(payload_dict.get("turn_id")).strip() or current_turn_id
                if turn_id:
                    pending_turns.pop(_pending_key(session_id, turn_id), None)
                if current_turn_id == turn_id:
                    current_turn_id = ""

        next_file_states[rel] = {
            "line": total_lines,
            "session_id": session_id,
            "cwd": cwd,
            "current_turn_id": current_turn_id,
        }

    next_cursor = {
        "version": 1,
        "files": next_file_states,
        "pending_turns": pending_turns,
    }
    return rows, input_rows, next_cursor


def convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for row in rows:
        session_id = _to_str(row.get("session_id")).strip() or default_session_id
        thread_id = _to_str(row.get("thread_id")).strip() or session_id or default_thread_id
        turn_id = _to_str(row.get("turn_id")).strip()
        if not turn_id:
            basis = {
                "session_id": session_id,
                "thread_id": thread_id,
                "timestamp": _to_str(row.get("timestamp")).strip(),
                "user_text": _to_str(row.get("user_text")).strip(),
                "assistant_text": _to_str(row.get("assistant_text")).strip(),
            }
            turn_id = f"codex-{_stable_digest(basis)[:16]}"
        turns.append(
            {
                "session_id": session_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_text": _to_str(row.get("user_text")).strip(),
                "assistant_text": _to_str(row.get("assistant_text")).strip(),
                "observations": _observations_for_turn(row),
                "timestamp": _to_str(row.get("timestamp")).strip(),
                "workspace": _to_str(row.get("workspace")).strip(),
            }
        )
    return turns


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert live Codex session JSONL logs into operator turn JSONL.")
    parser.add_argument("--input", required=True, help="Path to codex_live connector JSON config")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--session-id", default="codex_live", help="Fallback session_id")
    parser.add_argument("--thread-id", default="codex", help="Fallback thread_id")
    parser.add_argument("--cursor-json", default="", help="Optional cursor JSON file for incremental ingestion")
    return parser.parse_args(argv)


def _read_cursor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cursor(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    cursor_path = Path(args.cursor_json) if _to_str(args.cursor_json).strip() else None
    cursor = _read_cursor(cursor_path) if cursor_path is not None else {}
    rows, _, next_cursor = load_rows(Path(args.input), cursor=cursor)
    turns = convert_rows(rows, default_session_id=args.session_id, default_thread_id=args.thread_id)
    write_jsonl(Path(args.output), turns)
    if cursor_path is not None:
        _write_cursor(cursor_path, next_cursor)
    print(json.dumps({"rows": len(rows), "turns": len(turns), "output": args.output}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
