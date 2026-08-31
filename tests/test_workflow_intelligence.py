from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.workflow_intelligence.analysis import analyze_session
from memorymaster.workflow_intelligence.candidates import (
    refresh_candidates,
    review_candidate,
    write_proposal,
)
from memorymaster.workflow_intelligence.classification import classify_pending
from memorymaster.workflow_intelligence.models import (
    ActionRecord,
    FeedbackRecord,
    SessionRecord,
    TurnRecord,
)
from memorymaster.workflow_intelligence.redaction import public_excerpt
from memorymaster.workflow_intelligence.report import build_report, write_report
from memorymaster.workflow_intelligence.scanner import WorkflowScanner
from memorymaster.workflow_intelligence.storage import TABLES, WorkflowStore


def _jsonl(path: Path, rows: list[dict], *, partial: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row) + "\n" for row in rows) + partial
    path.write_text(body, encoding="utf-8")


def test_sidecar_schema_is_wal_and_complete(tmp_path: Path) -> None:
    db = tmp_path / "workflow.db"
    store = WorkflowStore(db)
    try:
        names = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert TABLES.issubset(names)
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.db_path == db
    finally:
        store.close()


def test_public_excerpt_redacts_paths_secrets_and_private_ips() -> None:
    text = (
        r"Open C:\Users\person\private\notes.txt on 192.168.1.20 "
        "with api_key=sk-test_12345678901234567890"
    )
    excerpt = public_excerpt(text, limit=400)
    assert "person" not in excerpt
    assert "192.168.1.20" not in excerpt
    assert "sk-test" not in excerpt
    assert len(excerpt) <= 400


def test_scanner_indexes_all_metadata_and_deep_parses_human_sessions(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    workspace = tmp_path / "workspace"
    transcript = claude / "projects" / "demo" / "root-session.jsonl"
    subagent = claude / "projects" / "demo" / "subagents" / "agent-a.jsonl"
    rollout = codex / "sessions" / "2026" / "01" / "02" / "rollout-root.jsonl"
    history = codex / "history.jsonl"

    _jsonl(
        transcript,
        [
            {
                "type": "user",
                "sessionId": "claude-root",
                "cwd": str(workspace / "demo"),
                "timestamp": "2026-01-02T10:00:00Z",
                "message": {"role": "user", "content": "Fix the login bug and test it"},
            },
            {
                "type": "assistant",
                "sessionId": "claude-root",
                "timestamp": "2026-01-02T10:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "app.py"}},
                        {"type": "text", "text": "I will inspect the call sites."},
                    ],
                },
            },
            {
                "type": "assistant",
                "sessionId": "claude-root",
                "timestamp": "2026-01-02T10:02:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "app.py"}}
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "claude-root",
                "timestamp": "2026-01-02T10:03:00Z",
                "message": {"role": "user", "content": "No, check the actual code path first."},
            },
        ],
    )
    _jsonl(
        subagent,
        [{"type": "user", "sessionId": "claude-child", "message": {"role": "user", "content": "Explore"}}],
    )
    _jsonl(
        rollout,
        [
            {
                "timestamp": "2026-01-02T11:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-root",
                    "cwd": str(workspace / "api"),
                    "model_provider": "openai",
                    "source": "cli",
                },
            },
            {
                "timestamp": "2026-01-02T11:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Refactor the API"}],
                },
            },
            {
                "timestamp": "2026-01-02T11:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "python -m pytest"}),
                },
            },
            {
                "timestamp": "2026-01-02T11:03:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "exit=0",
                },
            },
        ],
        partial='{"type":"response_item"',
    )
    _jsonl(history, [{"session_id": "codex-root", "ts": 1, "text": "Refactor the API"}])

    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        result = WorkflowScanner(
            store,
            claude_root=claude,
            codex_root=codex,
            workspace_root=workspace,
        ).scan(deep="human")

        assert result["source_files"] == 4
        assert result["deep_sessions"] == 2
        sessions = store.session_rows()
        by_external = {row["external_id"]: row for row in sessions}
        assert by_external["claude-root"]["session_kind"] == "human"
        assert by_external["claude-child"]["session_kind"] == "subagent"
        assert by_external["claude-child"]["deep_parsed"] == 0
        assert by_external["codex-root"]["provider"] == "codex"
        codex_source = store.connection.execute(
            "SELECT source_path FROM source_files WHERE id=?",
            (by_external["codex-root"]["source_file_id"],),
        ).fetchone()
        assert Path(codex_source["source_path"]).name == "rollout-root.jsonl"
        feedback = store.rows("feedback")
        assert feedback[0]["theme"] == "research_before_editing"
        assert "actual code path" in feedback[0]["excerpt"]
        actions = store.rows("actions")
        assert {row["kind"] for row in actions} >= {"read", "mutation", "verification"}
        assert len(store.rows("episodes")) == 3
        assert {row["signal"] for row in store.rows("outcome_signals")} >= {
            "completion:implemented", "completion:locally_verified", "user_correction",
        }
        indexed = {Path(row["source_path"]).name: row for row in store.rows("source_files")}
        assert indexed["rollout-root.jsonl"]["cursor_offset"] < rollout.stat().st_size

        cached = WorkflowScanner(
            store,
            claude_root=claude,
            codex_root=codex,
            workspace_root=workspace,
        ).scan(deep="human")
        assert cached["sessions"] == result["sessions"]
        assert cached["deep_sessions"] == result["deep_sessions"]
    finally:
        store.close()


