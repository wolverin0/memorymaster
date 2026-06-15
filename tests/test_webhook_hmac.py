"""Tests for v3.19.0-H3 webhook HMAC signing + replay protection.

Covers both directions:
- Outbound: fire_webhook adds X-MemoryMaster-Signature + X-MemoryMaster-Timestamp
  when MEMORYMASTER_WEBHOOK_SECRET is set; omits them otherwise (back-compat).
- Inbound: verify_webhook_signature accepts valid, rejects invalid/altered/replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.core import webhook
from memorymaster.core.webhook import (
    REPLAY_WINDOW_SECONDS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    fire_webhook,
    verify_webhook_signature,
)


@pytest.fixture(autouse=True)
def _clear_webhook_env(monkeypatch) -> Iterator[None]:
    for var in ("MEMORYMASTER_WEBHOOK_URL", "MEMORYMASTER_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Outbound signing
# ---------------------------------------------------------------------------


def test_outbound_no_secret_omits_signature_headers(monkeypatch):
    """Back-compat: when no secret is set, headers are unchanged from prior versions."""
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "http://example.com/wh")
    captured = {}

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured["headers"] = dict(req.headers)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock(status=200))
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with patch("memorymaster.core.webhook.urllib.request.urlopen", side_effect=fake_urlopen):
        assert fire_webhook("e", {}) is True

    keys_lower = {k.lower() for k in captured["headers"]}
    assert SIGNATURE_HEADER.lower() not in keys_lower
    assert TIMESTAMP_HEADER.lower() not in keys_lower


def test_outbound_with_secret_adds_signature_and_timestamp(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "http://example.com/wh")
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_SECRET", "shh-its-a-secret")
    captured: dict = {}

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured["headers"] = dict(req.headers)
        captured["body"] = bytes(req.data)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock(status=200))
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with patch("memorymaster.core.webhook.urllib.request.urlopen", side_effect=fake_urlopen):
        assert fire_webhook("claim_created", {"id": 7}) is True

    # urllib normalizes header capitalization to Title-Case, e.g. "X-Memorymaster-Signature".
    signature_key = next(
        (k for k in captured["headers"] if k.lower() == SIGNATURE_HEADER.lower()),
        None,
    )
    timestamp_key = next(
        (k for k in captured["headers"] if k.lower() == TIMESTAMP_HEADER.lower()),
        None,
    )
    assert signature_key is not None
    assert timestamp_key is not None

    signature = captured["headers"][signature_key]
    timestamp = captured["headers"][timestamp_key]
    body = captured["body"]
    assert signature.startswith("sha256=")
    assert timestamp.isdigit()

    # Independently recompute the expected signature and assert match.
    signing_input = f"{timestamp}.".encode("ascii") + body
    expected_mac = hmac.new(b"shh-its-a-secret", signing_input, hashlib.sha256).hexdigest()
    assert signature == f"sha256={expected_mac}"


# ---------------------------------------------------------------------------
# Inbound verification
# ---------------------------------------------------------------------------


def _make_signed_request(secret: str, body: bytes, *, timestamp_ms: int | None = None):
    """Helper: produce (signature_header, timestamp_header, body) for a signed request."""
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    signing_input = f"{ts}.".encode("ascii") + body
    mac = hmac.new(secret.encode(), signing_input, hashlib.sha256).hexdigest()
    return (f"sha256={mac}", str(ts), body)


def test_verify_valid_signature_passes():
    secret = "shared-secret"
    body = json.dumps({"event": "x", "data": {}}).encode()
    sig, ts, _ = _make_signed_request(secret, body)
    ok, reason = verify_webhook_signature(body, sig, ts, secret=secret)
    assert ok is True
    assert reason == "ok"


def test_verify_missing_signature_header_fails():
    secret = "s"
    body = b'{"event":"x","data":{}}'
    ok, reason = verify_webhook_signature(body, None, "1234", secret=secret)
    assert ok is False
    assert reason == "missing_headers"


def test_verify_missing_timestamp_header_fails():
    secret = "s"
    body = b'{}'
    ok, reason = verify_webhook_signature(body, "sha256=abc", None, secret=secret)
    assert ok is False
    assert reason == "missing_headers"


def test_verify_missing_secret_fails():
    body = b'{}'
    ok, reason = verify_webhook_signature(body, "sha256=abc", "1234", secret="")
    assert ok is False
    assert reason == "missing_secret"


def test_verify_invalid_timestamp_format_fails():
    secret = "s"
    body = b'{}'
    ok, reason = verify_webhook_signature(
        body, "sha256=abc", "not-a-number", secret=secret
    )
    assert ok is False
    assert reason == "invalid_timestamp"


def test_verify_altered_body_fails():
    secret = "s"
    original_body = b'{"amount":100}'
    sig, ts, _ = _make_signed_request(secret, original_body)
    tampered_body = b'{"amount":99999}'  # attacker edits the payload
    ok, reason = verify_webhook_signature(tampered_body, sig, ts, secret=secret)
    assert ok is False
    assert reason == "bad_signature"


def test_verify_wrong_secret_fails():
    body = b'{}'
    sig, ts, _ = _make_signed_request("correct-secret", body)
    ok, reason = verify_webhook_signature(body, sig, ts, secret="wrong-secret")
    assert ok is False
    assert reason == "bad_signature"


def test_verify_replayed_request_outside_window_fails():
    secret = "s"
    body = b'{}'
    old_ts = int(time.time() * 1000) - (REPLAY_WINDOW_SECONDS + 60) * 1000
    sig, ts, _ = _make_signed_request(secret, body, timestamp_ms=old_ts)
    ok, reason = verify_webhook_signature(body, sig, ts, secret=secret)
    assert ok is False
    assert reason == "replay_window"


def test_verify_future_timestamp_outside_window_fails():
    """Clock-skewed-forward attack: timestamp in the future also rejected."""
    secret = "s"
    body = b'{}'
    future_ts = int(time.time() * 1000) + (REPLAY_WINDOW_SECONDS + 60) * 1000
    sig, ts, _ = _make_signed_request(secret, body, timestamp_ms=future_ts)
    ok, reason = verify_webhook_signature(body, sig, ts, secret=secret)
    assert ok is False
    assert reason == "replay_window"


def test_verify_uses_env_secret_when_secret_arg_none(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_SECRET", "env-secret")
    body = b'{}'
    sig, ts, _ = _make_signed_request("env-secret", body)
    ok, reason = verify_webhook_signature(body, sig, ts)  # no secret kwarg
    assert ok is True
    assert reason == "ok"


def test_verify_constant_time_comparison_used():
    """Sanity check that hmac.compare_digest is the comparator (not == on strings).
    We can't directly observe the comparison primitive, but we can assert that
    a near-miss differing only in the last char fails — proving HMAC ran."""
    secret = "s"
    body = b'{}'
    sig, ts, _ = _make_signed_request(secret, body)
    # Flip the last hex char
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    ok, reason = verify_webhook_signature(body, flipped, ts, secret=secret)
    assert ok is False
    assert reason == "bad_signature"
