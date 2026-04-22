"""Pareto evaluation of claim-promotion thresholds (validator gate).

The task called it "the steward", but the actual promotion gate for candidate
claims lives in ``memorymaster/jobs/validator.py``. This harness replays that
gate offline against a labeled fixture harvested from the live DB in read-only
mode.

Labels:
  POSITIVE: past candidate -> confirmed, still confirmed, confidence >= 0.75
  NEGATIVE: past candidate -> {conflicted, archived, superseded} that never
            reached confirmed (excludes high-quality dedup losers)

Usage:
    python scripts/eval_steward_pareto.py --db memorymaster.db
    python scripts/eval_steward_pareto.py --skip-rebuild  # reuse fixture
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.jobs.validator import validation_score  # noqa: E402


class _ClaimLike:
    """Duck-typed minimal claim used by ``validation_score``."""
    __slots__ = ("text", "subject", "predicate", "object_value")

    def __init__(self, text, subject, predicate, object_value):
        self.text = text
        self.subject = subject
        self.predicate = predicate
        self.object_value = object_value


@dataclass(frozen=True)
class LabeledCase:
    claim_id: int
    label: str
    text: str
    subject: str
    predicate: str
    object_value: str
    citation_count: int
    prior_confidence: float


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


_Q_POS = """
SELECT cl.id, cl.text, COALESCE(cl.subject,''), COALESCE(cl.predicate,''),
       COALESCE(cl.object_value,''), cl.confidence,
       (SELECT COUNT(*) FROM citations WHERE claim_id=cl.id)
FROM claims cl
WHERE cl.status='confirmed'
  AND cl.confidence >= 0.75
  AND EXISTS (SELECT 1 FROM events ev
              WHERE ev.claim_id=cl.id
                AND ev.from_status='candidate'
                AND ev.to_status='confirmed')
ORDER BY cl.id DESC
LIMIT ?
"""

_Q_NEG = """
SELECT cl.id, cl.text, COALESCE(cl.subject,''), COALESCE(cl.predicate,''),
       COALESCE(cl.object_value,''), cl.confidence,
       (SELECT COUNT(*) FROM citations WHERE claim_id=cl.id)
FROM claims cl
WHERE EXISTS (SELECT 1 FROM events ev
              WHERE ev.claim_id=cl.id
                AND ev.from_status='candidate'
                AND ev.to_status IN ('conflicted','archived','superseded'))
  AND NOT EXISTS (SELECT 1 FROM events ev2
                  WHERE ev2.claim_id=cl.id
                    AND ev2.to_status='confirmed')
