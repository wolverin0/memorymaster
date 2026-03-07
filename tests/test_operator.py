from __future__ import annotations

import json
import os
import tempfile
from typing import Any
from pathlib import Path

from memorymaster.operator import HeuristicClaimExtractor, MemoryOperator, OperatorConfig, TurnInput, strip_private_blocks
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def test_heuristic_claim_extractor_covers_required_patterns() -> None:
    extractor = HeuristicClaimExtractor()
    text = """
    Support email is help@example.com.
    Release deadline is 2026-04-20.
    HQ address is 123 Main St, Springfield.
    Config path is C:\\work\\repo\\.env.
    token=sk-test-abc123
    API endpoint is https://api.example.com/v1
    """.strip()

    claims = extractor.extract(text)
    assert claims

    predicates = {str(item["predicate"]) for item in claims}
    assert "email" in predicates
    assert "deadline" in predicates
    assert "address" in predicates
    assert "path" in predicates
    assert "token" in predicates

    for item in claims:
        assert set(item.keys()) == {
            "text",
            "subject",
            "predicate",
            "object_value",
            "claim_type",
            "volatility",
            "confidence",
        }


def test_strip_private_blocks_excludes_private_content_from_extraction() -> None:
    extractor = HeuristicClaimExtractor()
    text = """
    <PrIvAtE>
    token=sk-secret-123
    Support email is hidden@example.com
    </PRIVATE>
    Release deadline is 2026-07-15.
    """.strip()

    stripped = strip_private_blocks(text)
    assert "sk-secret-123" not in stripped
    assert "hidden@example.com" not in stripped
    assert "Release deadline is 2026-07-15." in stripped

    claims = extractor.extract(stripped)
    predicates = {str(item["predicate"]) for item in claims}
    objects = {str(item["object_value"]) for item in claims}
    assert "deadline" in predicates
    assert "token" not in predicates
    assert "hidden@example.com" not in objects


def test_process_turn_progressive_retrieval_meta_tiers() -> None:
    db = _case_db("sqlite-operator-progressive")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    operator = MemoryOperator(
        service,
        config=OperatorConfig(
            policy_mode="legacy",
            retrieval_mode="legacy",
            progressive_retrieval=True,
            tier1_limit=4,
            tier2_limit=8,
            state_json_path=None,
        ),
    )

    calls: list[dict[str, Any]] = []
    original_query = service.query

    def spy_query(query_text: str, **kwargs: Any) -> Any:
        calls.append({"query_text": query_text, **kwargs})
        return original_query(query_text, **kwargs)

    service.query = spy_query  # type: ignore[method-assign]

    empty_db_turn = TurnInput(
        session_id="s1",
        thread_id="t1",
        turn_id="turn-empty",
        user_text="Project deadline is 2026-07-15",
        assistant_text="Noted.",
        observations=[],
        timestamp="2026-03-02T12:00:00+00:00",
    )
    first_summary = operator.process_turn(empty_db_turn)
    assert first_summary["retrieval_meta"] == {"mode": "progressive", "tier_used": "tier2", "rows": 0}
    assert len(calls) == 2
    assert calls[0]["include_stale"] is False
    assert calls[0]["include_conflicted"] is False
    assert calls[0]["limit"] == 4
    assert calls[1]["include_stale"] is True
    assert calls[1]["include_conflicted"] is True
    assert calls[1]["limit"] == 8

    calls.clear()
    one_db_turn = TurnInput(
        session_id="s1",
        thread_id="t1",
        turn_id="turn-one-db",
        user_text="Project deadline is 2026-07-15",
        assistant_text="Please remind me next week.",
        observations=[],
        timestamp="2026-03-02T12:01:00+00:00",
    )
    second_summary = operator.process_turn(one_db_turn)
    assert second_summary["retrieval_meta"]["mode"] == "progressive"
    assert second_summary["retrieval_meta"]["tier_used"] == "tier1"
    assert int(second_summary["retrieval_meta"]["rows"]) >= 1
    assert len(calls) == 1


