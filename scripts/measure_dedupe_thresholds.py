"""Sweep MEMORYMASTER_DEDUPE_JACCARD_HIGH against live candidates.

Runs find_near_duplicate against every status='candidate' claim at multiple
thresholds, reports archive counts per threshold, and samples top matches
for manual precision inspection.

Usage:
    python scripts/measure_dedupe_thresholds.py memorymaster.db
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter

from memorymaster.candidate_dedupe import find_near_duplicate


THRESHOLDS = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
SAMPLE_LIMIT = 20


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "memorymaster.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    candidates = conn.execute(
        """
        SELECT id, text, scope FROM claims
        WHERE status = 'candidate' AND length(trim(text)) >= 10
        ORDER BY id DESC LIMIT 2000
        """
    ).fetchall()

    print(f"# Sweep over {len(candidates)} candidate claims")

    archive_counts: Counter[float] = Counter()
    score_buckets: Counter[str] = Counter()
    samples_at_default: list[tuple[float, str, str]] = []

    score_bins = [
        (0.0, 0.2, "0.0-0.2"),
        (0.2, 0.4, "0.2-0.4"),
        (0.4, 0.6, "0.4-0.6"),
        (0.6, 0.7, "0.6-0.7"),
        (0.7, 0.8, "0.7-0.8"),
        (0.8, 0.85, "0.8-0.85"),
        (0.85, 0.9, "0.85-0.9"),
        (0.9, 0.95, "0.9-0.95"),
        (0.95, 1.0001, "0.95-1.0"),
    ]

    canonicals_for_sample: dict[int, str] = {}

    for row in candidates:
        result = find_near_duplicate(
            conn,
            candidate_id=row["id"],
            candidate_text=row["text"],
            candidate_scope=row["scope"] or "",
            jaccard_high=0.0,
        )
        score = result.jaccard_score or 0.0
        for low, high, label in score_bins:
            if low <= score < high:
                score_buckets[label] += 1
                break

        for t in THRESHOLDS:
            if score >= t:
                archive_counts[t] += 1

        if 0.75 <= score < 0.85 and len(samples_at_default) < SAMPLE_LIMIT:
            canonical_text = ""
            if result.canonical_claim_id is not None:
                canonical_row = conn.execute(
                    "SELECT text FROM claims WHERE id = ?",
                    (result.canonical_claim_id,),
                ).fetchone()
                if canonical_row:
                    canonical_text = canonical_row["text"] or ""
                    canonicals_for_sample[result.canonical_claim_id] = canonical_text
            samples_at_default.append((score, row["text"], canonical_text))

    print("\n## Archive count per threshold")
    print("| threshold | archives | rate |")
    print("|-----------|----------|------|")
    n = len(candidates)
    for t in THRESHOLDS:
        c = archive_counts[t]
        rate = (c / n * 100) if n else 0
        print(f"| {t:.2f}      | {c:>8} | {rate:5.1f}% |")

    print("\n## Score distribution (top-1 jaccard)")
    print("| bucket    | count |")
    print("|-----------|-------|")
    for low, high, label in score_bins:
        print(f"| {label:<9} | {score_buckets[label]:>5} |")

    print(f"\n## Sample would-archives at jaccard in [0.75, 0.85) (n={len(samples_at_default)})")
    for i, (score, candidate_text, canonical_text) in enumerate(samples_at_default[:10], 1):
        cand_short = (candidate_text or "")[:140].replace("\n", " ")
        canon_short = (canonical_text or "")[:140].replace("\n", " ")
        print(f"\n### sample {i} — jaccard={score:.3f}")
        print(f"  CANDIDATE: {cand_short}")
        print(f"  CANONICAL: {canon_short}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
