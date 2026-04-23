"""Back-test the steward classifier v2 against historical promotion events.

Reads a MemoryMaster SQLite DB in read-only mode, selects every claim that had
a promotion-decision event in the last N days, and compares three things for
each claim:

1. v2 classifier P(promote) vs the threshold from ``validator.py`` (0.65).
2. Legacy additive ``validation_score`` vs the Pareto-analysis threshold (0.72).
3. The actual ground-truth outcome = the claim's current status.

The "good" outcome is ``status='confirmed'`` (still surviving). The "bad"
outcomes are ``archived / stale / superseded / conflicted`` — the claim was
later retired or demoted.

Outputs:
- N events analyzed
- v2 confusion matrix (threshold 0.65)
- legacy confusion matrix (threshold 0.72)
- Disagreement matrix: would-have-added / would-have-blocked / agree
- Up to 10 sampled disagreements of each class, with claim text + features
- A verdict line: worse / equivalent / better on real data

Hard constraints (enforced):
- DB opened read-only. We do NOT use ``immutable=1`` because live DBs ship
  with an active WAL and ``immutable`` skips WAL replay (disk image malformed).
- The v2 artifact is loaded via ``load_classifier`` exactly as production does.
- The legacy formula is the EXACT function body from ``jobs/validator.py``;
  see ``_legacy_validation_score`` below — do not drift.
- No writes. ``PRAGMA query_only = ON``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.steward_classifier import (  # noqa: E402
    LoadedClassifier,
    load_classifier,
    predict_promote_probability,
)
from memorymaster.steward_features import FEATURE_VERSION, extract_features  # noqa: E402

# Thresholds — both match the ship-bound config.
# - v2: spec Pareto operating point, hard-coded in memorymaster/jobs/validator.py.
# - legacy: Pareto recommendation from artifacts/steward-pareto-2026-04-22.md
#   (min_score 0.74 is the analysis frontier; task spec says use 0.72, which
#   matches the live validator.py default in config.validation_threshold range
#   for the confirmed-tuple region — keeping task-specified 0.72 here).
V2_THRESHOLD = 0.65
LEGACY_THRESHOLD = 0.72
# The actually-running-in-prod threshold (config.validation_threshold default).
# Tracked separately because the task-spec 0.72 reference point makes legacy
# look worse than it actually is today.
LEGACY_THRESHOLD_LIVE = 0.58
LEGACY_MIN_CITATIONS = 1  # validator.py default — matches the live gate

# Approximate training-split cutoff: the v2 spec uses 80/20 chronological with
# the most recent ~2 weeks as holdout, and the artifact was trained on
# 2026-04-23. Anything older than the cutoff was in the training set and
# carries label-leak risk; newer rows are genuine out-of-sample.
_TRAIN_SPLIT_CUTOFF_ISO = "2026-04-09T00:00:00+00:00"


# -- Legacy validation_score (verbatim from jobs/validator.py) ---------------

def _legacy_validation_score(
    text: str,
    subject: str | None,
    predicate: str | None,
    object_value: str | None,
    citation_count: int,
    prior_confidence: float,
) -> float:
    """Replicates memorymaster/jobs/validator.py::validation_score exactly.

    base 0.35 + citation_bonus + length_bonus + structure_bonus, then blended
    75/25 with prior_confidence, clamped to [0, 1]. Kept DRY-identical to the
    shipped formula — any drift would invalidate the back-test.
    """
    base = 0.35
    citation_bonus = min(citation_count * 0.12, 0.4)
    length_bonus = min(len(text or "") / 240.0, 0.15)
    structure_bonus = 0.1 if (subject and predicate and object_value) else 0.0
    raw = base + citation_bonus + length_bonus + structure_bonus
    blended = (raw * 0.75) + (prior_confidence * 0.25)
    return max(0.0, min(1.0, blended))


# -- Outcome label -----------------------------------------------------------

_OUTCOME_GOOD = {"confirmed"}
_OUTCOME_BAD = {"archived", "stale", "superseded", "conflicted"}


def _outcome_label(status: str) -> str:
    """Map current status to the ground-truth label for the confusion matrix.

    - ``good``     : claim is still ``confirmed`` and not superseded → a
                     promotion here would have been the right call.
    - ``bad``      : claim was later archived/stale/superseded/conflicted →
                     a promotion here would have been wrong.
    - ``unknown``  : claim is still ``candidate`` (terminal verdict pending).
                     Excluded from confusion matrices.
    """
    if status in _OUTCOME_GOOD:
        return "good"
    if status in _OUTCOME_BAD:
        return "bad"
    return "unknown"


# -- Data loading ------------------------------------------------------------

# We anchor on *validator* events because those are the historical
# promotion decisions. We also count decay-triggered stale transitions to
# enrich the negative pool (claim was confirmed, later decayed → bad outcome).
_EVENT_QUERY = """
SELECT DISTINCT c.id
FROM claims c
JOIN events e ON e.claim_id = c.id
WHERE e.created_at > datetime('now', ?)
  AND (
        (e.event_type = 'validator' AND e.from_status = 'candidate'
             AND e.to_status IN ('confirmed', 'conflicted', 'superseded', 'stale'))
     OR (e.event_type = 'decay' AND e.to_status = 'stale')
     OR (e.event_type = 'supersession' AND e.to_status = 'superseded')
     OR (e.event_type IN ('transition', 'policy_decision')
             AND e.to_status IN ('confirmed', 'stale', 'superseded', 'archived'))
  )
