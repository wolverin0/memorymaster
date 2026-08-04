from __future__ import annotations

from benchmarks.capture_ack_benchmark import run_benchmark


def test_capture_ack_benchmark_preserves_replay_and_integrity_contracts() -> None:
    result = run_benchmark(text_count=3, url_count=1)

    assert result["text_samples"] == 3
    assert result["reference_samples"] == 1
    assert result["replay_failures"] == 0
    assert result["update_job_delta"] == 2
    assert result["secret_warning"] == 1
    assert result["secret_url_rejected"] == 1
    assert result["duplicate_jobs"] == 0
    assert result["orphan_jobs"] == 0
    assert result["orphan_evidence"] == 0
    assert result["nonterminal_jobs"] == 0
    assert result["leased_jobs"] == 0
    assert result["leaked_secret_rows"] == 0
    assert result["provider_calls"] == 0
    assert result["worker_completed"] == result["fake_extractor_calls"]