def test_memory_operator_process_turn_conflict_resolution() -> None:
    db = _case_db("sqlite-operator")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    config = OperatorConfig(
        policy_mode="legacy",
        retrieval_mode="legacy",
        retrieval_limit=10,
        min_citations=1,
        min_score=0.95,
        compact_every=0,
        state_json_path=None,
    )
    operator = MemoryOperator(service, config=config)

    first = TurnInput(
        session_id="s1",
        thread_id="t1",
        turn_id="turn-1",
        user_text="Support email is old@example.com.",
        assistant_text="Acknowledged.",
        observations=[],
        timestamp="2026-03-02T12:00:00+00:00",
    )
    first_summary = operator.process_turn(first)
    assert first_summary["extracted"]
    assert first_summary["ingested"]

    operator.config.min_score = 0.58
    second = TurnInput(
        session_id="s1",
        thread_id="t1",
        turn_id="turn-2",
        user_text="Correction support email is new@example.com.",
        assistant_text="I will use the new address.",
        observations=[],
        timestamp="2026-03-02T12:01:00+00:00",
    )
    second_summary = operator.process_turn(second)
    assert second_summary["ingested"]

    claims = service.list_claims(limit=25, include_archived=True)
    by_value = {claim.object_value: claim for claim in claims if claim.object_value}

    assert "new@example.com" in by_value
    assert by_value["new@example.com"].status == "confirmed"
    assert "old@example.com" in by_value
    assert by_value["old@example.com"].status == "conflicted"


def test_run_stream_counts_seen_events_and_handles_utf8_bom() -> None:
    db = _case_db("sqlite-operator-stream")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    log_path = Path(".tmp_cases") / "operator_stream_events.jsonl"
    state_path = Path(".tmp_cases") / "operator_stream_state.json"
    log_path.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    operator = MemoryOperator(
        service,
        config=OperatorConfig(
            policy_mode="legacy",
            log_jsonl_path=str(log_path),
            state_json_path=str(state_path),
        ),
    )

    inbox = Path(".tmp_cases") / "operator_stream_test.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        "\ufeff"
        + '{"session_id":"s1","thread_id":"t1","turn_id":"turn-1","user_text":"Support email is test@example.com","assistant_text":"","observations":[]}\n'
        + "not-json\n",
        encoding="utf-8",
    )

    summary = operator.run_stream(inbox, poll_seconds=0.05, max_events=2)
    assert summary["seen_events"] == 2
    assert summary["processed_events"] == 1
    assert summary["json_errors"] == 1
    assert summary["exit_reason"] == "max_events_reached"
    assert "log_jsonl" in summary
    assert summary["turns"][0]["retrieval_mode"] in {"progressive", "single"}
    assert summary["turns"][0]["retrieval_tier"] in {"tier1", "tier2", "single"}
    assert isinstance(summary["turns"][0]["retrieval_rows"], int)
    assert log_path.exists()
    events = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any('"event": "stream_start"' in line for line in events)
    assert any('"event": "turn_processed"' in line for line in events)
    assert any('"retrieval_tier": "tier' in line or '"retrieval_tier": "single"' in line for line in events)
    assert any('"event": "json_error"' in line for line in events)
    assert any('"event": "stream_exit"' in line for line in events)


def test_run_stream_exits_on_max_idle_seconds() -> None:
    db = _case_db("sqlite-operator-idle")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    operator = MemoryOperator(
        service,
        config=OperatorConfig(
            policy_mode="legacy",
            max_idle_seconds=0.25,
            log_jsonl_path=None,
            state_json_path=None,
        ),
    )

    inbox = Path(".tmp_cases") / "operator_stream_idle.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("", encoding="utf-8")

    summary = operator.run_stream(inbox, poll_seconds=0.05)
    assert summary["processed_events"] == 0
    assert summary["seen_events"] == 0
    assert summary["json_errors"] == 0
    assert summary["exit_reason"] == "idle_timeout"