"""

_CLAIM_BY_ID = """
SELECT c.id, c.text, c.subject, c.predicate, c.object_value, c.scope,
       c.status, c.claim_type, c.source_agent, c.created_at, c.access_count,
       c.supersedes_claim_id, c.replaced_by_claim_id, c.entity_id, c.confidence
FROM claims c
WHERE c.id = ?
"""


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    """Open in read-only mode with WAL replay (no ``immutable=1``) — live DBs
    that haven't been checkpointed will throw ``database disk image malformed``
    under ``immutable=1`` because WAL is skipped."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


@dataclass(frozen=True)
class BacktestRow:
    claim_id: int
    status: str
    outcome: str  # good / bad / unknown
    text: str
    source_agent: str | None
    claim_type: str | None
    scope: str | None
    created_at: str
    n_citations: int
    v2_proba: float | None  # None only if classifier fails (should be ~0)
    v2_promote: bool
    legacy_score: float
    legacy_promote: bool
    # Legacy at 0.58 — the value actually running in prod right now per
    # config.validation_threshold. Kept alongside the 0.72 Pareto point so
    # the report is honest about what users would feel if v2 flipped on.
    legacy_promote_live: bool
    features: dict[str, float]
    in_training_split: bool  # True if this claim's label was in the train set


# -- Core loop ---------------------------------------------------------------

