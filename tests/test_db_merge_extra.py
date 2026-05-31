"""Extra coverage for memorymaster.db_merge edge paths.

Each test anchors on a merge INVARIANT (why the branch exists), not just the
line it touches:

- A missing source DB must fail loudly, never silently no-op.
- Unparseable / naive / empty timestamps must never crash reconciliation and
  must order deterministically (the watermark used for "newer wins").
- Dedup must fall back to text-hash when no idempotency_key is present, so the
  same fact is reconciled (not duplicated) even when keys differ.
- A claim that cannot be inserted into the target must be counted as an error,
  never silently dropped from the stats contract.
- Missing optional columns (citations table, conflict columns) must degrade
  gracefully so merge works across schema-version drift.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from memorymaster import db_merge
from memorymaster.db_merge import (
    _parse_timestamp,
    merge_databases,
)
from test_db_merge_coverage_v2 import (
    _claims,
    _init_db,
    _insert_claim,
    _snapshot,
)


# ---------------------------------------------------------------------------
# Source DB precondition: missing file must raise, never silently no-op.
# ---------------------------------------------------------------------------
def test_missing_source_db_raises_file_not_found(tmp_path):
    target = tmp_path / "target.db"
    _init_db(target)

    with pytest.raises(FileNotFoundError):
        merge_databases(str(target), str(tmp_path / "does-not-exist.db"))


# ---------------------------------------------------------------------------
# Watermark parsing: the timestamp drives "newer wins" so every malformed
# shape must degrade to a deterministic floor instead of throwing.
# ---------------------------------------------------------------------------
def test_parse_timestamp_empty_value_is_floor():
    floor = datetime.min.replace(tzinfo=timezone.utc)
    assert _parse_timestamp(None) == floor
    assert _parse_timestamp("") == floor


def test_parse_timestamp_unparseable_value_is_floor():
    floor = datetime.min.replace(tzinfo=timezone.utc)
    assert _parse_timestamp("not-a-timestamp") == floor


def test_parse_timestamp_naive_value_gets_utc_tzinfo():
    parsed = _parse_timestamp("2026-05-11T12:00:00")
    assert parsed.tzinfo == timezone.utc
    assert parsed == datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def test_unparseable_target_updated_at_does_not_block_newer_source(tmp_path):
    """A corrupt local watermark parses to the floor, so a real source
    timestamp must clear the reconcile threshold and win."""
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(
        target,
        text="Watermark claim",
        idempotency_key="wm",
        confidence=0.1,
        updated_at="garbage-timestamp",  # parses to datetime.min floor
    )
    _insert_claim(
        source,
        text="Watermark claim",
        idempotency_key="wm",
        confidence=0.9,
        updated_at="2026-05-11T13:00:00+00:00",  # strictly above the floor
    )

    merge_databases(str(target), str(source))

    row = {r["idempotency_key"]: r for r in _claims(target)}["wm"]
    assert row["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Dedup fallback: without idempotency_key, identical text must reconcile
# against the existing target claim (text-hash path), not duplicate it.
# ---------------------------------------------------------------------------
def test_text_hash_fallback_dedups_without_idempotency_key(tmp_path):
    """Two claims with NO idempotency_key but identical (case/space-normalized)
    text must dedup via the text-hash fallback, never duplicate the fact."""
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    # No idempotency keys on either side; only the normalized text matches.
    _insert_claim(target, text="Same fact, no key", idempotency_key=None)
    _insert_claim(
        source,
        text="  SAME FACT, NO KEY  ",  # hash normalizes case + whitespace
        idempotency_key=None,
    )

    stats = merge_databases(str(target), str(source))
    rows = _claims(target)

    # Matched by text hash -> skipped, so the target keeps a single row.
    assert stats["scanned"] == 1
    assert stats["skipped"] == 1
    assert stats["merged"] == 0
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Stats contract: a claim that fails to insert is an ERROR, never dropped.
# ---------------------------------------------------------------------------
def test_failed_insert_is_counted_as_error(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(source, text="Will fail to insert", idempotency_key="boom")

    monkeypatch.setattr(
        db_merge,
        "_insert_claim_into_target",
        lambda *args, **kwargs: None,
    )

    stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 1, "merged": 0, "skipped": 0, "errors": 1}
    assert _claims(target) == []


# ---------------------------------------------------------------------------
# Schema drift: missing citations table must not abort the merge.
# ---------------------------------------------------------------------------
def test_missing_citations_table_does_not_abort_merge(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    # Drop citations on BOTH sides to exercise the OperationalError guards.
    for db in (source, target):
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE citations")
    _insert_claim(source, text="No citations table here", idempotency_key="nocite")

    stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert [r["text"] for r in _claims(target)] == ["No citations table here"]


# ---------------------------------------------------------------------------
# Conflict detection requires subject+predicate; a null tuple must be ignored
# (no conflict resolution attempted), and the claim still merges normally.
# ---------------------------------------------------------------------------
def test_null_subject_predicate_skips_conflict_resolution(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(
        source,
        text="Unstructured note",
        idempotency_key="unstructured",
        subject=None,
        predicate=None,
        object_value=None,
    )

    stats = merge_databases(str(target), str(source))
    rows = _claims(target)

    assert stats == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert rows[0]["status"] == "candidate"


# ---------------------------------------------------------------------------
# Missing idempotency_key on a NEW claim must get a synthesized merge- key so
# a re-merge is idempotent (the watermark/dedup invariant holds on round two).
# ---------------------------------------------------------------------------
def test_keyless_new_claim_gets_synthetic_key_and_is_idempotent(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(source, text="Keyless remote fact", idempotency_key=None)

    first = merge_databases(str(target), str(source))
    after_first = _snapshot(target)
    second = merge_databases(str(target), str(source))

    assert first == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert second == {"scanned": 1, "merged": 0, "skipped": 1, "errors": 0}
    merged_key = _claims(target)[0]["idempotency_key"]
    assert merged_key.startswith("merge-")
    assert _snapshot(target) == after_first


# ---------------------------------------------------------------------------
# Reconcile guard: when the target lacks the columns reconcile needs, a
# duplicate skip must still be safe (no crash, no change).
# ---------------------------------------------------------------------------
def test_reconcile_noop_when_target_missing_reconcile_columns(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    # Minimal target: has text + idempotency_key for dedup but no confidence.
    with sqlite3.connect(target) as conn:
        conn.execute(
            "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT NOT NULL, "
            "idempotency_key TEXT UNIQUE)"
        )
        conn.execute(
            "INSERT INTO claims (text, idempotency_key) VALUES ('Dup fact', 'dup')"
        )
    _insert_claim(source, text="Dup fact", idempotency_key="dup", confidence=0.99)

    stats = merge_databases(str(target), str(source))
    rows = _claims(target)

    # Matched as duplicate -> skipped; reconcile is a no-op (no confidence col).
    assert stats == {"scanned": 1, "merged": 0, "skipped": 1, "errors": 0}
    assert len(rows) == 1