def test_run_stream_supports_events_and_messages_shapes() -> None:
    db = _case_db("sqlite-operator-schema-shapes")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    operator = MemoryOperator(
        service,
        config=OperatorConfig(
            policy_mode="legacy",
            log_jsonl_path=None,
            state_json_path=None,
        ),
    )

    inbox = Path(".tmp_cases") / "operator_stream_schema_shapes.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        '{"session_id":"s1","thread_id":"t1","turn_id":"evt-1","events":[{"role":"user","text":"Support email is events@example.com"},{"role":"assistant","text":"ok"},{"role":"tool","text":"tool output"}]}\n'
        + '{"session_id":"s1","thread_id":"t1","messages":[{"role":"user","content":[{"type":"text","text":"Release deadline is 2026-05-01"}]},{"role":"assistant","content":"noted"}]}\n',
        encoding="utf-8",
    )

    summary = operator.run_stream(inbox, poll_seconds=0.05, max_events=2)
    assert summary["processed_events"] == 2
    assert summary["seen_events"] == 2
    assert summary["json_errors"] == 0
    assert summary["turns"][0]["turn_id"] == "evt-1"
    assert str(summary["turns"][1]["turn_id"]).startswith("turn-")

    claims = service.list_claims(limit=50, include_archived=True)
    objects = {claim.object_value for claim in claims if claim.object_value}
    assert "events@example.com" in objects
    assert "2026-05-01" in objects


def test_run_stream_resumes_from_checkpoint_state() -> None:
    db = _case_db("sqlite-operator-resume")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    base = Path(".tmp_cases")
    base.mkdir(parents=True, exist_ok=True)
    inbox = base / "operator_stream_resume.jsonl"
    state_path = base / "operator_stream_resume_state.json"
    log_path = base / "operator_stream_resume_events.jsonl"
    state_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    inbox.write_text(
        '{"session_id":"s1","thread_id":"t1","turn_id":"turn-1","user_text":"Support email is first@example.com","assistant_text":"","observations":[]}\n'
        + '{"session_id":"s1","thread_id":"t1","turn_id":"turn-2","user_text":"Support email is second@example.com","assistant_text":"","observations":[]}\n',
        encoding="utf-8",
    )

    config = OperatorConfig(
        policy_mode="legacy",
        log_jsonl_path=str(log_path),
        state_json_path=str(state_path),
    )

    first_run = MemoryOperator(service, config=config).run_stream(inbox, poll_seconds=0.05, max_events=1)
    assert first_run["processed_events"] == 1
    assert first_run["seen_events"] == 1
    assert first_run["start_offset"] == 0
    assert first_run["final_offset"] > 0
    assert first_run["turns"][0]["turn_id"] == "turn-1"

    second_run = MemoryOperator(service, config=config).run_stream(inbox, poll_seconds=0.05, max_events=1)
    assert second_run["processed_events"] == 1
    assert second_run["seen_events"] == 1
    assert second_run["start_offset"] == first_run["final_offset"]
    assert second_run["final_offset"] > second_run["start_offset"]
    assert second_run["turns"][0]["turn_id"] == "turn-2"

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["offset"] == second_run["final_offset"]
    assert state["seen_events"] == 2
    assert state["processed_events"] == 2


def test_run_stream_handles_corrupt_state_file() -> None:
    db = _case_db("sqlite-operator-state-corrupt")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    base = Path(".tmp_cases")
    base.mkdir(parents=True, exist_ok=True)
    inbox = base / "operator_stream_state_corrupt.jsonl"
    state_path = base / "operator_stream_state_corrupt.json"
    log_path = base / "operator_stream_state_corrupt_events.jsonl"
    log_path.unlink(missing_ok=True)
    inbox.write_text(
        '{"session_id":"s1","thread_id":"t1","turn_id":"turn-1","user_text":"Support email is test@example.com","assistant_text":"","observations":[]}\n',
        encoding="utf-8",
    )
    state_path.write_text("{invalid-json", encoding="utf-8")

    operator = MemoryOperator(
        service,
        config=OperatorConfig(
            policy_mode="legacy",
            log_jsonl_path=str(log_path),
            state_json_path=str(state_path),
        ),
    )

    summary = operator.run_stream(inbox, poll_seconds=0.05, max_events=1)
    assert summary["start_offset"] == 0
    assert summary["processed_events"] == 1
    assert summary["seen_events"] == 1
    assert summary["exit_reason"] == "max_events_reached"

    events = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any('"event": "state_error"' in line for line in events)
    assert any('"event": "state_saved"' in line for line in events)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["offset"] == summary["final_offset"]

