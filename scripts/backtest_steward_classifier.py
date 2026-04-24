"""Back-test the steward classifier (v2 and/or v3) against historical promotion events.

Reads a MemoryMaster SQLite DB in read-only mode, selects every claim that had
a promotion-decision event in the last N days, and for each version given via
``--versions`` (default ``v2,v3``) computes:

1. P(promote) from the version's classifier vs the shipped threshold (0.65).
2. The legacy additive ``validation_score`` vs the Pareto-analysis threshold
   (0.72) and the live-prod threshold (0.58).
3. The actual ground-truth outcome = the claim's current status.

The "good" outcome is ``status='confirmed'`` (still surviving). The "bad"
outcomes are ``archived / stale / superseded / conflicted``.

Outputs a single markdown report per run containing, PER VERSION:

- N events analyzed
- confusion matrix (threshold 0.65)
- legacy confusion matrices (task-spec 0.72 and live-prod 0.58)
- Disagreement matrices: version-vs-legacy AND v2-vs-v3
- Up to ``--sample-k`` disagreement samples of each class (min 10 for
  v2-vs-v3 disagreements — acceptance requirement)
- Verdict / recommendation line

Hard constraints (enforced):
- DB opened read-only. We do NOT use ``immutable=1`` because live DBs ship
  with an active WAL and ``immutable`` skips WAL replay ("disk image malformed").
- Each classifier artifact is loaded via raw ``joblib.load`` (bypassing the
  strict ``load_classifier`` version-check) because we intentionally run BOTH
  v2 and v3 in the same process even though only one extractor version is
  active at any given time.
- The legacy formula is the EXACT function body from ``jobs/validator.py``;
  see ``_legacy_validation_score`` below — do not drift.
- No writes. ``PRAGMA query_only = ON``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.steward_features import FEATURE_VERSION, extract_features  # noqa: E402
from memorymaster.wiki_similarity import load_wiki_corpus  # noqa: E402

_LOG = logging.getLogger(__name__)

# Thresholds — match the ship-bound config.
CLASSIFIER_THRESHOLD = 0.65
LEGACY_THRESHOLD = 0.72
LEGACY_THRESHOLD_LIVE = 0.58
LEGACY_MIN_CITATIONS = 1  # validator.py default — matches the live gate

# Approximate training-split cutoff: v2 trained 2026-04-23 with 80/20
# chronological; anything older than this was in the training set and
# carries label-leak risk; newer rows are genuine out-of-sample.
# v3 trained 2026-04-24 on the same fixture — cutoff stays the same.
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
    """Replicates memorymaster/jobs/validator.py::validation_score exactly."""
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
    if status in _OUTCOME_GOOD:
        return "good"
    if status in _OUTCOME_BAD:
        return "bad"
    return "unknown"


# -- Data loading ------------------------------------------------------------

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
       c.supersedes_claim_id, c.replaced_by_claim_id, c.entity_id,
       c.confidence, c.wiki_article
FROM claims c
WHERE c.id = ?
"""


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


# -- Classifier loading (version-agnostic) -----------------------------------


@dataclass(frozen=True)
class LoadedArtifact:
    version: str               # "v2" | "v3"
    path: Path
    feature_version: str       # as stored in the joblib
    feature_keys: tuple[str, ...]
    model: Any
    calibration: str           # "isotonic" | "sigmoid" | ""


def _load_artifact(version: str, path: Path) -> LoadedArtifact:
    """Load a classifier joblib without enforcing the extractor's version —
    we intentionally compare v2 and v3 side-by-side. Feature vectors for each
    artifact are built using that artifact's ``feature_keys`` so missing
    columns (e.g., v2 missing wiki_similarity_cosine) default to 0.0."""
    import joblib

    payload = joblib.load(path)
    feat_keys = tuple(payload.get("feature_keys", ()))
    if not feat_keys:
        raise ValueError(f"artifact {path} is missing feature_keys")
    return LoadedArtifact(
        version=version,
        path=path,
        feature_version=str(payload.get("feature_version", "")),
        feature_keys=feat_keys,
        model=payload["model"],
        calibration=str(payload.get("calibration", "")),
    )


