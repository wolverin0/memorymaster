from __future__ import annotations

from datetime import datetime, timezone

from memorymaster.capture import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.evaluation.temporal_projection import (
    project_evidence_episodes,
    project_temporal_claims,
    summarize_durative_states,
)


SCOPE = "project:synthetic"


def _service(tmp_path):
    service = MemoryService(tmp_path / "temporal.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _claim(service, text, *, status="confirmed", scope=SCOPE, citations=True, **temporal):
    refs = [CitationInput(source="fixture", locator=text)] if citations else []
    claim = service.ingest(text=text, citations=refs, scope=scope, **temporal)
    if status != "candidate":
        claim = service.store.apply_status_transition(
            claim, to_status=status, reason="fixture", event_type="validator"
        )
    if not citations:
        with service.store.connect() as conn:
            conn.execute("DELETE FROM citations WHERE claim_id=?", (claim.id,))
            conn.commit()
        claim = service.store.get_claim(claim.id)
    return claim


def _evidence(service, claim, key, *, chat_id, occurred_at, sensitivity="none"):
    source = service.upsert_external_source(
        source_type="synthetic", display_name="Temporal fixture", config_json={}
    )
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id=key,
        item_type="text",
        chat_id=chat_id,
        occurred_at=occurred_at,
        text=f"Evidence {key}",
        sensitivity=sensitivity,
    )
    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="text",
        text=f"Evidence {key}",
        sensitivity=sensitivity,
    )
    CaptureRepository(service.store).link_claim_evidence(
        claim_id=claim.id, evidence_item_id=evidence.id
    )
    return item, evidence


def test_current_projection_excludes_noncurrent_lifecycle_and_intervals(tmp_path):
    service = _service(tmp_path)
    current = _claim(service, "Current", valid_from="2026-01-01T00:00:00Z")
    _claim(service, "Stale", status="stale")
    _claim(service, "Candidate", status="candidate")
    expired = _claim(service, "Expired", valid_until="2026-02-01T00:00:00Z")

    report = project_temporal_claims(
        service,
        [current.id, expired.id],
        scope_allowlist=[SCOPE],
        intent="current",
        query_time="2026-03-01T00:00:00Z",
    )

    assert [row["claim_id"] for row in report["claims"]] == [current.id]
    assert report["intent"] == "current"


def test_occurrence_projection_prefers_event_time_over_capture_time(tmp_path):
    service = _service(tmp_path)
    claim = _claim(service, "Occurred earlier", event_time="2025-05-10T12:00:00Z")
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET created_at=? WHERE id=?",
            ("2026-08-08T12:00:00Z", claim.id),
        )
        conn.commit()

    report = project_temporal_claims(
        service,
        [claim.id],
        scope_allowlist=[SCOPE],
        intent="occurrence",
        query_start="2025-05-10T00:00:00Z",
        query_end="2025-05-10T23:59:59Z",
    )

    row = report["claims"][0]
    assert row["occurrence_time"] == "2025-05-10T12:00:00+00:00"
    assert row["capture_time"] == "2026-08-08T12:00:00Z"


def test_latest_projection_selects_newest_claim_per_semantic_key(tmp_path):
    service = _service(tmp_path)
    older = _claim(
        service,
        "Older state",
        status="stale",
        subject="Project A",
        predicate="state",
        event_time="2025-01-01T00:00:00Z",
        valid_from="2025-01-01T00:00:00Z",
    )
    newer = _claim(
        service,
        "Newer state",
        subject="Project A",
        predicate="state",
        event_time="2026-01-01T00:00:00Z",
        valid_from="2025-01-01T00:00:00Z",
    )

    report = project_temporal_claims(
        service,
        [older.id, newer.id],
        scope_allowlist=[SCOPE],
        intent="latest",
        query_time="2026-02-01T00:00:00Z",
    )

    assert [row["claim_id"] for row in report["claims"]] == [newer.id]


def test_historical_interval_overlap_includes_superseded_boundary(tmp_path):
    service = _service(tmp_path)
    replacement = _claim(service, "Replacement")
    prior = _claim(
        service,
        "Prior state",
        valid_from="2025-01-01T00:00:00Z",
        valid_until="2025-06-01T00:00:00Z",
    )
    prior = service.store.apply_status_transition(
        prior,
        to_status="superseded",
        reason="fixture",
        event_type="transition",
        replaced_by_claim_id=replacement.id,
    )

    report = project_temporal_claims(
        service,
        [prior.id, replacement.id],
        scope_allowlist=[SCOPE],
        intent="historical",
        query_start="2025-06-01T00:00:00Z",
        query_end="2025-06-01T00:00:00Z",
    )

    assert [row["claim_id"] for row in report["claims"]] == [prior.id]
    assert report["claims"][0]["replaced_by_claim_id"] == replacement.id


