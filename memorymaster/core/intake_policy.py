"""Intake policy — additive admission control for claim ingest.

This module sits at the single canonical chokepoint ``MemoryService.ingest``,
*after* the sacred sensitivity filter (``sanitize_claim_input``) and *before*
``store.create_claim``. It NEVER weakens, reorders, or gates the sensitivity
filter; it only ever RAISES THE BAR (rejects more, or attributes more), never
flips a previously-rejected claim into an accept.

Design (see ``.planning/P3-INTAKE-POLICY-SPEC.md``):

* ``evaluate_intake(...) -> IntakeDecision`` is a pure-ish function: given the
  inbound claim fields + an :class:`IntakePolicyConfig`, it returns an
  ``accept``/``reject`` decision plus any ``mutated_fields`` (e.g. a default
  ``source_agent`` tag). Quota/per-stop counters are the only stateful rules and
  they live in module-level token buckets keyed on ``source_agent`` /
  ``intake_batch_id`` (reset via :func:`reset_intake_state` for test isolation).
* Reject is surfaced by raising :class:`IntakeRejected`, a ``ValueError``
  subclass, so the existing ``except ValueError`` handlers in the MCP server and
  spool drainer surface it as a structured ``VALIDATION_ERROR`` with no new
  exception plumbing.

Every rule is configurable via env var with a SAFE DEFAULT that preserves
current accept behaviour, EXCEPT Rule B (session-state / heartbeat rejection),
which is the one intentional new default-rejection of non-claim telemetry.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class IntakeRejected(ValueError):
    """Raised when the intake policy rejects a claim.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers (MCP
    ``ingest_claim`` -> ``VALIDATION_ERROR``; spool drain per-envelope quarantine)
    surface it without new plumbing. Carries a machine-readable ``rule`` and
    ``reason`` for the observability counter + ``policy_decision`` event.
    """

    def __init__(self, message: str, *, rule: str, reason: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.reason = reason


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeDecision:
    """Outcome of :func:`evaluate_intake`.

    ``accept`` is always ``True`` here — rejection is signalled by raising
    :class:`IntakeRejected` so the call site needs no branch. ``mutated_fields``
    carries additive field overrides the caller must apply before
    ``create_claim`` (currently only ``source_agent`` default-tagging).
    """

    accept: bool = True
    mutated_fields: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config — read at call time so tests can monkeypatch os.environ
# ---------------------------------------------------------------------------


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = _env(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class IntakePolicyConfig:
    """All intake-policy knobs, resolved from env with safe defaults.

    Built via :meth:`from_env` at the start of every ``evaluate_intake`` call so
    a test (or a live operator) can flip a single env var and see it take effect
    on the next ingest without a process restart.
    """

    # Rule A — source_agent attribution.
    require_source_agent: str = "warn"  # warn | strict | off
    default_source_agent: str = "unknown"

    # Rule B — session-state / heartbeat rejection (the one new default-on).
    reject_session_state: bool = True
    rejected_scope_prefixes: tuple[str, ...] = ("session-state",)

    # Rule C — per-source_agent quota per window.
    quota_per_agent_per_window: int = 0  # 0 = unlimited (off)
    quota_window: str = "day"  # day | hour | cycle
    quota_exempt_agents: tuple[str, ...] = ()

    # Rule D — max distilled claims per stop-hook invocation.
    max_per_stop: int = 3  # 0 = unlimited

    @classmethod
    def from_env(cls) -> "IntakePolicyConfig":
        mode = _env("MEMORYMASTER_INTAKE_REQUIRE_SOURCE_AGENT", "warn").lower()
        if mode not in {"warn", "strict", "off"}:
            mode = "warn"
        window = _env("MEMORYMASTER_INTAKE_QUOTA_WINDOW", "day").lower()
        if window not in {"day", "hour", "cycle"}:
            window = "day"
        reject_ss = _env("MEMORYMASTER_INTAKE_REJECT_SESSION_STATE", "on").lower()
        prefixes = _env_csv(
            "MEMORYMASTER_INTAKE_REJECTED_SCOPE_PREFIXES", "session-state"
        ) or ("session-state",)
        return cls(
            require_source_agent=mode,
            default_source_agent=_env(
                "MEMORYMASTER_INTAKE_DEFAULT_SOURCE_AGENT", "unknown"
            )
            or "unknown",
            reject_session_state=reject_ss not in {"0", "off", "false", "no"},
            rejected_scope_prefixes=prefixes,
            quota_per_agent_per_window=_env_int(
                "MEMORYMASTER_INTAKE_QUOTA_PER_AGENT_PER_DAY", 0
            ),
            quota_window=window,
            quota_exempt_agents=_env_csv("MEMORYMASTER_INTAKE_QUOTA_EXEMPT_AGENTS"),
            max_per_stop=_env_int("MEMORYMASTER_INTAKE_MAX_PER_STOP", 3),
        )


# ---------------------------------------------------------------------------
# Stateful counters (Rules C + D) — module-level, lock-guarded, test-resettable
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
# Rule C: source_agent -> {window_key: count}. Only the current window is kept.
_QUOTA_COUNTS: dict[str, tuple[str, int]] = {}
# Rule D: intake_batch_id -> count.
_BATCH_COUNTS: dict[str, int] = {}


def reset_intake_state() -> None:
    """Clear quota + batch counters. Tests call this for isolation."""
    with _STATE_LOCK:
        _QUOTA_COUNTS.clear()
        _BATCH_COUNTS.clear()


def _window_key(window: str, now: float) -> str:
    """A deterministic bucket label for the configured rolling window.

    ``cycle`` is process-lifetime (single bucket) — it resets only when the
    counters are reset (e.g. at the start of a steward cycle via
    :func:`reset_intake_state`).
    """
    if window == "hour":
        return time.strftime("%Y-%m-%dT%H", time.gmtime(now))
    if window == "cycle":
        return "cycle"
    return time.strftime("%Y-%m-%d", time.gmtime(now))


# ---------------------------------------------------------------------------
# Heartbeat / session-state probe (Rule B) — deterministic, no LLM
# ---------------------------------------------------------------------------

_SESSION_ID_KEYS = ("session_id", "sessionId", "session")
_TS_KEYS = ("ts", "timestamp", "time", "at")


def _scope_is_rejected(scope: str | None, prefixes: tuple[str, ...]) -> bool:
    text = str(scope or "").strip().lower()
    if not text:
        return False
    for prefix in prefixes:
        p = prefix.strip().lower()
        if not p:
            continue
        if text == p or text.startswith(p + ".") or text.startswith(p + ":"):
            return True
    return False


def _is_heartbeat_shaped(text: str) -> bool:
    """True when ``text`` parses as a JSON object carrying a session id + a
    timestamp key and NO human-readable claim body — i.e. pure telemetry.

    Deterministic JSON probe (no LLM). Conservative: a JSON object that also
    carries a substantial free-text field (``text``/``claim``/``body``) is NOT
    treated as a heartbeat, so a real claim that merely happens to ship JSON
    metadata is never rejected by this probe.
    """
    candidate = (text or "").strip()
    if not candidate.startswith("{"):
        return False
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    has_session = any(k in parsed for k in _SESSION_ID_KEYS)
    has_ts = any(k in parsed for k in _TS_KEYS)
    if not (has_session and has_ts):
        return False
    # If there's a human-readable claim body, it's a real claim, not telemetry.
    for body_key in ("text", "claim", "body", "content", "message"):
        value = parsed.get(body_key)
        if isinstance(value, str) and len(value.strip()) >= 10:
            return False
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def evaluate_intake(
    *,
    text: str,
    claim_type: str | None,
    subject: str | None,
    scope: str | None,
    source_agent: str | None,
    require_source_agent: bool = False,
    intake_batch_id: str | None = None,
    intake_batch_max: int | None = None,
    config: IntakePolicyConfig | None = None,
    now: float | None = None,
) -> IntakeDecision:
    """Evaluate the intake policy for one inbound claim.

    Returns an :class:`IntakeDecision` (always ``accept=True`` with any additive
    ``mutated_fields``) or raises :class:`IntakeRejected`.

    Parameters
    ----------
    require_source_agent:
        Caller-class signal. ``True`` for explicit/external callers (MCP) so an
        empty ``source_agent`` is a hard reject under ``strict`` mode; ``False``
        for hooks/internal extractors so a missing tag is salvaged, never dropped.
    intake_batch_id / intake_batch_max:
        Stop-hook batch fence (Rule D). When the hook passes a stable batch id,
        the Nth+1 claim of that batch is rejected. ``intake_batch_max`` overrides
        the configured ``max_per_stop`` for that batch only.
    """
    cfg = config or IntakePolicyConfig.from_env()
    moment = time.time() if now is None else now
    mutated: dict[str, object] = {}

    agent = str(source_agent or "").strip()

    # --- Rule B — session-state / heartbeat telemetry (the one new default-on)
    if cfg.reject_session_state:
        if _scope_is_rejected(scope, cfg.rejected_scope_prefixes):
            raise IntakeRejected(
                "Session-state telemetry is not a knowledge claim; use the "
                "verbatim/session-state store, not the claims table.",
                rule="session_state",
                reason="scope_rejected",
            )
        if str(claim_type or "").strip().lower() == "heartbeat":
            raise IntakeRejected(
                "Heartbeat telemetry is not a knowledge claim; use the "
                "verbatim/session-state store, not the claims table.",
                rule="session_state",
                reason="heartbeat_type",
            )
        if _is_heartbeat_shaped(text):
            raise IntakeRejected(
                "Heartbeat-shaped JSON telemetry is not a knowledge claim; use "
                "the verbatim/session-state store, not the claims table.",
                rule="session_state",
                reason="heartbeat_shaped",
            )

    # --- Rule A — source_agent attribution (strict reject / warn default-tag)
    if not agent and cfg.require_source_agent != "off":
        if cfg.require_source_agent == "strict" and require_source_agent:
            raise IntakeRejected(
                "source_agent is required for explicit ingest calls.",
                rule="source_agent",
                reason="missing_source_agent",
            )
        # warn mode (or strict for an internal caller): default-tag, never drop.
        agent = cfg.default_source_agent or "unknown"
        mutated["source_agent"] = agent

    # Resolve the effective agent label used by the stateful rules below.
    effective_agent = agent or (cfg.default_source_agent or "unknown")

    # --- Rule D — max distilled claims per stop-hook invocation (batch fence)
    if intake_batch_id:
        cap = intake_batch_max if intake_batch_max is not None else cfg.max_per_stop
        if cap and cap > 0:
            with _STATE_LOCK:
                seen = _BATCH_COUNTS.get(intake_batch_id, 0)
                if seen >= cap:
                    raise IntakeRejected(
                        f"Stop-hook batch '{intake_batch_id}' exceeded the "
                        f"max-per-stop cap of {cap}.",
                        rule="max_per_stop",
                        reason="batch_cap_exceeded",
                    )
                _BATCH_COUNTS[intake_batch_id] = seen + 1

    # --- Rule C — per-source_agent quota per window
    quota = cfg.quota_per_agent_per_window
    if quota and quota > 0 and effective_agent not in cfg.quota_exempt_agents:
        wkey = _window_key(cfg.quota_window, moment)
        from memorymaster.core.usage_ledger import (
            UsageQuotaExceeded,
            reserve_intake_configured,
        )

        try:
            durable = reserve_intake_configured(
                actor=effective_agent,
                units=1,
                actor_limit=quota,
                window_key=wkey,
            )
        except UsageQuotaExceeded:
            if intake_batch_id:
                with _STATE_LOCK:
                    if intake_batch_id in _BATCH_COUNTS:
                        _BATCH_COUNTS[intake_batch_id] = max(
                            0, _BATCH_COUNTS[intake_batch_id] - 1
                        )
            raise IntakeRejected(
                f"source_agent '{effective_agent}' exceeded its durable quota "
                f"of {quota} claims per {cfg.quota_window}.",
                rule="quota",
                reason="quota_exceeded",
            ) from None
        if durable:
            return IntakeDecision(accept=True, mutated_fields=mutated)
        with _STATE_LOCK:
            current_window, count = _QUOTA_COUNTS.get(effective_agent, (wkey, 0))
            if current_window != wkey:
                count = 0
            if count >= quota:
                # Roll back the Rule D increment we just made so a quota reject
                # doesn't permanently consume a batch slot.
                if intake_batch_id and intake_batch_id in _BATCH_COUNTS:
                    _BATCH_COUNTS[intake_batch_id] = max(
                        0, _BATCH_COUNTS[intake_batch_id] - 1
                    )
                raise IntakeRejected(
                    f"source_agent '{effective_agent}' exceeded its quota of "
                    f"{quota} claims per {cfg.quota_window}.",
                    rule="quota",
                    reason="quota_exceeded",
                )
            _QUOTA_COUNTS[effective_agent] = (wkey, count + 1)

    return IntakeDecision(accept=True, mutated_fields=mutated)
