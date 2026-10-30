"""Recall self-check by rehearsal (T-1875): can recall find the claims we just stored?

For recent confirmed claims, ask the configured LLM provider for a few
questions each claim would answer, run the *real* governed recall for every
question and record hit@k per claim in a sidecar SQLite file. It never writes
to the claims DB (opened ``mode=ro``), never writes claims, and recall runs
without access/feedback recording so the rehearsal does not inflate tiering.

Fail-closed: no provider (or a provider that keeps returning nothing) aborts
the run; a claim whose text trips the sensitivity filter is skipped before any
egress; calls, tokens and claims per run are capped. Not scheduled anywhere.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memorymaster.core import llm_budget
from memorymaster.core.security import _sanitize_memory_claim_text, sanitize_persisted_text

QuestionGenerator = Callable[[str, int], list[str]]

DEFAULT_K = 5
DEFAULT_MAX_CLAIMS = 20
DEFAULT_QUESTIONS = 3
DEFAULT_SINCE_DAYS = 7
DEFAULT_MAX_TOKENS = 20_000
MAX_CLAIM_CHARS = 1_200
MAX_QUESTION_CHARS = 300
EXCERPT_CHARS = 140
CONSECUTIVE_FAILURE_LIMIT = 2  # an unconfigured provider returns "" every time: stop after 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS selfcheck_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    claim_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('evaluated', 'skipped_sensitive')),
    k INTEGER NOT NULL,
    retrieval_mode TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    ranks_json TEXT NOT NULL,
    hit INTEGER NOT NULL,
    best_rank INTEGER,
    excerpt TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selfcheck_claim ON selfcheck_results(claim_id);
"""

_PROMPT = (
    "You write retrieval test questions. Given ONE stored memory fact, write {n} short, "
    "distinct questions a developer might ask whose answer is this fact. Do not copy the "
    "fact verbatim and do not include secrets. Reply with ONLY a JSON array of {n} strings."
)


@dataclass
class SelfcheckRun:
    run_id: str
    status: str = "ok"  # ok | provider_unavailable | budget_exhausted | no_candidates
    reason: str | None = None
    claims_considered: int = 0
    claims_evaluated: int = 0
    claims_hit: int = 0
    claims_skipped_sensitive: int = 0
    llm_calls: int = 0
    est_tokens: int = 0
    unrecoverable: list[dict[str, Any]] = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        return round(self.claims_hit / self.claims_evaluated, 4) if self.claims_evaluated else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "claims_considered": self.claims_considered,
            "claims_evaluated": self.claims_evaluated,
            "claims_hit": self.claims_hit,
            "claims_skipped_sensitive": self.claims_skipped_sensitive,
            "hit_rate": self.hit_rate,
            "llm_calls": self.llm_calls,
            "est_tokens": self.est_tokens,
            "unrecoverable": self.unrecoverable,
        }


def default_sidecar_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".recall-selfcheck.sqlite")


