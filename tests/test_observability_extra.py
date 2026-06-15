"""Coverage hardening for observability counters/bumps and webhook HMAC paths.

These tests target behaviours that are load-bearing for production monitoring and
security, and that were previously unexercised:

- Counter/histogram invariants (non-negative-only, label normalization, the
  Prometheus text rendering) -- because a silently-accepted negative increment or
  a mislabeled counter corrupts every dashboard built on top of ``/metrics``.
- The sensitivity ``_filter_reason`` collapsing map -- the WHY is that filtered
  claims must be grouped by a *bounded* set of reasons (jwt/ip/api_key/
  password/token), never by the raw secret-typed finding string, so the metric
  can never leak which exact secret pattern fired.
- Webhook HMAC signing + verification round-trip and every rejection reason --
  because the signature is the only thing standing between a receiver and a
  forged/replayed claim event.
"""

from __future__ import annotations

import json

import pytest

from memorymaster.core import observability as obs
from memorymaster.core import webhook


@pytest.fixture(autouse=True)
def _clean_metrics():
    obs.reset_metrics()
    yield
    obs.reset_metrics()


# --------------------------------------------------------------------------- #
# Counters / bumps
# --------------------------------------------------------------------------- #


def test_bump_counter_rejects_negative_amount():
    # Counters are monotonic; accepting a negative would let a caller rewrite
    # history and break rate() queries downstream.
    with pytest.raises(ValueError):
        obs.bump_counter("claims_ingested_total", amount=-1)


def test_bump_counter_accumulates_per_label_set():
    obs.bump_claim_ingested("claude-session")
    obs.bump_claim_ingested("claude-session")
    obs.bump_claim_ingested("codex")
    assert obs.metric_value("claims_ingested_total", source_agent="claude-session") == 2
    assert obs.metric_value("claims_ingested_total", source_agent="codex") == 1


def test_blank_label_normalizes_to_unknown():
    # A missing source agent must still produce a queryable series, not a
    # collapsed/empty label that disappears from the dashboard.
    obs.bump_claim_ingested(None)
    assert obs.metric_value("claims_ingested_total", source_agent="unknown") == 1


def test_bump_claim_filtered_findings_iterates_all():
    obs.bump_claim_filtered_findings(["openai_key", "password", "openai_key"])
    assert obs.metric_value("claims_filtered_total", reason="api_key") == 2
    assert obs.metric_value("claims_filtered_total", reason="password") == 1


def test_bump_compactor_and_decay_runs():
    obs.bump_compactor_run("ok")
    obs.bump_decay_run("error")
    assert obs.metric_value("compactor_run_total", status="ok") == 1
    assert obs.metric_value("decay_run_total", status="error") == 1


@pytest.mark.parametrize(
    ("finding", "expected_reason"),
    [
        ("jwt_token", "jwt"),
        ("private_ip", "ip"),
        ("openai_key", "api_key"),
        ("anthropic_key", "api_key"),
        ("aws_access_key", "api_key"),
        ("some_random_api_key", "api_key"),
        ("session_key", "api_key"),
        ("user_password", "password"),
        ("db_credential", "password"),
        ("bearer_token", "token"),
        ("weird_finding", "weird_finding"),
    ],
)
def test_filter_reason_collapses_to_bounded_buckets(finding, expected_reason):
    # WHY: the filtered-claims metric must never carry the raw secret-typed
    # finding; it collapses to a small, leak-safe vocabulary.
    obs.bump_claim_filtered(finding)
    assert obs.metric_value("claims_filtered_total", reason=expected_reason) == 1


# --------------------------------------------------------------------------- #
# Histogram + timer
# --------------------------------------------------------------------------- #


def test_observe_duration_rejects_negative():
    with pytest.raises(ValueError):
        obs.observe_steward_cycle_duration(-0.1)


def test_steward_cycle_timer_records_one_sample():
    with obs.steward_cycle_timer():
        pass
    assert obs.metric_value("steward_cycle_duration_seconds_count") == 1
    assert obs.metric_value("steward_cycle_duration_seconds_sum") >= 0.0


def test_metric_value_unknown_counter_is_zero():
    assert obs.metric_value("does_not_exist_total", status="x") == 0


# --------------------------------------------------------------------------- #
# Prometheus text rendering
# --------------------------------------------------------------------------- #


def test_metrics_text_renders_all_families_and_escapes_labels():
    obs.bump_claim_ingested('weird"agent\\name')
    obs.bump_claim_filtered("jwt_token")
    obs.bump_compactor_run("ok")
    obs.bump_decay_run("ok")
    obs.observe_steward_cycle_duration(0.05)

    text = obs.metrics_text()

    assert "# TYPE claims_ingested_total counter" in text
    assert "# TYPE steward_cycle_duration_seconds histogram" in text
    assert 'reason="jwt"' in text
    assert "steward_cycle_duration_seconds_count 1" in text
    assert 'le="+Inf"' in text
    # Quotes and backslashes in a label value must be escaped so the exposition
    # format stays parseable by Prometheus.
    assert 'weird\\"agent\\\\name' in text
    assert text.endswith("\n")


