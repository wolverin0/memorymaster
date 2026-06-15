"""Export of mined rule-shaped claims (v3.28).

`export-rules` CLI / `rules_export` MCP both delegate to
:mod:`memorymaster.knowledge.rule_export`. These tests anchor on the requirement: the
export enumerates rule claims, filters by min-confidence + status, renders the
three formats with the documented columns, and never leaks sensitive rules.
"""
from __future__ import annotations

import csv
import io
import json

import pytest

from memorymaster.knowledge import rule_export
from memorymaster.core.models import CitationInput
from memorymaster.knowledge.rules import build_rule_fields
from memorymaster.core.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    service = MemoryService(tmp_path / "mm.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _ingest_rule(service, trigger, action, *, confidence, status=None, rationale="because"):
    claim = service.ingest(
        **build_rule_fields(trigger, action, rationale),
        citations=[CitationInput(source="verbatim", locator=f"rule-{trigger}")],
        scope="project:test",
        confidence=confidence,
        source_agent="rule-miner",
    )
    if status and status != "candidate":
        claim = service.store.apply_status_transition(
            claim, to_status=status, reason="test", event_type="validator"
        )
    return claim


def test_collect_filters_by_min_confidence(svc):
    _ingest_rule(svc, "low rule", "do low", confidence=0.40)
    _ingest_rule(svc, "high rule", "do high", confidence=0.70)

    rows = rule_export.collect_rules(svc, min_confidence=0.5)
    triggers = {r["trigger"] for r in rows}
    assert triggers == {"high rule"}
    assert rows[0]["confidence"] == 0.70


def test_collect_filters_by_status(svc):
    _ingest_rule(svc, "cand rule", "do c", confidence=0.6)  # candidate
    _ingest_rule(svc, "conf rule", "do f", confidence=0.6, status="confirmed")

    rows = rule_export.collect_rules(svc, status="confirmed")
    assert {r["trigger"] for r in rows} == {"conf rule"}
    assert rows[0]["status"] == "confirmed"


def test_collect_only_returns_rule_typed_claims(svc):
    _ingest_rule(svc, "real rule", "do it", confidence=0.6)
    # A plain descriptive claim must NOT appear in the rule export.
    svc.ingest(
        "The API uses bearer tokens.",
        citations=[CitationInput(source="doc", locator="x")],
        scope="project:test",
        confidence=0.9,
    )
    rows = rule_export.collect_rules(svc)
    assert {r["trigger"] for r in rows} == {"real rule"}


def test_export_carries_correction_count(svc):
    """A rule whose fingerprint has a rule_stats tally reports that count."""
    import sqlite3

    from memorymaster.knowledge.rule_miner import rule_fingerprint

    _ingest_rule(svc, "repeat rule", "do repeat", confidence=0.7)
    fp = rule_fingerprint("repeat rule", "do repeat")
    conn = sqlite3.connect(svc.store.db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rule_stats "
            "(rule_fingerprint TEXT PRIMARY KEY, correction_count INTEGER NOT NULL DEFAULT 1, "
            "last_mined TEXT NOT NULL, confidence_at_last_mine REAL)"
        )
        conn.execute(
            "INSERT INTO rule_stats(rule_fingerprint, correction_count, last_mined) VALUES (?, 3, ?)",
            (fp, "2026-05-30T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    rows = rule_export.collect_rules(svc)
    assert rows[0]["correction_count"] == 3


def test_render_json_shape(svc):
    _ingest_rule(svc, "json rule", "do json", confidence=0.6)
    rows = rule_export.collect_rules(svc)
    out = rule_export.render_rules(rows, "json")
    parsed = json.loads(out)
    assert parsed[0]["trigger"] == "json rule"
    assert set(rule_export._EXPORT_FIELDS).issubset(parsed[0].keys())


def test_render_csv_shape(svc):
    _ingest_rule(svc, "csv rule", "do csv", confidence=0.6)
    rows = rule_export.collect_rules(svc)
    out = rule_export.render_rules(rows, "csv")
    reader = list(csv.DictReader(io.StringIO(out)))
    assert reader[0]["trigger"] == "csv rule"
    assert reader[0]["confidence"] == "0.6"


def test_render_markdown_shape(svc):
    _ingest_rule(svc, "md rule", "do md", confidence=0.6)
    rows = rule_export.collect_rules(svc)
    out = rule_export.render_rules(rows, "markdown")
    lines = out.splitlines()
    assert lines[0].startswith("| claim_id |")
    assert "md rule" in out
    assert out.isascii(), "markdown export must be cp1252-console-safe ASCII"


def test_render_rejects_unknown_format(svc):
    with pytest.raises(ValueError, match="format must be"):
        rule_export.render_rules([], "yaml")


def test_export_drops_sensitive_rules(svc):
    """A rule whose persisted text the sensitivity layer flags must not leak via
    the default (allow_sensitive=False) export."""
    _ingest_rule(svc, "safe rule", "do safe", confidence=0.6)
    leak = "ghp_" + "B" * 36
    svc.ingest(
        **build_rule_fields("danger", "use token here", rationale=f"token {leak}"),
        citations=[CitationInput(source="verbatim", locator="leak")],
        scope="project:test",
        confidence=0.6,
        source_agent="rule-miner",
        visibility="sensitive",
    )
    rows = rule_export.collect_rules(svc, allow_sensitive=False)
    triggers = {r["trigger"] for r in rows}
    assert "danger" not in triggers
    assert "safe rule" in triggers