def _open_sidecar(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.executescript(_SCHEMA)
    return conn


def _excerpt(text: str) -> str:
    safe, _ = sanitize_persisted_text(" ".join(text.split()))
    return safe[:EXCERPT_CHARS]


def llm_question_generator(claim_text: str, n: int) -> list[str]:
    """Questions from the configured provider (``MEMORYMASTER_LLM_PROVIDER``); [] on failure."""
    from memorymaster.core.llm_provider import call_llm, parse_json_response

    raw = call_llm(_PROMPT.format(n=n), claim_text)
    if not raw:
        return []
    return [q for q in parse_json_response(raw) if isinstance(q, str)]


def _recent_candidates(
    db_path: Path, *, since: str, scope: str | None, limit: int, skip_ids: set[int]
) -> list[sqlite3.Row]:
    """Recent confirmed, public, untenanted claims, newest first, via a ``mode=ro`` connection."""
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        sql = (
            "SELECT id, text, scope FROM claims WHERE status='confirmed' "
            "AND COALESCE(visibility,'public')='public' AND tenant_id IS NULL "  # shortcut: tenant DBs unsupported, add per-tenant services if needed
            "AND created_at >= ?"
        )
        params: list[Any] = [since]
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        sql += " ORDER BY created_at DESC, id DESC"
        # Over-fetch so already-checked claims do not starve the batch.
        rows = conn.execute(sql + " LIMIT ?", (*params, limit + len(skip_ids))).fetchall()
    finally:
        conn.close()
    return [r for r in rows if r["id"] not in skip_ids][:limit]


def _recall_ids(service: Any, question: str, scope: str, k: int, retrieval_mode: str) -> list[int]:
    """Real governed recall, minus access recording (a rehearsal is not a use)."""
    from memorymaster.recall.execution import execute_planned_search
    from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan

    plan = build_retrieval_plan(RetrievalRequest(
        query_text=question, limit=k, trust_mode="trusted",
        retrieval_mode=retrieval_mode, scope_allowlist=(scope,),
    ))
    rows = execute_planned_search(
        plan, service.query_rows,
        record_accesses=False, limit=plan.limit,
        include_stale=False, include_conflicted=False, include_candidates=False,
        retrieval_mode=plan.effective_mode, retrieval_profile=plan.retrieval_profile,
        allow_sensitive=False, scope_allowlist=[scope],
        requesting_agent=plan.requesting_agent, query_type=plan.query_type,
    )
    return [row["claim"].id for row in rows]


def run_selfcheck(
    db_path: str | Path,
    *,
    sidecar_path: str | Path | None = None,
    generate: QuestionGenerator | None = None,
    k: int = DEFAULT_K,
    max_claims: int = DEFAULT_MAX_CLAIMS,
    questions_per_claim: int = DEFAULT_QUESTIONS,
    since_days: int = DEFAULT_SINCE_DAYS,
    max_calls: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    scope: str | None = None,
    retrieval_mode: str = "legacy",
    now: datetime | None = None,
) -> SelfcheckRun:
    """Rehearse recall for recent confirmed claims; append results to the sidecar."""
    from memorymaster.core.service import MemoryService

    db = Path(db_path)
    if not db.is_file():
        raise FileNotFoundError(f"claims DB not found: {db.name}")
    if retrieval_mode == "qdrant":
        raise ValueError("qdrant retrieval records accesses; the self-check supports legacy/hybrid only")
    k = max(1, k)
    questions_per_claim = max(1, min(questions_per_claim, 5))
    max_calls = max_claims if max_calls is None else max_calls
    generate = generate or llm_question_generator
    run = SelfcheckRun(run_id=uuid.uuid4().hex[:12])
    stamp = (now or datetime.now(timezone.utc))
    since = (stamp - timedelta(days=since_days)).isoformat()

    sidecar = _open_sidecar(Path(sidecar_path) if sidecar_path else default_sidecar_path(db))
    try:
        done = {r[0] for r in sidecar.execute("SELECT DISTINCT claim_id FROM selfcheck_results")}
        candidates = _recent_candidates(db, since=since, scope=scope, limit=max_claims, skip_ids=done)
        run.claims_considered = len(candidates)
        if not candidates:
            run.status = "no_candidates"
            return run
        service = MemoryService(str(db), workspace_root=db.parent, read_only=True)
        failures = 0
        # env caps (MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE ...) apply on top of the explicit ones
        with llm_budget.cycle_scope():
            for row in candidates:
                claim_id, text, claim_scope = int(row["id"]), str(row["text"] or ""), str(row["scope"])
                redacted, findings = _sanitize_memory_claim_text(text)
                if findings:  # fail-closed: nothing sensitive-looking leaves the process
                    _store(sidecar, run, claim_id, claim_scope, "skipped_sensitive", k, retrieval_mode, [], [], "", stamp)
                    run.claims_skipped_sensitive += 1
                    continue
                prompt_text = redacted[:MAX_CLAIM_CHARS]
                cost = llm_budget.estimate_tokens(_PROMPT, prompt_text) + 60 * questions_per_claim
                if run.llm_calls >= max_calls or run.est_tokens + cost > max_tokens:
                    run.status, run.reason = "budget_exhausted", "calls" if run.llm_calls >= max_calls else "tokens"
                    break
                run.llm_calls += 1
                run.est_tokens += cost
                try:
                    raw_questions = generate(prompt_text, questions_per_claim)
                except llm_budget.LLMBudgetExceeded as exc:
                    run.status, run.reason = "budget_exhausted", exc.reason
                    break
                questions = _clean_questions(raw_questions, questions_per_claim)
                if not questions:
                    failures += 1
                    if failures >= CONSECUTIVE_FAILURE_LIMIT:
                        run.status, run.reason = "provider_unavailable", "no questions returned"
                        break
                    continue  # unrecorded: retried on the next run
                failures = 0
                ranks = []
                for q in questions:
                    ids = _recall_ids(service, q, claim_scope, k, retrieval_mode)
                    ranks.append(ids.index(claim_id) + 1 if claim_id in ids else None)
                _store(sidecar, run, claim_id, claim_scope, "evaluated", k, retrieval_mode,
                       questions, ranks, _excerpt(redacted), stamp)
                run.claims_evaluated += 1
                if any(r is not None for r in ranks):
                    run.claims_hit += 1
                else:
                    run.unrecoverable.append(
                        {"claim_id": claim_id, "scope": claim_scope, "excerpt": _excerpt(redacted)}
                    )
        return run
    finally:
        sidecar.close()


def _clean_questions(raw: list[str], n: int) -> list[str]:
    out: list[str] = []
    for q in raw:
        text = " ".join(str(q).split())[:MAX_QUESTION_CHARS]
        safe, findings = sanitize_persisted_text(text)
        if text and not findings and safe not in out:
            out.append(safe)
    return out[:n]


def _store(
    sidecar: sqlite3.Connection, run: SelfcheckRun, claim_id: int, scope: str, outcome: str,
    k: int, mode: str, questions: list[str], ranks: list[int | None], excerpt: str, stamp: datetime,
) -> None:
    hit = any(r is not None for r in ranks)
    best = min((r for r in ranks if r is not None), default=None)
    sidecar.execute(
        "INSERT INTO selfcheck_results (run_id, checked_at, claim_id, scope, outcome, k, retrieval_mode, "
        "questions_json, ranks_json, hit, best_rank, excerpt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run.run_id, stamp.isoformat(), claim_id, scope, outcome, k, mode,
         json.dumps(questions), json.dumps(ranks), int(hit), best, excerpt),
    )
    sidecar.commit()