def _predict(artifact: LoadedArtifact, features: dict[str, float]) -> float:
    """Score ``features`` with ``artifact``'s model, using ONLY the keys the
    artifact was trained on. Missing keys default to 0.0 so v2 artifacts can
    score against v3 feature dicts (they simply ignore wiki_similarity_cosine).
    """
    vec = np.asarray(
        [[float(features.get(k, 0.0)) for k in artifact.feature_keys]],
        dtype=np.float64,
    )
    try:
        return float(artifact.model.predict_proba(vec)[0][1])
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("predict failed for %s: %s", artifact.version, exc)
        return 0.0


# -- Row types --------------------------------------------------------------


@dataclass(frozen=True)
class BacktestRow:
    claim_id: int
    status: str
    outcome: str
    text: str
    source_agent: str | None
    claim_type: str | None
    scope: str | None
    created_at: str
    n_citations: int
    # per-version classifier probabilities; key is version name.
    proba_by_version: dict[str, float]
    # per-version promote decision.
    promote_by_version: dict[str, bool]
    legacy_score: float
    legacy_promote: bool
    legacy_promote_live: bool
    features: dict[str, float]
    in_training_split: bool


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
    artifacts: list[LoadedArtifact],
    days: int = 30,
    wiki_scope: str = "project:memorymaster",
) -> list[BacktestRow]:
    """Load every claim with a promotion-decision event in the last ``days``,
    extract v3 features against the shared wiki corpus, and score with each
    artifact in ``artifacts``. Returns per-claim rows.
    """
    window = f"-{days} days"
    train_cutoff = _parse_iso_utc(_TRAIN_SPLIT_CUTOFF_ISO)
    corpus = load_wiki_corpus(scope=wiki_scope, repo_root=ROOT)
    with _ro_connect(db_path) as conn:
        ids = [int(r[0]) for r in conn.execute(_EVENT_QUERY, (window,))]
        rows: list[BacktestRow] = []
        for cid in ids:
            r = conn.execute(_CLAIM_BY_ID, (cid,)).fetchone()
            if r is None:
                continue
            d = dict(r)
            n_cit = _count_citations(conn, cid)
            feats = extract_features(d, conn, wiki_corpus=corpus)
            proba_by_version: dict[str, float] = {}
            promote_by_version: dict[str, bool] = {}
            for art in artifacts:
                p = _predict(art, feats)
                proba_by_version[art.version] = p
                promote_by_version[art.version] = (
                    p >= CLASSIFIER_THRESHOLD and n_cit >= LEGACY_MIN_CITATIONS
                )
            legacy = _legacy_validation_score(
                text=d["text"] or "",
                subject=d["subject"],
                predicate=d["predicate"],
                object_value=d["object_value"],
                citation_count=n_cit,
                prior_confidence=float(d["confidence"] or 0.0),
            )
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
            rows.append(BacktestRow(
                claim_id=cid,
                status=d["status"],
                outcome=_outcome_label(d["status"]),
                text=d["text"] or "",
                source_agent=d["source_agent"],
                claim_type=d["claim_type"],
                scope=d["scope"],
                created_at=d["created_at"],
                n_citations=n_cit,
                proba_by_version=proba_by_version,
                promote_by_version=promote_by_version,
                legacy_score=legacy,
                legacy_promote=legacy_promote,
                legacy_promote_live=legacy_promote_live,
                features=feats,
                in_training_split=in_train,
            ))
    return rows


# -- Reporting ---------------------------------------------------------------


