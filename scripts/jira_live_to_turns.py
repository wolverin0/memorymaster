from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from scripts import tickets_to_turns
except ImportError:
    import tickets_to_turns  # type: ignore[no-redef]

Requester = Callable[[str, dict[str, str]], tuple[Any, dict[str, str]]]


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = _to_str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _json_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[_to_str(key).lower()] = _to_str(value)
    return out


def _default_requester(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(url=url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            return payload, _json_headers(dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body[:300].replace("\n", " ").strip()
        raise RuntimeError(f"Jira API request failed ({exc.code}) for {url}: {detail}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Jira connector config must be a JSON object")
    return payload


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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _cursor_state(cursor: dict[str, Any] | None) -> tuple[str, set[str]]:
    if not isinstance(cursor, dict):
        return "", set()
    latest_ts = _to_str(cursor.get("latest_ts")).strip()
    raw_ids = cursor.get("latest_ids")
    if not isinstance(raw_ids, list):
        return latest_ts, set()
    latest_ids = {_to_str(item).strip() for item in raw_ids if _to_str(item).strip()}
    return latest_ts, latest_ids


def _stream_state(latest_ts: str, latest_ids: set[str]) -> dict[str, Any]:
    return {"latest_ts": latest_ts, "latest_ids": sorted(latest_ids)[:500]}


def _parse_adf_text(node: Any) -> list[str]:
    if isinstance(node, str):
        text = node.strip()
        return [text] if text else []
    if not isinstance(node, dict):
        return []
    out: list[str] = []
    text = _to_str(node.get("text")).strip()
    if text:
        out.append(text)
    content = node.get("content")
    if isinstance(content, list):
        for item in content:
            out.extend(_parse_adf_text(item))
    return out


def _adf_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return _to_str(value).strip()
    parts = _parse_adf_text(value)
    return "\n".join(part for part in parts if part).strip()


def _issue_updated(issue: dict[str, Any]) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    return _to_str(fields.get("updated")).strip() or _to_str(issue.get("updated")).strip()


def _issue_id(issue: dict[str, Any]) -> str:
    return _to_str(issue.get("key")).strip() or _to_str(issue.get("id")).strip()


def _auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _build_url(base_url: str, endpoint: str, params: dict[str, str]) -> str:
    root = base_url.rstrip("/")
    query = urllib.parse.urlencode(params)
    return f"{root}{endpoint}?{query}"


def _fetch_issue_comments(
    *,
    base_url: str,
    issue_key: str,
    headers: dict[str, str],
    max_comments: int,
    requester: Requester,
) -> list[str]:
    if max_comments <= 0:
        return []
    endpoint = f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/comment"
    url = _build_url(base_url, endpoint, {"startAt": "0", "maxResults": str(max_comments)})
    payload, _ = requester(url, headers)
    if not isinstance(payload, dict):
        return []
    comments_raw = payload.get("comments")
    if not isinstance(comments_raw, list):
        return []
    out: list[str] = []
    for item in comments_raw[:max_comments]:
        if not isinstance(item, dict):
            continue
        author_payload = item.get("author") if isinstance(item.get("author"), dict) else {}
        author = (
            _to_str(author_payload.get("displayName")).strip()
            or _to_str(author_payload.get("emailAddress")).strip()
            or _to_str(author_payload.get("accountId")).strip()
        )
        body = _adf_to_text(item.get("body"))
        if not body:
            continue
        out.append(f"{author}: {body}" if author else body)
    return out


def _issue_row(
    *,
    issue: dict[str, Any],
    project: str,
    source_name: str,
    comments: list[str],
) -> dict[str, Any]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    status_payload = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    priority_payload = fields.get("priority") if isinstance(fields.get("priority"), dict) else {}
    assignee_payload = fields.get("assignee") if isinstance(fields.get("assignee"), dict) else {}
    reporter_payload = fields.get("reporter") if isinstance(fields.get("reporter"), dict) else {}
    labels_raw = fields.get("labels")
    labels: list[str] = []
    if isinstance(labels_raw, list):
        for item in labels_raw:
            text = _to_str(item).strip()
            if text:
                labels.append(text)
    due = _to_str(fields.get("duedate")).strip()
    resolution_payload = fields.get("resolution") if isinstance(fields.get("resolution"), dict) else {}
    resolution = _to_str(resolution_payload.get("name")).strip()
    resolution_date = _to_str(fields.get("resolutiondate")).strip()
    updated = _issue_updated(issue)
    issue_key = _issue_id(issue)

    observations = [f"source={source_name}", f"project={project}", f"ticket={issue_key}"]
    if due:
        observations.append(f"due={due}")
    if resolution:
        observations.append(f"resolution={resolution}")
    if resolution_date:
        observations.append(f"resolution_date={resolution_date}")

    return {
        "kind": "issue",
        "key": issue_key,
        "title": _to_str(fields.get("summary")).strip(),
        "description": _adf_to_text(fields.get("description")),
        "status": _to_str(status_payload.get("name")).strip(),
        "priority": _to_str(priority_payload.get("name")).strip(),
        "assignee": {"name": _to_str(assignee_payload.get("displayName")).strip()},
        "reporter": _to_str(reporter_payload.get("displayName")).strip(),
        "labels": labels,
        "comments": comments,
        "updated_at": updated,
        "project": project,
        "thread_id": issue_key,
        "observations": observations,
    }


def fetch_rows(
    *,
    base_url: str,
    email: str,
    api_token: str,
    project: str,
    jql: str,
    per_page: int = 50,
    max_pages: int = 3,
    include_comments: bool = False,
    max_comments: int = 5,
    cursor: dict[str, Any] | None = None,
    requester: Requester | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    requester_fn = requester or _default_requester
    if not base_url.strip():
        raise ValueError("Jira base_url is required")
    if not email.strip():
        raise ValueError("Jira email is required")
    if not api_token.strip():
        raise ValueError("Jira api token is required")
    if not jql.strip():
        raise ValueError("Jira jql is required")
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": _auth_header(email.strip(), api_token.strip()),
        "User-Agent": "memorymaster-jira-live-connector",
    }

    existing_ts, existing_ids = _cursor_state(cursor)
    newest_ts = existing_ts
    newest_ids: set[str] = set(existing_ids)
    scanned = 0
    rows: list[dict[str, Any]] = []
    should_stop = False
    source_name = "jira_api"

    for page in range(max_pages):
        start_at = page * per_page
        url = _build_url(
            base_url,
            "/rest/api/3/search/jql",
            {
                "jql": jql,
                "startAt": str(start_at),
                "maxResults": str(per_page),
                "fields": "summary,description,status,priority,assignee,reporter,labels,updated,duedate,resolution,resolutiondate",
            },
        )
        payload, _ = requester_fn(url, headers)
        if not isinstance(payload, dict):
            raise ValueError("Jira API payload for search must be a JSON object")
        issues = payload.get("issues")
        total = int(payload.get("total") or 0)
        if not isinstance(issues, list) or not issues:
            break
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            scanned += 1
            issue_id = _issue_id(issue)
            updated = _issue_updated(issue)

            if updated:
                if not newest_ts or updated > newest_ts:
                    newest_ts = updated
                    newest_ids = set()
                if updated == newest_ts and issue_id:
                    newest_ids.add(issue_id)

            if existing_ts and updated:
                if updated < existing_ts:
                    should_stop = True
                    break
                if updated == existing_ts and issue_id and issue_id in existing_ids:
                    continue

            issue_comments: list[str] = []
            if include_comments and issue_id:
                issue_comments = _fetch_issue_comments(
                    base_url=base_url,
                    issue_key=issue_id,
                    headers=headers,
                    max_comments=max_comments,
                    requester=requester_fn,
                )

            rows.append(
                _issue_row(
                    issue=issue,
                    project=project,
                    source_name=source_name,
                    comments=issue_comments,
                )
            )

        if should_stop:
            break
        if start_at + len(issues) >= total:
            break

    if newest_ts == existing_ts:
        newest_ids = set(existing_ids).union(newest_ids)

    next_cursor = {
        "version": 1,
        "project": project,
        "latest_ts": newest_ts,
        "latest_ids": sorted(newest_ids)[:500],
    }
    rows.sort(
        key=lambda row: (_to_str(row.get("updated_at")).strip(), _to_str(row.get("key")).strip()),
        reverse=True,
    )
    return rows, next_cursor, scanned


def _load_config(path: Path) -> dict[str, Any]:
    config = _read_json(path)
    base_url = _to_str(config.get("base_url")).strip()
    project = _to_str(config.get("project")).strip()
    jql = _to_str(config.get("jql")).strip()
    if not base_url:
        raise ValueError("Jira config must include base_url")
    if not project:
        raise ValueError("Jira config must include project")
    if not jql:
        jql = f"project = {project} ORDER BY updated DESC"

    email = _to_str(config.get("email")).strip()
    email_env = _to_str(config.get("email_env")).strip()
    if not email and email_env:
        email = _to_str(os.environ.get(email_env)).strip()

    api_token = _to_str(config.get("api_token")).strip()
    api_token_env = _to_str(config.get("api_token_env")).strip()
    if not api_token and api_token_env:
        api_token = _to_str(os.environ.get(api_token_env)).strip()

    return {
        "base_url": base_url,
        "project": project,
        "jql": jql,
        "email": email,
        "api_token": api_token,
        "per_page": max(1, int(config.get("per_page") or 50)),
        "max_pages": max(1, int(config.get("max_pages") or 3)),
        "include_comments": _as_bool(config.get("include_comments"), default=False),
        "max_comments": max(1, int(config.get("max_comments") or 5)),
    }


def load_rows(
    path: Path,
    *,
    cursor: dict[str, Any] | None = None,
    requester: Requester | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    cfg = _load_config(path)
    rows, next_cursor, scanned = fetch_rows(
        base_url=cfg["base_url"],
        email=cfg["email"],
        api_token=cfg["api_token"],
        project=cfg["project"],
        jql=cfg["jql"],
        per_page=cfg["per_page"],
        max_pages=cfg["max_pages"],
        include_comments=cfg["include_comments"],
        max_comments=cfg["max_comments"],
        cursor=cursor,
        requester=requester,
    )
    return rows, scanned, next_cursor


def convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        row_copy = dict(row)
        row_copy.pop("kind", None)
        normalized_rows.append(row_copy)
    turns = tickets_to_turns.convert_rows(
        normalized_rows,
        default_session_id=default_session_id,
        default_thread_id=default_thread_id,
    )
    turns.sort(
        key=lambda turn: (_to_str(turn.get("timestamp")).strip(), _to_str(turn.get("turn_id")).strip()),
        reverse=True,
    )
    return turns


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull live Jira issues and convert to operator turns."
    )
    parser.add_argument("--input", required=True, help="Path to connector config JSON")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="jira_live", help="Default session_id")
    parser.add_argument("--thread-id", default="jira-live", help="Default thread_id")
    parser.add_argument(
        "--cursor-json",
        default=None,
        help="Optional cursor state JSON for incremental polling",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(f"Input config not found: {input_path}")

    cursor_path = Path(args.cursor_json) if args.cursor_json else None
    cursor = _read_cursor(cursor_path) if cursor_path else {}
    rows, scanned, next_cursor = load_rows(input_path, cursor=cursor)
    turns = convert_rows(
        rows,
        default_session_id=_to_str(args.session_id).strip() or "jira_live",
        default_thread_id=_to_str(args.thread_id).strip() or "jira-live",
    )
    write_jsonl(output_path, turns)
    if cursor_path:
        _write_cursor(cursor_path, next_cursor)
    print(f"scanned_rows={scanned} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
