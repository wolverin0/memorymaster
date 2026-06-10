"""Orphan-FK repair job + repair-fk CLI (P1 WAL-discipline spec §2.6).

WHY: the 2026-06-05 index-corruption recovery left 401 orphan FK rows on the
live DB (spec F10: events→claims 226, citations→claims 159, claim_links→
claims 6, claim_embeddings→claims 6, claims self-FK 4). Orphans poison joins
and make the daily integrity fk_check regression alert (spec §2.5.3)
permanently noisy. The repair must be: dry-run by default (the operator runs
--apply ONCE supervised on the live DB), audited (every disposed row exported
verbatim to quarantine JSONL before mutation — restorable), single-transaction
(a crash mid-repair must not leave a half-repaired DB), and idempotent (the
second run is a no-op). Claims rows are never deleted — only the dangling
pointer column is nulled, because the claim text/status is still real memory.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.govern.jobs import fk_repair
from memorymaster.stores.storage import SQLiteStore

# Five observed orphan shapes (spec F10): 2 claims self-FK violations +
# 2 events + 1 citation + 1 claim_link + 1 claim_embedding = 7 violations,
# each on its own row (each claims keeper row carries one dangling pointer).
SEEDED_VIOLATIONS = 7
SEEDED_ROWS = 7
TS = "2026-06-01T00:00:00+00:00"


@pytest.fixture()
def seeded(tmp_path: Path) -> dict[str, object]:
    """Temp DB with orphans in all 5 observed shapes (foreign_keys OFF seed,
    mirroring how recovery collateral bypassed FK enforcement)."""
    store = SQLiteStore(tmp_path / "fkrepair.db")
    store.init_db()
    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        keeper_supersedes = conn.execute(
            "INSERT INTO claims (text, status, created_at, updated_at, supersedes_claim_id)"
            " VALUES ('keeper with dangling supersedes', 'candidate', ?, ?, 999001)",
            (TS, TS),
        ).lastrowid
        keeper_replaced = conn.execute(
            "INSERT INTO claims (text, status, created_at, updated_at, replaced_by_claim_id)"
            " VALUES ('keeper with dangling replaced_by', 'candidate', ?, ?, 999002)",
            (TS, TS),
        ).lastrowid
        for _ in range(2):
            conn.execute(
                "INSERT INTO events (claim_id, event_type, details, created_at)"
                " VALUES (999003, 'system', 'orphan-seed', ?)",
                (TS,),
            )
        conn.execute(
            "INSERT INTO citations (claim_id, source, locator, excerpt, created_at)"
            " VALUES (999004, 'session://lost', 'turn-9', 'orphan citation', ?)",
            (TS,),
        )
        conn.execute(
            "INSERT INTO claim_links (source_id, target_id, link_type, created_at)"
            " VALUES (?, 999005, 'relates_to', ?)",
            (keeper_supersedes, TS),
        )
        conn.execute(
            "INSERT INTO claim_embeddings (claim_id, model, embedding_json, updated_at)"
            " VALUES (999006, 'test-model', '[0.1]', ?)",
            (TS,),
        )
        conn.commit()
    return {
        "store": store,
        "keeper_supersedes": keeper_supersedes,
        "keeper_replaced": keeper_replaced,
    }


def _orphan_count(store: SQLiteStore) -> int:
    with store.connect() as conn:
        return len(conn.execute("PRAGMA foreign_key_check").fetchall())


def test_dry_run_reports_grouping_without_mutating(seeded: dict, tmp_path: Path) -> None:
    """Dry-run must report the 401-style (table, parent) grouping and plan,
    and must NOT touch a single row or write a quarantine file.

    Intent: spec §2.6 — the operator reads the dry-run against the live
    3.47 GB DB to decide whether --apply is safe. A dry-run that mutates (or
    leaves quarantine litter) destroys the trust the supervised-apply
    procedure is built on.
    """
    store = seeded["store"]
    qdir = tmp_path / "quarantine"
    res = fk_repair.run(store, quarantine_dir=qdir)

    assert res["mode"] == "dry-run"
    assert res["before"] == SEEDED_VIOLATIONS
    assert res["groups"] == {
        "claims->claims": 2,
        "events->claims": 2,
        "citations->claims": 1,
        "claim_links->claims": 1,
        "claim_embeddings->claims": 1,
    }
    assert res["planned"] == {
        "delete": {"events": 2, "citations": 1, "claim_links": 1, "claim_embeddings": 1},
        "null_pointer": 2,
        "unhandled": {},
    }
    assert _orphan_count(store) == SEEDED_VIOLATIONS, "dry-run must not repair anything"
    assert not qdir.exists(), "dry-run must not write quarantine files"


def test_apply_quarantines_and_repairs_to_zero(seeded: dict, tmp_path: Path) -> None:
    """--apply must export every orphan row verbatim to quarantine JSONL,
    repair foreign_key_check to 0, keep (not delete) the self-FK claims, and
    leave the events append-only guard intact.

    Intent: spec §2.6.2-4 — disposal without an audit trail is data loss
    (the quarantine line is the only restorable copy); claims with dangling
    supersedes/replaced_by pointers are real memory and only the pointer is
    nulled (claims-lifecycle rules); and dropping the events append-only
    triggers to delete orphans must be strictly transaction-local — a repair
    that leaves events mutable reopens the tamper surface the hash chain
    exists to close.
    """
    store = seeded["store"]
    qdir = tmp_path / "quarantine"
    res = fk_repair.run(store, apply=True, quarantine_dir=qdir)

    assert res["ok"] is True
    assert res["before"] == SEEDED_VIOLATIONS
    assert res["after"] == 0
    assert res["deleted"] == {"events": 2, "citations": 1, "claim_links": 1, "claim_embeddings": 1}
    assert _orphan_count(store) == 0

    # Quarantine: one JSONL line per disposed/touched ROW, full row verbatim.
    qfile = Path(res["quarantine"])
    assert qfile.parent == qdir
    lines = [json.loads(line) for line in qfile.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == SEEDED_ROWS
    assert res["quarantined_rows"] == SEEDED_ROWS
    by_table = sorted(entry["table"] for entry in lines)
    assert by_table == ["citations", "claim_embeddings", "claim_links", "claims", "claims", "events", "events"]
    citation_line = next(e for e in lines if e["table"] == "citations")
    assert citation_line["row"]["excerpt"] == "orphan citation", "row must be exported verbatim (restorable)"
    assert citation_line["violations"] == [{"parent": "claims", "fkid": 0}]

    with store.connect() as conn:
        # Self-FK claims kept, pointer nulled.
        sup = conn.execute(
            "SELECT text, supersedes_claim_id FROM claims WHERE id = ?",
            (seeded["keeper_supersedes"],),
        ).fetchone()
        assert sup["text"] == "keeper with dangling supersedes", "claim row must be KEPT"
        assert sup["supersedes_claim_id"] is None
        rep = conn.execute(
            "SELECT replaced_by_claim_id FROM claims WHERE id = ?",
            (seeded["keeper_replaced"],),
        ).fetchone()
        assert rep["replaced_by_claim_id"] is None

        # fk_repair event emitted per touched claim.
        marked = conn.execute(
            "SELECT claim_id FROM events WHERE event_type = 'system' AND details = ?",
            (fk_repair.MARKER_FK_REPAIR,),
        ).fetchall()
        assert sorted(r["claim_id"] for r in marked) == sorted(
            [seeded["keeper_supersedes"], seeded["keeper_replaced"]]
        )

        # Append-only guard restored AND functional after the repair.
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_events_append_only%'"
            ).fetchall()
        }
        assert triggers == {"trg_events_append_only_update", "trg_events_append_only_delete"}
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM events")


def test_apply_second_run_is_noop(seeded: dict, tmp_path: Path) -> None:
    """A second --apply must find zero orphans, mutate nothing, and write no
    new quarantine file.

    Intent: spec §2.6 idempotency — the operator may re-run repair-fk after
    the supervised apply (or the steward may someday automate it); a non-
    idempotent repair would double-delete or spray empty quarantine files on
    every run, turning the audit dir into noise.
    """
    store = seeded["store"]
    qdir = tmp_path / "quarantine"
    first = fk_repair.run(store, apply=True, quarantine_dir=qdir)
    assert first["ok"] is True
    files_after_first = sorted(p.name for p in qdir.glob("fk-repair-*.jsonl"))
    assert len(files_after_first) == 1

    second = fk_repair.run(store, apply=True, quarantine_dir=qdir)
    assert second == {"mode": "apply", "before": 0, "after": 0, "noop": True}
    assert sorted(p.name for p in qdir.glob("fk-repair-*.jsonl")) == files_after_first


def test_cli_repair_fk_dry_run_default_then_apply(
    seeded: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`memorymaster repair-fk` must default to dry-run; only --apply repairs.

    Intent: spec §2.6 — the CLI is the operator's runbook interface for the
    one supervised live-DB repair. If the bare command ever mutated, a
    routine 'let me look first' invocation against the production DB would
    BE the apply. The dry-run default is the safety interlock.
    """
    from memorymaster.surfaces.cli import main

    store = seeded["store"]
    db = str(store.db_path)
    qdir = tmp_path / "quarantine"

    rc = main(["--db", db, "--json", "repair-fk"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["data"]["mode"] == "dry-run"
    assert out["data"]["before"] == SEEDED_VIOLATIONS
    assert _orphan_count(store) == SEEDED_VIOLATIONS, "bare CLI invocation must not repair"

    rc = main(["--db", db, "--json", "repair-fk", "--apply", "--quarantine-dir", str(qdir)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["data"]["ok"] is True
    assert out["data"]["after"] == 0
    assert _orphan_count(store) == 0
    assert list(qdir.glob("fk-repair-*.jsonl")), "apply must write the quarantine export"
