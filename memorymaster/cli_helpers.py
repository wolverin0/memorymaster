"""Shared helpers for the memorymaster CLI.

This module exists so that both `cli.py` (which builds the parser and runs
main) and the handler modules can import the same low-level helpers without
creating a circular import.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json

from memorymaster.models import CitationInput
from memorymaster.policy import POLICY_MODES
from memorymaster.service import MemoryService

STEALTH_DB_NAME = ".memorymaster-stealth.db"
_SCORE_KEYS = ("score", "lexical_score", "confidence_score", "freshness_score", "vector_score")


def parse_citation(raw: str) -> CitationInput:
    # Format: source|locator|excerpt (locator/excerpt optional).
    parts = [part.strip() for part in raw.split("|", 2)]
    source = parts[0] if parts else ""
    if not source:
        raise ValueError("Citation source is required.")
    locator = parts[1] if len(parts) > 1 and parts[1] else None
    excerpt = parts[2] if len(parts) > 2 and parts[2] else None
    return CitationInput(source=source, locator=locator, excerpt=excerpt)


def parse_scope_allowlist(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()] or None


def _claim_to_dict(claim) -> dict:
    """Serialize a Claim dataclass to a plain dict for JSON output."""
    return asdict(claim) if is_dataclass(claim) else dict(claim)


def _json_envelope(data, *, total: int | None = None, query_ms: float) -> str:
    """Format the standard JSON envelope for --json output."""
    meta: dict = {"query_ms": round(query_ms, 2), **({"total": total} if total is not None else {})}
    return json.dumps({"ok": True, "data": data, "meta": meta}, indent=2, default=_json_default)


def _json_error(message: str) -> str:
    """Format a JSON error envelope."""
    return json.dumps({"ok": False, "error": str(message)})


def _resolve_claim_id(service: MemoryService, raw: str | int) -> int:
    """Resolve a CLI claim identifier (numeric or human_id) to an integer ID."""
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        return service.store.resolve_claim_id(text)


def _add_cycle_policy_args(p: argparse.ArgumentParser, policy_default: str = "legacy") -> None:
    """Add shared --min-citations/--min-score/--policy-mode/--policy-limit args."""
    p.add_argument("--min-citations", type=int, default=1, help="Minimum citations to confirm candidate")
    p.add_argument("--min-score", type=float, default=0.58, help="Minimum score to confirm candidate")
    p.add_argument("--policy-mode", choices=list(POLICY_MODES), default=policy_default, help="Revalidation policy mode (legacy keeps candidate-only validation)")
    p.add_argument("--policy-limit", type=int, default=200, help="Max due claims selected for revalidation")


def print_claim(claim) -> None:
    hid = (getattr(claim, "human_id", None) or "")
    print(f"[{claim.id}]{f' {hid}' if hid else ''} {claim.status:<10} conf={claim.confidence:.3f} pin={int(claim.pinned)} "
          f"type={claim.claim_type or '-'} tuple=({claim.subject or '-'}, {claim.predicate or '-'}, {claim.object_value or '-'}) "
          f"scope={claim.scope} vol={claim.volatility} updated={claim.updated_at}\n  text: {claim.text}")
    if claim.supersedes_claim_id or claim.replaced_by_claim_id:
        print(f"  links: supersedes={claim.supersedes_claim_id or '-'} replaced_by={claim.replaced_by_claim_id or '-'}")
    for citation in claim.citations:
        print(f"  - cite: {citation.source}{f' | {citation.locator}' if citation.locator else ''}{f' | {citation.excerpt}' if citation.excerpt else ''}")


def _print_claim_brief(c) -> None:
    """Print a single-line claim summary used in ready/attention output."""
    hid = (getattr(c, "human_id", None) or "")
    print(f"  [{c.id}]{f' {hid}' if hid else ''} conf={c.confidence:.3f} scope={c.scope} {c.text[:80]}")


def _score_str_from_payload(payload_json: str | None) -> str:
    """Extract score from event payload_json for history display, or ''."""
    try:
        p = json.loads(payload_json) if payload_json else None
        return f"  score={p['score']}" if isinstance(p, dict) and "score" in p else ""
    except (json.JSONDecodeError, TypeError):
        return ""


def _event_to_timeline_entry(ev) -> dict:
    """Serialize an event into a timeline dict for history JSON output."""
    entry: dict = {"id": ev.id, "timestamp": ev.created_at, "event_type": ev.event_type}
    if ev.from_status or ev.to_status:
        entry.update({"from_status": ev.from_status, "to_status": ev.to_status})
    if ev.details:
        entry["details"] = ev.details
    if ev.payload_json:
        try:
            entry["payload"] = json.loads(ev.payload_json)
        except (json.JSONDecodeError, TypeError):
            entry["payload"] = ev.payload_json
    return entry


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "__dict__"):
        return value.__dict__
    return repr(value)



def _resolve_db_path(args: argparse.Namespace) -> str:
    """Resolve effective DB path; activates stealth if --stealth or stealth DB exists in cwd."""
    from pathlib import Path
    stealth_path = Path.cwd() / STEALTH_DB_NAME
    if args.stealth or (args.db == "memorymaster.db" and stealth_path.exists()):
        return str(stealth_path)
    return args.db


def _stealth_active(args: argparse.Namespace) -> bool:
    """Return True if stealth mode is active for the resolved args."""
    return _resolve_db_path(args) != args.db or args.stealth
