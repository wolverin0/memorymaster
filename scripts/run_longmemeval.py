"""LongMemEval benchmark harness for MemoryMaster.

Evaluates MemoryMaster's production recall stack on the public LongMemEval
oracle dataset (500 questions, https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
and compares to the MemPalace 96.6% reference.

Design
------
- Per question: seed a fresh temp DB with the question's haystack sessions
  via the normal ``MemoryService.ingest`` path (sensitivity filter active).
  Each session becomes 1+ claims (session chunked to ~3500 chars). The scope
  carries the session_id so we can map retrieved claims back to sessions.
- Recall: mirror ``memorymaster.context_hook.recall()`` end-to-end (tokenizer
  + per-token fanout + entity fanout + vector fallback + verbatim + BM25
  rescorer + ranker) so ranking is bit-identical to production.  We do NOT
  call recall() directly because it only returns a formatted string; we need
  the ranked claim IDs. The mirror imports the same private helpers.
- Scoring: a top-K result "hits" iff the claim's session_id is in
  ``question.answer_session_ids``. Emits hit@1, hit@5, MRR, latency.
- Configs (A/B/C/D) run in isolated subprocesses so env vars reset cleanly
  and the temp DB is wiped between runs.

Usage
-----
    python scripts/run_longmemeval.py --limit 100 --configs A,B,C,D
    python scripts/run_longmemeval.py --worker --config A --limit 100  # internal

    # Per-question DB isolation (roadmap 11.4). Eliminates cross-question
    # contamination; on the 500-Q oracle this lifts hit@5 from 0.430 to the
    # range measured in artifacts/longmemeval-per-q-2026-04-24.md.
    python scripts/run_longmemeval.py --isolate-per-q --limit 0 \
        --config-label isolated \
        --output-dir artifacts/longmemeval-per-q

Outputs
-------
- artifacts/longmemeval/results-<config>.jsonl — per-question records
- artifacts/longmemeval/summary.json — roll-up for all configs
- artifacts/longmemeval-2026-04-24.md — human-readable artifact (written
  separately by the caller from summary.json)

Read-only against the live ``memorymaster.db``. All seeding goes to a fresh
``artifacts/longmemeval/bench-<config>.db`` which is wiped before each config.
"""
from __future__ import annotations

import argparse
import gc
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Default output directory; override with --output-dir (propagated to worker
# via MEMORYMASTER_LONGMEMEVAL_OUTPUT_DIR). The dataset cache stays in the
# canonical location so we don't re-download 15 MB per run.
_DEFAULT_ART_DIR = REPO / "artifacts" / "longmemeval"
ART_DIR = _DEFAULT_ART_DIR
DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
    "resolve/main/longmemeval_oracle.json"
)
DATASET_PATH = _DEFAULT_ART_DIR / "longmemeval_oracle.json"

# Max chars per claim — session transcripts are chunked to survive recall's
# 300-char preview and keep token count reasonable. 3500 is a compromise
# between recall coverage and FTS5 noise.
_CHUNK_CHARS = 3500
# Top-K depth kept by recall() for ranking. Matches the hook's fanout budget.
_TOPK = 5

# Configs to sweep. Env vars applied ON TOP of a cleared baseline.
CONFIGS = {
    "A": {
        # Baseline — current shipped defaults: BM25 on, linear fusion,
        # entity/vector/verbatim off.
        "description": "baseline (default env)",
        "env": {},
    },
    "B": {
        # Entity fanout enabled unconditionally
        "description": "+ entity fanout (W_ENTITY=0.15)",
        "env": {"MEMORYMASTER_RECALL_W_ENTITY": "0.15"},
    },
    "C": {
        # Verbatim stream enabled (requires seeding verbatim_memories)
        "description": "+ verbatim (W_VERBATIM=0.2)",
        "env": {
            "MEMORYMASTER_RECALL_VERBATIM": "1",
            "MEMORYMASTER_RECALL_W_VERBATIM": "0.2",
        },
    },
    "D": {
        # NOTE: MEMORYMASTER_RECALL_FUSION=rrf is spec'd in the 6.1 task
        # but NOT implemented in this branch (no recall_fusion.py, no
        # FUSION gate in context_hook.py). We still run it for the table
        # row; in practice it behaves identically to baseline A.
        "description": "+ RRF fusion (env set, but unimplemented on this branch)",
        "env": {"MEMORYMASTER_RECALL_FUSION": "rrf"},
    },
}

