"""Connection retry logic with exponential backoff.

Wraps database connection creation to handle transient failures
(locked files, network blips, etc.) by retrying with exponential
backoff before giving up.

Configuration via environment variables
----------------------------------------
MEMORYMASTER_DB_RETRIES
    Maximum number of retry attempts (default: 3).

MEMORYMASTER_DB_RETRY_BASE
    Base delay in seconds for exponential backoff (default: 0.5).
    Actual delays: base * 2^attempt  ->  0.5s, 1.0s, 2.0s with defaults.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_RETRIES = 3
_DEFAULT_RETRY_BASE = 0.5


def _get_retry_config() -> tuple[int, float]:
    """Read retry settings from environment variables."""
    retries = int(os.environ.get("MEMORYMASTER_DB_RETRIES", str(_DEFAULT_RETRIES)))
    retry_base = float(os.environ.get("MEMORYMASTER_DB_RETRY_BASE", str(_DEFAULT_RETRY_BASE)))
    return max(retries, 0), max(retry_base, 0.0)


def connect_with_retry(connect_fn: Callable[[], T]) -> T:
    """Call *connect_fn* with exponential-backoff retries on failure.

    Parameters
    ----------
    connect_fn:
        A zero-argument callable that returns a database connection.
        If it raises any ``Exception``, the call is retried up to
        ``MEMORYMASTER_DB_RETRIES`` times.

    Returns
    -------
    The connection object returned by *connect_fn*.

    Raises
    ------
    Exception
        The original exception from *connect_fn* after all retries
        are exhausted, wrapped with additional context.
    """
    max_retries, base_delay = _get_retry_config()

    last_error: Exception | None = None
    for attempt in range(1 + max_retries):
        try:
            return connect_fn()
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "DB connection attempt %d/%d failed (%s: %s), retrying in %.1fs",
                    attempt + 1,
                    1 + max_retries,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "DB connection failed after %d attempts: %s: %s",
                    1 + max_retries,
                    type(exc).__name__,
                    exc,
                )

    # Should be unreachable, but satisfies the type checker.
    assert last_error is not None
    raise last_error
