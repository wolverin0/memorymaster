"""Turnkey BEAT-3 session-end ingest for Codex / generic MCP agents.

This is the non-Claude mirror of ``config_templates/hooks/memorymaster-auto-ingest.py``.
Claude gets BEAT 3 (session-end distilled ingest) for free via its ``Stop`` hook;
Codex and other agents have no native ``Stop`` event, so without this script their
only BEAT-3 path is the agent *voluntarily* calling ``ingest_claim`` (per AGENTS.md),
or the per-turn autologger which captures verbatim turns and — historically — dropped
``source_agent`` attribution entirely.

This script closes that gap with the SAME discipline the Claude hook uses:

  * distills **at most 3** learnings from the tail of a transcript via the shared
    cheap LLM (``memorymaster.core.llm_provider``), never verbatim per-turn capture;
  * drops anything that trips the canonical sensitivity filter
    (``memorymaster.core.security.redact_text``) *before* it reaches the DB;
  * routes every claim through ``MemoryService.ingest`` (the documented service path —
    sensitivity sanitize + intake policy + dedup + auto-citation), **never a raw INSERT**;
  * sets ``source_agent`` (default ``"codex-session"``, overridable) so the per-agent
    provenance view attributes the claims correctly — NON-NEGOTIABLE, never NULL;
  * stamps one ``intake_batch_id`` with ``intake_batch_max=3`` so the P3 intake policy
    Rule D fences the batch even if the ``[:3]`` slice is tampered with.

It is intentionally a standalone CLI so it can be wired as a Codex notify/exit hook,
launched manually at session end, or scheduled. It does NOT touch the live
``memorymaster.db`` unless pointed at it; tests point it at a temp DB.

Usage::

    python scripts/agent_session_end_ingest.py \
        --db /path/to/memorymaster.db \
        --transcript ~/.codex/sessions/rollout-XXXX.jsonl \
        --source-agent codex-session \
        --cwd /path/to/project
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# Number of distilled learnings per session end. Mirrors the Claude hook norm
# (memorymaster-auto-ingest.py:146 + intake policy Rule D). Both the [:N] slice
# below AND intake_batch_max are set to this so the policy layer fences a flood
# even if the slice is removed.
MAX_LEARNINGS = 3

DISTILL_PROMPT = (
    "You are a memory curator. Extract max 3 non-obvious learnings.\n"
    'Return JSON array: [{"text": "one-line", "claim_type": "fact|decision|constraint", '
    '"subject": "entity", "predicate": "aspect"}]\n'
    "Only: bug root causes, decisions, gotchas, constraints. "
    "Never: credentials, IPs, paths, code. Empty array if nothing worth remembering."
)


def _is_sensitive_claim(claim: dict[str, Any]) -> bool:
    """Return True if any of (text, subject, predicate, object_value) trips the
    canonical sensitivity filter. Caller drops the claim entirely.

    Mirrors memorymaster-auto-ingest.py:_is_sensitive_claim. Import is deferred so
    the module imports cleanly even before the package resolves on sys.path, and
    fails CLOSED (drop) if the filter cannot be imported."""
    try:
        from memorymaster.core.security import redact_text
    except ImportError:
        return True
    joined = " | ".join(
        str(claim.get(key, "") or "")
        for key in ("text", "subject", "predicate", "object_value")
    )
    _, findings = redact_text(joined)
    return bool(findings)


def _extract_assistant_text(transcript_path: str) -> str:
    """Concatenate the tail of assistant/model messages from a JSONL transcript.

    Supports both Claude-Code-style transcripts (role+content nested under
    ``message``) and Codex rollout JSONL (flat ``role``/``content``, possibly under
    a ``payload`` envelope). Returns "" when nothing usable is found."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    messages: list[str] = []
    try:
        lines = Path(transcript_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-200:]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        # Unwrap common envelopes: Claude uses `message`, Codex rollout uses
        # `payload` for the actual {role, content} record.
        record = entry
        for key in ("message", "payload"):
            inner = entry.get(key)
            if isinstance(inner, dict) and ("role" in inner or "content" in inner):
                record = inner
                break
        role = str(record.get("role") or "")
        if role not in ("assistant", "model"):
            continue
        content = record.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            )
        if text and len(text) > 30:
            messages.append(text[:500])
            if sum(len(m) for m in messages) > 3000:
                break
    if sum(len(m) for m in messages) < 50:
        return ""
    return "\n---\n".join(reversed(messages))


