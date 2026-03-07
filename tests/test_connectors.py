from __future__ import annotations

import json
import tempfile
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from scripts import (
    github_live_to_turns,
    git_to_turns,
    messages_to_turns,
    scheduled_ingest,
    tickets_to_turns,
    webhook_to_turns,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _case_dir(prefix: str) -> Path:
    base = REPO_ROOT / ".tmp_pytest"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))


def test_git_connector_normalizes_commits_with_deterministic_turn_ids() -> None:
    tmp = _case_dir("connectors_git_")
    export_path = tmp / "git_export.json"
    export_payload = {
        "commits": [
            {
                "sha": "abc123",
                "subject": "Fix flaky parser",
                "body": "Handle empty JSONL lines safely.",
                "author": {"name": "Ari", "email": "ari@example.com"},
                "files": ["scripts/messages_to_turns.py", "tests/test_connectors.py"],
                "authored_at": "2026-03-01T10:00:00+00:00",
                "repo": "memorymaster",
            }
        ]
    }
    export_path.write_text(json.dumps(export_payload), encoding="utf-8")

    rows, input_rows = git_to_turns.load_rows(export_path)
    assert input_rows == 1

    first = git_to_turns.convert_rows(rows, default_session_id="git", default_thread_id="repo-thread")
    second = git_to_turns.convert_rows(rows, default_session_id="git", default_thread_id="repo-thread")
    assert first == second
    assert len(first) == 1

    row = first[0]
    assert row["session_id"] == "git"
    assert row["thread_id"] == "repo-thread"
    assert row["turn_id"].startswith("git-")
    assert row["user_text"] == "Fix flaky parser\n\nHandle empty JSONL lines safely."
    assert "commit=abc123" in row["observations"]
    assert "repository=memorymaster" in row["observations"]
    assert row["timestamp"] == "2026-03-01T10:00:00+00:00"


def test_git_connector_maps_pr_payload_to_turns() -> None:
    rows = [
        {
            "repository": "acme/platform",
            "pull_request": {
                "number": 42,
                "title": "Ship staged rollout guardrail",
                "body": "Adds circuit breaker around deploy webhook retries.",
                "state": "closed",
                "merged": True,
                "html_url": "https://example.test/acme/platform/pull/42",
                "review_summary": "2 approvals, 0 change requests",
                "outcome": "merged",
            },
            "author": {"name": "Sam", "email": "sam@example.com"},
            "updated_at": "2026-03-03T13:12:11+00:00",
            "files": ["deploy/rollout.yaml"],
        }
    ]

    turns = git_to_turns.convert_rows(rows, default_session_id="git", default_thread_id="fallback-thread")
    assert len(turns) == 1
    row = turns[0]
    assert row["thread_id"] == "pr-42"
    assert row["turn_id"].startswith("git-pr-")
    assert "Ship staged rollout guardrail" in row["user_text"]
    assert "review_summary: 2 approvals, 0 change requests" in row["user_text"]
    assert "outcome: merged" in row["user_text"]
    assert "repository=acme/platform" in row["observations"]
    assert "pr=42" in row["observations"]
    assert "pr_state=closed" in row["observations"]
    assert "pr_merged=true" in row["observations"]
    assert "pr_outcome=merged" in row["observations"]
    assert row["timestamp"] == "2026-03-03T13:12:11+00:00"


def test_ticket_connector_normalizes_comments_and_metadata() -> None:
    rows = [
        {
            "key": "OPS-42",
            "title": "Rotate stale credentials",
            "description": "Rotate service account token before Friday.",
            "status": "In Progress",
            "priority": "High",
            "assignee": {"name": "Nora"},
            "reporter": "lead@example.com",
            "labels": ["security", "ops"],
            "comments": [
                {"author": {"name": "nora"}, "body": "Working on the rollout plan."},
                "Need approval from infra",
            ],
            "updated_at": "2026-03-02T12:30:00+00:00",
        }
    ]
    turns = tickets_to_turns.convert_rows(rows, default_session_id="tickets", default_thread_id="ops-board")
    assert len(turns) == 1

    row = turns[0]
    assert row["session_id"] == "tickets"
    assert row["thread_id"] == "ops-board"
    assert row["turn_id"].startswith("ticket-")
    assert row["assistant_text"] == ""
    assert row["user_text"] == "Rotate stale credentials\n\nRotate service account token before Friday."
    assert "ticket=OPS-42" in row["observations"]
    assert "status=In Progress" in row["observations"]
    assert "assignee=Nora" in row["observations"]
    assert "labels=security,ops" in row["observations"]
    assert "comment=nora: Working on the rollout plan." in row["observations"]
    assert row["timestamp"] == "2026-03-02T12:30:00+00:00"


