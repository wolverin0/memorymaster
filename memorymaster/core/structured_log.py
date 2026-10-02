"""JSON-lines logging for the long-lived surfaces: the shared MCP server and the scheduled jobs (T-0764).

One root handler renders every stdlib ``logging`` record, MemoryMaster's own
``logging.getLogger(__name__)`` calls and third-party ones (uvicorn) alike, as
one JSON object per line:

    {"ts": "...Z", "level": "info", "logger": "...", "component": "mcp-http",
     "event": "mcp_tool", "surface": "mcp", "tool": "recall", "scope": "project:x",
     "duration_ms": 12.3, "outcome": "ok"}

structlog's ``ProcessorFormatter`` renders when structlog is installed; a stdlib
formatter with the same keys is the fallback, so a runtime without structlog
still writes parseable lines. Structured fields travel on the record as
``mm_fields`` (see :func:`log_event`), never in the message text. Logging never
raises into the caller.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import IO, Any

_HANDLER_MARK = "_memorymaster_structured"
_FIELDS_ATTR = "mm_fields"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = getattr(record, _FIELDS_ATTR, None)
    return dict(fields) if isinstance(fields, Mapping) else {}


class _StdlibJsonFormatter(logging.Formatter):
    """Fallback renderer with the same keys as the structlog path."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_now(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "component": self.component,
            "event": record.getMessage(),
        }
        payload.update(_record_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _structlog_formatter(component: str) -> logging.Formatter | None:
    try:
        import structlog
    except ImportError:
        return None

    def add_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        record = event_dict.get("_record")
        event_dict.setdefault("component", component)
        if isinstance(record, logging.LogRecord):
            event_dict.setdefault("logger", record.name)
            event_dict.update(_record_fields(record))
        return event_dict

    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.stdlib.add_log_level,
            add_context,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False, default=str),
        ],
    )


def configure_structured_logging(stream: IO[str] | None = None, *, component: str,
                                 level: int = logging.INFO, exclusive: bool = False) -> str:
    """Route the root logger to ``stream`` as JSON lines; returns the renderer used.

    Idempotent: a handler installed by an earlier call is replaced, other handlers stay
    unless ``exclusive`` is set. A process whose log must be JSON only (the shared HTTP
    server) passes it: importing the MCP server already put FastMCP's RichHandler on
    the root logger, which wrote every record a second time as wrapped text.
    """
    formatter = _structlog_formatter(component)
    renderer = "structlog" if formatter is not None else "stdlib-json"
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter or _StdlibJsonFormatter(component))
    setattr(handler, _HANDLER_MARK, True)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if exclusive or getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)  # warnings.warn becomes a JSON line too
    return renderer


def reset_structured_logging() -> None:
    """Remove the handler :func:`configure_structured_logging` installed (other handlers stay)."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
    logging.captureWarnings(False)


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """One structured event; ``fields`` become top-level JSON keys. Never raises."""
    try:
        if logger.isEnabledFor(level):
            logger.log(level, event, extra={_FIELDS_ATTR: fields})
    except Exception:  # noqa: BLE001 - logging must never break the caller
        pass


@contextmanager
def timed_event(logger: logging.Logger, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log ``event`` once with ``duration_ms`` and ``outcome`` (``ok`` or ``error`` plus ``error_type``).

    The yielded dict can add fields before the event is written (for example a result count).
    """
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield extra
    except BaseException as exc:
        extra.pop("outcome", None)
        log_event(logger, event, level=logging.WARNING, **{**fields, **extra}, outcome="error",
                  error_type=type(exc).__name__, duration_ms=round((time.perf_counter() - started) * 1000, 1))
        raise
    outcome = extra.pop("outcome", "ok")  # a caller may report e.g. "failed" without raising
    log_event(logger, event, **{**fields, **extra}, outcome=outcome,
              duration_ms=round((time.perf_counter() - started) * 1000, 1))
