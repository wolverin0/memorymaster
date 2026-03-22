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
    or the request failed.  Failures are logged at WARNING level and never
    propagate exceptions to the caller.
    """
    url = os.environ.get("MEMORYMASTER_WEBHOOK_URL", "")
    if not url:
        return False
    body = json.dumps({"event": event_type, "data": payload}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
        return True
    except Exception as exc:
        logger.warning("Webhook failed: %s", exc)
        return False
