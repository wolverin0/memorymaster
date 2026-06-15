"""Build the steward-classifier training fixture (task #129).

Reads a MemoryMaster SQLite DB in read-only mode and emits one JSONL row per
eligible claim with its feature vector under the active ``FEATURE_VERSION``.

Positives:  ``status = 'confirmed'``.
Negatives:  ``status IN ('archived', 'stale')`` where the most recent event
            is NOT a scope-migration archive and NOT a stop-hook backfill
            (those are label-leaking per the spec's risk section).

For v3 we also load a ``WikiCorpus`` once and reuse it across every claim so
the embedding backend (sentence-transformers or TF-IDF fallback) is
initialised exactly once. Disk-side cache at ``artifacts/feature-cache/``
speeds up re-runs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.govern.steward_features import FEATURE_VERSION, extract_features  # noqa: E402
from memorymaster.knowledge.wiki_similarity import load_wiki_corpus  # noqa: E402


_Q_POSITIVES = """
SELECT id, text, subject, predicate, object_value, scope, status,
       claim_type, source_agent, created_at, access_count,
       supersedes_claim_id, replaced_by_claim_id, entity_id,
       wiki_article
FROM claims WHERE status = 'confirmed' ORDER BY created_at ASC
"""

_Q_NEGATIVES = """
SELECT cl.id, cl.text, cl.subject, cl.predicate, cl.object_value, cl.scope,
       cl.status, cl.claim_type, cl.source_agent, cl.created_at, cl.access_count,
       cl.supersedes_claim_id, cl.replaced_by_claim_id, cl.entity_id,
       cl.wiki_article
FROM claims cl
WHERE cl.status IN ('archived', 'stale')
  AND NOT EXISTS (
    SELECT 1 FROM events ev
    WHERE ev.claim_id = cl.id
      AND ev.id = (SELECT MAX(id) FROM events WHERE claim_id = cl.id)
      AND (LOWER(COALESCE(ev.details, '')) LIKE 'migration:%'
           OR LOWER(COALESCE(ev.details, '')) LIKE '%llm-stop-hook-backfill%')
  )
ORDER BY cl.created_at ASC
"""


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _emit(row: sqlite3.Row, label: int, conn: sqlite3.Connection, corpus) -> dict:
    return {
        "claim_id": int(row["id"]),
        "label": int(label),
        "created_at": row["created_at"],
        "feature_version": FEATURE_VERSION,
        "features": extract_features(dict(row), conn, wiki_corpus=corpus),
    }


def build(db_path: Path, out_path: Path, *, neg_ratio: int = 3,
          pos_limit: int | None = None,
          wiki_scope: str = "project:memorymaster",
          wiki_root: Path | None = None,
          repo_root: Path | None = None) -> tuple[int, int, str]:
    corpus = load_wiki_corpus(
        scope=wiki_scope, wiki_root=wiki_root, repo_root=repo_root or ROOT,
    )
    with _ro(db_path) as conn:
        positives = list(conn.execute(_Q_POSITIVES))
        negatives = list(conn.execute(_Q_NEGATIVES))
        if pos_limit is not None:
            positives = positives[-pos_limit:]
        target_neg = min(len(negatives), max(1, neg_ratio * len(positives)))
        if len(negatives) > target_neg:
            negatives = negatives[-target_neg:]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for row in positives:
                fh.write(json.dumps(_emit(row, 1, conn, corpus)) + "\n")
            for row in negatives:
                fh.write(json.dumps(_emit(row, 0, conn, corpus)) + "\n")
    return len(positives), len(negatives), corpus.embedding_backend


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True, help="Path to memorymaster.db (read-only)")
    ap.add_argument("--out", type=Path,
                    default=Path("tests/fixtures/steward_training.jsonl"))
    ap.add_argument("--neg-ratio", type=int, default=3)
    ap.add_argument("--pos-limit", type=int, default=None)
    ap.add_argument("--wiki-scope", default="project:memorymaster")
    ap.add_argument("--wiki-root", type=Path, default=None)
    args = ap.parse_args()
    n_pos, n_neg, backend = build(
        args.db, args.out, neg_ratio=args.neg_ratio, pos_limit=args.pos_limit,
        wiki_scope=args.wiki_scope, wiki_root=args.wiki_root,
    )
    print(f"[training-set] positives={n_pos} negatives={n_neg} backend={backend} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