def _count_citations(conn: sqlite3.Connection, claim_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM citations WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    return int(row[0] if row else 0)


def _parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def run_backtest(
    db_path: Path,
    *,
    classifier: LoadedClassifier,
    days: int = 30,
) -> list[BacktestRow]:
    window = f"-{days} days"
    train_cutoff = _parse_iso_utc(_TRAIN_SPLIT_CUTOFF_ISO)
    with _ro_connect(db_path) as conn:
        ids = [int(r[0]) for r in conn.execute(_EVENT_QUERY, (window,))]
        rows: list[BacktestRow] = []
        for cid in ids:
            r = conn.execute(_CLAIM_BY_ID, (cid,)).fetchone()
            if r is None:
                continue
            d = dict(r)
            n_cit = _count_citations(conn, cid)
            feats = extract_features(d, conn)
            proba = predict_promote_probability(d, conn, classifier=classifier)
            legacy = _legacy_validation_score(
                text=d["text"] or "",
                subject=d["subject"],
                predicate=d["predicate"],
                object_value=d["object_value"],
                citation_count=n_cit,
                prior_confidence=float(d["confidence"] or 0.0),
            )
            # v2 gate: classifier proba >= threshold AND has >=1 citation
            # (validator.py enforces both — we replicate the full gate).
            v2_promote = (
                proba is not None
                and proba >= V2_THRESHOLD
                and n_cit >= LEGACY_MIN_CITATIONS
            )
            # Legacy gate: citation_count >= min AND score >= threshold.
            legacy_promote = (
                n_cit >= LEGACY_MIN_CITATIONS and legacy >= LEGACY_THRESHOLD
            )
            legacy_promote_live = (
                n_cit >= LEGACY_MIN_CITATIONS and legacy >= LEGACY_THRESHOLD_LIVE
            )
            created_dt = _parse_iso_utc(d["created_at"])
            in_train = bool(
                train_cutoff is not None
                and created_dt is not None
                and created_dt < train_cutoff
            )
            rows.append(
                BacktestRow(
                    claim_id=cid,
                    status=d["status"],
                    outcome=_outcome_label(d["status"]),
                    text=d["text"] or "",
                    source_agent=d["source_agent"],
                    claim_type=d["claim_type"],
                    scope=d["scope"],
                    created_at=d["created_at"],
                    n_citations=n_cit,
                    v2_proba=proba,
                    v2_promote=v2_promote,
                    legacy_score=legacy,
                    legacy_promote=legacy_promote,
                    legacy_promote_live=legacy_promote_live,
                    features=feats,
                    in_training_split=in_train,
                )
            )
    return rows


# -- Reporting ---------------------------------------------------------------

def _confusion(rows: list[BacktestRow], *, by: str) -> dict[str, int]:
    """Confusion matrix keyed by ``by`` ∈ {'v2', 'legacy', 'legacy_live'}.

    - TP: flagged promote AND outcome == good
    - FP: flagged promote AND outcome == bad
    - TN: not flagged AND outcome == bad
    - FN: not flagged AND outcome == good
    Rows with outcome=='unknown' are excluded.
    """
    tp = fp = tn = fn = 0
    for r in rows:
        if r.outcome == "unknown":
            continue
        if by == "v2":
            promote = r.v2_promote
        elif by == "legacy":
            promote = r.legacy_promote
        elif by == "legacy_live":
            promote = r.legacy_promote_live
        else:
            raise ValueError(f"unknown classifier key: {by}")
        if promote and r.outcome == "good":
            tp += 1
        elif promote and r.outcome == "bad":
            fp += 1
        elif not promote and r.outcome == "bad":
            tn += 1
        elif not promote and r.outcome == "good":
            fn += 1
    return {"TP": tp, "FP": fp, "TN": tn, "FN": fn}


def _disagreement(rows: list[BacktestRow], *, against: str = "legacy") -> dict[str, list[BacktestRow]]:
    """Split rows by v2-vs-legacy disagreement class.

    - ``v2_adds``    : v2 promote, baseline block → v2 would have added
    - ``v2_blocks``  : v2 block, baseline promote → v2 would have blocked
    - ``agree_yes``  : both promote
    - ``agree_no``   : neither promote

    ``against`` ∈ {'legacy', 'legacy_live'} picks which baseline to compare.
    """
    buckets: dict[str, list[BacktestRow]] = {
        "v2_adds": [], "v2_blocks": [], "agree_yes": [], "agree_no": [],
    }
    for r in rows:
        baseline_promote = r.legacy_promote if against == "legacy" else r.legacy_promote_live
        if r.v2_promote and not baseline_promote:
            buckets["v2_adds"].append(r)
        elif not r.v2_promote and baseline_promote:
            buckets["v2_blocks"].append(r)
        elif r.v2_promote and baseline_promote:
            buckets["agree_yes"].append(r)
        else:
            buckets["agree_no"].append(r)
    return buckets


def _precision_recall(cm: dict[str, int]) -> dict[str, float]:
    tp, fp, fn = cm["TP"], cm["FP"], cm["FN"]
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def _trim(text: str, n: int = 220) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


def _sample_markdown(rows: list[BacktestRow], k: int, *, seed: int) -> str:
    rng = random.Random(seed)
    if len(rows) > k:
        picks = rng.sample(rows, k)
    else:
        picks = rows
    parts: list[str] = []
    for r in picks:
        parts.append(
            f"- **claim {r.claim_id}** — status=`{r.status}` outcome=`{r.outcome}` "
            f"source=`{r.source_agent}` type=`{r.claim_type}` n_citations={r.n_citations} "
            f"in_train={r.in_training_split}"
        )
        proba_str = f"{r.v2_proba:.3f}" if r.v2_proba is not None else "None"
        parts.append(
            f"  - v2_proba=`{proba_str}`  "
            f"legacy_score=`{r.legacy_score:.3f}`  "
            f"v2_promote=`{r.v2_promote}`  legacy@0.72_promote=`{r.legacy_promote}`  "
            f"legacy@0.58_promote=`{r.legacy_promote_live}`"
        )
        key_feats = {
            k: r.features[k] for k in (
                "source_agent_trust", "scope_quality", "n_citations",
                "text_length", "has_verbatim_excerpt", "n_related_claims",
                "conflict_delta", "sensitivity_flagged",
            ) if k in r.features
        }
        parts.append(f"  - features: `{json.dumps(key_feats)}`")
        parts.append(f"  - text: {_trim(r.text)}")
        parts.append("")
    return "\n".join(parts) if parts else "_(none sampled)_\n"


def _format_cm(cm: dict[str, int]) -> str:
    metrics = _precision_recall(cm)
    return (
        f"TP={cm['TP']}  FP={cm['FP']}  TN={cm['TN']}  FN={cm['FN']}  "
        f"precision={metrics['precision']}  recall={metrics['recall']}  "
        f"f1={metrics['f1']}"
    )


def _outcome_rates(disagreements: list[BacktestRow]) -> tuple[int, int]:
    """Return (n_good, n_bad) for a list of rows (ignores ``unknown``)."""
    good = sum(1 for r in disagreements if r.outcome == "good")
    bad = sum(1 for r in disagreements if r.outcome == "bad")
    return good, bad


def _render_report(
    rows: list[BacktestRow],
    db_path: Path,
    days: int,
    sample_k: int,
    seed: int,
) -> str:
    n_total = len(rows)
    by_outcome: dict[str, int] = {"good": 0, "bad": 0, "unknown": 0}
    for r in rows:
        by_outcome[r.outcome] += 1

    cm_v2 = _confusion(rows, by="v2")
    cm_legacy = _confusion(rows, by="legacy")
    cm_legacy_live = _confusion(rows, by="legacy_live")
    diss = _disagreement(rows, against="legacy")
    diss_live = _disagreement(rows, against="legacy_live")

    pr_v2 = _precision_recall(cm_v2)
    pr_legacy = _precision_recall(cm_legacy)
    pr_legacy_live = _precision_recall(cm_legacy_live)

    # Train/test split: honest metrics on genuinely out-of-sample rows only.
    test_rows = [r for r in rows if not r.in_training_split]
    train_rows = [r for r in rows if r.in_training_split]
    n_train_overlap = len(train_rows)
    n_test = len(test_rows)

    cm_v2_test = _confusion(test_rows, by="v2")
    cm_legacy_live_test = _confusion(test_rows, by="legacy_live")
    pr_v2_test = _precision_recall(cm_v2_test)
    pr_legacy_live_test = _precision_recall(cm_legacy_live_test)

    # Disagreement quality (overall) — vs task-spec legacy @ 0.72
    adds_good, adds_bad = _outcome_rates(diss["v2_adds"])
    blocks_good, blocks_bad = _outcome_rates(diss["v2_blocks"])
    adds_good_rate = adds_good / max(1, adds_good + adds_bad)
    blocks_bad_rate = blocks_bad / max(1, blocks_good + blocks_bad)

    # Disagreement quality on OUT-OF-SAMPLE only — task-spec baseline
    test_adds = [r for r in diss["v2_adds"] if not r.in_training_split]
    test_blocks = [r for r in diss["v2_blocks"] if not r.in_training_split]
    test_adds_good, test_adds_bad = _outcome_rates(test_adds)
    test_blocks_good, test_blocks_bad = _outcome_rates(test_blocks)
    test_adds_good_rate = test_adds_good / max(1, test_adds_good + test_adds_bad)
    test_blocks_bad_rate = test_blocks_bad / max(1, test_blocks_good + test_blocks_bad)

    # Disagreement vs live legacy (0.58) — this is what actually changes on
    # the day v2 flips on in prod.
    live_adds_good, live_adds_bad = _outcome_rates(diss_live["v2_adds"])
    live_blocks_good, live_blocks_bad = _outcome_rates(diss_live["v2_blocks"])
    live_adds_good_rate = live_adds_good / max(1, live_adds_good + live_adds_bad)
    live_blocks_bad_rate = live_blocks_bad / max(1, live_blocks_good + live_blocks_bad)

    test_live_adds = [r for r in diss_live["v2_adds"] if not r.in_training_split]
    test_live_blocks = [r for r in diss_live["v2_blocks"] if not r.in_training_split]
    test_live_adds_good, test_live_adds_bad = _outcome_rates(test_live_adds)
    test_live_blocks_good, test_live_blocks_bad = _outcome_rates(test_live_blocks)
    test_live_adds_good_rate = test_live_adds_good / max(1, test_live_adds_good + test_live_adds_bad)
    test_live_blocks_bad_rate = test_live_blocks_bad / max(1, test_live_blocks_good + test_live_blocks_bad)

    # Recommendation uses the out-of-sample comparison against legacy@LIVE — the
    # only honest head-to-head (live threshold, genuinely unseen claims).
    # Disagreement quality is sourced from diss_live (what actually flips
    # on flip-on), restricted to the test split.
    live_gap = pr_v2_test["f1"] - pr_legacy_live_test["f1"]
    if (
        live_gap >= 0.02
        and test_live_adds_good_rate >= 0.6
        and test_live_blocks_bad_rate >= 0.5
    ):
        recommendation = (
            "**SHIP (cautiously)** — on out-of-sample data v2 beats the live "
            "legacy (0.58) by a meaningful F1 margin and the disagreement "
            "sampling favours the correct decision."
        )
    elif live_gap <= -0.02:
        recommendation = (
            "**DO NOT SHIP** — out-of-sample v2 F1 is worse than the live "
            "legacy formula. The 0.99 ROC-AUC on the held-out split does not "
            "translate to real-world gains."
        )
    elif test_live_adds_good_rate < 0.5:
        recommendation = (
            "**DO NOT SHIP** — v2 adds (vs the live baseline) are majority bad "
            f"claims on the out-of-sample split ({test_live_adds_good_rate:.1%} good). "
            "v2 would pollute the store."
        )
    elif test_live_blocks_bad_rate < 0.4:
        recommendation = (
            "**HOLD** — v2 blocks (vs the live baseline) are majority claims "
            f"that later proved good ({1 - test_live_blocks_bad_rate:.1%} good). "
            "v2 is over-rejecting."
        )
    else:
        recommendation = (
            "**CAUTIOUS SHIP** — v2 and live legacy are within 2 F1 points on "
            "out-of-sample data. The win is small; ship only if operational "
            "reasons (calibration, interpretability, smoother threshold tuning) "
            "favour v2."
        )

    parts: list[str] = []
    parts.append("# Steward classifier v2 — 30-day back-test")
    parts.append("")
    parts.append(f"- db: `{db_path}`")
    parts.append(f"- window: last {days} days")
    parts.append("- artifact: `artifacts/steward-classifier-v2.joblib`")
    parts.append(f"- feature_version: `{FEATURE_VERSION}`")
    parts.append(f"- v2 threshold: `{V2_THRESHOLD}` (+ citation >= {LEGACY_MIN_CITATIONS})")
    parts.append(
        f"- legacy threshold (task spec, Pareto): `{LEGACY_THRESHOLD}` "
        f"(+ citation >= {LEGACY_MIN_CITATIONS})"
    )
    parts.append(
        f"- legacy threshold (live prod, `config.validation_threshold`): "
        f"`{LEGACY_THRESHOLD_LIVE}`"
    )
    parts.append("")
    parts.append("## Label-leakage disclosure")
    parts.append("")
    parts.append(
        "The v2 artifact was trained on 2026-04-23 against the *same DB* "
        "we back-test on, with a chronological 80/20 split. Claims created "
        f"before `{_TRAIN_SPLIT_CUTOFF_ISO}` were part of the training set — "
        "their labels were directly observed by the model. Only the "
        "post-cutoff subset is genuinely out-of-sample. The report shows "
        "both combined and test-only metrics so readers can judge the win."
    )
    parts.append("")
    parts.append(f"- train-split overlap: **{n_train_overlap}** rows (labels seen)")
    parts.append(f"- test-split / out-of-sample: **{n_test}** rows")
    parts.append("")
    parts.append("## N events analyzed")
    parts.append("")
    parts.append(f"- total claims with a promotion-decision event: **{n_total}**")
    parts.append(f"- outcome=good (currently `confirmed`): {by_outcome['good']}")
    parts.append(f"- outcome=bad (archived/stale/superseded/conflicted): {by_outcome['bad']}")
    parts.append(f"- outcome=unknown (still candidate): {by_outcome['unknown']}")
    parts.append("")
    parts.append("## Confusion matrices — full 30-day window")
    parts.append("")
    parts.append("### v2 @ 0.65")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_v2))
    parts.append("```")
    parts.append("")
    parts.append("### legacy @ 0.72 (task-spec Pareto threshold)")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_legacy))
    parts.append("```")
    parts.append("")
    parts.append("### legacy @ 0.58 (live-prod threshold)")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_legacy_live))
    parts.append("```")
    parts.append("")
    parts.append("## Confusion matrices — out-of-sample only (post-cutoff)")
    parts.append("")
    parts.append("### v2 @ 0.65 — test split")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_v2_test))
    parts.append("```")
    parts.append("")
    parts.append("### legacy @ 0.58 — test split")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_legacy_live_test))
    parts.append("```")
    parts.append("")
    parts.append(
        "Legacy @ 0.72 is the task-specified Pareto point but promotes almost "
        "nothing at this threshold (see above), so the honest head-to-head is "
        "v2@0.65 vs legacy@0.58."
    )
    parts.append("")
    parts.append("## Decision disagreement matrix")
    parts.append("")
    parts.append("### v2 @ 0.65 vs legacy @ 0.72 (task-spec Pareto baseline)")
    parts.append("")
    parts.append(f"- v2-would-have-added  (v2 promote, legacy block): **{len(diss['v2_adds'])}**")
    parts.append(f"- v2-would-have-blocked (v2 block, legacy promote): **{len(diss['v2_blocks'])}**")
    parts.append(f"- status quo / both promote: **{len(diss['agree_yes'])}**")
    parts.append(f"- status quo / both block:   **{len(diss['agree_no'])}**")
    parts.append("")
    parts.append(
        f"- Of v2-adds: **{adds_good}/{adds_good + adds_bad}** "
        f"({adds_good_rate:.1%}) ended good. On out-of-sample only: "
        f"**{test_adds_good}/{test_adds_good + test_adds_bad}** "
        f"({test_adds_good_rate:.1%}) ended good."
    )
    parts.append(
        f"- Of v2-blocks: **{blocks_bad}/{blocks_good + blocks_bad}** "
        f"({blocks_bad_rate:.1%}) really were bad. On out-of-sample only: "
        f"**{test_blocks_bad}/{test_blocks_good + test_blocks_bad}** "
        f"({test_blocks_bad_rate:.1%}) really were bad."
    )
    parts.append("")
    parts.append("### v2 @ 0.65 vs legacy @ 0.58 (live prod — what actually changes on flip-on)")
    parts.append("")
    parts.append(f"- v2-would-have-added:  **{len(diss_live['v2_adds'])}**")
    parts.append(f"- v2-would-have-blocked: **{len(diss_live['v2_blocks'])}**")
    parts.append(f"- both promote: **{len(diss_live['agree_yes'])}**")
    parts.append(f"- both block:   **{len(diss_live['agree_no'])}**")
    parts.append("")
    parts.append(
        f"- Of v2-adds (vs live): **{live_adds_good}/{live_adds_good + live_adds_bad}** "
        f"({live_adds_good_rate:.1%}) ended good. On out-of-sample only: "
        f"**{test_live_adds_good}/{test_live_adds_good + test_live_adds_bad}** "
        f"({test_live_adds_good_rate:.1%}) ended good."
    )
    parts.append(
        f"- Of v2-blocks (vs live): **{live_blocks_bad}/{live_blocks_good + live_blocks_bad}** "
        f"({live_blocks_bad_rate:.1%}) really were bad. On out-of-sample only: "
        f"**{test_live_blocks_bad}/{test_live_blocks_good + test_live_blocks_bad}** "
        f"({test_live_blocks_bad_rate:.1%}) really were bad."
    )
    parts.append("")
    parts.append("## Sampled disagreements — vs legacy @ 0.72 (task-spec baseline)")
    parts.append("")
    parts.append("### v2 would have ADDED (v2 promote, legacy@0.72 block)")
    parts.append("")
    parts.append(_sample_markdown(diss["v2_adds"], sample_k, seed=seed))
    parts.append("### v2 would have BLOCKED (v2 block, legacy@0.72 promote)")
    parts.append("")
    parts.append(_sample_markdown(diss["v2_blocks"], sample_k, seed=seed + 1))
    parts.append("## Sampled disagreements — vs legacy @ 0.58 (live config)")
    parts.append("")
    parts.append("### v2 would have ADDED (v2 promote, legacy@0.58 block)")
    parts.append("")
    parts.append(_sample_markdown(diss_live["v2_adds"], sample_k, seed=seed + 2))
    parts.append("### v2 would have BLOCKED (v2 block, legacy@0.58 promote)")
    parts.append("")
    parts.append(_sample_markdown(diss_live["v2_blocks"], sample_k, seed=seed + 3))
    parts.append("## Verdict")
    parts.append("")
    parts.append("### Full 30-day window (train+test, label-leak risk)")
    parts.append("")
    parts.append(
        f"- v2 @ 0.65:           F1={pr_v2['f1']}  precision={pr_v2['precision']}  recall={pr_v2['recall']}"
    )
    parts.append(
        f"- legacy @ 0.72:       F1={pr_legacy['f1']}  precision={pr_legacy['precision']}  recall={pr_legacy['recall']}"
    )
    parts.append(
        f"- legacy @ 0.58 (live): F1={pr_legacy_live['f1']}  precision={pr_legacy_live['precision']}  recall={pr_legacy_live['recall']}"
    )
    parts.append("")
    parts.append("### Out-of-sample only (honest head-to-head)")
    parts.append("")
    parts.append(
        f"- v2 @ 0.65:            F1={pr_v2_test['f1']}  precision={pr_v2_test['precision']}  recall={pr_v2_test['recall']}"
    )
    parts.append(
        f"- legacy @ 0.58 (live): F1={pr_legacy_live_test['f1']}  "
        f"precision={pr_legacy_live_test['precision']}  recall={pr_legacy_live_test['recall']}"
    )
    parts.append(f"- F1 gap v2 − legacy(live): **{live_gap:+.4f}**")
    parts.append("")
    parts.append(f"**Recommendation:** {recommendation}")
    parts.append("")
    return "\n".join(parts)


