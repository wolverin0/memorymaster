from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from scripts import git_to_turns, tickets_to_turns
except ImportError:
    import git_to_turns  # type: ignore[no-redef]
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


def _first_non_empty(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            continue
        text = _to_str(value).strip()
        if text:
            return text
    return ""


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
        raise RuntimeError(f"GitHub API request failed ({exc.code}) for {url}: {detail}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub connector config must be a JSON object")
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


def _cursor_stream(cursor: dict[str, Any] | None, stream_name: str) -> tuple[str, set[str]]:
    if not isinstance(cursor, dict):
        return "", set()
    raw = cursor.get(stream_name)
    if not isinstance(raw, dict):
        return "", set()
    latest_ts = _to_str(raw.get("latest_ts")).strip()
    raw_ids = raw.get("latest_ids")
    if not isinstance(raw_ids, list):
        return latest_ts, set()
    return latest_ts, {_to_str(item).strip() for item in raw_ids if _to_str(item).strip()}


def _stream_state(latest_ts: str, latest_ids: set[str]) -> dict[str, Any]:
    return {"latest_ts": latest_ts, "latest_ids": sorted(latest_ids)[:500]}


def _build_url(base_url: str, repo: str, endpoint: str, params: dict[str, str]) -> str:
    safe_repo = urllib.parse.quote(repo, safe="/")
    root = base_url.rstrip("/")
    query = urllib.parse.urlencode(params)
    return f"{root}/repos/{safe_repo}/{endpoint}?{query}"


def _commit_timestamp(item: dict[str, Any]) -> str:
    commit = item.get("commit")
    if isinstance(commit, dict):
        for key in ("author", "committer"):
            actor = commit.get(key)
            if isinstance(actor, dict):
                text = _to_str(actor.get("date")).strip()
                if text:
                    return text
    return _first_non_empty(item, "timestamp", "updated_at", "created_at")


def _commit_id(item: dict[str, Any]) -> str:
    return _first_non_empty(item, "sha", "id")


def _pr_timestamp(item: dict[str, Any]) -> str:
    return _first_non_empty(item, "updated_at", "created_at", "closed_at")


def _pr_id(item: dict[str, Any]) -> str:
    return _to_str(item.get("number")).strip() or _first_non_empty(item, "id")


def _issue_timestamp(item: dict[str, Any]) -> str:
    return _first_non_empty(item, "updated_at", "created_at", "closed_at")


def _issue_id(item: dict[str, Any]) -> str:
    return _to_str(item.get("number")).strip() or _first_non_empty(item, "id")


def _fetch_stream_items(
    *,
    stream_name: str,
    base_url: str,
    repo: str,
    endpoint: str,
    headers: dict[str, str],
    params: dict[str, str],
    per_page: int,
    max_pages: int,
    cursor: dict[str, Any] | None,
    id_getter: Callable[[dict[str, Any]], str],
    ts_getter: Callable[[dict[str, Any]], str],
    item_filter: Callable[[dict[str, Any]], bool] | None,
    requester: Requester,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    existing_ts, existing_ids = _cursor_stream(cursor, stream_name)
    newest_ts = existing_ts
    newest_ids: set[str] = set(existing_ids)
    collected: list[dict[str, Any]] = []
    scanned = 0
    should_stop = False

    for page in range(1, max_pages + 1):
        page_params = dict(params)
        page_params["per_page"] = str(per_page)
        page_params["page"] = str(page)
        url = _build_url(base_url, repo, endpoint, page_params)
        payload, _ = requester(url, headers)
        if not isinstance(payload, list):
            raise ValueError(f"GitHub API payload for {endpoint} must be a JSON array")
        if not payload:
            break

        for item in payload:
            if not isinstance(item, dict):
                continue
            scanned += 1
            item_id = id_getter(item)
            item_ts = ts_getter(item)

            if item_ts:
                if not newest_ts or item_ts > newest_ts:
                    newest_ts = item_ts
                    newest_ids = set()
                if item_ts == newest_ts and item_id:
                    newest_ids.add(item_id)

            if existing_ts and item_ts:
                if item_ts < existing_ts:
                    should_stop = True
                    break
                if item_ts == existing_ts and item_id and item_id in existing_ids:
                    continue

            if item_filter is not None and not item_filter(item):
                continue
            collected.append(item)

        if should_stop or len(payload) < per_page:
            break

    if newest_ts == existing_ts:
        newest_ids = set(existing_ids).union(newest_ids)
    stream_cursor = _stream_state(newest_ts, newest_ids)
    return collected, stream_cursor, scanned


def _commit_row(repo: str, item: dict[str, Any]) -> dict[str, Any]:
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    message = _to_str(commit.get("message")).strip()
    subject, _, body = message.partition("\n")
    author_payload = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    actor = item.get("author") if isinstance(item.get("author"), dict) else {}
    committer = item.get("committer") if isinstance(item.get("committer"), dict) else {}
    author_name = _to_str(author_payload.get("name")).strip() or _to_str(actor.get("login")).strip()
    author_email = _to_str(author_payload.get("email")).strip()
    if not author_email:
        author_email = _to_str(actor.get("email")).strip() or _to_str(committer.get("email")).strip()
    observations = [f"source=github_api", f"repository={repo}"]
    return {
        "kind": "commit",
        "repo": repo,
        "sha": _commit_id(item),
        "subject": subject.strip(),
        "body": body.strip(),
        "author": {"name": author_name, "email": author_email},
        "authored_at": _commit_timestamp(item),
        "files": [],
        "observations": observations,
    }


def _pull_outcome(item: dict[str, Any]) -> str:
    if _to_str(item.get("merged_at")).strip():
        return "merged"
    state = _to_str(item.get("state")).strip().lower()
    if state == "closed":
        return "closed"
    if state == "open" and bool(item.get("draft")):
        return "draft"
    return state


def _pull_review_summary(item: dict[str, Any]) -> str:
    comments = _to_str(item.get("comments")).strip()
    review_comments = _to_str(item.get("review_comments")).strip()
    if comments and review_comments:
        return f"comments={comments}, review_comments={review_comments}"
    if comments:
        return f"comments={comments}"
    if review_comments:
        return f"review_comments={review_comments}"
    if bool(item.get("draft")):
        return "draft"
    return ""


def _pull_row(repo: str, item: dict[str, Any]) -> dict[str, Any]:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    number = _to_str(item.get("number")).strip()
    review_summary = _pull_review_summary(item)
    outcome = _pull_outcome(item)
    return {
        "kind": "pull_request",
        "repository": repo,
        "pull_request": {
            "number": number,
            "title": _to_str(item.get("title")).strip(),
            "body": _to_str(item.get("body")).strip(),
            "state": _to_str(item.get("state")).strip(),
            "merged": bool(item.get("merged_at")),
            "html_url": _to_str(item.get("html_url")).strip(),
            "review_summary": review_summary,
            "outcome": outcome,
        },
        "author": {"name": _to_str(user.get("login")).strip(), "email": ""},
        "updated_at": _pr_timestamp(item),
        "files": [],
        "observations": [f"source=github_api", f"repository={repo}", f"pr={number}"],
    }


def _issue_row(repo: str, item: dict[str, Any]) -> dict[str, Any]:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    assignee_payload = item.get("assignee") if isinstance(item.get("assignee"), dict) else {}
    number = _to_str(item.get("number")).strip()
    labels_value = item.get("labels")
    labels: list[str] = []
    if isinstance(labels_value, list):
        for label in labels_value:
            if isinstance(label, dict):
                name = _to_str(label.get("name")).strip()
                if name:
                    labels.append(name)
            else:
                name = _to_str(label).strip()
                if name:
                    labels.append(name)

    comments_count = _to_str(item.get("comments")).strip()
    comments: list[str] = [f"comments={comments_count}"] if comments_count else []
    return {
        "kind": "issue",
        "key": f"{repo}#{number}" if number else _first_non_empty(item, "id"),
        "title": _to_str(item.get("title")).strip(),
        "description": _to_str(item.get("body")).strip(),
        "status": _to_str(item.get("state")).strip(),
        "assignee": {"name": _to_str(assignee_payload.get("login")).strip()},
        "reporter": _to_str(user.get("login")).strip(),
        "labels": labels,
        "comments": comments,
        "updated_at": _issue_timestamp(item),
        "project": repo,
        "thread_id": f"issue-{number}" if number else "",
        "observations": [f"source=github_api", f"repository={repo}", f"issue={number}"],
    }


def _row_timestamp_for_sort(row: dict[str, Any]) -> str:
    return _first_non_empty(row, "updated_at", "authored_at", "created_at", "timestamp")


def fetch_rows(
    *,
    repo: str,
    token: str = "",
    base_url: str = "https://api.github.com",
    include_commits: bool = True,
    include_pulls: bool = True,
    include_issues: bool = True,
    per_page: int = 50,
    max_pages: int = 3,
    cursor: dict[str, Any] | None = None,
    requester: Requester | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    requester_fn = requester or _default_requester
    if "/" not in repo:
        raise ValueError("GitHub repo must be in 'owner/name' format")
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "memorymaster-github-live-connector",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"

    next_cursor: dict[str, Any] = {
        "version": 1,
        "repo": repo,
        "commits": {"latest_ts": "", "latest_ids": []},
        "pulls": {"latest_ts": "", "latest_ids": []},
        "issues": {"latest_ts": "", "latest_ids": []},
    }

    total_scanned = 0
    rows: list[dict[str, Any]] = []

    if include_commits:
        commit_items, commit_cursor, scanned = _fetch_stream_items(
            stream_name="commits",
            base_url=base_url,
            repo=repo,
            endpoint="commits",
            headers=headers,
            params={},
            per_page=per_page,
            max_pages=max_pages,
            cursor=cursor,
            id_getter=_commit_id,
            ts_getter=_commit_timestamp,
            item_filter=None,
            requester=requester_fn,
        )
        total_scanned += scanned
        rows.extend(_commit_row(repo, item) for item in commit_items)
        next_cursor["commits"] = commit_cursor
    else:
        _, existing_ids = _cursor_stream(cursor, "commits")
        existing_ts, _ = _cursor_stream(cursor, "commits")
        next_cursor["commits"] = _stream_state(existing_ts, existing_ids)

    if include_pulls:
        pull_items, pull_cursor, scanned = _fetch_stream_items(
            stream_name="pulls",
            base_url=base_url,
            repo=repo,
            endpoint="pulls",
            headers=headers,
            params={"state": "all", "sort": "updated", "direction": "desc"},
            per_page=per_page,
            max_pages=max_pages,
            cursor=cursor,
            id_getter=_pr_id,
            ts_getter=_pr_timestamp,
            item_filter=None,
            requester=requester_fn,
        )
        total_scanned += scanned
        rows.extend(_pull_row(repo, item) for item in pull_items)
        next_cursor["pulls"] = pull_cursor
    else:
        existing_ts, existing_ids = _cursor_stream(cursor, "pulls")
        next_cursor["pulls"] = _stream_state(existing_ts, existing_ids)

    if include_issues:
        issue_items, issue_cursor, scanned = _fetch_stream_items(
            stream_name="issues",
            base_url=base_url,
            repo=repo,
            endpoint="issues",
            headers=headers,
            params={"state": "all", "sort": "updated", "direction": "desc"},
            per_page=per_page,
            max_pages=max_pages,
            cursor=cursor,
            id_getter=_issue_id,
            ts_getter=_issue_timestamp,
            item_filter=lambda item: not isinstance(item.get("pull_request"), dict),
            requester=requester_fn,
        )
        total_scanned += scanned
        rows.extend(_issue_row(repo, item) for item in issue_items)
        next_cursor["issues"] = issue_cursor
    else:
        existing_ts, existing_ids = _cursor_stream(cursor, "issues")
        next_cursor["issues"] = _stream_state(existing_ts, existing_ids)

    rows.sort(
        key=lambda row: (
            _row_timestamp_for_sort(row),
            _first_non_empty(row, "kind", "key", "sha"),
        ),
        reverse=True,
    )
    return rows, next_cursor, total_scanned


def _load_config(path: Path) -> dict[str, Any]:
    config = _read_json(path)
    repo = _to_str(config.get("repo")).strip()
    if not repo:
        raise ValueError("GitHub config must include 'repo' in owner/name format")

    token = _to_str(config.get("token")).strip()
    token_env = _to_str(config.get("token_env")).strip()
    if not token and token_env:
        token = _to_str(os.environ.get(token_env)).strip()

    include_commits = _as_bool(config.get("include_commits"), default=True)
    include_pulls = _as_bool(config.get("include_pulls"), default=True)
    include_issues = _as_bool(config.get("include_issues"), default=True)
    if not (include_commits or include_pulls or include_issues):
        raise ValueError("At least one stream must be enabled: include_commits/pulls/issues")

    return {
        "repo": repo,
        "token": token,
        "base_url": _to_str(config.get("base_url")).strip() or "https://api.github.com",
        "include_commits": include_commits,
        "include_pulls": include_pulls,
        "include_issues": include_issues,
        "per_page": max(1, int(config.get("per_page") or 50)),
        "max_pages": max(1, int(config.get("max_pages") or 3)),
    }


def load_rows(
    path: Path,
    *,
    cursor: dict[str, Any] | None = None,
    requester: Requester | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    cfg = _load_config(path)
    rows, next_cursor, scanned = fetch_rows(
        repo=cfg["repo"],
        token=cfg["token"],
        base_url=cfg["base_url"],
        include_commits=cfg["include_commits"],
        include_pulls=cfg["include_pulls"],
        include_issues=cfg["include_issues"],
        per_page=cfg["per_page"],
        max_pages=cfg["max_pages"],
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
    git_rows: list[dict[str, Any]] = []
    issue_rows: list[dict[str, Any]] = []
    for row in rows:
        kind = _to_str(row.get("kind")).strip().lower()
        row_copy = dict(row)
        row_copy.pop("kind", None)
        if kind in {"commit", "pull_request"}:
            git_rows.append(row_copy)
            continue
        if kind == "issue":
            issue_rows.append(row_copy)
            continue

    turns = git_to_turns.convert_rows(
        git_rows,
        default_session_id=default_session_id,
        default_thread_id=default_thread_id,
    )
    issue_turns = tickets_to_turns.convert_rows(
        issue_rows,
        default_session_id=default_session_id,
        default_thread_id=default_thread_id,
    )
    all_turns = [*turns, *issue_turns]
    all_turns.sort(
        key=lambda turn: (
            _first_non_empty(turn, "timestamp", "turn_id"),
            _first_non_empty(turn, "turn_id"),
        ),
        reverse=True,
    )
    return all_turns


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull live GitHub REST data (commits/PRs/issues) and convert to operator turns."
    )
    parser.add_argument("--input", required=True, help="Path to connector config JSON")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="github_live", help="Default session_id")
    parser.add_argument("--thread-id", default="github-live", help="Default thread_id")
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
        default_session_id=_to_str(args.session_id).strip() or "github_live",
        default_thread_id=_to_str(args.thread_id).strip() or "github-live",
    )
    write_jsonl(output_path, turns)
    if cursor_path:
        _write_cursor(cursor_path, next_cursor)
    print(f"scanned_rows={scanned} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