def _distill(assistant_text: str) -> list[dict[str, Any]]:
    """Distill <=MAX_LEARNINGS learnings via the shared cheap LLM, then drop any
    that trip the sensitivity filter. Returns at most MAX_LEARNINGS claims."""
    from memorymaster.core.llm_provider import call_llm, parse_json_response

    response = call_llm(DISTILL_PROMPT, assistant_text)
    claims = parse_json_response(response)
    if not isinstance(claims, list):
        return []
    clean = [c for c in claims if isinstance(c, dict) and not _is_sensitive_claim(c)]
    return clean[:MAX_LEARNINGS]


def _scope_for(cwd: str | None) -> str:
    if not cwd:
        return "global"
    return "project:" + os.path.basename(cwd).lower().replace(" ", "-")


def ingest_learnings(
    db_path: str,
    claims: list[dict[str, Any]],
    *,
    source_agent: str,
    cwd: str | None,
) -> int:
    """Ingest distilled learnings through MemoryService.ingest with attribution.

    NON-NEGOTIABLE invariants (do not relax):
      * routes through service.ingest (sensitivity sanitize + intake policy + dedup),
        NEVER a raw INSERT;
      * source_agent is always set (never NULL);
      * one intake_batch_id with intake_batch_max=MAX_LEARNINGS fences the batch.
    Returns the number of claims successfully ingested."""
    if not source_agent or not source_agent.strip():
        raise ValueError("source_agent is required and must be non-empty.")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_path, workspace_root=Path(cwd or os.getcwd()))
    scope = _scope_for(cwd)
    batch_id = "session-end-" + uuid.uuid4().hex[:16]
    ingested = 0
    for claim in claims[:MAX_LEARNINGS]:
        text = str(claim.get("text", "") or "")
        if not text or len(text) < 10:
            continue
        # Defense-in-depth: the LLM output already passed _is_sensitive_claim in
        # _distill, but re-check here so a caller that supplies claims directly
        # (e.g. a test or a different distiller) still gets the drop.
        if _is_sensitive_claim(claim):
            continue
        text_hash = hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]
        try:
            svc.ingest(
                text=text,
                citations=[
                    CitationInput(source=source_agent, locator=scope, excerpt=text[:200])
                ],
                idempotency_key=f"{source_agent}-{text_hash}",
                claim_type=str(claim.get("claim_type", "fact") or "fact"),
                subject=str(claim.get("subject", "codebase") or "codebase"),
                predicate=str(claim.get("predicate", "observation") or "observation"),
                object_value=claim.get("object_value"),
                scope=scope,
                confidence=0.6,
                source_agent=source_agent,
                intake_batch_id=batch_id,
                intake_batch_max=MAX_LEARNINGS,
            )
            ingested += 1
        except Exception:
            # One bad claim must not abort the rest (mirrors the Claude hook).
            continue
    return ingested


def run(
    db_path: str,
    transcript_path: str,
    *,
    source_agent: str,
    cwd: str | None,
) -> int:
    """Distill the transcript tail and ingest <=MAX_LEARNINGS attributed claims.
    Returns the number ingested (0 when nothing worth remembering)."""
    assistant_text = _extract_assistant_text(transcript_path)
    if not assistant_text:
        return 0
    claims = _distill(assistant_text)
    if not claims:
        return 0
    return ingest_learnings(db_path, claims, source_agent=source_agent, cwd=cwd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "BEAT-3 session-end distilled ingest for Codex / generic MCP agents. "
            "Distills <=3 learnings from a transcript and ingests them via "
            "MemoryService.ingest with source_agent attribution."
        )
    )
    parser.add_argument("--db", required=True, help="Path to memorymaster.db")
    parser.add_argument(
        "--transcript",
        required=True,
        help="Path to the session transcript JSONL (Codex rollout or generic)",
    )
    parser.add_argument(
        "--source-agent",
        default="codex-session",
        help="Attribution tag (default: codex-session). MUST be non-empty.",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Project working directory (drives scope:project:<basename>)",
    )
    args = parser.parse_args(argv)

    if not args.source_agent or not args.source_agent.strip():
        parser.error("--source-agent must be non-empty")

    try:
        ingested = run(
            args.db,
            args.transcript,
            source_agent=args.source_agent.strip(),
            cwd=args.cwd,
        )
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"[MemoryMaster] session-end ingest error: {exc}\n")
        return 1
    sys.stderr.write(
        f"[MemoryMaster] session-end: ingested {ingested} learning(s) "
        f"as source_agent={args.source_agent}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