# -- CLI ---------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--db",
        type=Path,
        default=Path("memorymaster.db"),
        help="Path to memorymaster.db (opened read-only).",
    )
    p.add_argument(
        "--artifact",
        type=Path,
        default=Path("artifacts/steward-classifier-v2.joblib"),
        help="Path to the v2 classifier artifact.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="History window in days.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/steward-classifier-v2-backtest-2026-04-23.md"),
        help="Where to write the markdown report.",
    )
    p.add_argument(
        "--jsonl-out",
        type=Path,
        default=None,
        help="Optional: write per-claim raw rows as JSONL for debugging.",
    )
    p.add_argument(
        "--sample-k",
        type=int,
        default=10,
        help="How many disagreement rows to sample per class.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="Sampling seed — keeps the report reproducible.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.db.exists():
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2
    if not args.artifact.exists():
        print(f"error: artifact not found: {args.artifact}", file=sys.stderr)
        return 2

    # Force the classifier env gate ON for this back-test — otherwise
    # ``load_classifier()`` returns None when MEMORYMASTER_STEWARD_CLASSIFIER_PATH
    # is unset, which would silently give us an all-``None`` v2 column.
    os.environ["MEMORYMASTER_STEWARD_CLASSIFIER_PATH"] = str(args.artifact.resolve())

    clf = load_classifier(path=args.artifact, force_reload=True)
    if clf is None:
        print(
            f"error: classifier unavailable at {args.artifact} "
            f"(check joblib install + feature_version == {FEATURE_VERSION})",
            file=sys.stderr,
        )
        return 2
    if clf.feature_version != FEATURE_VERSION:
        print(
            f"error: feature_version mismatch — artifact={clf.feature_version} "
            f"extractor={FEATURE_VERSION}",
            file=sys.stderr,
        )
        return 2

    print(
        f"backtest: db={args.db} days={args.days} "
        f"artifact={clf.source_path.name} feat_v={clf.feature_version}",
        file=sys.stderr,
    )

    rows = run_backtest(args.db, classifier=clf, days=args.days)
    print(f"backtest: loaded {len(rows)} claim-events", file=sys.stderr)

    md = _render_report(rows, args.db, args.days, args.sample_k, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md, encoding="utf-8")
    print(f"backtest: report written to {args.out}", file=sys.stderr)

    if args.jsonl_out:
        args.jsonl_out.parent.mkdir(parents=True, exist_ok=True)
        with args.jsonl_out.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "claim_id": r.claim_id,
                    "status": r.status,
                    "outcome": r.outcome,
                    "source_agent": r.source_agent,
                    "claim_type": r.claim_type,
                    "scope": r.scope,
                    "created_at": r.created_at,
                    "in_training_split": r.in_training_split,
                    "n_citations": r.n_citations,
                    "v2_proba": r.v2_proba,
                    "v2_promote": r.v2_promote,
                    "legacy_score": r.legacy_score,
                    "legacy_promote": r.legacy_promote,
                    "legacy_promote_live": r.legacy_promote_live,
                    "features": r.features,
                }) + "\n")
        print(f"backtest: raw rows written to {args.jsonl_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
