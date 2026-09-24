"""Evaluate graph-expanded recall arms on a frozen cohort of real questions.

Arms: ``baseline`` (graph off), ``current_enrichment`` (capitalized-entity
enrichment, hybrid only), ``vector_first`` and the experimental ``graph_first``
(``MEMORYMASTER_RECALL_GRAPH_MODE``).  Two subcommands:

* ``build-cohort`` samples real query texts from ``usage_feedback`` of a
  database opened READ-ONLY (``file:...?mode=ro``), keeps question-shaped texts
  with no local path, no phone number or long digit run, and that the single
  egress redactor leaves untouched, excludes the 953 labelled prompts, splits
  relational/ordinary by a fixed cue list and freezes the result with a sha256.
  These filters do NOT make a text public-safe (first names, customer details
  and operator phrasing survive them), so the cohort is written in two halves:
  a manifest with ids, classes and text hashes only (may be committed) and a
  texts file that is refused unless git ignores its path.
* ``run`` replays the frozen cohort through ``MemoryService(read_only=True)``
  with ``record_accesses=False`` and a local hash embedding (no provider call),
  and reports oracle coverage BEFORE any ranking metric.  Without trustworthy
  relevance labels, hit@5 and precision are reported as UNMEASURED.

Nothing is written to the database.  Evidence holds question ids, claim ids and
aggregate numbers -- never query text, claim text or local paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COHORT_VERSION = 2
LABEL_FILES = (
    ROOT / "artifacts" / "real-prompts-1000-labels-top50.json",
    ROOT / "artifacts" / "real-prompts-1000-top50-labels.json",
    ROOT / "artifacts" / "real-prompts-1000-labels.json",
)
QUESTION_RE = re.compile(
    r"\?|^(what|how|why|where|which|who|when|is|are|does|do|did|can|should|could"
    r"|que|qué|como|cómo|por que|por qué|donde|dónde|cual|cuál|quien|quién|cuando|cuándo)\b",
    re.IGNORECASE,
)
RELATIONAL_RE = re.compile(
    r"\b(depend\w*|relat\w*|connect\w*|link\w*|between|uses?|using|used by|owner|owns?|"
    r"who|why|because|caus\w*|affect\w*|impact\w*|runs? on|deployed|hosted|upstream|"
    r"downstream|calls?|integrat\w*|relationship|depende\w*|relacion\w*|conect\w*|entre|"
    r"usa|quien|quién|por qu[eé]|afecta\w*|corre en|desplegad\w*)\b",
    re.IGNORECASE,
)
# Drive-letter paths (either slash), UNC/WSL shares and POSIX home-like roots.
LOCAL_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/]|\\\\|(?:^|\s)(?:/home/|/Users/|/mnt/|~/)")
# Phone numbers and other long digit runs (ids, card or account numbers): seven
# or more digits, allowing short separators as in "+54 9 2477 31-5837".  The
# egress redactor does not cover them, so the cohort drops them itself.
DIGIT_RUN_RE = re.compile(r"\d(?:[\s().\-/]{0,2}\d){6,}")
NOISE_RE = re.compile(
    r"^\s*<|task-notification|system-reminder|\[Request interrupted|Caveat:|"
    r"command-name|tool_use|```",
    re.IGNORECASE,
)


def has_phone_or_digit_run(text: str) -> bool:
    return DIGIT_RUN_RE.search(text) is not None


def _require_git_ignored(path: Path) -> None:
    """Raw query texts may only be written where git will not pick them up.

    Outside any repository is fine; inside one, the path must be ignored (and
    not already tracked).  Anything that cannot be verified is refused.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "-C", str(path.parent), "check-ignore", "-q", "--", path.name],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(
            f"cannot verify that the cohort texts file is git-ignored ({type(exc).__name__})"
        ) from None
    if proc.returncode == 0:
        return
    if proc.returncode == 128 and "not a git repository" in (proc.stderr or "").lower():
        return
    raise SystemExit(
        f"refusing to write raw query texts to {path.name}: the path is not git-ignored "
        "(use an ignored location such as artifacts/)"
    )