def test_messages_connector_expands_threads_and_keeps_stable_ids() -> None:
    tmp = _case_dir("connectors_messages_")
    export_path = tmp / "messages_export.json"
    export_payload = {
        "session_id": "slack-import",
        "threads": [
            {
                "id": "thread-100",
                "channel": "deployments",
                "messages": [
                    {"id": "m1", "user": "alice", "text": "Deploy done", "ts": "1710000000.000100"},
                    {"id": "m2", "role": "bot", "text": "Health checks are green", "ts": "1710000001.000200"},
                ],
            }
        ],
    }
    export_path.write_text(json.dumps(export_payload), encoding="utf-8")

    rows, input_rows = messages_to_turns.load_rows(export_path)
    assert input_rows == 1
    assert len(rows) == 2

    turns = messages_to_turns.convert_rows(rows, default_session_id="messages", default_thread_id="default-thread")
    assert len(turns) == 2

    first = turns[0]
    second = turns[1]
    assert first["session_id"] == "slack-import"
    assert first["thread_id"] == "thread-100"
    assert first["turn_id"].startswith("msg-")
    assert first["user_text"] == "Deploy done"
    assert first["assistant_text"] == ""
    assert "from=alice" in first["observations"]
    assert "channel=deployments" in first["observations"]

    assert second["thread_id"] == "thread-100"
    assert second["user_text"] == ""
    assert second["assistant_text"] == "Health checks are green"

    turns_again = messages_to_turns.convert_rows(rows, default_session_id="messages", default_thread_id="default-thread")
    assert turns == turns_again


def test_scheduled_ingest_idempotency_key_is_deterministic() -> None:
    turn = {
        "session_id": "s1",
        "thread_id": "t1",
        "turn_id": "turn-7",
        "user_text": "Deploy to staging",
        "assistant_text": "",
        "observations": ["build=green", "region=us-east-1"],
    }
    key_a = scheduled_ingest.make_idempotency_key("messages", turn)
    key_b = scheduled_ingest.make_idempotency_key("messages", dict(turn))
    assert key_a == key_b

    changed = dict(turn)
    changed["user_text"] = "Deploy to production"
    key_changed = scheduled_ingest.make_idempotency_key("messages", changed)
    assert key_changed != key_a

    key_other_connector = scheduled_ingest.make_idempotency_key("tickets", turn)
    assert key_other_connector != key_a


def test_scheduled_ingest_cursor_filters_already_seen_turn_signatures() -> None:
    turn_a = {
        "session_id": "git",
        "thread_id": "pr-1",
        "turn_id": "t-1",
        "timestamp": "2026-03-03T13:00:00+00:00",
        "user_text": "first",
        "assistant_text": "",
        "observations": [],
    }
    turn_b = dict(turn_a)
    turn_b["turn_id"] = "t-2"

    state = {"seen_signatures": [scheduled_ingest._turn_signature("git", turn_a)]}  # noqa: SLF001
    seen_list, seen_set = scheduled_ingest._load_seen_signatures(state, limit=1000)  # noqa: SLF001
    assert len(seen_list) == 1

    signatures = []
    fresh: list[dict[str, str]] = []
    for turn in [turn_a, turn_b]:
        sig = scheduled_ingest._turn_signature("git", turn)  # noqa: SLF001
        if sig in seen_set:
            continue
        signatures.append(sig)
        seen_set.add(sig)
        fresh.append(turn)

    assert len(fresh) == 1
    assert fresh[0]["turn_id"] == "t-2"
    appended = scheduled_ingest._append_seen_signatures(seen_list, signatures, limit=1000)  # noqa: SLF001
    assert len(appended) == 2