def test_selected_deep_parse_includes_explicit_subagent(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    child = claude / "projects" / "demo" / "subagents" / "child.jsonl"
    _jsonl(
        child,
        [{"type": "user", "sessionId": "child-id", "message": {"role": "user", "content": "Inspect tests"}}],
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        scanner = WorkflowScanner(store, claude_root=claude, codex_root=tmp_path / "none")
        scanner.scan(deep="selected", session_ids=["child-id"])
        row = store.session_rows()[0]
        assert row["session_kind"] == "subagent"
        assert row["deep_parsed"] == 1
    finally:
        store.close()


def test_analysis_detects_retry_premature_edit_and_verification_gap() -> None:
    session = SessionRecord(
        session_id="session-hash",
        external_id="root",
        provider="codex",
        session_kind="human",
        project_scope="project:demo",
    )
    turns = [
        TurnRecord("t1", "session-hash", 1, "user", "Fix it", "2026-01-01T00:00:00Z", 0, 10),
        TurnRecord("t2", "session-hash", 2, "assistant", "Done and working.", "2026-01-01T00:01:00Z", 11, 30),
    ]
    actions = [
        ActionRecord("a1", "session-hash", "t2", 1, "mutation", "apply_patch", "success", "patch"),
        ActionRecord("a2", "session-hash", "t2", 2, "command", "exec_command", "failed", "pytest"),
        ActionRecord("a3", "session-hash", "t2", 3, "command", "exec_command", "failed", "pytest"),
    ]

    result = analyze_session(session, turns, actions, [])

    assert result["mutation_before_research"] is True
    assert result["retry_loops"] == 1
    assert result["completion_state"] == "implemented"
    assert result["verification_tier"] == "none"
    assert "completion_without_verification" in result["flags"]


def test_report_is_self_contained_and_does_not_disclose_source_paths(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        store.upsert_session(
            SessionRecord(
                session_id="safe-hash",
                external_id="visible-id",
                provider="claude",
                session_kind="human",
                project_scope="project:demo",
                initial_request_excerpt="Fix the issue",
            ),
            source_file_id=None,
        )
        report = build_report(store)
        output = write_report(report, tmp_path / "reports" / "run-1")
        html = output["html"].read_text(encoding="utf-8")
        payload = json.loads(output["json"].read_text(encoding="utf-8"))
        assert "<script src=" not in html
        assert "<link rel=" not in html
        assert str(tmp_path) not in html
        assert payload["schema_version"] == "memorymaster.workflow-intelligence.v1"
        assert payload["dataset"]["human_sessions"] == 1
    finally:
        store.close()


def test_classification_is_explicit_bounded_and_records_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        store.upsert_session(
            SessionRecord(
                session_id="s1",
                external_id="root",
                provider="codex",
                session_kind="human",
                project_scope="project:demo",
                initial_request_excerpt="Debug the failing deployment",
            ),
            source_file_id=None,
        )
        seen: list[str] = []

        def fake_call(prompt: str, text: str) -> str:
            seen.append(text)
            return json.dumps(
                {
                    "task_category": "devops",
                    "outcome": "unknown",
                    "confidence": 0.7,
                    "rationale": "Deployment debugging request",
                }
            )

        monkeypatch.setattr("memorymaster.workflow_intelligence.classification.llm_provider.call_llm", fake_call)
        result = classify_pending(store, limit=1)
        row = store.session_rows()[0]
        assert result["classified"] == 1
        assert len(seen[0]) <= 4_000
        assert row["task_category"] == "devops"
        assert row["classification_provider"]
        assert row["classification_prompt_hash"]
        assert row["classification_authoritative"] == 0
    finally:
        store.close()


def test_candidates_require_three_roots_and_two_projects_for_global(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        for index, scope in enumerate(("project:a", "project:a", "project:b"), start=1):
            session_id = f"s{index}"
            store.upsert_session(
                SessionRecord(session_id, f"external-{index}", "codex", "human", scope),
                source_file_id=None,
            )
            turn = TurnRecord(f"t{index}", session_id, 2, "user", "Test it before claiming done", "", 0, 1)
            feedback = FeedbackRecord(
                f"f{index}", session_id, turn.turn_id, "correction", "verification_missing",
                turn.excerpt, 0.9, True,
            )
            store.replace_details(session_id, [turn], [], [feedback])

        result = refresh_candidates(store)
        rows = {row["scope"]: row for row in store.rows("candidates")}
        assert result["groups"] == 3
        assert rows["project:a"]["status"] == "watch"
        assert rows["user"]["status"] == "proposed"
        assert rows["user"]["support_count"] == 3
        review = review_candidate(store, rows["user"]["candidate_id"], "accept_pattern")
        assert review["status"] == "reviewed"
        proposal = write_proposal(store, rows["user"]["candidate_id"], tmp_path / "proposal.json")
        payload = json.loads(proposal.read_text(encoding="utf-8"))
        assert payload["inert"] is True
        assert payload["promotion"]["automatic"] is False
    finally:
        store.close()
