"""Webhook notifications for claim events.

v3.19.0-H3 adds HMAC-SHA-256 signing + timestamp + replay protection. Signing
activates when ``MEMORYMASTER_WEBHOOK_SECRET`` is set; otherwise the wire
format is unchanged from prior versions (back-compat).

Outbound headers added when signing is enabled:
    X-MemoryMaster-Signature   "sha256=<hexdigest>"
    X-MemoryMaster-Timestamp   "<unix-ms-utc>"

The signing input is the literal byte string ``{timestamp}.{body}`` —
canonical and unambiguous. Receivers should split on the first ``.`` and
recompute HMAC-SHA-256 with the shared secret. Replay protection: the
timestamp must be within ``REPLAY_WINDOW_SECONDS`` of receive time.

Inbound verifier helper: ``verify_webhook_signature`` for receivers built
on top of MemoryMaster's webhook contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


REPLAY_WINDOW_SECONDS = 300  # 5 minutes; receivers reject timestamps outside this
SIGNATURE_HEADER = "X-MemoryMaster-Signature"
TIMESTAMP_HEADER = "X-MemoryMaster-Timestamp"


def _webhook_secret() -> str:
    return os.environ.get("MEMORYMASTER_WEBHOOK_SECRET", "").strip()


def _signing_input(timestamp_ms: int, body: bytes) -> bytes:
    """Canonical signing input — ``{timestamp_ms}.{body}`` as bytes."""
    return f"{timestamp_ms}.".encode("ascii") + body


def _sign(secret: str, signing_input: bytes) -> str:
    """Return ``sha256=<hexdigest>`` over the signing input with the secret."""
    mac = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def fire_webhook(event_type: str, payload: dict) -> bool:
    """Fire an HTTP POST to MEMORYMASTER_WEBHOOK_URL.

    Returns True if the request succeeded, False if the URL is not configured
    or the request failed. Returns False immediately for empty URLs.
    Failures are logged at WARNING level and never propagate exceptions to the caller.

    Timeout is configurable via MEMORYMASTER_WEBHOOK_TIMEOUT env var (default: 5s).

    When ``MEMORYMASTER_WEBHOOK_SECRET`` is set, the request includes
    ``X-MemoryMaster-Signature`` and ``X-MemoryMaster-Timestamp`` headers
    (HMAC-SHA-256 over ``{timestamp}.{body}``). Otherwise the wire format
    is unchanged from earlier versions.
    """
    url = os.environ.get("MEMORYMASTER_WEBHOOK_URL", "").strip()
    if not url:
        logger.debug("fire_webhook: MEMORYMASTER_WEBHOOK_URL not configured")
        return False

    if not isinstance(event_type, str) or not isinstance(payload, dict):
        logger.warning("fire_webhook: invalid event_type or payload")
        return False

    try:
        timeout = float(os.environ.get("MEMORYMASTER_WEBHOOK_TIMEOUT", "5"))
        timeout = max(0.1, min(timeout, 60))  # Clamp between 0.1s and 60s
    except (ValueError, TypeError):
        timeout = 5

    try:
        body = json.dumps({"event": event_type, "data": payload}).encode()
    except (TypeError, ValueError) as exc:
        logger.warning("fire_webhook: failed to serialize payload: %s", exc)
        return False

    headers: dict[str, str] = {"Content-Type": "application/json"}
    secret = _webhook_secret()
    if secret:
        # HMAC over {timestamp_ms}.{body} — receivers must reconstruct the
        # same signing input to verify. Timestamp protects against replay.
        timestamp_ms = int(time.time() * 1000)
        signature = _sign(secret, _signing_input(timestamp_ms, body))
        headers[SIGNATURE_HEADER] = signature
        headers[TIMESTAMP_HEADER] = str(timestamp_ms)

    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status >= 400:
                logger.warning("Webhook returned HTTP %d", resp.status)
                return False
        return True
    except Exception as exc:
        logger.warning("Webhook failed: %s", exc)
        return False


def verify_webhook_signature(
    body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    *,
    secret: str | None = None,
    replay_window_seconds: int = REPLAY_WINDOW_SECONDS,
    now_ms: int | None = None,
) -> tuple[bool, str]:
    """Verify a received webhook's HMAC signature and replay window.

    Returns ``(ok, reason)``. Reason is one of:
        - ``"ok"``                — signature valid, timestamp within window
        - ``"missing_headers"``   — signature or timestamp header absent
        - ``"missing_secret"``    — no secret configured to verify against
        - ``"invalid_timestamp"`` — timestamp header not a parseable int
        - ``"replay_window"``     — timestamp outside ``replay_window_seconds``
        - ``"bad_signature"``     — HMAC mismatch (either wrong secret or
                                     altered body)

    Uses ``hmac.compare_digest`` to prevent timing attacks. The caller
    should always treat ``ok=False`` as a hard rejection.
    """
    if secret is None:
        secret = _webhook_secret()
    if not secret:
        return (False, "missing_secret")

    if not signature_header or not timestamp_header:
        return (False, "missing_headers")

    try:
        timestamp_ms = int(timestamp_header)
    except (ValueError, TypeError):
        return (False, "invalid_timestamp")

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    window_ms = replay_window_seconds * 1000
    if abs(now - timestamp_ms) > window_ms:
        return (False, "replay_window")

    expected = _sign(secret, _signing_input(timestamp_ms, body))
    if not hmac.compare_digest(expected, signature_header):
        return (False, "bad_signature")

    return (True, "ok")