def _ro_connect(db: Path) -> sqlite3.Connection:
    uri = f"file:{db.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _labelled_prompt_ids() -> set[str]:
    ids: set[str] = set()
    for path in LABEL_FILES:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        labels = data.get("labels", data) if isinstance(data, dict) else {}
        if isinstance(labels, dict):
            ids.update(str(key) for key in labels)
    return ids


def _sha1_16(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _excluded_prompt_texts(paths: list[Path]) -> set[str]:
    """Texts of labelled prompt sets (JSONL with a ``text`` field)."""
    texts: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.add(_norm(str(json.loads(line).get("text", ""))))
    return texts


def build_cohort(
    db: Path,
    out: Path,
    *,
    texts_out: Path,
    per_class: int,
    seed: str,
    exclude_prompts: list[Path] = (),
) -> dict[str, Any]:
    from memorymaster.decisions.egress import prepare_egress

    if texts_out.resolve() == out.resolve():
        raise SystemExit("the cohort manifest and its texts file must be different files")
    _require_git_ignored(texts_out)
    excluded_ids = _labelled_prompt_ids()
    excluded_texts = _excluded_prompt_texts(list(exclude_prompts))
    conn = _ro_connect(db)
    try:
        texts = [str(row[0]) for row in conn.execute(
            "SELECT DISTINCT query_text FROM usage_feedback WHERE query_text IS NOT NULL"
        )]
    finally:
        conn.close()
    counters: Counter[str] = Counter(distinct_query_texts=len(texts))
    pools: dict[str, list[str]] = {"relational": [], "ordinary": []}
    seen: set[str] = set()
    for raw in texts:
        text = " ".join(raw.split())
        if text in seen:
            continue
        seen.add(text)
        if not 12 <= len(text) <= 300 or NOISE_RE.search(text):
            counters["dropped_noise_or_length"] += 1
            continue
        if not QUESTION_RE.search(text):
            counters["dropped_not_question"] += 1
            continue
        if (
            {_sha1_16(raw), _sha1_16(raw.strip()), _sha1_16(text)} & excluded_ids
            or _norm(text) in excluded_texts
        ):
            counters["dropped_953_labelled"] += 1
            continue
        if LOCAL_PATH_RE.search(text):
            counters["dropped_local_path"] += 1
            continue
        if has_phone_or_digit_run(text):
            counters["dropped_phone_or_digit_run"] += 1
            continue
        egress = prepare_egress(text)
        if egress.blocked or egress.counts or egress.text != text:
            counters["dropped_private_or_redactable"] += 1
            continue
        pools["relational" if RELATIONAL_RE.search(text) else "ordinary"].append(text)
    questions = []
    texts_by_id: dict[str, str] = {}
    for klass, pool in pools.items():
        counters[f"eligible_{klass}"] = len(pool)
        ordered = sorted(pool, key=lambda t: _sha(seed + "\x00" + t))
        for text in ordered[:per_class]:
            digest = _sha(text)
            questions.append({"id": digest[:16], "class": klass, "text_sha256": digest})
            texts_by_id[digest[:16]] = text
    cohort = {
        "version": COHORT_VERSION,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "usage_feedback.query_text (production DB opened read-only)",
        "selection": {
            "per_class": per_class,
            "seed": seed,
            "question_filter": "contains '?' or starts with an interrogative (en/es)",
            "relational_cues": RELATIONAL_RE.pattern,
            "excluded": "953 labelled prompts (sha1[:16] ids from artifacts/*labels*.json)",
            "excluded_label_ids": len(excluded_ids),
            "excluded_prompt_texts": len(excluded_texts),
            "privacy": (
                "texts with local filesystem paths, phone numbers or 7+ digit runs, "
                "or changed/blocked by decisions.egress.prepare_egress, are dropped; "
                "raw texts live only in the git-ignored texts file"
            ),
        },
        "counters": dict(counters),
        "texts_file": texts_out.name,
        "questions": questions,
    }
    cohort["questions_sha256"] = _sha(json.dumps(questions, sort_keys=True, ensure_ascii=False))
    texts_out.write_text(
        json.dumps(
            {"version": COHORT_VERSION, "questions_sha256": cohort["questions_sha256"],
             "texts": texts_by_id},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cohort, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cohort


def _load_cohort(path: Path, texts_path: Path) -> dict[str, Any]:
    """Join the frozen manifest with its private texts, verifying both halves."""
    cohort = json.loads(path.read_text(encoding="utf-8"))
    if cohort.get("version") != COHORT_VERSION:
        raise SystemExit(f"cohort version {cohort.get('version')} is not {COHORT_VERSION}; rebuild it")
    digest = _sha(json.dumps(cohort["questions"], sort_keys=True, ensure_ascii=False))
    if digest != cohort.get("questions_sha256"):
        raise SystemExit("cohort questions changed since it was frozen (sha256 mismatch)")
    private = json.loads(texts_path.read_text(encoding="utf-8"))
    if private.get("questions_sha256") != digest:
        raise SystemExit("texts file belongs to another cohort (questions_sha256 mismatch)")
    texts = private.get("texts") or {}
    questions = []
    for question in cohort["questions"]:
        text = texts.get(question["id"])
        if text is None or _sha(text) != question["text_sha256"]:
            raise SystemExit(
                f"cohort text {question['id']} changed since it was frozen (sha256 mismatch)"
            )
        questions.append({**question, "text": text})
    return {**cohort, "questions": questions}


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return round(ordered[index], 1)


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text or "") / 4))