ORDER BY cl.id DESC
LIMIT ?
"""


def _fetch(conn: sqlite3.Connection, q: str, limit: int, label: str) -> list[LabeledCase]:
    return [
        LabeledCase(cid, label, t, s, p, o, cc, conf)
        for (cid, t, s, p, o, conf, cc) in conn.execute(q, (limit,))
    ]


def build_fixture(db_path: Path, n_pos: int, n_neg: int) -> list[LabeledCase]:
    with _ro(db_path) as conn:
        return _fetch(conn, _Q_POS, n_pos, "positive") + _fetch(conn, _Q_NEG, n_neg, "negative")


def _score_case(case: LabeledCase) -> float:
    claim = _ClaimLike(case.text, case.subject, case.predicate, case.object_value)
    return validation_score(claim, citation_count=case.citation_count,
                            prior_confidence=case.prior_confidence)  # type: ignore[arg-type]


def evaluate(cases: Iterable[LabeledCase], *, min_citations: int, min_score: float) -> dict:
    tp = fp = tn = fn = 0
    for case in cases:
        promoted = case.citation_count >= min_citations and _score_case(case) >= min_score
        if case.label == "positive":
            tp += promoted; fn += not promoted
        else:
            fp += promoted; tn += not promoted
    n_pos, n_neg = tp + fn, fp + tn
    return {
        "min_citations": min_citations, "min_score": round(min_score, 3),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn, "n_pos": n_pos, "n_neg": n_neg,
        "promote_rate": round(tp / n_pos, 4) if n_pos else 0.0,
        "false_promote_rate": round(fp / n_neg, 4) if n_neg else 0.0,
    }


def pareto_frontier(results: list[dict]) -> list[dict]:
    frontier: list[dict] = []
    for cand in results:
        dominated = False
        for other in results:
            if other is cand:
                continue
            bf = other["false_promote_rate"] < cand["false_promote_rate"]
            ef = other["false_promote_rate"] == cand["false_promote_rate"]
            br = other["promote_rate"] > cand["promote_rate"]
            er = other["promote_rate"] == cand["promote_rate"]
            if (bf and (br or er)) or (ef and br):
                dominated = True
                break
        if not dominated:
            frontier.append(cand)
    frontier.sort(key=lambda r: (r["false_promote_rate"], -r["promote_rate"]))
    return frontier


def pick_recommended(frontier: list[dict], fpr_cap: float) -> dict | None:
    eligible = [p for p in frontier if p["false_promote_rate"] <= fpr_cap]
    if eligible:
        return max(eligible, key=lambda p: (p["promote_rate"], -p["false_promote_rate"]))
    return min(frontier, key=lambda p: abs(p["false_promote_rate"] - fpr_cap)) if frontier else None


def write_fixture(cases: list[LabeledCase], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps({
                "claim_id": case.claim_id, "label": case.label, "text": case.text,
                "subject": case.subject, "predicate": case.predicate,
                "object_value": case.object_value,
                "citation_count": case.citation_count,
                "prior_confidence": case.prior_confidence,
            }) + "\n")


def load_fixture(path: Path) -> list[LabeledCase]:
    cases: list[LabeledCase] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            cases.append(LabeledCase(
                claim_id=d["claim_id"], label=d["label"], text=d["text"],
                subject=d["subject"], predicate=d["predicate"],
                object_value=d["object_value"],
                citation_count=int(d["citation_count"]),
                prior_confidence=float(d["prior_confidence"]),
            ))
    return cases


def sweep(cases: list[LabeledCase]) -> list[dict]:
    return [
        evaluate(cases, min_citations=mc, min_score=ms / 100.0)
        for mc in (0, 1, 2)
        for ms in range(40, 81)
    ]


def render_report(*, baseline, frontier, recommended, n_pos, n_neg, fpr_cap):
    lines = [
        "# Steward promotion Pareto sweep — 2026-04-22",
        "",
        f"Fixture: {n_pos} positives, {n_neg} negatives",
        f"FPR cap: {fpr_cap:.2%}",
        "",
        "## Baseline (current defaults: min_citations=1, min_score=0.58)",
        "",
        f"- promote_rate: {baseline['promote_rate']:.2%} (TP={baseline['tp']}, FN={baseline['fn']})",
        f"- false_promote_rate: {baseline['false_promote_rate']:.2%} (FP={baseline['fp']}, TN={baseline['tn']})",
        "",
        "## Pareto frontier",
        "",
        "| min_citations | min_score | promote_rate | false_promote_rate | TP | FP | FN | TN |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in frontier:
        lines.append(
            f"| {row['min_citations']} | {row['min_score']:.2f} | "
            f"{row['promote_rate']:.2%} | {row['false_promote_rate']:.2%} | "
            f"{row['tp']} | {row['fp']} | {row['fn']} | {row['tn']} |"
        )
    lines += ["", "## Recommendation", ""]
    if recommended is None:
        lines.append("_No points found._")
    else:
        under = recommended["false_promote_rate"] <= fpr_cap
        tag = "within cap" if under else "CLOSEST TO CAP (exceeds — accept with caution)"
        lines += [
            f"- min_citations = **{recommended['min_citations']}**",
            f"- min_score    = **{recommended['min_score']:.2f}**",
            f"- promote_rate = **{recommended['promote_rate']:.2%}**",
            f"- false_promote_rate = **{recommended['false_promote_rate']:.2%}** ({tag})",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("memorymaster.db"))
    ap.add_argument("--out-fixture", type=Path, default=Path("tests/fixtures/steward_eval.jsonl"))
    ap.add_argument("--out-report", type=Path, default=Path("artifacts/steward-pareto-2026-04-22.md"))
    ap.add_argument("--n-pos", type=int, default=100)
    ap.add_argument("--n-neg", type=int, default=100)
    ap.add_argument("--fpr-cap", type=float, default=0.05)
    ap.add_argument("--skip-rebuild", action="store_true",
                    help="Reuse existing fixture file instead of re-querying the DB.")
    args = ap.parse_args()

    if args.skip_rebuild and args.out_fixture.exists():
        cases = load_fixture(args.out_fixture)
    else:
        cases = build_fixture(args.db, args.n_pos, args.n_neg)
        write_fixture(cases, args.out_fixture)

    n_pos = sum(1 for c in cases if c.label == "positive")
    n_neg = sum(1 for c in cases if c.label == "negative")
    print(f"[fixture] positives={n_pos} negatives={n_neg}")

    baseline = evaluate(cases, min_citations=1, min_score=0.58)
    print(f"[baseline] {baseline}")

    frontier = pareto_frontier(sweep(cases))
    recommended = pick_recommended(frontier, args.fpr_cap)
    print(f"[recommended] {recommended}")

    args.out_report.parent.mkdir(parents=True, exist_ok=True)
    args.out_report.write_text(
        render_report(baseline=baseline, frontier=frontier, recommended=recommended,
                      n_pos=n_pos, n_neg=n_neg, fpr_cap=args.fpr_cap),
        encoding="utf-8",
    )
    print(f"[report] wrote {args.out_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
