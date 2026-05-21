"""Mine the verbatim archive for corrections -> rule-shaped claims (v3.21.0-R1b).

The Stop hook archives every conversation turn into ``verbatim_memories`` but
nothing reads that archive. This module turns it into value: it scans user
turns for *corrections* ("no, do X instead") and the assistant turn they
reply to, asks an LLM to distill the exchange into a prescriptive rule, and
ingests it as a rule-shaped claim (see :mod:`memorymaster.rules`).

Cost discipline:
- A cheap SQL keyword pre-filter is the ONLY thing that touches the full
  table; the LLM is called only on candidate windows.
- The whole run is wrapped in :func:`llm_budget.cycle_scope` so the
  ``MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE`` cap aborts cleanly.
- A resumable watermark (``miner_state.rule_miner.last_verbatim_id``) means
  re-runs only scan rows ingested since the last pass.

Safety: rules land as low-confidence ``candidate`` claims (the steward must
promote them), provenance is cited back to the source verbatim rows, and any
rule whose text trips the sensitivity filter is dropped, not stored.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator

from memorymaster import llm_budget, llm_provider
from memorymaster.models import CitationInput
from memorymaster.rules import build_rule_fields
from memorymaster.security import redact_text

logger = logging.getLogger(__name__)

WATERMARK_KEY = "rule_miner.last_verbatim_id"
DEFAULT_PROVIDER = "claude_cli"
_TURN_TRUNCATE = 1500
_MIN_WINDOW_CHARS = 40

# Cheap pre-filter: a user turn is a *candidate* correction only if it
# contains one of these markers. Keeps the LLM off the other ~99% of rows.
_CORRECTION_KEYWORDS = (
    "no,", "no.", "don't", "do not", "dont ", "instead", "actually",
    "wrong", "not what", "that's not", "thats not", "revert", "undo",
    "should have", "shouldn't", "why did you", "no need", "stop ",
)

_MINER_STATE_DDL = """
CREATE TABLE IF NOT EXISTS miner_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
)
""".strip()

_CORRECTION_PROMPT = """You analyze ONE exchange between an AI coding assistant and its user.
The ASSISTANT line is what the assistant did or proposed.
The USER line is the user's reply.

Decide: does the USER reply CORRECT the assistant's behavior — does it tell the
assistant to act differently in a way that should change how it behaves next time?

If YES, output ONE JSON object and nothing else:
{"trigger": "<the recurring situation, short>", "action": "<the corrected behavior, imperative>", "rationale": "<why, one short clause>"}

If the reply is praise, thanks, a brand-new task, a question, a clarification
request, or anything that is NOT a behavioral correction, output exactly: {}