# --------------------------------------------------------------------------- #
# Webhook: HMAC sign / verify
# --------------------------------------------------------------------------- #


def test_verify_round_trip_accepts_genuine_signature():
    secret = "topsecret"
    body = json.dumps({"event": "claim.created", "data": {"id": 1}}).encode()
    ts = 1_700_000_000_000
    sig = webhook._sign(secret, webhook._signing_input(ts, body))

    ok, reason = webhook.verify_webhook_signature(
        body, sig, str(ts), secret=secret, now_ms=ts
    )
    assert ok is True
    assert reason == "ok"


def test_verify_rejects_tampered_body():
    secret = "topsecret"
    ts = 1_700_000_000_000
    sig = webhook._sign(secret, webhook._signing_input(ts, b'{"a":1}'))
    ok, reason = webhook.verify_webhook_signature(
        b'{"a":2}', sig, str(ts), secret=secret, now_ms=ts
    )
    assert ok is False
    assert reason == "bad_signature"


def test_verify_missing_secret(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_WEBHOOK_SECRET", raising=False)
    ok, reason = webhook.verify_webhook_signature(b"{}", "sha256=x", "123")
    assert ok is False
    assert reason == "missing_secret"


def test_verify_missing_headers():
    ok, reason = webhook.verify_webhook_signature(b"{}", None, None, secret="s")
    assert (ok, reason) == (False, "missing_headers")


def test_verify_invalid_timestamp():
    ok, reason = webhook.verify_webhook_signature(
        b"{}", "sha256=x", "not-an-int", secret="s"
    )
    assert (ok, reason) == (False, "invalid_timestamp")


def test_verify_outside_replay_window():
    secret = "s"
    ts = 1_700_000_000_000
    sig = webhook._sign(secret, webhook._signing_input(ts, b"{}"))
    # 10 minutes later -- outside the default 5-minute window.
    later = ts + 600_000
    ok, reason = webhook.verify_webhook_signature(
        b"{}", sig, str(ts), secret=secret, now_ms=later
    )
    assert (ok, reason) == (False, "replay_window")


# --------------------------------------------------------------------------- #
# Webhook: fire_webhook
# --------------------------------------------------------------------------- #


def test_fire_webhook_no_url_returns_false(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_WEBHOOK_URL", raising=False)
    assert webhook.fire_webhook("e", {}) is False


def test_fire_webhook_rejects_bad_arg_types(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "https://example.test/hook")
    assert webhook.fire_webhook(123, {}) is False  # type: ignore[arg-type]
    assert webhook.fire_webhook("e", ["not", "a", "dict"]) is False  # type: ignore[arg-type]


def test_fire_webhook_unserializable_payload(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "https://example.test/hook")
    assert webhook.fire_webhook("e", {"x": object()}) is False


def test_fire_webhook_signs_and_posts(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_TIMEOUT", "3")
    monkeypatch.setattr(webhook.urllib.request, "urlopen", _fake_urlopen)

    assert webhook.fire_webhook("claim.created", {"id": 7}) is True
    # Signing must be active when a secret is configured.
    assert webhook.SIGNATURE_HEADER.lower() in captured["headers"]
    assert webhook.TIMESTAMP_HEADER.lower() in captured["headers"]
    assert captured["timeout"] == 3.0
    # And the signature must verify against the exact body that was sent.
    ts = int(captured["headers"][webhook.TIMESTAMP_HEADER.lower()])
    expected = webhook._sign("shh", webhook._signing_input(ts, captured["data"]))
    assert captured["headers"][webhook.SIGNATURE_HEADER.lower()] == expected


def test_fire_webhook_unsigned_when_no_secret(monkeypatch):
    captured = {}

    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _Resp()

    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.delenv("MEMORYMASTER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webhook.urllib.request, "urlopen", _fake_urlopen)

    assert webhook.fire_webhook("e", {"a": 1}) is True
    assert webhook.SIGNATURE_HEADER.lower() not in captured["headers"]


def test_fire_webhook_bad_timeout_falls_back_to_default(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.delenv("MEMORYMASTER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_TIMEOUT", "not-a-number")
    monkeypatch.setattr(webhook.urllib.request, "urlopen", _fake_urlopen)

    assert webhook.fire_webhook("e", {}) is True
    assert captured["timeout"] == 5


def test_fire_webhook_http_error_status_returns_false(monkeypatch):
    class _Resp:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.delenv("MEMORYMASTER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook.urllib.request, "urlopen", lambda req, timeout=None: _Resp()
    )
    assert webhook.fire_webhook("e", {}) is False


def test_fire_webhook_swallows_transport_exception(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.delenv("MEMORYMASTER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webhook.urllib.request, "urlopen", _boom)
    # Failures must never propagate to the caller (claim ingest must not break
    # because a webhook endpoint is down).
    assert webhook.fire_webhook("e", {}) is False
