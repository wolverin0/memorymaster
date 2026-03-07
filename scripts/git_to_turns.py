from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

# Supported local export shapes:
# 1) JSON array of commit objects
# 2) JSON object with "commits": [...]
# 3) JSONL where each line is a commit object
#
# Output shape is operator inbox JSONL:
# {"session_id","thread_id","turn_id","user_text","assistant_text","observations","timestamp"}

_TIMESTAMP_KEYS = (
    "timestamp",
    "authored_at",
    "committed_at",
    "created_at",
    "updated_at",
    "date",
    "time",
    "ts",
)


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


def _extract_timestamp(payload: dict[str, Any]) -> str:
    for key in _TIMESTAMP_KEYS:
        text = _to_str(payload.get(key)).strip()
        if text:
            return text
    return ""


def _first_non_empty(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            continue
        text = _to_str(value).strip()
        if text:
            return text
    return ""


def _author_name(row: dict[str, Any]) -> str:
    author = row.get("author")
    if isinstance(author, dict):
        for key in ("name", "login", "username", "id"):
            text = _to_str(author.get(key)).strip()
            if text:
                return text
    return _first_non_empty(row, "author_name", "author", "committer_name")


def _author_email(row: dict[str, Any]) -> str:
    author = row.get("author")
    if isinstance(author, dict):
        text = _to_str(author.get("email")).strip()
        if text:
            return text
    return _first_non_empty(row, "author_email", "committer_email", "email")


def _file_list(row: dict[str, Any]) -> list[str]:
    for key in ("files", "changed_files", "paths"):
        value = row.get(key)
        if isinstance(value, list):
            out: list[str] = []
            for item in value:
                text = _to_str(item).strip()
                if text:
                    out.append(text)
            if out:
                return out
    return []


def _pr_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("pull_request", "pr"):
        value = row.get(key)
        if isinstance(value, dict):
            return value
    return None


def _pr_number(row: dict[str, Any]) -> str:
    pr = _pr_payload(row) or {}
    for key in ("number", "id"):
        value = pr.get(key)
        text = _to_str(value).strip()
        if text:
            return text
    return _first_non_empty(row, "pr_number", "pull_request_number")


def _pr_title(row: dict[str, Any]) -> str:
    pr = _pr_payload(row) or {}
    title = _to_str(pr.get("title")).strip()
    if title:
        return title
    return _first_non_empty(row, "pr_title")


def _pr_body(row: dict[str, Any]) -> str:
    pr = _pr_payload(row) or {}
    body = _to_str(pr.get("body")).strip()
    if body:
        return body
    return _first_non_empty(row, "pr_body")


def _pr_state(row: dict[str, Any]) -> str:
    pr = _pr_payload(row) or {}
    for key in ("state", "status"):
        text = _to_str(pr.get(key)).strip()
        if text:
            return text
    return _first_non_empty(row, "pr_state", "pull_request_state")


def _pr_review_summary(row: dict[str, Any]) -> str:
    pr = _pr_payload(row) or {}
    for key in ("review_summary", "review_outcome"):
        text = _to_str(pr.get(key)).strip()
        if text:
            return text
    return _first_non_empty(row, "review_summary", "review_outcome")


def _pr_outcome(row: dict[str, Any]) -> str:
    pr = _pr_payload(row) or {}
    for key in ("outcome", "merge_state", "result"):
        text = _to_str(pr.get(key)).strip()
        if text:
            return text
    return _first_non_empty(row, "pr_outcome", "outcome")


def _pr_merged(row: dict[str, Any]) -> bool | None:
    pr = _pr_payload(row) or {}
    value = pr.get("merged")
    if isinstance(value, bool):
        return value
    raw = _to_str(value).strip().lower()
    if raw in {"true", "1", "yes"}:
        return True
    if raw in {"false", "0", "no"}:
        return False
    return None


def _pr_url(row: dict[str, Any]) -> str:
    pr = _pr_payload(row) or {}
    for key in ("html_url", "url", "web_url"):
        text = _to_str(pr.get(key)).strip()
        if text:
            return text
    return _first_non_empty(row, "pr_url", "pull_request_url")


def _repository(row: dict[str, Any]) -> str:
    direct = _first_non_empty(row, "repository", "repo", "project")
    if direct:
        return direct
    pr = _pr_payload(row) or {}
    base = pr.get("base")
    if isinstance(base, dict):
        repo = base.get("repo")
        if isinstance(repo, dict):
            for key in ("full_name", "name"):
                text = _to_str(repo.get(key)).strip()
                if text:
                    return text
    return ""


def _commit_identity(row: dict[str, Any]) -> str:
    return _first_non_empty(row, "commit", "sha", "hash", "id")


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _turn_id_for_row(row: dict[str, Any]) -> str:
    pr_number = _pr_number(row)
    if pr_number:
        identity_payload = {
            "pr_number": pr_number,
            "title": _pr_title(row),
            "state": _pr_state(row),
            "outcome": _pr_outcome(row),
            "review_summary": _pr_review_summary(row),
            "timestamp": _extract_timestamp(row),
        }
        return f"git-pr-{_stable_digest(identity_payload)[:16]}"

    commit_id = _commit_identity(row)
    if commit_id:
        return f"git-{hashlib.sha256(commit_id.encode('utf-8')).hexdigest()[:16]}"
    identity_payload = {
        "message": _first_non_empty(row, "subject", "title", "summary", "message"),
        "body": _first_non_empty(row, "body", "description", "details"),
        "author": _author_name(row),
        "email": _author_email(row),
        "timestamp": _extract_timestamp(row),
        "files": _file_list(row),
    }
    return f"git-{_stable_digest(identity_payload)[:16]}"


def _rows_from_parsed(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        rows: list[dict[str, Any]] = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"JSON array item {idx} must be an object")
            rows.append(item)
        return rows

    if isinstance(parsed, dict):
        commits = parsed.get("commits")
        if isinstance(commits, list):
            rows: list[dict[str, Any]] = []
            for idx, item in enumerate(commits, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"commits[{idx}] must be an object")
                rows.append(item)
            return rows
        return [parsed]

    raise ValueError("Input must be a JSON object, JSON array, or JSONL objects")


def load_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    raw_text = path.read_text(encoding="utf-8-sig")
    stripped = raw_text.strip()
    if not stripped:
        return [], 0

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for idx, line in enumerate(raw_text.splitlines(), start=1):
            payload = line.strip()
            if not payload:
                continue
            try:
                item = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {idx}: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"JSONL line {idx} must be an object")
            rows.append(item)
        return rows, len(rows)

    rows = _rows_from_parsed(parsed)
    return rows, len(rows)


def convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for row in rows:
        repository = _repository(row)
        pr_number = _pr_number(row)
        pr_title = _pr_title(row)
        pr_body = _pr_body(row)
        pr_review_summary = _pr_review_summary(row)
        pr_outcome = _pr_outcome(row)
        subject = pr_title or _first_non_empty(row, "subject", "title", "summary", "message")
        body = pr_body or _first_non_empty(row, "body", "description", "details")
        text_parts: list[str] = []
        if subject:
            text_parts.append(subject)
        if body and body != subject:
            text_parts.append(body)
        if pr_review_summary:
            text_parts.append(f"review_summary: {pr_review_summary}")
        if pr_outcome:
            text_parts.append(f"outcome: {pr_outcome}")
        user_text = "\n\n".join(part.strip() for part in text_parts if part.strip()).strip()
        if not user_text:
            user_text = json.dumps(row, ensure_ascii=True, sort_keys=True)

        observations: list[str] = []
        commit_id = _commit_identity(row)
        if commit_id:
            observations.append(f"commit={commit_id}")
        if repository:
            observations.append(f"repository={repository}")
        if pr_number:
            observations.append(f"pr={pr_number}")
        pr_state = _pr_state(row)
        if pr_state:
            observations.append(f"pr_state={pr_state}")
        pr_merged = _pr_merged(row)
        if pr_merged is not None:
            observations.append(f"pr_merged={str(pr_merged).lower()}")
        pr_url = _pr_url(row)
        if pr_url:
            observations.append(f"pr_url={pr_url}")
        if pr_outcome:
            observations.append(f"pr_outcome={pr_outcome}")
        if pr_review_summary:
            observations.append(f"pr_review_summary={pr_review_summary}")
        author = _author_name(row)
        if author:
            observations.append(f"author={author}")
        email = _author_email(row)
        if email:
            observations.append(f"email={email}")
        files = _file_list(row)
        if files:
            observations.append("files=" + ",".join(files))
        observations.extend(_normalize_observations(row.get("observations")))

        row_session = _first_non_empty(row, "session_id") or default_session_id
        pr_thread = f"pr-{pr_number}" if pr_number else ""
        row_thread = _first_non_empty(row, "thread_id", "branch") or pr_thread or default_thread_id
        turn_id = _first_non_empty(row, "turn_id") or _turn_id_for_row(row)
        timestamp = _extract_timestamp(row)

        converted.append(
            {
                "session_id": row_session,
                "thread_id": row_thread,
                "turn_id": turn_id,
                "user_text": user_text,
                "assistant_text": "",
                "observations": observations,
                "timestamp": timestamp,
            }
        )
    return converted


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert local git export JSON/JSONL to operator inbox turns.")
    parser.add_argument("--input", required=True, help="Path to git export file")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="git", help="Default session_id")
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Default thread_id (defaults to input filename stem)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    thread_id = _to_str(args.thread_id).strip() or input_path.stem
    rows, input_rows = load_rows(input_path)
    turns = convert_rows(
        rows,
        default_session_id=_to_str(args.session_id).strip() or "git",
        default_thread_id=thread_id,
    )
    write_jsonl(output_path, turns)
    print(f"input_rows={input_rows} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
