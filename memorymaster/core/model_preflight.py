"""Model availability checks shared by Dreaming and compiled profiles."""

from __future__ import annotations

import inspect
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def record_failure(provider: str, model: str, error: BaseException) -> None:
    """Persist one operational alert in the Dreaming state ledger."""
    from memorymaster.core.capture_control import capture_state_path
    from memorymaster.dreaming.ledger import DreamLedger

    ledger_path = Path(os.environ.get("MEMORYMASTER_CAPTURE_STATE_DB", "").strip() or capture_state_path())
    DreamLedger(ledger_path).record_hook_error(
        provider, f"model_preflight:{model}:{type(error).__name__}",
        now=datetime.now(timezone.utc),
    )


def preflight(client: object, model: str) -> None:
    """Raise when the provider says the configured model is unavailable.

    The check is looked up on the client's class, never through ``__getattr__``:
    test doubles and proxies that refuse unknown attributes must not turn the
    preflight itself into a failure.
    """
    if client is None or not callable(getattr(type(client), "model_available", None)):
        return
    try:
        available = client.model_available(model)
    except Exception as exc:  # noqa: BLE001 - a transport failure is inconclusive, the run's breaker owns it
        LOGGER.debug("model preflight inconclusive for %s: %s", model, type(exc).__name__)
        return
    if not available:
        raise RuntimeError("configured model is not available")


def preflight_provider(provider: object) -> None:
    """Preflight one Dreaming/profile provider: its ``client`` and ``model`` attributes."""
    # Gemini providers are their own HTTP client; Antigravity providers wrap one in ``client``.
    client = inspect.getattr_static(provider, "client", None) or provider
    model = inspect.getattr_static(provider, "model", "")
    preflight(client, str(model))


def alert(provider: str, model: str, error: BaseException) -> None:
    """Emit the single operator-facing alert and persist its review row."""
    ledger_error_name = None
    try:
        record_failure(provider, model, error)
    except Exception as ledger_error:  # noqa: BLE001 - keep the original alert visible
        ledger_error_name = type(ledger_error).__name__
    LOGGER.error("provider model preflight failed: provider=%s model=%s error=%s ledger_error=%s",
                 provider, model, type(error).__name__, ledger_error_name or "none")
