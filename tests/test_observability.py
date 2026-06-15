from __future__ import annotations

from memorymaster.core import observability


def setup_function() -> None:
    observability.reset_metrics()


def test_counter_increments() -> None:
    observability.bump_claim_ingested("claude-session")
    observability.bump_claim_ingested("claude-session")
    observability.bump_claim_ingested("codex-session")

    assert observability.metric_value(
        "claims_ingested_total",
        source_agent="claude-session",
    ) == 2
    assert observability.metric_value(
        "claims_ingested_total",
        source_agent="codex-session",
    ) == 1


def test_metrics_text_format() -> None:
    observability.bump_claim_ingested("claude-session")
    observability.bump_claim_ingested("claude-session")
    observability.bump_claim_ingested("claude-session")
    observability.bump_claim_filtered("api_key")

    text = observability.metrics_text()

    assert 'claims_ingested_total{source_agent="claude-session"} 3' in text
    assert 'claims_filtered_total{reason="api_key"} 1' in text
    assert "# TYPE claims_ingested_total counter" in text


def test_steward_duration_histogram(monkeypatch) -> None:
    ticks = iter([10.0, 10.25])
    monkeypatch.setattr(observability.time, "monotonic", lambda: next(ticks))

    with observability.steward_cycle_timer():
        pass

    assert observability.metric_value("steward_cycle_duration_seconds_count") == 1
    assert observability.metric_value("steward_cycle_duration_seconds_sum") == 0.25
    assert "steward_cycle_duration_seconds_count 1" in observability.metrics_text()