def _oracle_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """Is there any trustworthy relevance signal in the database?"""
    row = conn.execute(
        """SELECT COUNT(*), SUM(was_returned = 1), SUM(score IS NULL)
           FROM usage_feedback"""
    ).fetchone()
    total, returned, null_score = (int(v or 0) for v in row)
    return {
        "usage_feedback_rows": total,
        "was_returned_1": returned,
        "score_null": null_score,
        "trustworthy": False,
        "why": (
            "usage_feedback only logs what recall returned (was_returned=1 and "
            "score NULL on every row): a label derived from it would grade the "
            "baseline retriever against its own output"
        ) if total and returned == total and null_score == total else "not audited",
    }


def _support_audit(conn: sqlite3.Connection, claim_ids: list[int]) -> dict[str, int]:
    """Independent SQL re-check of path supports (not the code under test)."""
    if not claim_ids:
        return {"supports": 0, "inactive": 0, "uncited": 0, "retired_evidence": 0}
    marks = ",".join("?" * len(claim_ids))
    rows = conn.execute(
        f"""SELECT c.id, c.status,
                   (SELECT COUNT(*) FROM citations ci
                    WHERE ci.claim_id = c.id AND TRIM(ci.source) <> '') AS cites,
                   (SELECT COUNT(*) FROM claim_evidence_links cel
                    JOIN evidence_items ei ON ei.id = cel.evidence_item_id
                    JOIN source_items si ON si.id = ei.source_item_id
                    WHERE cel.claim_id = c.id AND si.retired_at IS NOT NULL) AS retired
            FROM claims c WHERE c.id IN ({marks})""",
        claim_ids,
    ).fetchall()
    return {
        "supports": len(rows),
        "inactive": sum(1 for r in rows if r[1] != "confirmed"),
        "uncited": sum(1 for r in rows if not r[2]),
        "retired_evidence": sum(1 for r in rows if r[3]),
    }