def test_durative_summary_preserves_every_contributing_citation(tmp_path):
    service = _service(tmp_path)
    first = _claim(
        service,
        "Project phase one",
        status="stale",
        subject="Project A",
        predicate="phase",
        object_value="one",
        valid_from="2025-01-01T00:00:00Z",
        valid_until="2025-02-01T00:00:00Z",
    )
    second = _claim(
        service,
        "Project phase two",
        status="stale",
        subject="Project A",
        predicate="phase",
        object_value="two",
        valid_from="2025-02-01T00:00:00Z",
        valid_until="2025-03-01T00:00:00Z",
    )
    uncited = _claim(
        service,
        "Uncited phase",
        status="stale",
        subject="Project A",
        predicate="phase",
        object_value="uncited",
        citations=False,
    )
    report = project_temporal_claims(
        service,
        [first.id, second.id, uncited.id],
        scope_allowlist=[SCOPE],
        intent="historical",
    )

    summaries = summarize_durative_states(report["claims"])

    assert summaries["states"][0]["claim_ids"] == [first.id, second.id]
    assert len(summaries["states"][0]["contributions"]) == 2
    assert summaries["omitted_uncited_claim_ids"] == [uncited.id]


def test_episode_windows_are_bounded_stable_and_authorized(tmp_path):
    service = _service(tmp_path)
    claims = [_claim(service, f"Episode {index}") for index in range(4)]
    evidence_ids = []
    for index, claim in enumerate(claims):
        _, evidence = _evidence(
            service,
            claim,
            f"message-{index}",
            chat_id="stable-session",
            occurred_at=f"2026-01-01T00:0{index}:00Z",
        )
        evidence_ids.append(evidence.id)
    cross = _claim(service, "Other scope", scope="project:other")
    _evidence(
        service,
        cross,
        "cross",
        chat_id="stable-session",
        occurred_at="2026-01-01T00:04:00Z",
    )

    first = project_evidence_episodes(
        service, [row.id for row in claims] + [cross.id],
        scope_allowlist=[SCOPE], max_window=3,
    )
    second = project_evidence_episodes(
        service, [row.id for row in claims] + [cross.id],
        scope_allowlist=[SCOPE], max_window=3,
    )

    assert first == second
    assert first["episodes"][0]["evidence_ids"] == evidence_ids[:3]
    assert first["episodes"][0]["has_more"] is True
    assert first["episodes"][0]["recurring"] is True


def test_retired_sensitive_and_malformed_temporal_rows_fail_closed(tmp_path):
    service = _service(tmp_path)
    retired = _claim(service, "Retired source")
    item, _ = _evidence(
        service,
        retired,
        "retired",
        chat_id="retired-session",
        occurred_at="2026-01-01T00:00:00Z",
    )
    CaptureRepository(service.store).retire_source(item.id, reason="fixture")
    malformed = _claim(service, "Malformed temporal")
    sensitive = _claim(service, "Sensitive placeholder")
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET event_time='not-a-date' WHERE id=?", (malformed.id,))
        conn.execute(
            "UPDATE claims SET text=?, event_time=? WHERE id=?",
            ("secret token sk-test-abcdefghijklmnopqrstuvwxyz", "2026-06-01T00:00:00Z", sensitive.id),
        )
        conn.commit()

    temporal = project_temporal_claims(
        service,
        [malformed.id, sensitive.id],
        scope_allowlist=[SCOPE],
        intent="occurrence",
        query_start="2026-01-01T00:00:00Z",
        query_end="2026-12-31T00:00:00Z",
    )
    episodes = project_evidence_episodes(
        service, [retired.id], scope_allowlist=[SCOPE]
    )

    assert temporal["claims"] == []
    assert temporal["diagnostics"]["malformed_temporal"] == 1
    assert temporal["diagnostics"]["unauthorized"] == 1
    assert episodes["episodes"] == []


def test_projection_rejects_naive_query_time(tmp_path):
    service = _service(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert now.tzinfo is not None

    try:
        project_temporal_claims(
            service, [], scope_allowlist=[SCOPE], intent="current",
            query_time="2026-01-01T00:00:00",
        )
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive query time must fail")