def test_github_live_connector_fetches_with_cursor_and_converts_turns() -> None:
    tmp = _case_dir("connectors_github_live_")
    config_path = tmp / "github_live.json"
    config_path.write_text(
        json.dumps(
            {
                "repo": "acme/platform",
                "include_commits": True,
                "include_pulls": True,
                "include_issues": True,
                "per_page": 20,
                "max_pages": 2,
            }
        ),
        encoding="utf-8",
    )

    cursor = {
        "commits": {"latest_ts": "2026-03-01T10:00:00Z", "latest_ids": ["sha-old"]},
        "pulls": {"latest_ts": "2026-03-01T09:00:00Z", "latest_ids": ["7"]},
        "issues": {"latest_ts": "2026-03-01T08:00:00Z", "latest_ids": ["4"]},
    }
    calls: list[str] = []

    def fake_requester(url: str, _headers: dict[str, str]) -> tuple[object, dict[str, str]]:
        calls.append(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.path.endswith("/commits"):
            return (
                [
                    {
                        "sha": "sha-new",
                        "commit": {
                            "message": "Fix race in ingest cursor\n\nAdds deterministic ordering.",
                            "author": {"name": "Sam", "email": "sam@example.com", "date": "2026-03-02T12:00:00Z"},
                        },
                        "author": {"login": "sam"},
                    },
                    {
                        "sha": "sha-old",
                        "commit": {
                            "message": "Previously ingested commit",
                            "author": {"name": "Sam", "email": "sam@example.com", "date": "2026-03-01T10:00:00Z"},
                        },
                        "author": {"login": "sam"},
                    },
                ],
                {},
            )
        if parsed.path.endswith("/pulls"):
            return (
                [
                    {
                        "number": 9,
                        "title": "Ship webhook bridge",
                        "body": "Adds file-tail cursor support.",
                        "state": "closed",
                        "merged_at": "2026-03-03T08:30:00Z",
                        "updated_at": "2026-03-03T08:30:00Z",
                        "html_url": "https://example.test/acme/platform/pull/9",
                        "comments": 3,
                        "review_comments": 2,
                        "user": {"login": "nora"},
                    }
                ],
                {},
            )
        if parsed.path.endswith("/issues"):
            return (
                [
                    {
                        "number": 15,
                        "title": "Backfill scheduler cursor state",
                        "body": "Persist connector cursor in scheduler state.",
                        "state": "open",
                        "updated_at": "2026-03-03T09:00:00Z",
                        "comments": 1,
                        "user": {"login": "lee"},
                        "assignee": {"login": "lee"},
                        "labels": [{"name": "ingest"}, {"name": "reliability"}],
                    }
                ],
                {},
            )
        raise AssertionError(f"Unexpected URL: {url}")

    rows, input_rows, next_cursor = github_live_to_turns.load_rows(
        config_path,
        cursor=cursor,
        requester=fake_requester,
    )
    assert input_rows == 4
    assert len(rows) == 3
    assert {row["kind"] for row in rows} == {"commit", "pull_request", "issue"}

    turns = github_live_to_turns.convert_rows(rows, default_session_id="github", default_thread_id="repo-main")
    assert len(turns) == 3
    assert any(turn["turn_id"].startswith("git-") for turn in turns)
    assert any(turn["turn_id"].startswith("git-pr-") for turn in turns)
    assert any(turn["turn_id"].startswith("ticket-") for turn in turns)
    assert any("repository=acme/platform" in turn["observations"] for turn in turns)
    assert next_cursor["commits"]["latest_ts"] == "2026-03-02T12:00:00Z"
    assert "sha-new" in next_cursor["commits"]["latest_ids"]
    assert next_cursor["pulls"]["latest_ts"] == "2026-03-03T08:30:00Z"
    assert "9" in next_cursor["pulls"]["latest_ids"]
    assert next_cursor["issues"]["latest_ts"] == "2026-03-03T09:00:00Z"
    assert "15" in next_cursor["issues"]["latest_ids"]
    assert any("/commits?" in call for call in calls)
    assert any("/pulls?" in call for call in calls)
    assert any("/issues?" in call for call in calls)


def test_webhook_bridge_connector_tails_directory_with_cursor() -> None:
    tmp = _case_dir("connectors_webhook_")
    events_path = tmp / "001_events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "delivery_id": "d-1",
                        "event_type": "issues",
                        "action": "opened",
                        "source": "github_webhook",
                        "timestamp": "2026-03-03T10:00:00Z",
                        "payload": {
                            "issue": {"number": 7, "title": "Rotate credentials", "body": "Rotate before Friday"},
                            "repository": {"full_name": "acme/platform"},
                            "sender": {"login": "nora"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "delivery_id": "d-2",
                        "event_type": "deployments",
                        "action": "succeeded",
                        "source": "deploy_hook",
                        "timestamp": "2026-03-03T10:01:00Z",
                        "text": "Production deploy completed",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows, input_rows, cursor = webhook_to_turns.load_rows(tmp, cursor={})
    assert input_rows == 2
    assert len(rows) == 2

    turns = webhook_to_turns.convert_rows(rows, default_session_id="webhook", default_thread_id="hooks-main")
    turns_again = webhook_to_turns.convert_rows(rows, default_session_id="webhook", default_thread_id="hooks-main")
    assert turns == turns_again
    assert any(turn["thread_id"] == "issue-7" for turn in turns)
    assert any("event=issues" in turn["observations"] for turn in turns)
    assert any("source=github_webhook" in turn["observations"] for turn in turns)

    rows_again, input_rows_again, cursor_again = webhook_to_turns.load_rows(tmp, cursor=cursor)
    assert input_rows_again == 0
    assert rows_again == []
    assert cursor_again["last_file"] == cursor["last_file"]

    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "delivery_id": "d-3",
                    "event_type": "issues",
                    "action": "closed",
                    "source": "github_webhook",
                    "timestamp": "2026-03-03T10:02:00Z",
                    "payload": {"issue": {"number": 7, "title": "Rotate credentials"}},
                }
            )
            + "\n"
        )

    new_rows, new_input_rows, new_cursor = webhook_to_turns.load_rows(tmp, cursor=cursor)
    assert new_input_rows == 1
    assert len(new_rows) == 1
    assert new_cursor["last_line"] > cursor["last_line"]


