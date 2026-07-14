"""Packaged session-end distillation for Codex and generic agents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

MAX_LEARNINGS = 3
DISTILL_PROMPT = (
    "Extract at most 3 non-obvious bug causes, decisions, gotchas, or constraints. "
    "Return JSON objects with text, claim_type, subject, and predicate. "
    "Never include credentials, IPs, paths, or code; return [] when empty."
)


def _sensitive(claim: dict[str, Any]) -> bool:
    from memorymaster.core.security import redact_text

    joined = " | ".join(
        str(claim.get(key, "") or "")
        for key in ("text", "subject", "predicate", "object_value")
    )
    return bool(redact_text(joined)[1])


def _assistant_text(transcript_path: str) -> str:
    path = Path(transcript_path)
    if not path.is_file():
        return ""
    messages: list[str] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines[-200:]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        record = entry
        for key in ("message", "payload"):
            if isinstance(entry.get(key), dict):
                record = entry[key]
                break
        if str(record.get("role") or "") not in {"assistant", "model"}:
            continue
        content = record.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            )
        if isinstance(content, str) and len(content.strip()) > 30:
            messages.append(content[:500])
            if sum(map(len, messages)) > 3000:
                break
    return "\n---\n".join(reversed(messages)) if sum(map(len, messages)) >= 50 else ""


def _distill(text: str) -> list[dict[str, Any]]:
    from memorymaster.core.llm_provider import call_llm, parse_json_response

    claims = parse_json_response(call_llm(DISTILL_PROMPT, text))
    clean = [claim for claim in claims if isinstance(claim, dict) and not _sensitive(claim)]
    return clean[:MAX_LEARNINGS]


def ingest_learnings(
    db_path: str,
    claims: list[dict[str, Any]],
    *,
    source_agent: str,
    cwd: str | None,
) -> int:
    if not source_agent.strip():
        raise ValueError("source_agent is required")
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"DB not found: {db_path}")
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    service = MemoryService(db_path, workspace_root=Path(cwd or os.getcwd()))
    scope = "global" if not cwd else f"project:{Path(cwd).name.lower().replace(' ', '-')}"
    batch_id = f"session-end-{uuid.uuid4().hex[:16]}"
    ingested = 0
    for claim in claims[:MAX_LEARNINGS]:
        text = str(claim.get("text", "") or "").strip()
        if len(text) < 10 or _sensitive(claim):
            continue
        digest = hashlib.sha256(text.lower().encode()).hexdigest()[:16]
        service.ingest(
            text=text,
            citations=[CitationInput(source=source_agent, locator=scope, excerpt=text[:200])],
            idempotency_key=f"{source_agent}-{digest}",
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
    return ingested


def run(
    db_path: str,
    transcript_path: str,
    *,
    source_agent: str,
    cwd: str | None,
) -> int:
    text = _assistant_text(transcript_path)
    return ingest_learnings(
        db_path, _distill(text), source_agent=source_agent, cwd=cwd
    ) if text else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Distill and ingest session-end learnings")
    parser.add_argument("--db", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--source-agent", default="codex-session")
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(argv)
    try:
        count = run(
            args.db,
            args.transcript,
            source_agent=args.source_agent,
            cwd=args.cwd,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"[MemoryMaster] session-end ingest error: {exc}", file=sys.stderr)
        return 1
    print(f"[MemoryMaster] session-end: ingested {count} learning(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
