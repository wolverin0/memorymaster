"""Model availability checks shared by Dreaming and compiled profiles."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def record_failure(provider: str, model: str, error: BaseException, *,
                   ledger: object | None = None, now: datetime | None = None) -> None:
    """Persist one operational alert in the Dreaming state ledger (the caller's when it has one)."""
    if ledger is None:
        from memorymaster.core.capture_control import capture_state_path
        from memorymaster.dreaming.ledger import DreamLedger

        ledger_path = Path(os.environ.get("MEMORYMASTER_CAPTURE_STATE_DB", "").strip() or capture_state_path())
        ledger = DreamLedger(ledger_path)
    ledger.record_hook_error(  # type: ignore[attr-defined]
        provider, f"model_preflight:{model}:{type(error).__name__}",
        now=now or datetime.now(timezone.utc),
    )


def preflight(client: object, model: str) -> None:
    """Raise when the provider says the configured model is unavailable.

    The check is looked up on the client's class, never through ``__getattr__``:
    test doubles and proxies that refuse unknown attributes must not turn the
    preflight itself into a failure. A configuration error (missing key) is
    conclusive and propagates; a transport failure is inconclusive.
    """
    if client is None or not callable(getattr(type(client), "model_available", None)):
        return
    from memorymaster.dreaming.providers import ProviderConfigError

    try:
        available = client.model_available(model)
    except ProviderConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - a transport failure is inconclusive, the run's breaker owns it
        LOGGER.debug("model preflight inconclusive for %s: %s", model, type(exc).__name__)
        return
    if not available:
        raise RuntimeError("configured model is not available")


def preflight_provider(provider: object) -> None:
    """Preflight one Dreaming/profile provider: its ``client`` and ``model`` attributes."""
    try:
        # Gemini providers are their own HTTP client; Antigravity providers wrap one in ``client``.
        client = getattr(provider, "client", None) or provider
        model = getattr(provider, "model", "")
    except Exception:  # noqa: BLE001 - a provider that refuses attribute reads has nothing to preflight
        return
    preflight(client, str(model))


def alert(provider: str, model: str, error: BaseException, *,
          ledger: object | None = None, now: datetime | None = None) -> None:
    """Emit the single operator-facing alert and persist its review row."""
    ledger_error_name = None
    try:
        record_failure(provider, model, error, ledger=ledger, now=now)
    except Exception as ledger_error:  # noqa: BLE001 - keep the original alert visible
        ledger_error_name = type(ledger_error).__name__
    LOGGER.error("provider model preflight failed: provider=%s model=%s error=%s ledger_error=%s",
                 provider, model, type(error).__name__, ledger_error_name or "none")