Output ONLY JSON. No markdown fences, no commentary."""


# ---------------------------------------------------------------------------
# Watermark (miner_state KV table)
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_miner_state(conn: sqlite3.Connection) -> None:
    """Idempotently create the watermark table.

    Migration ``0002_miner_state`` is the canonical creator (and adds the
    Postgres variant); this guard lets the miner run even if ``migrate`` was
    not invoked first — mirrors how MigrationRunner self-creates its own
    bookkeeping table.
    """
    conn.execute(_MINER_STATE_DDL)
    conn.commit()


def _get_watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM miner_state WHERE key = ?", (WATERMARK_KEY,)
    ).fetchone()
    if not row or row["value"] is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _set_watermark(conn: sqlite3.Connection, last_id: int) -> None:
    conn.execute(
        """INSERT INTO miner_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (WATERMARK_KEY, str(last_id), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Candidate scanning
# ---------------------------------------------------------------------------


def _candidate_batch(
    conn: sqlite3.Connection, since_id: int, batch_size: int
) -> list[sqlite3.Row]:
    """Return up to ``batch_size`` user turns after ``since_id`` that match a
    correction keyword. id-bounded + indexed — the only full-table contact."""
    like_clause = " OR ".join("lower(content) LIKE ?" for _ in _CORRECTION_KEYWORDS)
    params: list[Any] = [since_id]
    params.extend(f"%{kw}%" for kw in _CORRECTION_KEYWORDS)
    params.append(batch_size)
    return conn.execute(
        f"""SELECT id, session_id, content, scope FROM verbatim_memories
            WHERE role = 'user' AND id > ? AND ({like_clause})
            ORDER BY id ASC LIMIT ?""",
        params,
    ).fetchall()


def _preceding_assistant(
    conn: sqlite3.Connection, session_id: str, user_id: int
) -> sqlite3.Row | None:
    """The assistant turn immediately before ``user_id`` in the same session."""
    return conn.execute(
        """SELECT id, content FROM verbatim_memories
           WHERE session_id = ? AND id < ? AND role = 'assistant'
           ORDER BY id DESC LIMIT 1""",
        (session_id, user_id),
    ).fetchone()


def _build_window(assistant_content: str, user_content: str) -> str:
    asst = (assistant_content or "").strip()[:_TURN_TRUNCATE]
    user = (user_content or "").strip()[:_TURN_TRUNCATE]
    return f"ASSISTANT: {asst}\nUSER: {user}"


def _extract_rule(window: str) -> dict[str, str] | None:
    """Ask the LLM to distill a rule. Returns ``{trigger, action, rationale}``
    or ``None`` when there is no correction. May raise ``LLMBudgetExceeded``."""
    raw = llm_provider.call_llm(_CORRECTION_PROMPT, window)
    if not raw or not raw.strip():
        return None
    for item in llm_provider.parse_json_response(raw):
        if not isinstance(item, dict):
            continue
        trigger = (item.get("trigger") or "").strip()
        action = (item.get("action") or "").strip()
        if trigger and action:
            return {
                "trigger": trigger,
                "action": action,
                "rationale": (item.get("rationale") or "").strip(),
            }
    return None


def _is_sensitive_rule(rule: dict[str, str]) -> bool:
    joined = " | ".join(filter(None, (rule["trigger"], rule["action"], rule["rationale"])))
    _, findings = redact_text(joined)
    return bool(findings)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def mine_rules(
    db_path: str,
    service: Any,
    *,
    since_id: int | None = None,
    limit: int | None = None,
    batch_size: int = 200,
    provider: str = DEFAULT_PROVIDER,
    reset: bool = False,
) -> dict[str, Any]:
    """Scan verbatim corrections and ingest rule-shaped claims.

    Args:
        db_path: SQLite DB holding ``verbatim_memories`` (verbatim is
            SQLite-only; a Postgres DSN is rejected).
        service: a ``MemoryService`` used for ``ingest`` + dedup lookups.
        since_id: override the stored watermark start point.
        limit: max candidate windows examined this run (caps LLM calls).
        batch_size: rows fetched per SQL pre-filter page.
        provider: ``MEMORYMASTER_LLM_PROVIDER`` to use for this run.
        reset: clear the stored watermark before running.

    Returns a stats dict: ``candidates, llm_calls, ingested, duplicates,
    skipped, aborted_reason, last_id``.
    """
    if "://" in str(db_path):
        raise ValueError("rule mining is SQLite-only (verbatim_memories lives in SQLite)")

    stats: dict[str, Any] = {
        "candidates": 0,
        "llm_calls": 0,
        "ingested": 0,
        "duplicates": 0,
        "skipped": 0,
        "aborted_reason": None,
        "last_id": 0,
    }

    conn = _connect(db_path)
    try:
        _ensure_miner_state(conn)
        if reset:
            _set_watermark(conn, 0)
        start_id = since_id if since_id is not None else _get_watermark(conn)
        last_id = start_id
        stats["last_id"] = last_id

        saved_provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER")
        if provider:
            os.environ["MEMORYMASTER_LLM_PROVIDER"] = provider
        try:
            with llm_budget.cycle_scope() as budget:
                for row in _iter_candidates(conn, start_id, batch_size, limit):
                    stats["candidates"] += 1
                    outcome = _process_candidate(conn, service, row, stats)
                    if outcome == "aborted":
                        break
                    last_id = int(row["id"])
                    stats["last_id"] = last_id
                if budget.aborted_reason:
                    stats["aborted_reason"] = budget.aborted_reason
        finally:
            if saved_provider is None:
                os.environ.pop("MEMORYMASTER_LLM_PROVIDER", None)
            else:
                os.environ["MEMORYMASTER_LLM_PROVIDER"] = saved_provider

        _set_watermark(conn, last_id)
    finally:
        conn.close()
    return stats


def _iter_candidates(
    conn: sqlite3.Connection, start_id: int, batch_size: int, limit: int | None
) -> Iterator[sqlite3.Row]:
    """Yield candidate user rows across pages, honoring ``limit``."""
    cursor = start_id
    yielded = 0
    while True:
        batch = _candidate_batch(conn, cursor, batch_size)
        if not batch:
            return
        for row in batch:
            yield row
            cursor = int(row["id"])
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def _process_candidate(
    conn: sqlite3.Connection, service: Any, row: sqlite3.Row, stats: dict[str, Any]
) -> str:
    """Handle one candidate. Returns "aborted" if the LLM budget was hit,
    else "done". Mutates ``stats`` in place."""
    asst = _preceding_assistant(conn, row["session_id"], int(row["id"]))
    if asst is None:
        stats["skipped"] += 1
        return "done"

    window = _build_window(asst["content"], row["content"])
    if len(window) < _MIN_WINDOW_CHARS:
        stats["skipped"] += 1
        return "done"

    try:
        rule = _extract_rule(window)
    except llm_budget.LLMBudgetExceeded as exc:
        stats["aborted_reason"] = exc.reason
        return "aborted"

    stats["llm_calls"] += 1
    if rule is None or _is_sensitive_rule(rule):
        stats["skipped"] += 1
        return "done"

    idem = f"rule-miner-v{int(asst['id'])}-{int(row['id'])}"
    store = getattr(service, "store", None)
    if store is not None and hasattr(store, "get_claim_by_idempotency_key"):
        if store.get_claim_by_idempotency_key(idem) is not None:
            stats["duplicates"] += 1
            return "done"

    service.ingest(
        **build_rule_fields(rule["trigger"], rule["action"], rule["rationale"]),
        citations=[CitationInput(source="verbatim", locator=idem)],
        scope=row["scope"] or "project",
        confidence=0.4,
        source_agent="rule-miner",
        idempotency_key=idem,
    )
    stats["ingested"] += 1
    return "done"