logger = logging.getLogger("longmemeval")


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def ensure_dataset() -> Path:
    """Download the oracle subset if it isn't already on disk."""
    _DEFAULT_ART_DIR.mkdir(parents=True, exist_ok=True)
    if DATASET_PATH.exists() and DATASET_PATH.stat().st_size > 1_000_000:
        return DATASET_PATH
    logger.info("Downloading LongMemEval oracle from %s", DATASET_URL)
    urllib.request.urlretrieve(DATASET_URL, DATASET_PATH)
    if not DATASET_PATH.exists() or DATASET_PATH.stat().st_size < 1_000_000:
        raise RuntimeError(
            f"LongMemEval oracle download failed or truncated at {DATASET_PATH}"
        )
    return DATASET_PATH


def load_questions(limit: int | None = None) -> list[dict]:
    path = ensure_dataset()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if limit is not None and limit > 0:
        data = data[:limit]
    return data


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _chunk(text: str, size: int = _CHUNK_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out: list[str] = []
    for i in range(0, len(text), size):
        out.append(text[i : i + size])
    return out


def _serialize_session(session_turns: list[dict]) -> str:
    """Flatten a LongMemEval session into a single transcript string."""
    parts: list[str] = []
    for t in session_turns:
        role = t.get("role", "")
        content = (t.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def seed_question(
    svc,
    question: dict,
    *,
    seed_verbatim: bool,
    db_path: str,
    scope_prefix: str,
) -> dict[str, int]:
    """Ingest all haystack sessions for one question.

    Returns stats dict.  Each session is represented as one claim per chunk,
    all sharing the same scope ``<scope_prefix>:<session_id>`` so the claim
    back-maps to the session_id for scoring.
    """
    from memorymaster.models import CitationInput

    stats = {"claims": 0, "verbatim_rows": 0, "skipped": 0}
    qid = question["question_id"]
    sessions = question.get("haystack_sessions", [])
    ids = question.get("haystack_session_ids", [])
    dates = question.get("haystack_dates", [])

    if seed_verbatim:
        from memorymaster import verbatim_store

    for idx, session_turns in enumerate(sessions):
        sid = ids[idx] if idx < len(ids) else f"{qid}_s{idx}"
        sdate = dates[idx] if idx < len(dates) else ""
        scope = f"{scope_prefix}:{sid}"
        transcript = _serialize_session(session_turns)
        if not transcript:
            stats["skipped"] += 1
            continue

        chunks = _chunk(transcript)
        for chunk_idx, chunk in enumerate(chunks):
            subject = f"session:{sid}#{chunk_idx}" if len(chunks) > 1 else f"session:{sid}"
            try:
                svc.ingest(
                    text=chunk,
                    citations=[CitationInput(source="longmemeval", locator=sid)],
                    claim_type="fact",
                    subject=subject,
                    scope=scope,
                    confidence=0.8,
                    event_time=sdate or None,
                    source_agent="longmemeval-harness",
                )
                stats["claims"] += 1
            except Exception as exc:
                logger.debug("ingest failed for %s chunk %d: %s", sid, chunk_idx, exc)
                stats["skipped"] += 1

        if seed_verbatim:
            for turn in session_turns:
                role = turn.get("role", "")
                content = (turn.get("content") or "").strip()
                if not content or len(content) < 20:
                    continue
                try:
                    row_id = verbatim_store.store_verbatim(
                        db_path=db_path,
                        session_id=sid,
                        role=role,
                        content=content,
                        scope=scope,
                        source_agent="longmemeval-harness",
                        timestamp=sdate or None,
                    )
                    if row_id:
                        stats["verbatim_rows"] += 1
                except Exception as exc:
                    logger.debug("verbatim store failed %s: %s", sid, exc)

    return stats


# ---------------------------------------------------------------------------
# Recall (mirror of memorymaster.context_hook.recall — returns ranked IDs)
# ---------------------------------------------------------------------------

@dataclass
class RecallResult:
    ranked_claim_ids: list[int] = field(default_factory=list)
    ranked_session_ids: list[str] = field(default_factory=list)
    latency_ms: float = 0.0


def recall_with_ranked_ids(query: str, db_path: str, *, top_k: int = _TOPK) -> RecallResult:
    """Call the production recall pipeline and return ranked claim + session IDs.

    Mirrors ``memorymaster.context_hook.recall()`` end-to-end, reusing its
    private helpers (``_entity_fanout_claim_ids``, ``_apply_vector_fallback``,
    ``_recall_weight``, ``_bm25_enabled``, ``_bm25_param``, BM25 body from
    _relevance). This matches production ranking bit-for-bit under the
    same env vars.
    """
    import math
    from memorymaster import context_hook as ch
    from memorymaster.recall_tokenizer import _candidate_tokens, extract_query_tokens
    from memorymaster.service import MemoryService

    t0 = time.perf_counter()
    svc = MemoryService(db_target=db_path, workspace_root=REPO)

    fts_query = extract_query_tokens(query, db_path, max_tokens=6)
    token_list = fts_query.split() if fts_query else []

    rows: list = []
    seen_ids: set[int] = set()
    if token_list:
        per_token_limit = max(3, 8 // max(1, len(token_list)))
        for tok in token_list:
            batch = svc.query_rows(
                query_text=tok,
                limit=per_token_limit,
                retrieval_mode="legacy",
                include_candidates=True,
                scope_allowlist=None,
            )
            for row in batch:
                claim = row.get("claim")
                cid = getattr(claim, "id", None)
                if cid is None or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                rows.append(row)
            if len(rows) >= 8:
                break
    if not rows:
        rows = svc.query_rows(
            query_text=query,
            limit=8,
            retrieval_mode="legacy",
            include_candidates=True,
            scope_allowlist=None,
        )
        for row in rows:
            claim = row.get("claim")
            cid = getattr(claim, "id", None)
            if cid is not None:
                seen_ids.add(cid)

    w_entity_probe = ch._recall_weight("W_ENTITY")
    should_fanout = (not rows) or (w_entity_probe > 0.0)
    if should_fanout:
        try:
            from memorymaster.security import is_sensitive_claim
        except Exception:
            is_sensitive_claim = lambda _c: False  # noqa: E731
        fanout_ids = ch._entity_fanout_claim_ids(svc.store, query, seen_ids)
        for cid in fanout_ids:
            try:
                claim = svc.store.get_claim(cid, include_citations=True)
            except Exception:
                continue
            if claim is None or getattr(claim, "status", "") == "archived":
                continue
            if is_sensitive_claim(claim):
                continue
            rows.append(ch._row_for_claim(claim))

    rows = ch._apply_vector_fallback(svc, query, rows, seen_ids)

    # Verbatim stream
    try:
        from memorymaster.verbatim_recall import (
            hit_to_synthetic_row,
            is_enabled as _verbatim_enabled,
            recall_verbatim,
        )
    except Exception:
        _verbatim_enabled = lambda: False  # noqa: E731
        recall_verbatim = lambda *a, **k: []  # noqa: E731
        hit_to_synthetic_row = None

    if _verbatim_enabled():
        try:
            verbatim_hits = recall_verbatim(query, scope=None, db_path=db_path, limit=5)
        except Exception:
            verbatim_hits = []
        if verbatim_hits and hit_to_synthetic_row is not None:
            scope_to_rows: dict[str, list[dict]] = {}
            for row in rows:
                claim = row.get("claim")
                s = getattr(claim, "scope", "") or ""
                if s:
                    scope_to_rows.setdefault(s, []).append(row)
            added_excerpts: set[str] = set()
            for hit in verbatim_hits:
                existing = scope_to_rows.get(hit.scope) or []
                if existing:
                    target = existing[0]
                    prev = float(target.get("verbatim_score") or 0.0)
                    if hit.score > prev:
                        target["verbatim_score"] = hit.score
                        target["_verbatim_id"] = hit.verbatim_id
                    continue
                key = hit.excerpt[:100]
                if key in added_excerpts:
                    continue
                added_excerpts.add(key)
                rows.append(hit_to_synthetic_row(hit))

    if not rows:
        return RecallResult(latency_ms=(time.perf_counter() - t0) * 1000.0)

    # Re-rank
    query_words = set(fts_query.lower().split()) or set(query.lower().split())
    w_matches = ch._recall_weight("W_MATCHES")
    w_phrase = ch._recall_weight("W_PHRASE")
    w_all = ch._recall_weight("W_ALL")
    w_lexical = ch._recall_weight("W_LEXICAL")
    w_confidence = ch._recall_weight("W_CONFIDENCE")
    w_freshness = ch._recall_weight("W_FRESHNESS")
    w_vector = ch._recall_weight("W_VECTOR")
    w_entity = ch._recall_weight("W_ENTITY")
    w_verbatim = ch._recall_weight("W_VERBATIM")

    bm25_on = ch._bm25_enabled()
    bm25_scores: dict[int, float] = {}
    if bm25_on:
        def _doc_tokens(claim_obj) -> list[str]:
            subject = getattr(claim_obj, "subject", "") or ""
            text = getattr(claim_obj, "text", "") or ""
            joined = f"{subject} {text}"
            return [t for t in _candidate_tokens(joined) if len(t) >= 3]

        doc_tokens_by_id: dict[int, list[str]] = {}
        df: dict[str, int] = {}
        for r in rows:
            c = r.get("claim")
            cid = getattr(c, "id", None)
            if cid is None or cid in doc_tokens_by_id:
                continue
            toks = _doc_tokens(c)
            doc_tokens_by_id[cid] = toks
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n_docs = len(doc_tokens_by_id)
        avg_dl = (
            sum(len(v) for v in doc_tokens_by_id.values()) / n_docs if n_docs else 0.0
        )
        k1 = ch._bm25_param("K1", ch._BM25_K1_DEFAULT)
        b = ch._bm25_param("B", ch._BM25_B_DEFAULT)
        q_tokens = [t for t in _candidate_tokens(query) if len(t) >= 3]
        if not q_tokens:
            q_tokens = [w for w in query_words if len(w) >= 3]
        if n_docs > 0 and avg_dl > 0 and q_tokens:
            for cid, doc_tokens in doc_tokens_by_id.items():
                if not doc_tokens:
                    continue
                tf: dict[str, int] = {}
                for t in doc_tokens:
                    tf[t] = tf.get(t, 0) + 1
                dl = len(doc_tokens)
                score = 0.0
                for qt in q_tokens:
                    f = tf.get(qt, 0)
                    if f == 0:
                        continue
                    n_q = df.get(qt, 0)
                    idf = math.log(
                        ((n_docs - n_q + 0.5) / (n_q + 0.5)) + 1.0
                    )
                    norm = 1.0 - b + b * (dl / avg_dl)
                    score += idf * ((f * (k1 + 1.0)) / (f + k1 * norm))
                bm25_scores[cid] = score

    def _relevance(row):
        claim = row.get("claim")
        text = (claim.text if hasattr(claim, "text") else "").lower()
        tokens_gt2 = [w for w in query_words if len(w) > 2]
        matches = sum(1 for w in tokens_gt2 if w in text)
        phrase_bonus = 1.0 if query.lower() in text else 0.0
        all_present = 1.0 if tokens_gt2 and matches == len(tokens_gt2) else 0.0
        if bm25_on:
            cid = getattr(claim, "id", None)
            lexical = bm25_scores.get(cid, 0.0) if cid is not None else 0.0
        else:
            lexical = float(row.get("lexical_score") or 0.0)
        conf = float(row.get("confidence_score") or 0.0)
        freshness = float(row.get("freshness_score") or 0.0)
        vector = float(row.get("vector_score") or 0.0)
        entity = float(row.get("entity_score") or 0.0)
        verbatim = float(row.get("verbatim_score") or 0.0)
        return (
            matches * w_matches
            + phrase_bonus * w_phrase
            + all_present * w_all
            + lexical * w_lexical
            + conf * w_confidence
            + freshness * w_freshness
            + vector * w_vector
            + entity * w_entity
            + verbatim * w_verbatim
        )

    # Fusion mode: linear (default, legacy bit-identical) or RRF.  Mirror of
    # memorymaster/context_hook.py's fusion branch so the harness reflects
    # what a production recall call would actually rank under this env.
    fusion_mode = os.environ.get("MEMORYMASTER_RECALL_FUSION", "linear").strip().lower()

    if fusion_mode == "rrf":
        from memorymaster.recall_fusion import rrf_fuse

        def _row_cid(r: dict) -> int | None:
            c = r.get("claim")
            return getattr(c, "id", None)

        def _ranking(score_fn) -> list[int]:
            scored = [
                (cid, score_fn(r))
                for r in rows
                if (cid := _row_cid(r)) is not None
            ]
            if all(s == 0.0 for _, s in scored):
                return []
            scored.sort(key=lambda x: x[1], reverse=True)
            return [cid for cid, _ in scored]

        rankings: dict[str, list[int]] = {}
        bm25_ranking = _ranking(
            lambda r: bm25_scores.get(_row_cid(r), 0.0) if bm25_on else 0.0
        )
        if bm25_ranking:
            rankings["bm25"] = bm25_ranking
        entity_ranking = _ranking(lambda r: float(r.get("entity_score") or 0.0))
        if entity_ranking:
            rankings["entity"] = entity_ranking
        vector_ranking = _ranking(lambda r: float(r.get("vector_score") or 0.0))
        if vector_ranking:
            rankings["vector"] = vector_ranking
        verbatim_ranking = _ranking(lambda r: float(r.get("verbatim_score") or 0.0))
        if verbatim_ranking:
            rankings["verbatim"] = verbatim_ranking
        freshness_ranking = _ranking(lambda r: float(r.get("freshness_score") or 0.0))
        if freshness_ranking:
            rankings["freshness"] = freshness_ranking

        if rankings:
            rrf_scores = rrf_fuse(rankings)
            ranked = sorted(
                rows,
                key=lambda r: rrf_scores.get(_row_cid(r) or -1, 0.0),
                reverse=True,
            )
        else:
            ranked = sorted(rows, key=_relevance, reverse=True)
    else:
        ranked = sorted(rows, key=_relevance, reverse=True)
    dt_ms = (time.perf_counter() - t0) * 1000.0

    # Back-map each ranked row to its session_id via claim.scope.
    # Scope is formatted "<prefix>:<session_id>" during seeding. For synthetic
    # verbatim rows we use row["scope"] instead.
    ranked_cids: list[int] = []
    ranked_sids: list[str] = []
    for row in ranked[:top_k]:
        claim = row.get("claim")
        cid = getattr(claim, "id", None)
        ranked_cids.append(int(cid) if cid is not None else -1)
        # Return the full scope — the caller strips its own prefix so we
        # don't lose leading segments when the prefix contains colons.
        scope = getattr(claim, "scope", None) or row.get("scope") or ""
        ranked_sids.append(scope)

    return RecallResult(
        ranked_claim_ids=ranked_cids,
        ranked_session_ids=ranked_sids,
        latency_ms=dt_ms,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_question(ranked_sids: list[str], golden_sids: Iterable[str]) -> dict:
    golden = set(golden_sids)
    hit_at_1 = 1 if ranked_sids and ranked_sids[0] in golden else 0
    hit_at_5 = 1 if any(s in golden for s in ranked_sids[:5]) else 0
    rr = 0.0
    for idx, s in enumerate(ranked_sids, start=1):
        if s in golden:
            rr = 1.0 / idx
            break
    return {"hit_at_1": hit_at_1, "hit_at_5": hit_at_5, "mrr": rr}


# ---------------------------------------------------------------------------
# Per-question DB isolation (roadmap 11.4)
# ---------------------------------------------------------------------------

def _per_q_root(*, keep_dbs: bool) -> Path:
    """Return root dir for per-question ephemeral bench DBs.

    When ``keep_dbs`` is True, persist under ``artifacts/longmemeval-per-q/``
    for debugging. Otherwise scope to a UUID-namespaced temp dir under the
    OS tmp root (e.g. ``%TEMP%/memorymaster-longmemeval/<uuid>``) so parallel
    workers can't collide and nothing ever leaks into the repo tree.
    """
    if keep_dbs:
        root = REPO / "artifacts" / "longmemeval-per-q"
    else:
        base = Path(tempfile.gettempdir()) / "memorymaster-longmemeval"
        root = base / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def _per_q_db_path(root: Path, qid: str) -> str:
    """Return the sqlite path for a single question's ephemeral bench DB."""
    # Sanitise qid for filesystem: LongMemEval IDs look like
    # ``multi_session_synthesis_1_0`` — already safe — but be defensive.
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in qid)
    return str(root / f"{safe}.db")


def _cleanup_per_q_db(db_path: str) -> None:
    """Delete a per-question bench DB and its WAL/SHM sidecars."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(db_path + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError as exc:
                logger.debug("failed to unlink %s: %s", p, exc)


# ---------------------------------------------------------------------------
# Worker — single-config run, invoked in a subprocess
# ---------------------------------------------------------------------------

def run_worker(config_key: str, limit: int) -> None:
    """Run one config over the sampled questions and emit results JSONL.

    ``config_key`` may be either a known CONFIGS entry (A/B/C/D) OR a custom
    label supplied via ``--config-label``. Custom labels inherit whatever
    ``MEMORYMASTER_RECALL_*`` env vars the caller set — the worker does NOT
    clobber them — and seed_verbatim is gated on the presence of
    ``MEMORYMASTER_RECALL_VERBATIM=1`` rather than the config letter.
    """
    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
    global ART_DIR
    output_dir_override = os.environ.get("MEMORYMASTER_LONGMEMEVAL_OUTPUT_DIR")
    if output_dir_override:
        ART_DIR = Path(output_dir_override).resolve()

    if config_key in CONFIGS:
        cfg = CONFIGS[config_key]
        seed_verbatim = config_key == "C"
    else:
        # Custom label: pick up the running env verbatim so the caller owns
        # the knobs. seed_verbatim is inferred from the verbatim gate.
        cfg = {
            "description": f"custom ({config_key}) — env-driven",
            "env": {
                k: v for k, v in os.environ.items()
                if k.startswith("MEMORYMASTER_RECALL_")
            },
        }
        seed_verbatim = os.environ.get("MEMORYMASTER_RECALL_VERBATIM") == "1"

    ART_DIR.mkdir(parents=True, exist_ok=True)

    # Per-Q isolation knobs (roadmap 11.4). When enabled, each question gets
    # its own fresh SQLite file — scoring no longer sees other questions'
    # claims during FTS5 candidate generation, BM25 re-ranking, or entity
    # fanout. Baseline behaviour (shared bench DB) is preserved when the env
    # var is unset so the legacy A/B/C/D sweep stays bit-identical.
    isolate_per_q = os.environ.get("MEMORYMASTER_LONGMEMEVAL_ISOLATE_PER_Q") == "1"
    keep_dbs = os.environ.get("MEMORYMASTER_LONGMEMEVAL_KEEP_DBS") == "1"
    per_q_root: Path | None = None
    shared_db_path: str | None = None

    if isolate_per_q:
        per_q_root = _per_q_root(keep_dbs=keep_dbs)
    else:
        shared_db_path = str(ART_DIR / f"bench-{config_key}.db")
        # Wipe any prior bench DB (and WAL/SHM sidecars) — we seed fresh.
        for suffix in ("", "-wal", "-shm"):
            p = Path(shared_db_path + suffix)
            if p.exists():
                p.unlink()

    # Block the qdrant fallback from contacting an external service by
    # clearing QDRANT_URL in the worker env. MEMORYMASTER_RECALL_VECTOR_FALLBACK
    # is not set either, so _apply_vector_fallback short-circuits.
    os.environ.pop("QDRANT_URL", None)

    from memorymaster.service import MemoryService

    if not isolate_per_q:
        svc_shared = MemoryService(db_target=shared_db_path, workspace_root=REPO)
        svc_shared.init_db()

    questions = load_questions(limit=limit)
    out_path = ART_DIR / f"results-{config_key}.jsonl"

    n_hits_1 = 0
    n_hits_5 = 0
    mrr_sum = 0.0
    latency_total = 0.0
    n = 0
    scope_prefix = f"q:{config_key}"
    t_run = time.perf_counter()

    with out_path.open("w", encoding="utf-8") as fh:
        for q in questions:
            qid = q["question_id"]

            if isolate_per_q:
                assert per_q_root is not None  # narrowing for type-checkers
                db_path = _per_q_db_path(per_q_root, qid)
                # Defensive: if a stale file exists (rerun with --keep), wipe
                # before seeding so we never measure with yesterday's claims.
                _cleanup_per_q_db(db_path)
                svc = MemoryService(db_target=db_path, workspace_root=REPO)
                svc.init_db()
            else:
                db_path = shared_db_path  # type: ignore[assignment]
                svc = svc_shared

            # Scope prefix keeps the back-strip logic working whether we're
            # in shared or isolated mode. In isolated mode, cross-question
            # leaks are impossible at the SQLite layer, but the prefix is
            # still required for the scoring strip and synthetic verbatim rows.
            seed_stats = seed_question(
                svc,
                q,
                seed_verbatim=seed_verbatim,
                db_path=db_path,
                scope_prefix=f"{scope_prefix}:{qid}",
            )
            result = recall_with_ranked_ids(q["question"], db_path=db_path, top_k=_TOPK)

            # Strip the scope prefix from retrieved session_ids so scoring
            # lines up with the raw haystack_session_ids. The seeded scope
            # format is "<scope_prefix>:<qid>:<session_id>".
            stripped_sids: list[str] = []
            pref = f"{scope_prefix}:{qid}:"
            for s in result.ranked_session_ids:
                if s.startswith(pref):
                    stripped_sids.append(s[len(pref):])
                else:
                    # Cross-question leak (FTS5 matched another question's
                    # claim) or synthetic verbatim row without a claim scope.
                    # Record the full scope verbatim — it won't match golden
                    # anyway, so scoring naturally treats it as a miss.
                    stripped_sids.append(s)

            scores = score_question(stripped_sids, q["answer_session_ids"])
            record = {
                "qid": qid,
                "config": config_key,
                "question": q["question"][:300],
                "question_type": q.get("question_type", ""),
                "golden_session_ids": q["answer_session_ids"],
                "retrieved_session_ids": stripped_sids,
                "retrieved_claim_ids": result.ranked_claim_ids,
                "seed_claims": seed_stats["claims"],
                "seed_verbatim_rows": seed_stats["verbatim_rows"],
                "hit_at_1": scores["hit_at_1"],
                "hit_at_5": scores["hit_at_5"],
                "mrr": scores["mrr"],
                "latency_ms": round(result.latency_ms, 2),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            n += 1
            n_hits_1 += scores["hit_at_1"]
            n_hits_5 += scores["hit_at_5"]
            mrr_sum += scores["mrr"]
            latency_total += result.latency_ms

            # Per-Q cleanup (skip when --keep-bench-dbs was requested).
            if isolate_per_q:
                # Drop the local service reference and force GC so any lingering
                # sqlite connections from MemoryService/SQLiteStore are finalised
                # before we try to unlink the file (Windows + WAL is stricter
                # than POSIX here — an open handle blocks ``.db`` removal).
                del svc
                gc.collect()
                if not keep_dbs:
                    _cleanup_per_q_db(db_path)

    dt_run = time.perf_counter() - t_run

    # After the whole sweep, remove the UUID-namespaced temp root so we don't
    # leak hundreds of empty directories into %TEMP%. In keep mode we leave
    # everything under ``artifacts/longmemeval-per-q/`` untouched so the
    # caller can diff individual DBs.
    if isolate_per_q and not keep_dbs and per_q_root is not None:
        try:
            shutil.rmtree(per_q_root, ignore_errors=True)
        except OSError as exc:
            logger.debug("failed to remove per-q root %s: %s", per_q_root, exc)
    summary = {
        "config": config_key,
        "description": cfg["description"],
        "n_questions": n,
        "hit_at_1": round(n_hits_1 / n, 4) if n else 0.0,
        "hit_at_5": round(n_hits_5 / n, 4) if n else 0.0,
        "mrr": round(mrr_sum / n, 4) if n else 0.0,
        "mean_latency_ms": round(latency_total / n, 2) if n else 0.0,
        "total_runtime_s": round(dt_run, 1),
        "env_applied": cfg["env"],
        "isolate_per_q": isolate_per_q,
        "keep_bench_dbs": keep_dbs if isolate_per_q else False,
    }
    sum_path = ART_DIR / f"summary-{config_key}.json"
    sum_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_config_subprocess(
    config_key: str,
    limit: int,
    *,
    output_dir: Path | None = None,
    inherit_env: bool = False,
    isolate_per_q: bool = False,
    keep_bench_dbs: bool = False,
) -> dict:
    """Run one config in a clean subprocess with its env vars applied.

    Args:
        config_key: named config (A/B/C/D) or custom label.
        limit: number of questions (0 = all 500).
        output_dir: optional override for artifact directory. Propagated to
            the worker via ``MEMORYMASTER_LONGMEMEVAL_OUTPUT_DIR``.
        inherit_env: when True, keep the caller's ``MEMORYMASTER_RECALL_*``
            env untouched (used by ``--config-label`` mode where the caller
            explicitly sets the knobs). When False (default, legacy multi-
            config sweep), scrub the recall env and apply ``CONFIGS`` entry.
        isolate_per_q: when True, seed a fresh bench DB per question so FTS5
            candidate generation and BM25 re-ranking only see that question's
            own claims (roadmap 11.4). Propagated via
            ``MEMORYMASTER_LONGMEMEVAL_ISOLATE_PER_Q=1``.
        keep_bench_dbs: when True (with ``isolate_per_q``), preserve each
            per-question DB under ``artifacts/longmemeval-per-q/<qid>.db``
            instead of cleaning up. Debugging aid.
    """
    if config_key in CONFIGS:
        cfg = CONFIGS[config_key]
    else:
        cfg = {"description": f"custom ({config_key})", "env": {}}
    env = os.environ.copy()
    if not inherit_env:
        # Remove any leftover MEMORYMASTER_RECALL_* knobs from the parent shell
        # so we truly start from shipped defaults.
        for k in list(env.keys()):
            if k.startswith("MEMORYMASTER_RECALL_"):
                env.pop(k, None)
        env.update(cfg["env"])
    # Also clear QDRANT_URL so the subprocess can't hit an external service.
    env.pop("QDRANT_URL", None)
    # Ensure Python can import the project package.
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    if output_dir is not None:
        env["MEMORYMASTER_LONGMEMEVAL_OUTPUT_DIR"] = str(output_dir.resolve())
    if isolate_per_q:
        env["MEMORYMASTER_LONGMEMEVAL_ISOLATE_PER_Q"] = "1"
    else:
        env.pop("MEMORYMASTER_LONGMEMEVAL_ISOLATE_PER_Q", None)
    if keep_bench_dbs:
        env["MEMORYMASTER_LONGMEMEVAL_KEEP_DBS"] = "1"
    else:
        env.pop("MEMORYMASTER_LONGMEMEVAL_KEEP_DBS", None)

    print(f"[longmemeval] running config {config_key}: {cfg['description']}", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--config",
            config_key,
            "--limit",
            str(limit),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        print(f"[longmemeval] config {config_key} FAILED ({dt:.1f}s):", flush=True)
        print("  stderr:", proc.stderr[:4000], flush=True)
        return {"config": config_key, "error": proc.stderr[-500:]}

    # Parse summary from stdout's last JSON line.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    if not lines:
        return {"config": config_key, "error": "no summary emitted"}
    try:
        summary = json.loads(lines[-1])
    except Exception as exc:
        return {"config": config_key, "error": f"parse fail: {exc}"}
    print(
        f"[longmemeval] config {config_key} done in {dt:.1f}s — "
        f"hit@1={summary['hit_at_1']:.3f} hit@5={summary['hit_at_5']:.3f} "
        f"MRR={summary['mrr']:.3f} lat={summary['mean_latency_ms']:.1f}ms",
        flush=True,
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100,
                        help="How many questions to evaluate (0 = all 500).")
    parser.add_argument("--questions", type=int, default=None,
                        help="Alias for --limit (0 = all 500).")
    parser.add_argument("--configs", type=str, default="A,B,C,D")
    parser.add_argument(
        "--config-label",
        type=str,
        default="",
        help="Run a single custom-labeled config inheriting the caller's "
             "MEMORYMASTER_RECALL_* env. Skips the A/B/C/D sweep. Writes "
             "results-<label>.jsonl and summary-<label>.json.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="Override artifact directory (default artifacts/longmemeval).",
    )
    parser.add_argument(
        "--isolate-per-q",
        action="store_true",
        help="Seed a fresh ephemeral bench DB for each question so FTS5 "
             "candidate generation and BM25 re-ranking only see that "
             "question's own claims. Eliminates cross-question contamination "
             "on the LongMemEval harness (roadmap 11.4). Opt-in; default "
             "preserves legacy shared-DB behaviour.",
    )
    parser.add_argument(
        "--keep-bench-dbs",
        action="store_true",
        help="With --isolate-per-q, persist each per-question DB under "
             "artifacts/longmemeval-per-q/<qid>.db for debugging. Default "
             "places them in a UUID-namespaced temp dir and deletes after "
             "scoring.",
    )
    parser.add_argument("--worker", action="store_true",
                        help="Internal: run a single config end-to-end.")
    parser.add_argument("--config", type=str, default="",
                        help="Internal (worker mode).")
    args = parser.parse_args()

    # --questions takes precedence when explicitly provided.
    limit = args.questions if args.questions is not None else args.limit

    if args.worker:
        # Worker accepts either a known config key or a custom label.
        run_worker(args.config, limit or 0)
        return 0

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    ensure_dataset()
    global ART_DIR
    output_dir = Path(args.output_dir).resolve() if args.output_dir else _DEFAULT_ART_DIR
    ART_DIR = output_dir
    ART_DIR.mkdir(parents=True, exist_ok=True)

    # Single-label custom-config path (new). Inherits caller's env so, e.g.,
    # MEMORYMASTER_RECALL_FUSION=rrf gets through untouched.
    if args.config_label:
        summary = run_config_subprocess(
            args.config_label,
            limit,
            output_dir=output_dir,
            inherit_env=True,
            isolate_per_q=args.isolate_per_q,
            keep_bench_dbs=args.keep_bench_dbs,
        )
        roll_up_path = ART_DIR / f"summary-{args.config_label}.json"
        if "error" not in summary:
            roll_up_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(f"[longmemeval] wrote {roll_up_path}", flush=True)
        else:
            print(f"[longmemeval] run FAILED: {summary}", flush=True)
            return 1
        return 0

    # Legacy multi-config sweep path (unchanged behaviour).
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    for c in configs:
        if c not in CONFIGS:
            parser.error(f"unknown config {c!r}; valid: {list(CONFIGS)}")

    all_summaries: list[dict] = []
    for c in configs:
        summary = run_config_subprocess(
            c,
            limit,
            output_dir=output_dir,
            isolate_per_q=args.isolate_per_q,
            keep_bench_dbs=args.keep_bench_dbs,
        )
        all_summaries.append(summary)

    roll_up_path = ART_DIR / "summary.json"
    roll_up_path.write_text(
        json.dumps({"summaries": all_summaries, "limit": limit}, indent=2),
        encoding="utf-8",
    )
    print(f"[longmemeval] wrote {roll_up_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
