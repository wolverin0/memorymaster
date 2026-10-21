"""Disposable regression coverage for the explicitly selected recovery CLI."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.govern.jobs import validator
from memorymaster.stores.storage import SQLiteStore
from scripts import revalidate_supersession_drain as recovery


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(validator, "load_classifier", lambda: None)
    monkeypatch.setenv("MEMORYMASTER_VALIDATION_THRESHOLD", "0.58")
    monkeypatch.delenv("MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES", raising=False)
    result = SQLiteStore(tmp_path / "recovery.db")
    result.init_db()
    return result


def _claim(store, label, **fields):
    return store.create_claim(
        text=f"Evidence-backed recovery fixture {label}. " * 6,
        citations=[CitationInput(source="fixture://recovery", excerpt=label)],
        scope="project:recovery-test",
        confidence=0.95,
        **fields,
    )


def _legacy(store, label, *, linked=True, event_type="transition", reason=None, **fields):
    old = _claim(store, f"old-{label}")
    claim = _claim(store, label, **fields)
    if linked:
        store.mark_superseded(old.id, claim.id, reason="fixture")
    else:
        transition_claim(store, old.id, "archived", reason="fixture")
    transition_claim(store, claim.id, "conflicted", reason="fixture")
    transition_claim(
        store, claim.id, "stale", event_type=event_type,
        reason=reason or "curation_drain: conflicto sin rival confirmado vigente",
    )
    return store.get_claim(claim.id, include_citations=False)


def _snapshot(store):
    with contextlib.closing(sqlite3.connect(store.db_path)) as conn:
        return tuple(conn.iterdump())


def test_default_census_is_read_only_and_matches_exact_legacy_selector(store, monkeypatch, capsys):
    target = _legacy(store, "selected")
    _legacy(store, "unlinked", linked=False)
    _legacy(store, "other event", event_type="validator")
    _legacy(store, "other reason", reason="ordinary decay")
    no_longer_stale = _legacy(store, "archived")
    transition_claim(store, no_longer_stale.id, "archived", reason="fixture")
    store.record_event(
        claim_id=target.id, event_type="transition", to_status="stale",
        details="curation_drain: conflicto sin rival confirmado vigente",
    )
    before = _snapshot(store)
    connect = sqlite3.connect
    opened = []

    def read_only_connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro") and kwargs["uri"] is True
        conn = connect(database_uri, **kwargs)
        opened.append(conn)
        return conn

    def forbidden_store(*args, **kwargs):
        pytest.fail("The census must not construct a store or initialize a service")

    monkeypatch.setattr(sqlite3, "connect", read_only_connect)
    monkeypatch.setattr(SQLiteStore, "__init__", forbidden_store)
    assert recovery.main(["--db", store.db_path]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"count": 1, "human_ids": [target.human_id]}
    assert len(opened) == 1
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert _snapshot(store) == before


def test_census_sets_query_only(store, monkeypatch):
    _legacy(store, "query-only")
    connect = sqlite3.connect
    statements = []

    def trace_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", trace_connect)
    recovery.census(Path(store.db_path))
    assert statements[0] == "PRAGMA query_only = ON"
    assert all(statement.lstrip().upper().startswith(("PRAGMA", "SELECT")) for statement in statements)


def test_db_environment_and_explicit_override(store, tmp_path, monkeypatch, capsys):
    target = _legacy(store, "environment")
    monkeypatch.setenv("MEMORYMASTER_DB", store.db_path)
    assert recovery.main([]) == 0
    assert json.loads(capsys.readouterr().out)["human_ids"] == [target.human_id]
    other = SQLiteStore(tmp_path / "other.db")
    other.init_db()
    assert recovery.main(["--db", other.db_path]) == 0
    assert json.loads(capsys.readouterr().out) == {"count": 0, "human_ids": []}


@pytest.mark.parametrize("arguments", [["--apply"], ["--human-id", "mm-missing"]])
def test_selection_flags_are_required_together(store, arguments):
    before = _snapshot(store)
    with pytest.raises(SystemExit) as exc:
        recovery.main(["--db", store.db_path, *arguments])
    assert exc.value.code == 2
    assert _snapshot(store) == before


def test_missing_database_is_not_created_and_error_omits_path(tmp_path, capsys):
    missing = tmp_path / "private-database.db"
    assert recovery.main(["--db", str(missing)]) == 1
    output = capsys.readouterr()
    assert str(tmp_path) not in output.err
    assert not missing.exists()


def test_unknown_id_rejects_whole_selection_before_any_writes(store, monkeypatch, capsys):
    selected = _legacy(store, "known")
    before = _snapshot(store)

    def forbidden_validator(*args, **kwargs):
        pytest.fail("Unknown selections must be refused before validator writes")

    monkeypatch.setattr(validator, "run", forbidden_validator)
    assert recovery.main([
        "--db", store.db_path, "--apply", "--human-id", selected.human_id,
        "--human-id", "mm-unknown",
    ]) == 1
    assert "uniquely match the census" in capsys.readouterr().err
    assert _snapshot(store) == before


def test_apply_revalidates_only_selected_without_schema_init(store, monkeypatch, capsys):
    selected = _legacy(store, "selected", subject="release", predicate="uses", object_value="new")
    untouched = _legacy(store, "unselected")
    candidate = _claim(store, "unrelated candidate")
    observation = _claim(store, "unrelated observation", claim_type="observation")
    before = {c.id: store.get_claim(c.id, include_citations=False) for c in (untouched, candidate, observation)}
    before_events = {c.id: store.list_events(claim_id=c.id) for c in (untouched, candidate, observation)}

    def forbidden_init(*args, **kwargs):
        pytest.fail("Recovery must never initialize the schema")

    monkeypatch.setattr(SQLiteStore, "init_db", forbidden_init)
    assert recovery.main([
        "--db", store.db_path, "--apply", "--human-id", selected.human_id,
        "--human-id", selected.human_id,
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["count"] == 1
    assert report["result"]["revalidation_processed"] == 1
    assert report["result"]["candidate_processed"] == 0
    assert report["result"]["observation_checked"] == 0
    assert store.get_claim(selected.id).status == "confirmed"
    events = store.list_events(claim_id=selected.id, event_type="validator")
    assert any(event.to_status == "confirmed" for event in events)
    assert all('"actor": "operator"' not in (event.payload_json or "") for event in events)
    for claim_id, old_claim in before.items():
        assert store.get_claim(claim_id, include_citations=False) == old_claim
        assert store.list_events(claim_id=claim_id) == before_events[claim_id]


@pytest.mark.parametrize("fields,hold", [
    ({"source_agent": "llm-stop-hook"}, "held_by_source_policy"),
    ({"subject": None, "predicate": "PROJECT"}, "held_by_structure_policy"),
])
def test_apply_preserves_automatic_promotion_holds(store, fields, hold):
    selected = _legacy(store, "held", **fields)
    result = recovery.apply_selected(Path(store.db_path), [selected.human_id])
    assert result[hold] == 1
    assert result["confirmed"] == 0
    assert store.get_claim(selected.id).status == "stale"


def test_pinned_selection_rejects_entire_batch_without_writes(store):
    ordinary = _legacy(store, "ordinary")
    pinned = _legacy(store, "pinned")
    store.set_pinned(pinned.id, True, reason="operator fixture")
    before = _snapshot(store)
    with pytest.raises(ValueError, match="Pinned"):
        recovery.apply_selected(Path(store.db_path), [ordinary.human_id, pinned.human_id])
    assert _snapshot(store) == before


def test_selection_is_rechecked_before_writes(store, monkeypatch):
    selected = _legacy(store, "raced")
    before = _snapshot(store)
    census = recovery.census
    calls = 0

    def changed_census(path):
        nonlocal calls
        calls += 1
        return census(path) if calls == 1 else []

    monkeypatch.setattr(recovery, "census", changed_census)
    with pytest.raises(ValueError, match="Selection changed"):
        recovery.apply_selected(Path(store.db_path), [selected.human_id])
    assert _snapshot(store) == before