def _confusion(rows: list[BacktestRow], *, key: str, version: str | None = None) -> dict[str, int]:
    """Confusion matrix keyed by decision source:
    - key == "classifier": use ``promote_by_version[version]``
    - key == "legacy"    : use legacy_promote (0.72)
    - key == "legacy_live": use legacy_promote_live (0.58)
    """
    tp = fp = tn = fn = 0
    for r in rows:
        if r.outcome == "unknown":
            continue
        if key == "classifier":
            assert version is not None
            promote = r.promote_by_version.get(version, False)
        elif key == "legacy":
            promote = r.legacy_promote
        elif key == "legacy_live":
            promote = r.legacy_promote_live
        else:
            raise ValueError(f"unknown key: {key}")
        if promote and r.outcome == "good":
            tp += 1
        elif promote and r.outcome == "bad":
            fp += 1
        elif not promote and r.outcome == "bad":
            tn += 1
        elif not promote and r.outcome == "good":
            fn += 1
    return {"TP": tp, "FP": fp, "TN": tn, "FN": fn}


def _precision_recall(cm: dict[str, int]) -> dict[str, float]:
    tp, fp, fn = cm["TP"], cm["FP"], cm["FN"]
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def _format_cm(cm: dict[str, int]) -> str:
    metrics = _precision_recall(cm)
    return (
        f"TP={cm['TP']}  FP={cm['FP']}  TN={cm['TN']}  FN={cm['FN']}  "
        f"precision={metrics['precision']}  recall={metrics['recall']}  "
        f"f1={metrics['f1']}"
    )


def _disagreement(
    rows: list[BacktestRow], *, a: str, b: str
) -> dict[str, list[BacktestRow]]:
    """Partition by (version ``a`` decision, version ``b`` decision)."""
    buckets: dict[str, list[BacktestRow]] = {
        f"{a}_only": [], f"{b}_only": [], "agree_yes": [], "agree_no": [],
    }
    for r in rows:
        a_prom = r.promote_by_version.get(a, False)
        b_prom = r.promote_by_version.get(b, False)
        if a_prom and not b_prom:
            buckets[f"{a}_only"].append(r)
        elif b_prom and not a_prom:
            buckets[f"{b}_only"].append(r)
        elif a_prom and b_prom:
            buckets["agree_yes"].append(r)
        else:
            buckets["agree_no"].append(r)
    return buckets


def _disagreement_vs_legacy(
    rows: list[BacktestRow], *, version: str, legacy_key: str
) -> dict[str, list[BacktestRow]]:
    buckets: dict[str, list[BacktestRow]] = {
        "version_adds": [], "version_blocks": [], "agree_yes": [], "agree_no": [],
    }
    for r in rows:
        v_prom = r.promote_by_version.get(version, False)
        l_prom = r.legacy_promote if legacy_key == "legacy" else r.legacy_promote_live
        if v_prom and not l_prom:
            buckets["version_adds"].append(r)
        elif l_prom and not v_prom:
            buckets["version_blocks"].append(r)
        elif v_prom and l_prom:
            buckets["agree_yes"].append(r)
        else:
            buckets["agree_no"].append(r)
    return buckets


def _outcome_rates(items: list[BacktestRow]) -> tuple[int, int]:
    good = sum(1 for r in items if r.outcome == "good")
    bad = sum(1 for r in items if r.outcome == "bad")
    return good, bad


def _trim(text: str, n: int = 220) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "..."


def _sample_markdown(
    rows: list[BacktestRow], k: int, *, seed: int, versions: list[str],
) -> str:
    rng = random.Random(seed)
    picks = rng.sample(rows, k) if len(rows) > k else list(rows)
    parts: list[str] = []
    for r in picks:
        parts.append(
            f"- **claim {r.claim_id}** - status=`{r.status}` outcome=`{r.outcome}` "
            f"source=`{r.source_agent}` type=`{r.claim_type}` n_citations={r.n_citations} "
            f"in_train={r.in_training_split}"
        )
        proba_pieces = []
        for v in versions:
            p = r.proba_by_version.get(v)
            proba_pieces.append(f"{v}_proba=`{p:.3f}`" if p is not None else f"{v}_proba=`None`")
            proba_pieces.append(f"{v}_promote=`{r.promote_by_version.get(v)}`")
        parts.append(
            f"  - {'  '.join(proba_pieces)}  legacy_score=`{r.legacy_score:.3f}`  "
            f"legacy@0.72_promote=`{r.legacy_promote}`  legacy@0.58_promote=`{r.legacy_promote_live}`"
        )
        key_feats_src = (
            "source_agent_trust", "scope_quality", "n_citations",
            "text_length", "has_verbatim_excerpt", "n_related_claims",
            "conflict_delta", "wiki_similarity_cosine",
        )
        key_feats = {k: round(float(r.features.get(k, 0.0)), 4) for k in key_feats_src}
        parts.append(f"  - features: `{json.dumps(key_feats)}`")
        parts.append(f"  - text: {_trim(r.text)}")
        parts.append("")
    return "\n".join(parts) if parts else "_(none sampled)_\n"


