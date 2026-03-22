"""Webhook notifications for claim events."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def fire_webhook(event_type: str, payload: dict) -> bool:
    """Fire an HTTP POST to MEMORYMASTER_WEBHOOK_URL.

    Returns True if the request succeeded, False if the URL is not configured
    or the request failed. Returns False immediately for empty URLs.
    Failures are logged at WARNING level and never propagate exceptions to the caller.

    Timeout is configurable via MEMORYMASTER_WEBHOOK_TIMEOUT env var (default: 5s).
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

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
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