def build_report(sidecar_path: str | Path, *, scope: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Hit rate per scope plus the unrecoverable claims, from the latest result per claim."""
    path = Path(sidecar_path)
    if not path.is_file():
        return {"status": "no_sidecar", "total": {"evaluated": 0, "hit": 0, "hit_rate": None},
                "scopes": {}, "unrecoverable": []}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT r.* FROM selfcheck_results r JOIN "
            "(SELECT claim_id, MAX(id) AS mid FROM selfcheck_results GROUP BY claim_id) m ON r.id = m.mid "
            "WHERE r.outcome='evaluated'"
        )
        params: list[Any] = []
        if scope:
            sql += " AND r.scope = ?"
            params.append(scope)
        rows = conn.execute(sql + " ORDER BY r.id", params).fetchall()
        skipped = conn.execute(
            "SELECT COUNT(DISTINCT claim_id) FROM selfcheck_results WHERE outcome='skipped_sensitive'"
        ).fetchone()[0]
    finally:
        conn.close()
    scopes: dict[str, dict[str, Any]] = {}
    misses: list[dict[str, Any]] = []
    for r in rows:
        s = scopes.setdefault(r["scope"], {"evaluated": 0, "hit": 0, "hit_rate": None})
        s["evaluated"] += 1
        s["hit"] += r["hit"]
        if not r["hit"]:
            misses.append({"claim_id": r["claim_id"], "scope": r["scope"], "k": r["k"],
                           "checked_at": r["checked_at"], "excerpt": r["excerpt"]})
    for s in scopes.values():
        s["hit_rate"] = round(s["hit"] / s["evaluated"], 4)
    evaluated = sum(s["evaluated"] for s in scopes.values())
    hit = sum(s["hit"] for s in scopes.values())
    return {
        "status": "ok" if evaluated else "empty",
        "total": {"evaluated": evaluated, "hit": hit,
                  "hit_rate": round(hit / evaluated, 4) if evaluated else None},
        "scopes": dict(sorted(scopes.items())),
        "skipped_sensitive": skipped,
        "unrecoverable": misses[:limit],
    }
