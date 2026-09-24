"""Freeze the existing stratified sampler's local review inputs; never apply memory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.core.security import _sanitize_memory_claim_text
from memorymaster.dreaming.sampling import _date, _entry, sample_sources

REVIEW_STRATA = {"zero_candidates", "candidates_no_actions", "candidates_with_actions"}


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _scrub(value: str) -> str:
    text = value
    patterns = [r"```[\s\S]*?```", r"(?<!\w)\d{8,}(?!\w)",
                r"(?<!\w)(?:~/|/(?:home|Users|root|mnt)/)[^\s<>\"'`]+",
                r"\b[A-Za-z]:[\\/][^\n\r\"<>]+", r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
                r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", r"\b[A-Za-z0-9_+/=-]{40,}\b"]
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text)
    return _sanitize_memory_claim_text(text)[0]


def _source(conn, entry: dict) -> dict:
    row = conn.execute("SELECT c.*, (SELECT COUNT(*) FROM dream_applications a WHERE a.capture_id=c.id) actions "
                       "FROM dream_captures c WHERE c.id=?", (entry["capture_id"],)).fetchone()
    if row is None or _entry(row) != entry:
        raise ValueError("sample changed during freeze; retry with a new cutoff")
    messages = json.loads(row["messages_json"])
    candidates = json.loads(row["extraction_json"] or "[]")
    exact = {candidate["candidate_id"]: any(
        message["message_id"] == candidate.get("evidence_message_id")
        and bool(candidate.get("evidence_quote")) and candidate["evidence_quote"] in message["text"]
        for message in messages) for candidate in candidates}
    applications = [dict(item) for item in conn.execute(
        "SELECT candidate_id, action, created_claim_id, target_claim_id, applied_at "
        "FROM dream_applications WHERE capture_id=? ORDER BY application_key", (row["id"],))]
    return {**entry, "scope": _scrub(row["scope"]), "captured_at": row["captured_at"],
            "state_at_freeze": row["state"], "messages": [
                {"message_id": m["message_id"], "role": m["role"], "timestamp": m.get("timestamp"), "text": _scrub(m["text"])}
                for m in messages],
            "candidates": [{key: _scrub(value) if isinstance(value, str) else value for key, value in c.items()}
                           for c in candidates], "applications": applications, "evidence_exact": exact,
            "redacted": True}


def _usage(conn, since: str, until: str) -> dict:
    rows = conn.execute("SELECT provider, model, COUNT(*) calls, SUM(input_tokens) input_tokens, "
                        "SUM(output_tokens) output_tokens, SUM(latency_ms) latency_ms, "
                        "SUM(outcome!='ok') errors FROM dream_provider_usage "
                        "WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) "
                        "GROUP BY provider, model ORDER BY provider, model", (since, until)).fetchall()
    return {"scope": "all Dreaming provider calls in the cohort time window; shared across useful and rejected work",
            "providers": [dict(row) for row in rows], "savings": None, "cost_per_useful_memory": None}


def freeze_cohort(ledger: Path, *, since: str, until: str, previous_until: str,
                  version: str, per_stratum: int = 20) -> tuple[dict, list[dict]]:
    if _date(since) < _date(previous_until):
        raise ValueError("cohort overlaps the previous evaluation")
    if not version.strip() or not 1 <= per_stratum <= 20:
        raise ValueError("version and at most 20 captures per stratum are required")
    sample = sample_sources(ledger, since=since, until=until, per_stratum=per_stratum)
    conn = sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        sources = [_source(conn, entry) for entry in sample["sample"] if entry["stratum"] in REVIEW_STRATA]
        usage = _usage(conn, sample["since"], sample["until"])
    finally:
        conn.close()
    selected = {stratum: sum(source["stratum"] == stratum for source in sources) for stratum in sorted(REVIEW_STRATA)}
    return {**sample, "cohort_version": version, "previous_until": _date(previous_until),
            "frozen_at": datetime.now(timezone.utc).isoformat(), "sources_fingerprint": _fingerprint(sources),
            "selected_by_stratum": selected, "not_selected": sample["population"] - len(sources),
            "sampling_shortfalls": {key: per_stratum - value for key, value in selected.items()},
            "provider_usage": usage, "acceptance": "PENDING_HUMAN_REVIEW"}, sources


def verify_sources(manifest: dict, sources: list[dict]) -> None:
    if _fingerprint(sources) != manifest["sources_fingerprint"]:
        raise ValueError("frozen review sources do not match the cohort fingerprint")


def decision_template(source: dict, candidate: dict | None, manifest: dict) -> dict:
    candidate_id = candidate["candidate_id"] if candidate else "source-control"
    application = next((a for a in source["applications"] if a["candidate_id"] == candidate_id), None)
    action = application["action"] if application else "none"
    return {"record_id": f"capture-{source['capture_id']}:{candidate_id}", "capture_id": source["capture_id"],
            "cohort_fingerprint": manifest["sources_fingerprint"], "candidate_id": candidate_id,
            "label_origin": "unreviewed", "should_emit": None, "emitted": action not in {"none", "ignore"},
            "structured_valid": True, "evidence_exact": source["evidence_exact"].get(candidate_id),
            "expected_scope": None, "actual_scope": "personal" if candidate and candidate.get("scope_class") == "personal" else source["scope"],
            "expected_action": None, "actual_action": action, "semantic_sufficiency": None,
            "current_validity": None, "useful": None, "rationale": "", "human_accept": None}


def write_cohort(directory: Path, manifest: dict, sources: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    for filename, payload in (("manifest.json", manifest), ("sources.json", sources)):
        (directory / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    templates = [decision_template(source, candidate, manifest) for source in sources
                 for candidate in (source["candidates"] or [None])]
    (directory / "labels-template.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in templates), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("ledger", "since", "until", "previous-until", "version", "out"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    manifest, sources = freeze_cohort(Path(args.ledger), since=args.since, until=args.until,
                                      previous_until=args.previous_until, version=args.version)
    write_cohort(Path(args.out), manifest, sources)
    print(json.dumps({key: manifest[key] for key in ("cohort_version", "population", "selected_by_stratum", "sources_fingerprint", "acceptance")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
