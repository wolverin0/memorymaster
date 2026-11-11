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
from memorymaster.stores.storage import ConcurrentModificationError  # noqa: E402

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


def _victim(svc, topic: str, *, declared=True, retire_detail=DEDUP, scope_b=None):
    a = _ingest(svc, f"The {topic} service listens on port 8080")
    lifecycle.transition_claim(svc.store, a.id, "confirmed", reason="fixture", event_type="validator")
    b = _ingest(svc, f"The {topic} service listens on port 9443", supersedes=[a.id] if declared else None)
    with svc.store.connect() as conn:  # what the old dedupe wrote: raw status + pointer
        conn.execute("UPDATE claims SET status='archived', replaced_by_claim_id=? WHERE id=?", (a.id, b.id))
        if scope_b:
            conn.execute("UPDATE claims SET scope=? WHERE id=?", (scope_b, b.id))
        conn.commit()
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


def test_midway_failure_is_reported_with_exit_1(svc, monkeypatch, capsys) -> None:
    _victim(svc, "foxtrot")
    monkeypatch.setattr(repair, "transition_claim", lambda *a, **k: (_ for _ in ()).throw(ValueError("nope")))
    assert _run(svc.store.db_path, "--apply") == 1
    assert "FAILED" in capsys.readouterr().out


def test_clear_replaced_by_is_guarded(svc) -> None:
    a, b = _victim(svc, "golf")
    with pytest.raises(ConcurrentModificationError):  # wrong expected value
        svc.store.clear_replaced_by(b, expected_replaced_by=a + 99, reason="t")
    with pytest.raises(ConcurrentModificationError):  # a superseded claim keeps its pointer
        svc.store.clear_replaced_by(a, expected_replaced_by=b, reason="t")
    svc.store.clear_replaced_by(b, expected_replaced_by=a, reason="t")
    assert svc.store.get_claim(b, include_citations=False).replaced_by_claim_id is None


def test_postgres_store_fails_closed() -> None:
    from memorymaster.stores.postgres_store import PostgresStore

    with pytest.raises(NotImplementedError):
        PostgresStore.clear_replaced_by(object(), 1, expected_replaced_by=2, reason="t")