def test_scheduled_ingest_wires_github_live_connector_cursor_flow() -> None:
    tmp = _case_dir("scheduled_github_live_")
    input_path = tmp / "github_live.json"
    output_path = tmp / "turns.jsonl"
    input_path.write_text(json.dumps({"repo": "acme/platform"}), encoding="utf-8")

    rows = [{"kind": "commit", "sha": "sha-1", "subject": "A", "body": "", "authored_at": "2026-03-03T00:00:00Z"}]
    turns = [
        {
            "session_id": "github_live",
            "thread_id": "repo-main",
            "turn_id": "git-abc",
            "user_text": "A",
            "assistant_text": "",
            "observations": ["repository=acme/platform"],
            "timestamp": "2026-03-03T00:00:00Z",
        }
    ]
    expected_cursor = {"version": 1, "commits": {"latest_ts": "2026-03-03T00:00:00Z", "latest_ids": ["sha-1"]}}

    def fake_load_rows(path: Path, *, cursor: dict[str, object] | None = None, requester: object = None) -> tuple[list[dict[str, object]], int, dict[str, object]]:
        assert path == input_path
        assert cursor == {"checkpoint": "x"}
        return rows, 1, expected_cursor

    def fake_convert_rows(
        _rows: list[dict[str, object]],
        *,
        default_session_id: str,
        default_thread_id: str,
    ) -> list[dict[str, object]]:
        assert default_session_id == "github_live"
        assert default_thread_id == "repo-main"
        return turns

    def fake_write_jsonl(path: Path, out_rows: list[dict[str, object]]) -> None:
        path.write_text("".join(json.dumps(item) + "\n" for item in out_rows), encoding="utf-8")

    with (
        patch.object(github_live_to_turns, "load_rows", side_effect=fake_load_rows),
        patch.object(github_live_to_turns, "convert_rows", side_effect=fake_convert_rows),
        patch.object(github_live_to_turns, "write_jsonl", side_effect=fake_write_jsonl),
    ):
        input_rows, imported_turns, next_cursor = scheduled_ingest._run_import_with_cursor(  # noqa: SLF001
            connector="github_live",
            input_path=input_path,
            output_path=output_path,
            session_id="github_live",
            thread_id="repo-main",
            connector_cursor={"checkpoint": "x"},
        )

    assert input_rows == 1
    assert imported_turns == turns
    assert next_cursor == expected_cursor
    assert output_path.exists()
