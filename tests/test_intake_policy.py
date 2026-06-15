"""Intent-anchored tests for the P3 intake policy.

Each test encodes WHY the behavior matters, anchored on the policy requirement
(see ``.planning/P3-INTAKE-POLICY-SPEC.md``), not on incidental implementation
details. The policy is ADDITIVE: it may reject more (raise the bar) or attribute
more, but must never (a) flip a previously-rejected claim into an accept, nor
(b) shadow / weaken the sacred sensitivity filter.

All ingest tests use tmp DBs via the ``svc`` fixture — never the live DB.
"""
from __future__ import annotations

import sqlite3

import pytest

from memorymaster.core.intake_policy import (
    IntakePolicyConfig,
    IntakeRejected,
    evaluate_intake,
    reset_intake_state,
)
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.core import observability


@pytest.fixture
def svc(tmp_path):
    """Real MemoryService on a tmp SQLite DB (never the live memorymaster.db)."""
    s = MemoryService(db_target=str(tmp_path / "intake.db"), workspace_root=tmp_path)
    s.init_db()
    return s


@pytest.fixture(autouse=True)
def _clean_policy_state(monkeypatch):
    """Each test starts from a clean quota/batch state and explicit env so a
    leaked env var from the live process or another test can't mask a regression.
    """
    reset_intake_state()
    for var in (
        "MEMORYMASTER_INTAKE_REQUIRE_SOURCE_AGENT",
        "MEMORYMASTER_INTAKE_DEFAULT_SOURCE_AGENT",
        "MEMORYMASTER_INTAKE_REJECT_SESSION_STATE",
        "MEMORYMASTER_INTAKE_REJECTED_SCOPE_PREFIXES",
        "MEMORYMASTER_INTAKE_QUOTA_PER_AGENT_PER_DAY",
        "MEMORYMASTER_INTAKE_QUOTA_WINDOW",
        "MEMORYMASTER_INTAKE_QUOTA_EXEMPT_AGENTS",
        "MEMORYMASTER_INTAKE_MAX_PER_STOP",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reset_intake_state()


def _good_citation():
    return [CitationInput(source="test.py", locator="project:test")]


# ---------------------------------------------------------------------------
# (a) NULL source_agent attribution per spec (Rule A)
# ---------------------------------------------------------------------------


def test_explicit_caller_empty_source_agent_rejected_in_strict_mode(monkeypatch):
    """WHY: An explicit/external (MCP) caller that deliberately sends an empty
    source_agent breaks the attribution contract. In strict mode the policy must
    REJECT it rather than silently writing a NULL-attributed claim — this is the
    attribution-loss root cause the policy exists to close.
    """
    monkeypatch.setenv("MEMORYMASTER_INTAKE_REQUIRE_SOURCE_AGENT", "strict")
    with pytest.raises(IntakeRejected) as exc:
        evaluate_intake(
            text="a real learning",
            claim_type="fact",
            subject="x",
            scope="project:test",
            source_agent="",
            require_source_agent=True,
        )
    assert exc.value.rule == "source_agent"


def test_internal_caller_missing_source_agent_is_default_tagged_not_dropped():
    """WHY: A missing tag from a hook/internal extractor must NEVER drop a real
    learning. The safe default ('warn') salvages it by attributing 'unknown' so
    the claim stays queryable/auditable instead of becoming a lost NULL.
    """
    decision = evaluate_intake(
        text="a real learning",
        claim_type="fact",
        subject="x",
        scope="project:test",
        source_agent=None,  # internal caller, require_source_agent defaults False
    )
    assert decision.accept is True
    assert decision.mutated_fields.get("source_agent") == "unknown"


def test_service_ingest_default_tags_null_source_agent(svc):
    """WHY: The attribution fix must land at the chokepoint, not just in the pure
    function — a NULL source_agent reaching service.ingest must be persisted as
    'unknown', killing attribution loss at the source.
    """
    claim = svc.ingest(
        text="default tag reaches the store",
        citations=_good_citation(),
        claim_type="fact",
        scope="project:test",
        source_agent=None,
    )
    assert claim.source_agent == "unknown"


# ---------------------------------------------------------------------------
# (b) session-state / heartbeat-shaped claim rejected (Rule B)
# ---------------------------------------------------------------------------


def test_session_state_scope_rejected_from_claims(svc):
    """WHY: The watchkeeper flood (80% of NULLs) wrote session heartbeats into
    the claims table via session-state.* scope. These are telemetry, not
    knowledge claims — they must be rejected from claims (belong in the verbatim
    store). This is the one intentional new default-rejection.
    """
    with pytest.raises(ValueError):  # IntakeRejected is a ValueError subclass
        svc.ingest(
            text="anything",
            citations=_good_citation(),
            scope="session-state.watchkeeper",
            source_agent="watchkeeper",
        )
    # And nothing landed in the claims table.
    with sqlite3.connect(svc.store.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    assert n == 0


def test_heartbeat_shaped_json_rejected():
    """WHY: Even under a non-session-state scope, a JSON heartbeat envelope
    (session_id + ts, no human claim body) is non-claim telemetry and must be
    rejected by the deterministic probe — no LLM, no false-negative on the flood.
    """
    with pytest.raises(IntakeRejected) as exc:
        evaluate_intake(
            text='{"session_id":"wk-session-9","ts":"2026-06-15T00:00:00Z"}',
            claim_type=None,
            subject=None,
            scope="project:test",
            source_agent="wk",
        )
    assert exc.value.rule == "session_state"


def test_heartbeat_type_rejected():
    """WHY: claim_type=='heartbeat' is unambiguously telemetry regardless of body."""
    with pytest.raises(IntakeRejected):
        evaluate_intake(
            text="cpu ok",
            claim_type="heartbeat",
            subject=None,
            scope="project:test",
            source_agent="wk",
        )


def test_json_claim_with_real_body_is_not_a_heartbeat():
    """WHY: The probe must be conservative — a JSON object that carries a real
    human-readable claim body must NOT be misclassified as a heartbeat, or we'd
    flip a real-claim accept into a reject (forbidden by boundary #2).
    """
    decision = evaluate_intake(
        text='{"session_id":"s","ts":"t","text":"Postgres parity needs schema sync on both stores"}',
        claim_type="fact",
        subject="postgres",
        scope="project:test",
        source_agent="claude-session",
    )
    assert decision.accept is True


def test_reject_session_state_can_be_disabled(monkeypatch, svc):
    """WHY: Rule B has a documented opt-out for operators who genuinely want
    heartbeats in claims. With the flag off, the prior accept behavior is
    preserved (additive guarantee: configurable + safe-default-overridable).
    """
    monkeypatch.setenv("MEMORYMASTER_INTAKE_REJECT_SESSION_STATE", "off")
    claim = svc.ingest(
        text="operator opted into session telemetry",
        citations=_good_citation(),
        scope="session-state.watchkeeper",
        source_agent="watchkeeper",
    )
    assert claim.id > 0


# ---------------------------------------------------------------------------
# (c) over-quota ingest from one source_agent is throttled (Rule C)
# ---------------------------------------------------------------------------


def test_per_agent_quota_throttles_a_single_agent(monkeypatch):
    """WHY: A flooding agent (today only MCP is rate-limited) must be throttled at
    the chokepoint so ALL callers are covered. Once the per-agent window quota is
    spent, further ingests from that agent are rejected.
    """
    monkeypatch.setenv("MEMORYMASTER_INTAKE_QUOTA_PER_AGENT_PER_DAY", "2")
    for i in range(2):
        evaluate_intake(
            text=f"claim {i}",
            claim_type="fact",
            subject="s",
            scope="project:test",
            source_agent="spammer",
        )
    with pytest.raises(IntakeRejected) as exc:
        evaluate_intake(
            text="claim 3",
            claim_type="fact",
            subject="s",
            scope="project:test",
            source_agent="spammer",
        )
    assert exc.value.rule == "quota"


def test_quota_is_per_agent_rotation_does_not_bypass_other_agents_quota(monkeypatch):
    """WHY: The quota is keyed per source_agent, so each agent gets its own bucket.
    A *different* agent is independently throttled — quota is not a shared global
    that one noisy agent can starve from another (each is bounded).
    """
    monkeypatch.setenv("MEMORYMASTER_INTAKE_QUOTA_PER_AGENT_PER_DAY", "1")
    evaluate_intake(text="a1", claim_type="fact", subject="s", scope="project:test", source_agent="agent-a")
    evaluate_intake(text="b1", claim_type="fact", subject="s", scope="project:test", source_agent="agent-b")
    with pytest.raises(IntakeRejected):
        evaluate_intake(text="a2", claim_type="fact", subject="s", scope="project:test", source_agent="agent-a")


def test_exempt_agent_bypasses_quota(monkeypatch):
    """WHY: Bulk importers must be allowlistable so a legitimate batch import is
    never throttled (false-positive mitigation, spec §4.5).
    """
    monkeypatch.setenv("MEMORYMASTER_INTAKE_QUOTA_PER_AGENT_PER_DAY", "1")
    monkeypatch.setenv("MEMORYMASTER_INTAKE_QUOTA_EXEMPT_AGENTS", "bulk-importer")
    for i in range(5):
        decision = evaluate_intake(
            text=f"bulk {i}",
            claim_type="fact",
            subject="s",
            scope="project:test",
            source_agent="bulk-importer",
        )
        assert decision.accept is True


def test_quota_default_off_is_unlimited():
    """WHY: SAFE DEFAULT — quota defaults to 0 (off). Nothing that passes today is
    throttled, satisfying the additive boundary.
    """
    for i in range(50):
        evaluate_intake(
            text=f"unbounded {i}",
            claim_type="fact",
            subject="s",
            scope="project:test",
            source_agent="anyone",
        )


# ---------------------------------------------------------------------------
# (d) >N distilled claims per stop-hook invocation is capped (Rule D)
# ---------------------------------------------------------------------------


def test_max_per_stop_caps_a_batch():
    """WHY: The documented norm is max 3 learnings per Stop. Make it a policy
    invariant so an edited hook or compromised LLM response can't flood past 3,
    even if the [:3] slice is removed. The 4th claim of a batch is rejected.
    """
    for i in range(3):
        evaluate_intake(
            text=f"learning {i}",
            claim_type="fact",
            subject="s",
            scope="project:test",
            source_agent="llm-stop-hook",
            intake_batch_id="stop-abc",
            intake_batch_max=3,
        )
    with pytest.raises(IntakeRejected) as exc:
        evaluate_intake(
            text="learning 4",
            claim_type="fact",
            subject="s",
            scope="project:test",
            source_agent="llm-stop-hook",
            intake_batch_id="stop-abc",
            intake_batch_max=3,
        )
    assert exc.value.rule == "max_per_stop"


def test_separate_batches_are_independent():
    """WHY: A fresh batch id per Stop invocation must reset the cap — otherwise a
    second legitimate Stop would be wrongly starved.
    """
    for i in range(3):
        evaluate_intake(text=f"a{i}", claim_type="fact", subject="s", scope="project:test",
                        source_agent="llm-stop-hook", intake_batch_id="stop-1", intake_batch_max=3)
    # Different batch id -> independent budget.
    decision = evaluate_intake(text="b0", claim_type="fact", subject="s", scope="project:test",
                               source_agent="llm-stop-hook", intake_batch_id="stop-2", intake_batch_max=3)
    assert decision.accept is True


def test_bulk_script_without_batch_id_is_unaffected_by_max_per_stop():
    """WHY: Rule D only bites callers that pass an intake_batch_id (the stop hook).
    Bulk scripts that don't pass one must be unaffected (no false positive).
    """
    for i in range(20):
        evaluate_intake(text=f"bulk {i}", claim_type="fact", subject="s",
                        scope="project:test", source_agent="bulk-script")


# ---------------------------------------------------------------------------
# (e) REGRESSION: a normal good claim still ingests
# ---------------------------------------------------------------------------


def test_normal_good_claim_still_ingests(svc):
    """WHY: The whole policy is additive. A normal, attributed knowledge claim
    must pass end-to-end through service.ingest exactly as before.
    """
    claim = svc.ingest(
        text="Steward cycle runs every 6 hours and decays stale claims",
        citations=_good_citation(),
        claim_type="fact",
        subject="steward",
        scope="project:memorymaster",
        source_agent="claude-session",
    )
    assert claim.id > 0
    assert claim.source_agent == "claude-session"
    assert claim.text.startswith("Steward cycle runs")


# ---------------------------------------------------------------------------
# (f) REGRESSION: db_merge re-ingest of an existing claim with a real
#     source_agent still succeeds (exempt by construction)
# ---------------------------------------------------------------------------


def test_dedup_reingest_with_real_source_agent_returns_existing(svc):
    """WHY: A re-ingest of an already-stored claim (the merge-fidelity path) that
    carries a real source_agent must still succeed (dedup returns the existing
    row) — the policy must not reject a legitimate idempotent re-ingest.
    """
    first = svc.ingest(
        text="OpenClaw sync merges claims bidirectionally",
        citations=_good_citation(),
        claim_type="fact",
        subject="db_merge",
        scope="project:memorymaster",
        source_agent="claude-session",
        idempotency_key="merge-key-1",
    )
    second = svc.ingest(
        text="OpenClaw sync merges claims bidirectionally",
        citations=_good_citation(),
        claim_type="fact",
        subject="db_merge",
        scope="project:memorymaster",
        source_agent="claude-session",
        idempotency_key="merge-key-1",
    )
    assert second.id == first.id  # idempotent re-ingest, not a new row


def test_db_merge_does_not_route_through_service_ingest():
    """WHY: db_merge copies rows verbatim via raw INSERT (db_merge.py), bypassing
    service.ingest entirely — so it is exempt from the policy by construction.
    This guards that invariant: if a refactor ever routed db_merge through
    service.ingest, a session-state row from another node would start getting
    rejected (merge fidelity regression). We assert merge does not import/call it.
    """
    import inspect

    from memorymaster.bridges import db_merge

    source = inspect.getsource(db_merge)
    # The merge writer must not depend on MemoryService.ingest for row copy; it
    # copies rows verbatim via raw `INSERT INTO claims`, so the chokepoint policy
    # can never touch it.
    assert "svc.ingest(" not in source
    assert "service.ingest(" not in source
    assert "INSERT INTO claims" in source


# ---------------------------------------------------------------------------
# (g) SAFETY: the sensitivity filter still rejects/redacts a sensitive payload,
#     and the intake policy did NOT shadow it (filter runs FIRST, unchanged)
# ---------------------------------------------------------------------------


def test_sensitive_payload_still_caught_by_filter_not_shadowed_by_policy(svc):
    """WHY: The sensitivity filter is SACRED and runs BEFORE the intake policy.
    A claim carrying a secret must still be flagged + redacted-at-rest by the
    filter — proving the policy did not reorder, gate, or shadow it. The stored
    text must NOT contain the raw secret.
    """
    secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHiiiijjjj"
    claim = svc.ingest(
        text=f"The anthropic key is {secret} do not leak it",
        citations=_good_citation(),
        claim_type="fact",
        subject="config",
        scope="project:test",
        source_agent="claude-session",
    )
    # Filter ran: the raw secret is redacted out of the stored text.
    assert secret not in claim.text
    # Filter independently recorded a sensitive redaction (proves it executed
    # on this exact ingest, not bypassed by the new policy gate).
    with sqlite3.connect(svc.store.db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM events WHERE claim_id = ? AND event_type = 'policy_decision'",
            (claim.id,),
        ).fetchall()
    assert any("sensitive_redaction_applied" in (r[0] or "") for r in rows)


def test_policy_rejection_emits_observability_counter_and_event(svc):
    """WHY: Every rejection must be MEASURABLE (counter + policy_decision event)
    so the flood can be observed after shipping. A session-state rejection bumps
    the claims_policy_rejected_total counter.
    """
    observability.reset_metrics()
    with pytest.raises(ValueError):
        svc.ingest(
            text="hb",
            citations=_good_citation(),
            scope="session-state.watchkeeper",
            claim_type="heartbeat",
            source_agent="wk",
        )
    count = observability.metric_value(
        "claims_policy_rejected_total", rule="session_state", reason="scope_rejected"
    )
    assert count >= 1


# ---------------------------------------------------------------------------
# Config isolation — each rule is independently togglable
# ---------------------------------------------------------------------------


def test_config_from_env_safe_defaults(monkeypatch):
    """WHY: Safe defaults must hold even with no env set: warn attribution,
    session-state rejection ON, quota OFF, max-per-stop 3.
    """
    cfg = IntakePolicyConfig.from_env()
    assert cfg.require_source_agent == "warn"
    assert cfg.default_source_agent == "unknown"
    assert cfg.reject_session_state is True
    assert cfg.quota_per_agent_per_window == 0
    assert cfg.max_per_stop == 3
