"""Opt-in AO-7 admission checks; receipts contain identities and verdicts only."""
from __future__ import annotations

import json
import os
import re
from dataclasses import replace

from memorymaster.core.scope_utils import _scope_aliases, resolve_transcript_workspace_scope
from memorymaster.core.security import redact_text, scan_persisted_value, validate_persisted_metadata
from memorymaster.dreaming.models import DreamCandidate, DreamDecision
from memorymaster.dreaming.source_review import bind_source
from memorymaster.govern.jobs.extractor import normalize_claim_text

FLAG = "MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS"
COUNTERS = ("generic", "duplicate", "sensitive", "ambiguous_scope", "rescoped")
_PATH = re.compile(r'''["'`]((?:[A-Za-z]:[/\\]|/)[^"'`\r\n]+)["'`]|(?:[A-Za-z]:[/\\]|/)[^\s"'`<>]+''')
_RULE = re.compile(r"^(?:(?:always|never|siempre|nunca)\s+)?(?:verify|check|run|verificar)\s+(?:the\s+)?(?:evidence|tests|checks|evidencia|pruebas)\b", re.I)
_GENERAL = {"agent", "agents", "assistant", "assistants", "developer", "developers", "user", "users"}


def enabled() -> bool:
    return os.environ.get(FLAG) == "1"


def _names(rows, known_scopes):
    aliases = _scope_aliases()
    names = dict(aliases)
    for scope in {*aliases.values(), *known_scopes, *(row["scope"] for row in rows)}:
        if scope and scope.startswith("project:"):
            names.setdefault(scope.removeprefix("project:"), scope)
    return names


def _content_scopes(candidate, names):
    # Only the cited quote and subject establish ownership, not a generated text hint.
    evidence = candidate.subject + "\n" + candidate.evidence_quote
    scopes = set()
    for match in _PATH.finditer(evidence):
        path = match.group(1) or match.group().rstrip(".,;:)")
        scope = resolve_transcript_workspace_scope(path)
        if scope:
            scopes.add(scope)
    # A verified worktree path is stronger than its disposable checkout name.
    without_paths = _PATH.sub(" ", evidence)
    for name, scope in names.items():
        if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", without_paths, re.I):
            scopes.add(scope)
    return scopes


def _generic(candidate, inventory_scope):
    if candidate.scope_class == "personal":
        return False
    text = candidate.text.strip()
    # Unqualified API calls still identify a tool of this repository.
    if inventory_scope == "project:memorymaster" and re.search(r"\b(?:remember|recall|forget|improve)\s*\(", text):
        return False
    if re.search(r"[\w/\\-]+\.(?:py|ts|tsx|js|sql)\b|\bproject:\w+", text):
        return False
    return bool(_RULE.search(text) or (
        candidate.subject.strip().lower() in _GENERAL
        and re.search(r"\b(?:must|should|always|never|debe|siempre|nunca)\b", text, re.I)
    ))


def _reference_catalogue(service, own_ids):
    """Known projects and normalized public text, under the service's authority."""
    texts = {}
    for status in ("candidate", "confirmed"):
        cursor = ""
        while True:
            claims, cursor = service.list_claims_page(
                status=status, limit=200, cursor=cursor,
                requesting_agent=getattr(service, "principal", None) or "dream-worker",
            )
            for claim in claims:
                if claim.tenant_id == service.tenant_id and claim.visibility == "public":
                    bucket = texts.setdefault(claim.scope, set())
                    if claim.id not in own_ids:
                        bucket.add(normalize_claim_text(claim.text))
            if not cursor:
                break
    return texts


def prepare(rows, captures, service, own_ids):
    """Return scope groups and terminal ignore decisions before any provider call."""
    texts = _reference_catalogue(service, own_ids)
    names, groups, decisions = _names(rows, texts), {}, {}
    metadata = {"version": 1, "candidates": {}, **dict.fromkeys(COUNTERS, 0)}
    for capture in captures:
        notes = metadata["candidates"][str(capture["id"])] = {}
        decisions[capture["id"]] = []
        for raw in capture.get("extraction") or []:
            candidate = DreamCandidate(**raw)
            # Do not copy unchecked identities, rejected text or findings into a receipt.
            if scan_persisted_value(candidate.candidate_id):
                raise ValueError("unsafe backfill candidate identity")
            scope = "personal" if candidate.scope_class == "personal" else capture["scope"]
            note = {"scope": scope, "scope_class": candidate.scope_class, "reason": "accepted"}
            fields = (candidate.text, candidate.subject, candidate.predicate,
                      candidate.object_value or "", candidate.evidence_quote)
            if any(redact_text(value)[1] for value in fields) or scan_persisted_value(raw):
                note["reason"] = "sensitive"
            else:
                scopes = _content_scopes(candidate, names)
                if len(scopes) > 1:
                    note["reason"] = "ambiguous_scope"
                elif scopes:
                    note.update(scope=scopes.pop(), scope_class="project")
                elif _generic(candidate, scope):
                    note.update(scope_class="generic", reason="generic")
                if note["reason"] == "accepted":
                    scope = note["scope"]
                    if normalize_claim_text(candidate.text) in texts.get(scope, set()):
                        note["reason"] = "duplicate"
            notes[candidate.candidate_id] = note
            validate_persisted_metadata({"guard_scope": note["scope"], "guard_scope_class": note["scope_class"]})
            if note["reason"] != "accepted":
                metadata[note["reason"]] += 1
                decisions[capture["id"]].append(DreamDecision(
                    candidate.candidate_id, "ignore", "backfill guard: " + note["reason"], 1.0,
                ).to_dict())
                continue
            metadata["rescoped"] += int(note["scope"] != capture["scope"] and note["scope_class"] == "project")
            candidate = replace(candidate, scope_class=note["scope_class"])
            effective = {**capture, "scope": note["scope"]}
            groups.setdefault(note["scope"], []).append((effective, bind_source(candidate, effective)))
    previous = rows[0]["metadata"].get("consolidate_guards")
    rows[0]["metadata"]["consolidate_guards"] = metadata
    return groups, decisions, previous != metadata


def candidate_context(rows, capture, candidate):
    """Carry a reviewed scope through quality receipts and eventual application."""
    for row in rows:
        note = (row["metadata"].get("consolidate_guards", {}).get("candidates", {})
                .get(str(capture["id"]), {}).get(candidate.candidate_id))
        if note:
            if note["reason"] != "accepted":
                return None, candidate
            return {**capture, "scope": note["scope"]}, replace(candidate, scope_class=note["scope_class"])
    return capture, candidate


def status_counts(rows):
    receipts = [json_value for row in rows if (json_value := row["consolidate_guards"])]
    if not receipts:
        return {}
    parsed = [json.loads(value) for value in receipts]
    return {"consolidate_guards": {key: sum(item.get(key, 0) for item in parsed) for key in COUNTERS}}