def _statuses(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {
        int(row[0]): str(row[1])
        for row in conn.execute(f"SELECT id, status FROM claims WHERE id IN ({marks})", ids)
    }


def _latent_reach(conn: sqlite3.Connection, base_ids: dict[str, list[int]]) -> dict[str, Any]:
    """Independent SQL: do the baseline rows have any expandable edge at all?

    Walks one hop over the same edge families the expansion may use
    (``claim_links`` of positive types, ``claim_edges`` and supported entity
    relations) without any authorization, so it bounds what expansion could
    ever add; it is not the code under test.
    """
    from memorymaster.recall.graph_expansion import EXPANSION_EDGE_KINDS, EXPANSION_LINK_TYPES

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    consts = {f"l{i}": value for i, value in enumerate(EXPANSION_LINK_TYPES)}
    consts.update({f"k{i}": value for i, value in enumerate(EXPANSION_EDGE_KINDS)})
    links = ",".join(f":l{i}" for i in range(len(EXPANSION_LINK_TYPES)))
    kinds = ",".join(f":k{i}" for i in range(len(EXPANSION_EDGE_KINDS)))
    selects = []
    if "claim_links" in tables:
        selects.append(
            "SELECT CASE WHEN source_id = :id THEN target_id ELSE source_id END FROM claim_links"
            f" WHERE (source_id = :id OR target_id = :id) AND link_type IN ({links})"
        )
    if "claim_edges" in tables:
        selects.append(
            "SELECT CASE WHEN src_claim_id = :id THEN dst_claim_id ELSE src_claim_id END"
            " FROM claim_edges WHERE (src_claim_id = :id OR dst_claim_id = :id)"
            f" AND edge_kind IN ({kinds})"
        )
    if {"claim_entity_links", "entity_edge_supports"} <= tables:
        selects.append(
            """SELECT b.claim_id FROM claim_entity_links a
               JOIN entity_edge_supports es
                 ON es.source_entity_id = a.entity_id OR es.target_entity_id = a.entity_id
               JOIN claim_entity_links b
                 ON b.entity_id = CASE WHEN es.source_entity_id = a.entity_id
                                       THEN es.target_entity_id ELSE es.source_entity_id END
               WHERE a.claim_id = :id AND b.claim_id <> :id"""
        )
    sql = " UNION ".join(selects)
    base_status: Counter[str] = Counter()
    neighbour_status: Counter[str] = Counter()
    with_edge = confirmed_pairs = 0
    for ids in base_ids.values():
        statuses = _statuses(conn, ids)
        base_status.update(statuses.values())
        neighbours: dict[int, str] = {}
        confirmed_pair = False
        for claim_id in ids if sql else []:
            found = {int(row[0]) for row in conn.execute(sql, {**consts, "id": claim_id})}
            found_status = _statuses(conn, sorted(found - set(ids)))
            confirmed_pair |= (
                statuses.get(claim_id) == "confirmed" and "confirmed" in found_status.values()
            )
            neighbours.update(found_status)
        neighbour_status.update(neighbours.values())
        with_edge += bool(neighbours)
        confirmed_pairs += confirmed_pair
    return {
        "base_row_status": dict(base_status),
        "questions_with_any_edge_from_base": with_edge,
        "neighbour_status": dict(neighbour_status),
        "questions_with_confirmed_to_confirmed_edge": confirmed_pairs,
    }


def _load_labels(path: Path | None, question_ids: set[str]) -> tuple[dict[str, set[int]], int]:
    """Relevance labels: ``{"labels": {question_id: [claim_id, ...]}}``."""
    if path is None:
        return {}, 0
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("labels", data) if isinstance(data, dict) else {}
    labels = {
        str(qid): {int(cid) for cid in claim_ids}
        for qid, claim_ids in raw.items()
        if str(qid) in question_ids and claim_ids
    }
    return labels, sum(1 for qid in raw if str(qid) not in question_ids)


ARMS = {
    "legacy": [
        ("baseline", {"graph_mode": "off"}),
        ("vector_first", {"graph_mode": "vector_first"}),
        ("graph_first", {"graph_mode": "graph_first"}),
    ],
    "hybrid": [
        ("baseline", {"graph_mode": "off"}),
        ("current_enrichment", {"graph_mode": "off", "enrich_with_entities": True}),
        ("vector_first", {"graph_mode": "vector_first"}),
        ("graph_first", {"graph_mode": "graph_first"}),
    ],
}


def run(
    db: Path,
    cohort_path: Path,
    out_dir: Path,
    *,
    texts: Path,
    limit: int,
    modes: list[str],
    labels: Path | None = None,
    min_labelled: int = 30,
) -> dict[str, Any]:
    from memorymaster.core.service import MemoryService
    from memorymaster.recall.embeddings import EmbeddingProvider

    cohort = _load_cohort(cohort_path, texts)
    relevant, unknown_labels = _load_labels(labels, {q["id"] for q in cohort["questions"]})
    measured = bool(relevant) and len(relevant) >= min_labelled
    svc = MemoryService(db, read_only=True)
    if not getattr(svc.store, "read_only", False):
        raise SystemExit("refusing to evaluate on a writable store")
    svc.embedding_provider = EmbeddingProvider(model="hash-v1", dims=1536)
    audit_conn = _ro_connect(db)
    per_question: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "cohort": {
            "file": cohort_path.name,
            "questions_sha256": cohort["questions_sha256"],
            "size": len(cohort["questions"]),
            "by_class": dict(Counter(q["class"] for q in cohort["questions"])),
        },
        "limit": limit,
        "embedding": "hash-v1 (local, no provider call; semantic recall NOT exercised)",
        "oracle": _oracle_audit(audit_conn),
        "arms": {},
    }
    summary["oracle"].update({
        "labels_file": labels.name if labels is not None else None,
        "labelled_questions": len(relevant),
        "labels_unknown_ids": unknown_labels,
        "coverage": round(len(relevant) / max(len(cohort["questions"]), 1), 4),
        "min_labelled": min_labelled,
        "ranking_metrics": "measured" if measured else "UNMEASURED",
    })
    baselines: dict[tuple[str, str], list[int]] = {}
    for mode in modes:
        for arm, kwargs in ARMS[mode]:
            key = f"{mode}/{arm}"
            latencies: list[float] = []
            fallbacks: Counter[str] = Counter()
            rejections: Counter[str] = Counter()
            per_class: dict[str, Counter[str]] = {}
            totals: Counter[str] = Counter()
            graph_supports: list[int] = []
            for question in cohort["questions"]:
                t0 = time.perf_counter()
                rows = svc.query_rows(
                    question["text"], limit=limit, retrieval_mode=mode,
                    record_accesses=False, **kwargs,
                )
                latencies.append((time.perf_counter() - t0) * 1000.0)
                ids = [int(row["claim"].id) for row in rows]
                graph_rows = [
                    row for row in rows
                    if row.get("source") in {"graph_expansion", "entity_graph"}
                ]
                report = getattr(svc, "last_graph_expansion", None) if kwargs.get("graph_mode") != "off" else None
                if report is not None:
                    fallbacks[report["fallback_reason"] or "applied"] += 1
                    rejections.update(report["stats"].get("rejections", {}))
                    svc.last_graph_expansion = None
                for row in graph_rows:
                    explanation = row.get("graph_explanation") or {}
                    graph_supports.extend(int(v) for v in explanation.get("supporting_claim_ids", []))
                    totals["graph_rows_with_citations"] += bool(explanation.get("citations"))
                if arm == "baseline":
                    baselines[(mode, question["id"])] = ids
                base_ids = baselines.get((mode, question["id"]), ids)
                klass = per_class.setdefault(question["class"], Counter())
                klass["questions"] += 1
                klass["changed_vs_baseline"] += ids != base_ids
                klass["with_graph_rows"] += bool(graph_rows)
                totals["questions"] += 1
                totals["rows"] += len(rows)
                totals["graph_rows"] += len(graph_rows)
                totals["cited_rows"] += sum(
                    1 for row in rows
                    if any((c.source or "").strip() for c in (row["claim"].citations or []))
                )
                totals["tokens"] += sum(_tokens(row["claim"].text) for row in rows)
                totals["base_top1_kept"] += bool(not base_ids or (ids and ids[0] == base_ids[0]))
                totals["overlap_at_k"] += len(set(ids) & set(base_ids))
                totals["base_rows"] += len(base_ids)
                if question["id"] in relevant:
                    found = len(set(ids[:5]) & relevant[question["id"]])
                    totals["labelled"] += 1
                    totals["hits_at_5"] += bool(found)
                    totals["relevant_at_5"] += found
                per_question.append({
                    "arm": key, "question_id": question["id"], "class": question["class"],
                    "ids": ids, "graph_ids": [int(r["claim"].id) for r in graph_rows],
                    "latency_ms": round(latencies[-1], 1),
                    "fallback_reason": (report or {}).get("fallback_reason"),
                })
            audit = _support_audit(audit_conn, sorted(set(graph_supports)))
            q = max(totals["questions"], 1)
            summary["arms"][key] = {
                "questions": totals["questions"],
                "p50_ms": _percentile(latencies, 50),
                "p95_ms": _percentile(latencies, 95),
                "rows_per_question": round(totals["rows"] / q, 2),
                "graph_rows_total": totals["graph_rows"],
                "questions_with_graph_rows": sum(c["with_graph_rows"] for c in per_class.values()),
                "questions_changed_vs_baseline": sum(c["changed_vs_baseline"] for c in per_class.values()),
                "by_class": {k: dict(v) for k, v in per_class.items()},
                "citation_precision": round(totals["cited_rows"] / max(totals["rows"], 1), 4),
                "supported_path_rate": (
                    round(totals["graph_rows_with_citations"] / totals["graph_rows"], 4)
                    if totals["graph_rows"] else None
                ),
                "support_audit_sql": audit,
                "retired_support_exposed": audit["inactive"] + audit["retired_evidence"],
                "base_top1_kept_rate": round(totals["base_top1_kept"] / q, 4),
                "overlap_with_baseline": round(totals["overlap_at_k"] / max(totals["base_rows"], 1), 4),
                "tokens_per_question_est": round(totals["tokens"] / q, 1),
                "provider_cost_usd": 0.0,
                "fallbacks": dict(fallbacks),
                "rejections": dict(rejections),
                "labelled_questions": totals["labelled"],
                "hit_at_5": (
                    round(totals["hits_at_5"] / totals["labelled"], 4)
                    if measured else "UNMEASURED"
                ),
                "precision_at_5": (
                    round(totals["relevant_at_5"] / (5 * totals["labelled"]), 4)
                    if measured else "UNMEASURED"
                ),
            }
            if arm == "baseline":
                summary["arms"][key]["latent_reach"] = _latent_reach(audit_conn, {
                    qid: ids for (m, qid), ids in baselines.items() if m == mode
                })
    audit_conn.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "per_question.jsonl").open("w", encoding="utf-8") as fh:
        for row in per_question:
            fh.write(json.dumps(row) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-cohort")
    build.add_argument("--db", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True,
                       help="manifest: ids, classes and text hashes (safe to commit)")
    build.add_argument("--texts-out", type=Path, required=True,
                       help="raw query texts; must be a git-ignored path (e.g. artifacts/)")
    build.add_argument("--per-class", type=int, default=60)
    build.add_argument("--seed", default="graphrag-2026-09-23")
    build.add_argument("--exclude-prompts", type=Path, nargs="*", default=[],
                       help="labelled prompt JSONL files whose texts must not be reused")
    evaluate = sub.add_parser("run")
    evaluate.add_argument("--db", type=Path, required=True)
    evaluate.add_argument("--cohort", type=Path, required=True)
    evaluate.add_argument("--texts", type=Path, required=True,
                          help="the private texts file written by build-cohort")
    evaluate.add_argument("--out-dir", type=Path, required=True)
    evaluate.add_argument("--limit", type=int, default=5)
    evaluate.add_argument("--modes", default="legacy,hybrid")
    evaluate.add_argument("--labels", type=Path, default=None,
                          help='relevance labels {"labels": {question_id: [claim_id, ...]}}')
    evaluate.add_argument("--min-labelled", type=int, default=30,
                          help="below this many labelled questions hit@5/precision stay UNMEASURED")
    args = parser.parse_args(argv)
    if args.command == "build-cohort":
        cohort = build_cohort(args.db, args.out, texts_out=args.texts_out,
                              per_class=args.per_class, seed=args.seed,
                              exclude_prompts=args.exclude_prompts)
        print(json.dumps({"counters": cohort["counters"], "size": len(cohort["questions"]),
                          "questions_sha256": cohort["questions_sha256"]}, indent=2))
        return 0
    modes = [m.strip() for m in args.modes.split(",") if m.strip() in ARMS]
    summary = run(args.db, args.cohort, args.out_dir, texts=args.texts, limit=args.limit,
                  modes=modes, labels=args.labels, min_labelled=args.min_labelled)
    print(json.dumps({k: {m: v[m] for m in ("p50_ms", "p95_ms", "graph_rows_total",
                                             "questions_changed_vs_baseline", "fallbacks")}
                      for k, v in summary["arms"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