def _render_report(
    rows: list[BacktestRow],
    artifacts: list[LoadedArtifact],
    db_path: Path,
    days: int,
    sample_k: int,
    seed: int,
) -> str:
    versions = [a.version for a in artifacts]
    by_outcome: dict[str, int] = {"good": 0, "bad": 0, "unknown": 0}
    for r in rows:
        by_outcome[r.outcome] += 1

    parts: list[str] = []
    parts.append(f"# Steward classifier — {' vs '.join(versions)} back-test")
    parts.append("")
    parts.append(f"- db: `{db_path}`")
    parts.append(f"- window: last {days} days")
    parts.append(f"- extractor FEATURE_VERSION: `{FEATURE_VERSION}`")
    for a in artifacts:
        parts.append(
            f"- {a.version} artifact: `{a.path}` "
            f"(feature_version=`{a.feature_version}`, "
            f"calibration=`{a.calibration or 'unknown'}`, "
            f"n_keys={len(a.feature_keys)})"
        )
    parts.append(f"- classifier threshold: `{CLASSIFIER_THRESHOLD}` "
                 f"(+ citation >= {LEGACY_MIN_CITATIONS})")
    parts.append(f"- legacy thresholds: task-spec=`{LEGACY_THRESHOLD}` / "
                 f"live-prod=`{LEGACY_THRESHOLD_LIVE}`")
    parts.append("")

    # Label-leak disclosure (same as v2 report).
    train_rows = [r for r in rows if r.in_training_split]
    test_rows = [r for r in rows if not r.in_training_split]
    parts.append("## Label-leakage disclosure")
    parts.append("")
    parts.append(
        "Both v2 and v3 artifacts were trained on the SAME DB we back-test on "
        "using a daily-stratified 80/20 split; claims created before "
        f"`{_TRAIN_SPLIT_CUTOFF_ISO}` were in the training corpus and carry "
        "label-leak risk. We report full-window AND out-of-sample-only metrics "
        "so readers can judge honestly."
    )
    parts.append("")
    parts.append(f"- train-split overlap: **{len(train_rows)}** rows (labels seen)")
    parts.append(f"- test-split / out-of-sample: **{len(test_rows)}** rows")
    parts.append("")

    parts.append("## N events analyzed")
    parts.append("")
    parts.append(f"- total: **{len(rows)}**")
    parts.append(f"- outcome=good (currently confirmed): {by_outcome['good']}")
    parts.append(f"- outcome=bad (archived/stale/superseded/conflicted): {by_outcome['bad']}")
    parts.append(f"- outcome=unknown (still candidate): {by_outcome['unknown']}")
    parts.append("")

    # ---- Per-version confusion matrices ------------------------------------
    parts.append("## Confusion matrices — full 30-day window")
    parts.append("")
    for v in versions:
        cm = _confusion(rows, key="classifier", version=v)
        parts.append(f"### {v} @ {CLASSIFIER_THRESHOLD}")
        parts.append("")
        parts.append("```")
        parts.append(_format_cm(cm))
        parts.append("```")
        parts.append("")
    cm_legacy = _confusion(rows, key="legacy")
    cm_legacy_live = _confusion(rows, key="legacy_live")
    parts.append(f"### legacy @ {LEGACY_THRESHOLD} (task-spec Pareto)")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_legacy))
    parts.append("```")
    parts.append("")
    parts.append(f"### legacy @ {LEGACY_THRESHOLD_LIVE} (live-prod)")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_legacy_live))
    parts.append("```")
    parts.append("")

    # ---- Out-of-sample only ----
    parts.append("## Confusion matrices — out-of-sample only")
    parts.append("")
    for v in versions:
        cm = _confusion(test_rows, key="classifier", version=v)
        parts.append(f"### {v} @ {CLASSIFIER_THRESHOLD} — test split")
        parts.append("")
        parts.append("```")
        parts.append(_format_cm(cm))
        parts.append("```")
        parts.append("")
    cm_legacy_live_test = _confusion(test_rows, key="legacy_live")
    parts.append(f"### legacy @ {LEGACY_THRESHOLD_LIVE} — test split")
    parts.append("")
    parts.append("```")
    parts.append(_format_cm(cm_legacy_live_test))
    parts.append("```")
    parts.append("")

    # ---- Version-vs-version disagreement (main acceptance ask) -------------
    if len(versions) >= 2:
        a, b = versions[0], versions[1]
        diss = _disagreement(rows, a=a, b=b)
        a_only_good, a_only_bad = _outcome_rates(diss[f"{a}_only"])
        b_only_good, b_only_bad = _outcome_rates(diss[f"{b}_only"])

        parts.append(f"## Disagreement: {a} vs {b}")
        parts.append("")
        parts.append(f"- only {a} promotes  (v2 says promote, v3 says archive): **{len(diss[f'{a}_only'])}**  "
                     f"(of which good={a_only_good}, bad={a_only_bad})")
        parts.append(f"- only {b} promotes  (v3 says promote, v2 says archive): **{len(diss[f'{b}_only'])}**  "
                     f"(of which good={b_only_good}, bad={b_only_bad})")
        parts.append(f"- both promote: **{len(diss['agree_yes'])}**")
        parts.append(f"- both block: **{len(diss['agree_no'])}**")
        parts.append("")

        # Sampled disagreement rows — acceptance-spec requires >=10 per class.
        k = max(sample_k, 10)
        parts.append(f"### Sampled claims — only {a} promotes (v2=promote, v3=archive)")
        parts.append("")
        parts.append(_sample_markdown(diss[f"{a}_only"], k, seed=seed, versions=versions))
        parts.append(f"### Sampled claims — only {b} promotes (v2=archive, v3=promote)")
        parts.append("")
        parts.append(_sample_markdown(diss[f"{b}_only"], k, seed=seed + 1, versions=versions))

    # ---- Version-vs-legacy samples (as v2 report had) ----------------------
    for v in versions:
        diss_task = _disagreement_vs_legacy(rows, version=v, legacy_key="legacy")
        diss_live = _disagreement_vs_legacy(rows, version=v, legacy_key="legacy_live")
        parts.append(f"## {v} vs legacy disagreement")
        parts.append("")
        parts.append(f"- {v} adds vs legacy@0.72: **{len(diss_task['version_adds'])}** "
                     f"(good={_outcome_rates(diss_task['version_adds'])[0]}, "
                     f"bad={_outcome_rates(diss_task['version_adds'])[1]})")
        parts.append(f"- {v} blocks vs legacy@0.72: **{len(diss_task['version_blocks'])}**")
        parts.append(f"- {v} adds vs legacy@0.58: **{len(diss_live['version_adds'])}** "
                     f"(good={_outcome_rates(diss_live['version_adds'])[0]}, "
                     f"bad={_outcome_rates(diss_live['version_adds'])[1]})")
        parts.append(f"- {v} blocks vs legacy@0.58: **{len(diss_live['version_blocks'])}**")
        parts.append("")

    # ---- Verdict ----
    parts.append("## Verdict")
    parts.append("")
    parts.append("### Full 30-day window")
    parts.append("")
    for v in versions:
        cm = _confusion(rows, key="classifier", version=v)
        pr = _precision_recall(cm)
        parts.append(f"- {v}: F1={pr['f1']} precision={pr['precision']} recall={pr['recall']}")
    pr_legacy_live = _precision_recall(cm_legacy_live)
    parts.append(
        f"- legacy@0.58 (live): F1={pr_legacy_live['f1']} "
        f"precision={pr_legacy_live['precision']} recall={pr_legacy_live['recall']}"
    )
    parts.append("")

    parts.append("### Out-of-sample only (honest)")
    parts.append("")
    for v in versions:
        cm = _confusion(test_rows, key="classifier", version=v)
        pr = _precision_recall(cm)
        parts.append(f"- {v}: F1={pr['f1']} precision={pr['precision']} recall={pr['recall']}")
    pr_legacy_live_test = _precision_recall(cm_legacy_live_test)
    parts.append(
        f"- legacy@0.58 (live): F1={pr_legacy_live_test['f1']} "
        f"precision={pr_legacy_live_test['precision']} recall={pr_legacy_live_test['recall']}"
    )
    parts.append("")

    if len(versions) >= 2:
        cm_a = _confusion(test_rows, key="classifier", version=versions[0])
        cm_b = _confusion(test_rows, key="classifier", version=versions[1])
        gap = _precision_recall(cm_b)["f1"] - _precision_recall(cm_a)["f1"]
        parts.append(
            f"**F1 gap on out-of-sample ({versions[1]} − {versions[0]}): {gap:+.4f}**"
        )
        parts.append("")

    return "\n".join(parts)


