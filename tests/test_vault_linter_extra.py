"""Coverage hardening for memorymaster.knowledge.vault_linter detection branches.

The existing suite (test_vault_linter_orphan.py) only exercises the
orphan-*article* path. These tests cover the *claim-level* detectors that
`lint_vault` aggregates — contradictions, weak-link orphans, entity gaps,
stale claims — plus the empty-DB short-circuit, the scope filter, and the
LLM contradiction-verification hook.

Each test anchors on WHY the signal exists (truth drift, dead-end claims,
promised-but-missing entities, decayed low-confidence facts) rather than on
incidental wording, so it survives message tweaks but fails if the detector's
contract regresses.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.knowledge.vault_linter import lint_vault


def _fresh_db(tmp_path: Path) -> Path:
    from memorymaster.storage import SQLiteStore

    db = tmp_path / "memory.db"
    SQLiteStore(str(db)).init_db()
    return db


def _insert(
    db: Path,
    *,
    text: str,
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
    scope: str = "project:test",
    status: str = "candidate",
    confidence: float = 0.8,
    updated_at: str = "2026-01-01",
) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """INSERT INTO claims (
                   text, claim_type, subject, predicate, object_value, scope,
                   status, confidence, created_at, updated_at, valid_from, tier
               ) VALUES (?, 'fact', ?, ?, ?, ?, ?, ?, '2026-01-01', ?,
                         '2026-01-01', 'working')""",
            (
                text,
                subject,
                predicate,
                object_value,
                scope,
                status,
                confidence,
                updated_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _lint(db: Path) -> dict:
    # wiki_root points at a non-existent dir so the article-level checks stay
    # inert and the assertions isolate the claim-level detectors.
    return lint_vault(
        str(db),
        verify_with_llm=False,
        wiki_root=Path(str(db.parent / "no_such_wiki")),
    )


def test_empty_db_short_circuits_with_zero_claims(tmp_path: Path) -> None:
    # WHY: a fresh/empty memory must lint cleanly (no crash, claims=0) so the
    # CLI and cron callers do not treat "nothing to check" as an error.
    db = _fresh_db(tmp_path)

    report = _lint(db)

    assert report["claims"] == 0
    assert report["contradictions"] == []
    assert report["orphans"] == []
    assert report["gaps"] == []
    assert report["stale"] == []


def test_contradiction_detected_for_same_subject_predicate(tmp_path: Path) -> None:
    # WHY: two confident claims with the SAME subject+predicate but DIFFERENT
    # object_value are mutually exclusive truths; surfacing them is the whole
    # point of the linter — silent disagreement corrupts recall.
    db = _fresh_db(tmp_path)
    _insert(db, text="db is sqlite", subject="db", predicate="is", object_value="sqlite", confidence=0.9)
    _insert(db, text="db is postgres", subject="db", predicate="is", object_value="postgres", confidence=0.6)

    report = _lint(db)

    keys = {c["key"] for c in report["contradictions"]}
    assert "db|is" in keys
    contradiction = next(c for c in report["contradictions"] if c["key"] == "db|is")
    # Claims are sorted by descending confidence so the strongest reads first.
    assert contradiction["claims"][0]["confidence"] >= contradiction["claims"][1]["confidence"]


def test_agreeing_claims_are_not_a_contradiction(tmp_path: Path) -> None:
    # WHY: same subject+predicate+value is reinforcement, not conflict — the
    # detector must NOT raise a false positive on agreement.
    db = _fresh_db(tmp_path)
    _insert(db, text="db is sqlite", subject="db", predicate="is", object_value="sqlite")
    _insert(db, text="db is sqlite again", subject="db", predicate="is", object_value="sqlite")

    report = _lint(db)

    assert all(c["key"] != "db|is" for c in report["contradictions"])


def test_orphan_claim_with_no_subject_or_predicate(tmp_path: Path) -> None:
    # WHY: a claim with neither subject nor predicate is a dead-end node — it
    # can never be linked into the knowledge graph and must be flagged.
    db = _fresh_db(tmp_path)
    _insert(db, text="some floating observation", subject=None, predicate=None)

    report = _lint(db)

    assert any(o["type"] == "orphan" for o in report["orphans"])


def test_single_mention_subject_is_weak_link_orphan(tmp_path: Path) -> None:
    # WHY: a "weak-link orphan" is a subject that appears in exactly ONE claim —
    # a topic mentioned once and never connected to anything else. vault_linter's
    # documented contract is "subject appears only once". This test anchors that
    # REQUIREMENT (single-mention => weak_link, multi-mention => not).
    #
    # The previous version of this test pinned a BUG: _detect_orphans keyed the
    # weak-link branch on `subject not in all_subjects`, which is always False for
    # a present subject, so the check was unreachable and lint-vault reported a
    # false "clean" for single-mention subjects. The v3.27 audit fixed the
    # detector to count subject occurrences; this test follows the contract, not
    # the old (buggy) implementation.
    db = _fresh_db(tmp_path)
    _insert(db, text="loner fact", subject="Loner", predicate="has", object_value="x")
    # A subject mentioned more than once is well-connected and must NOT be weak.
    _insert(db, text="hub one", subject="Hub", predicate="has", object_value="a")
    _insert(db, text="hub two", subject="Hub", predicate="rel", object_value="b")

    report = _lint(db)

    weak_subjects = {o.get("subject") for o in report["orphans"] if o["type"] == "weak_link"}
    assert "Loner" in weak_subjects, "a single-mention subject must be a weak_link orphan"
    assert "Hub" not in weak_subjects, "a multi-mention subject must not be weak_link"


def test_gap_detected_for_entity_mentioned_thrice(tmp_path: Path) -> None:
    # WHY: a known entity mentioned in >=3 claim texts but never made the
    # subject of its own claim is a coverage gap — a topic discussed but not
    # documented. Below the threshold it must stay silent.
    db = _fresh_db(tmp_path)
    for i in range(3):
        _insert(db, text=f"we use qdrant for vectors {i}", subject="task", predicate="uses")
    _insert(db, text="docker is mentioned once only", subject="task", predicate="runs")

    report = _lint(db)

    gap_entities = {g["entity"] for g in report["gaps"]}
    assert "qdrant" in gap_entities
    assert "docker" not in gap_entities


def test_stale_detected_for_old_low_confidence_confirmed_claim(tmp_path: Path) -> None:
    # WHY: a CONFIRMED claim that is both old (past the freshness window) AND
    # low-confidence has likely decayed; flagging it drives re-validation.
    db = _fresh_db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    _insert(
        db,
        text="ancient shaky fact",
        subject="old",
        predicate="was",
        object_value="true",
        status="confirmed",
        confidence=0.4,
        updated_at=old,
    )

    report = _lint(db)

    assert any(s["type"] == "stale" for s in report["stale"])
    assert report["stale"][0]["age_days"] > 30


def test_recent_or_high_confidence_claim_is_not_stale(tmp_path: Path) -> None:
    # WHY: the stale gate requires BOTH age AND low confidence. A recent claim
    # and an old-but-confident claim must both escape the stale bucket, proving
    # neither condition alone trips it.
    db = _fresh_db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    _insert(db, text="old but trusted", subject="a", predicate="is", object_value="1",
            status="confirmed", confidence=0.95, updated_at=old)
    _insert(db, text="fresh and shaky", subject="b", predicate="is", object_value="2",
            status="confirmed", confidence=0.3, updated_at=recent)

    report = _lint(db)

    assert report["stale"] == []


def test_stale_handles_naive_timestamp(tmp_path: Path) -> None:
    # WHY: legacy rows store updated_at without a timezone. The stale detector
    # must treat a naive timestamp as UTC (not crash on aware/naive subtraction)
    # so old low-confidence claims are still caught regardless of tz format.
    db = _fresh_db(tmp_path)
    naive_old = (datetime.now(timezone.utc) - timedelta(days=200)).replace(
        tzinfo=None
    ).isoformat()
    _insert(
        db,
        text="naive old shaky fact",
        subject="legacy",
        predicate="was",
        object_value="true",
        status="confirmed",
        confidence=0.3,
        updated_at=naive_old,
    )

    report = _lint(db)

    assert any(s["type"] == "stale" for s in report["stale"])


def test_stale_swallows_unparseable_timestamp(tmp_path: Path) -> None:
    # WHY: a malformed updated_at must NOT abort the whole lint run — the
    # detector catches the parse error and skips that one claim, keeping the
    # health check robust against dirty data.
    db = _fresh_db(tmp_path)
    _insert(
        db,
        text="corrupt timestamp fact",
        subject="corrupt",
        predicate="is",
        object_value="bad",
        status="confirmed",
        confidence=0.3,
        updated_at="not-a-real-date",
    )

    report = _lint(db)

    assert report["stale"] == []


def test_scope_filter_restricts_claims(tmp_path: Path) -> None:
    # WHY: scoping a lint run to one project must exclude other projects'
    # claims entirely, so cross-project noise never pollutes a focused report.
    db = _fresh_db(tmp_path)
    _insert(db, text="mine", subject="m", predicate="is", object_value="1", scope="project:mine")
    _insert(db, text="theirs", subject="t", predicate="is", object_value="2", scope="project:other")

    report = lint_vault(
        str(db),
        scope_filter="project:mine",
        verify_with_llm=False,
        wiki_root=Path(str(db.parent / "no_such_wiki")),
    )

    assert report["claims"] == 1


def test_verify_with_llm_filters_out_false_positive_contradictions(
    tmp_path: Path, monkeypatch
) -> None:
    # WHY: the LLM verification pass is the false-positive firewall. When the
    # auditor marks a contradiction as not-real, lint_vault must DROP it — a
    # regression here would flood operators with spurious conflicts.
    db = _fresh_db(tmp_path)
    _insert(db, text="db is sqlite", subject="db", predicate="is", object_value="sqlite", confidence=0.9)
    _insert(db, text="db is postgres", subject="db", predicate="is", object_value="postgres", confidence=0.6)

    import memorymaster.llm_provider as llm

    monkeypatch.setattr(llm, "call_llm", lambda *a, **k: "[]")
    monkeypatch.setattr(
        llm,
        "parse_json_response",
        lambda _resp: [{"key": "db|is", "real": False, "explanation": "different eras"}],
    )

    report = lint_vault(
        str(db),
        verify_with_llm=True,
        wiki_root=Path(str(db.parent / "no_such_wiki")),
    )

    assert all(c["key"] != "db|is" for c in report["contradictions"])
