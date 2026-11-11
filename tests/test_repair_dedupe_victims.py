"""X-1 repair: a declared correction B that the old dedupe archived is served again; A stays superseded by B."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import repair_dedupe_victims as repair  # noqa: E402

from memorymaster.core import lifecycle  # noqa: E402
from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.stores._storage_shared import ConcurrentModificationError  # noqa: E402

SCOPE = "project:x1repair"
DEDUP = "dedupe-archived: jaccard>=0.80 canonical-status=confirmed"


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "x1.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _ingest(svc, text, **kw):
    return svc.ingest(text, [CitationInput(source="test://x1", locator="x")], scope=kw.pop("scope", SCOPE),
                      source_agent=kw.pop("source_agent", "fixture"), **kw)


def _event(svc, claim_id, details, *, from_status=None, to_status=None):
    with svc.store.connect() as conn:
        svc.store._insert_event_row(conn, claim_id=claim_id, event_type="transition", from_status=from_status,
                                    to_status=to_status, details=details, payload_json=None,
                                    created_at="2026-01-01T00:00:00+00:00")
        conn.commit()


def _victim(svc, topic: str, *, declared=True, retire_detail=DEDUP, scope_b=None, source_b="fixture",
            tuple_=False, dedup_event=True):
    a = _ingest(svc, f"The {topic} service listens on port 8080")
    lifecycle.transition_claim(svc.store, a.id, "confirmed", reason="fixture", event_type="validator")
    b = _ingest(svc, f"The {topic} service listens on port 9443", supersedes=[a.id] if declared else None, source_agent=source_b,
                **({"subject": f"svc-{topic}", "predicate": "port"} if tuple_ else {}))
    with svc.store.connect() as conn:  # what the old dedupe wrote: raw status + pointer
        conn.execute("UPDATE claims SET status='archived', replaced_by_claim_id=? WHERE id=?", (a.id, b.id))
        if scope_b:
            conn.execute("UPDATE claims SET scope=? WHERE id=?", (scope_b, b.id))
        conn.commit()
    if dedup_event:
        _event(svc, b.id, DEDUP)
    svc.store.mark_superseded(a.id, b.id, "applied later")  # the supersession lands on A afterwards
    if not declared:
        with svc.store.connect() as conn:
            conn.execute("UPDATE claims SET supersedes_claim_id=NULL WHERE id=?", (b.id,))
            conn.commit()
    if retire_detail != DEDUP:
        _event(svc, b.id, retire_detail, from_status="candidate", to_status="archived")
    return a.id, b.id


def _snap(svc, *ids):
    out = []
    for i in ids:
        c = svc.store.get_claim(i, include_citations=False)
        out.append((c.status, c.replaced_by_claim_id, c.supersedes_claim_id, c.version, c.archived_at, c.scope,
                    len(svc.list_events(claim_id=i, limit=500))))
    return out


def _served(svc, topic):
    return {r["claim"].id for r in svc.query_rows(f"{topic} service listens on port", include_candidates=False)}


def _run(db, *flags):
    return repair.main(["--db", str(db), *flags])


def test_dry_run_lists_the_pair_and_changes_nothing(svc, capsys) -> None:
    a, b = _victim(svc, "alpha")
    before = _snap(svc, a, b)
    assert _run(svc.store.db_path) == 0
    out = capsys.readouterr().out
    assert f"A={a}" in out and f"B={b}" in out and "-> repair" in out and "DRY-RUN" in out
    assert _snap(svc, a, b) == before


def test_apply_restores_b_keeps_a_superseded_and_serves_the_correction(svc) -> None:
    a, b = _victim(svc, "bravo")
    assert _served(svc, "bravo") == set(), "precondition: neither version is served"
    assert _run(svc.store.db_path, "--apply") == 0
    ca, cb = (svc.store.get_claim(i, include_citations=False) for i in (a, b))
    assert (cb.status, cb.replaced_by_claim_id, cb.supersedes_claim_id, cb.archived_at) == ("confirmed", None, a, None)
    assert (ca.status, ca.replaced_by_claim_id) == ("superseded", b)
    assert _served(svc, "bravo") == {b}
    events = svc.list_events(claim_id=b, limit=500)
    assert any(json.loads(e.payload_json).get("repair") == "x1_dedupe_victim" for e in events if e.payload_json)
    assert any(e.details.startswith(lifecycle.REVIVAL_REASON_PREFIX) for e in events)
    report = svc.store.reconcile_integrity()
    assert sum(report["summary"].values()) == 0, report["issues"]


def test_second_apply_is_a_no_op(svc, capsys) -> None:
    a, b = _victim(svc, "charlie")
    assert _run(svc.store.db_path, "--apply") == 0
    after = _snap(svc, a, b)
    capsys.readouterr()
    assert _run(svc.store.db_path, "--apply") == 0
    assert "0 victim pair(s)" in capsys.readouterr().out
    assert _snap(svc, a, b) == after


@pytest.mark.parametrize("case, verdict", [
    ("not_declared", "skipped_not_declared"),
    ("forget", "skipped_explicit_forget"),
    ("scope", "skipped_scope_mismatch"),
])
def test_skip_cases_are_reported_and_untouched(svc, capsys, case, verdict) -> None:
    kw = {"not_declared": {"declared": False}, "forget": {"retire_detail": "friendly_forget_claim"},
          "scope": {"scope_b": "project:other"}}[case]
    a, b = _victim(svc, "delta", **kw)
    before = _snap(svc, a, b)
    assert _run(svc.store.db_path, "--apply") == 0
    assert verdict in capsys.readouterr().out
    assert _snap(svc, a, b) == before


def test_refused_confirm_leaves_both_claims_exactly_as_they_were(svc, monkeypatch, capsys) -> None:
    a, b = _victim(svc, "echo")
    before = _snap(svc, a, b)
    monkeypatch.setattr(repair, "review_allows_confirmation", lambda *_: False)
    assert _run(svc.store.db_path, "--apply") == 1
    assert "refused" in capsys.readouterr().out
    assert _snap(svc, a, b) == before


def test_midway_failure_rolls_the_whole_pair_back(svc, monkeypatch, capsys) -> None:
    a, b = _victim(svc, "foxtrot")
    before = _snap(svc, a, b)
    real, calls = repair.SQLiteStore._insert_event_row, []

    def flaky(self, conn, **kw):
        calls.append(kw["to_status"])
        if len(calls) == 2:  # the confirmation event: status UPDATE and revival event already written
            raise RuntimeError("disk full")
        return real(self, conn, **kw)

    monkeypatch.setattr(repair.SQLiteStore, "_insert_event_row", flaky)
    assert _run(svc.store.db_path, "--apply") == 1
    assert "rolled back" in capsys.readouterr().out and len(calls) == 2
    assert _snap(svc, a, b) == before
    monkeypatch.undo()
    assert _run(svc.store.db_path, "--apply") == 0  # and the pair is still repairable afterwards
    assert svc.store.get_claim(b, include_citations=False).status == "confirmed"


def test_restore_refuses_anything_but_the_victim_shape(svc) -> None:
    a, b = _victim(svc, "golf")
    kw = {"reason": "t", "event_payload": {}}
    for victim, pred in ((a, b), (b, a + 99)):  # superseded claim as victim / wrong predecessor
        with pytest.raises(ConcurrentModificationError):
            svc.store.restore_declared_correction(victim, pred, **kw)
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET status='stale' WHERE id=?", (b,))
        conn.commit()
    with pytest.raises(ConcurrentModificationError):  # archived is required
        svc.store.restore_declared_correction(b, a, **kw)


def test_only_dedupe_retirements_are_selected(svc, capsys) -> None:
    a, b = _victim(svc, "hotel", dedup_event=False, retire_detail="operator_archive")
    before = _snap(svc, a, b)
    assert _run(svc.store.db_path, "--apply") == 0
    assert "0 victim pair(s)" in capsys.readouterr().out
    assert _snap(svc, a, b) == before


def test_the_confirmation_is_stamped_with_the_operator_actor(svc) -> None:
    _a, b = _victim(svc, "india")
    assert _run(svc.store.db_path, "--apply") == 0
    confirm = [e for e in svc.list_events(claim_id=b, limit=500) if e.to_status == "confirmed"][-1]
    assert json.loads(confirm.payload_json)["actor"] == "operator"


def _set(svc, sql, *args):
    with svc.store.connect() as conn:
        conn.execute(sql, args)
        conn.commit()


def test_a_live_claim_with_the_same_tuple_blocks_the_repair(svc, capsys) -> None:
    a, b = _victim(svc, "juliet", tuple_=True)
    c = _ingest(svc, "The juliet service now listens on port 7000 after the move", subject="svc-juliet",
                predicate="port")
    before = _snap(svc, a, b)
    assert _run(svc.store.db_path, "--apply") == 0
    assert "skipped_newer_value" in capsys.readouterr().out and c.id
    assert _snap(svc, a, b) == before


def test_a_live_claim_declaring_supersedes_b_blocks_the_repair(svc, capsys) -> None:
    a, b = _victim(svc, "kilo")
    c = _ingest(svc, "The kilo service listens on port 5555 per the latest runbook")
    _set(svc, "UPDATE claims SET supersedes_claim_id=? WHERE id=?", b, c.id)
    before = _snap(svc, a, b)
    assert _run(svc.store.db_path, "--apply") == 0
    assert "skipped_newer_value" in capsys.readouterr().out
    assert _snap(svc, a, b) == before


def test_a_redacted_b_is_skipped(svc, capsys) -> None:
    a, b = _victim(svc, "lima")
    _set(svc, "UPDATE claims SET text='[REDACTED_CLAIM_TEXT]' WHERE id=?", b)
    before = _snap(svc, a, b)
    assert _run(svc.store.db_path, "--apply") == 0
    assert "skipped_redacted" in capsys.readouterr().out
    assert _snap(svc, a, b) == before


def test_policy_blocked_author_needs_the_override(svc, capsys) -> None:
    a, b = _victim(svc, "mike", source_b="llm-stop-hook")
    before = _snap(svc, a, b)
    assert _run(svc.store.db_path, "--apply") == 0
    assert "skipped_policy" in capsys.readouterr().out
    assert _snap(svc, a, b) == before
    assert _run(svc.store.db_path, "--apply", "--allow-policy-override") == 0
    assert svc.store.get_claim(b, include_citations=False).status == "confirmed"


def test_a_b_without_citations_needs_the_override(svc, capsys) -> None:
    a, b = _victim(svc, "november")
    _set(svc, "DELETE FROM citations WHERE claim_id=?", b)
    assert _run(svc.store.db_path, "--apply") == 0
    assert "skipped_policy" in capsys.readouterr().out
    assert svc.store.get_claim(b, include_citations=False).status == "archived"


def test_dry_run_prints_what_a_human_needs_to_decide(svc, capsys) -> None:
    _victim(svc, "oscar", tuple_=True)
    _run(svc.store.db_path)
    out = capsys.readouterr().out
    assert "B.source=fixture" in out and "B.citations=1" in out and "B.tuple=yes" in out and "created" in out
    assert "listens on port" not in out, "claim text must not be printed"


def test_pair_selection_starts_from_archived_rows_and_reaches_a_by_key(svc) -> None:
    with svc.store.connect() as conn:
        plan = " | ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + repair.PAIRS_SQL).fetchall())
    assert "SCAN a" not in plan and "SEARCH a" in plan, plan


def test_postgres_store_fails_closed() -> None:
    from memorymaster.stores.postgres_store import PostgresStore

    with pytest.raises(NotImplementedError):
        PostgresStore.restore_declared_correction(object(), 1, 2, reason="t", event_payload={})