# -- CLI ---------------------------------------------------------------------


def _parse_versions_arg(raw: str) -> list[tuple[str, Path]]:
    pairs: list[tuple[str, Path]] = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        if "=" in token:
            v, p = token.split("=", 1)
            pairs.append((v.strip(), Path(p.strip())))
        else:
            pairs.append((token, Path(f"artifacts/steward-classifier-{token}.joblib")))
    if not pairs:
        raise ValueError("--versions needs at least one entry (e.g. 'v2,v3')")
    return pairs


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path("memorymaster.db"),
                   help="Path to memorymaster.db (opened read-only).")
    p.add_argument("--versions", type=str, default="v2,v3",
                   help="Comma-separated list of versions to back-test. "
                        "Each entry can be 'v2' (resolves to "
                        "artifacts/steward-classifier-v2.joblib) or "
                        "'v3=path/to/custom.joblib'.")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--out", type=Path,
                   default=Path("artifacts/steward-classifier-v3-backtest-2026-04-24.md"))
    p.add_argument("--jsonl-out", type=Path, default=None)
    p.add_argument("--sample-k", type=int, default=10,
                   help="Samples per disagreement class (minimum 10 for v2-vs-v3 "
                        "per acceptance spec).")
    p.add_argument("--seed", type=int, default=1337)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.db.exists():
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2

    version_pairs = _parse_versions_arg(args.versions)
    artifacts: list[LoadedArtifact] = []
    for version, path in version_pairs:
        if not path.exists():
            print(f"error: artifact not found for {version}: {path}", file=sys.stderr)
            return 2
        artifacts.append(_load_artifact(version, path))
        print(
            f"loaded {version}: path={path.name} "
            f"feature_version={artifacts[-1].feature_version} "
            f"n_keys={len(artifacts[-1].feature_keys)}",
            file=sys.stderr,
        )

    # Defensive: make sure the env gate is set for the v2/v3 production
    # classifier cache BUT the backtest itself bypasses it via _load_artifact.
    os.environ.setdefault("MEMORYMASTER_STEWARD_CLASSIFIER_PATH", str(artifacts[0].path.resolve()))

    rows = run_backtest(args.db, artifacts=artifacts, days=args.days)
    print(f"backtest: loaded {len(rows)} claim-events", file=sys.stderr)

    md = _render_report(rows, artifacts, args.db, args.days, args.sample_k, args.seed)
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
                    "proba_by_version": r.proba_by_version,
                    "promote_by_version": r.promote_by_version,
                    "legacy_score": r.legacy_score,
                    "legacy_promote": r.legacy_promote,
                    "legacy_promote_live": r.legacy_promote_live,
                    "features": r.features,
                }) + "\n")
        print(f"backtest: raw rows written to {args.jsonl_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
