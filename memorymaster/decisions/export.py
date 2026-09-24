"""Training / off-policy-evaluation export: one JSON line per decided item.

Each row carries the question versions and full answer distributions, the
action taken vs Jev's and legacy actions, the logging propensity, exposure and
delivery, every joined outcome with its ``label_source``, and a time-split flag
(``train`` before ``split_at``, ``test`` after).  Redacted state is included only
on request.  Consumers must not treat ``jev``, ``unknown_actor`` or
``unattributed_override`` labels as ground truth.  An unreadable ledger raises
``LedgerReadError`` instead of looking empty.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from memorymaster.decisions.ledger import DecisionLedger, utc_iso

EXPORT_VERSION = "jev-export/1"


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def iter_training_rows(
    ledger: DecisionLedger,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    split_at: datetime | None = None,
    include_state: bool = False,
) -> Iterator[dict[str, Any]]:
    clauses, params = [], []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(utc_iso(since))
    if until is not None:
        clauses.append("ts <= ?")
        params.append(utc_iso(until))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    decisions = ledger.query(f"SELECT * FROM decisions {where} ORDER BY ts, decision_id", params)
    split = utc_iso(split_at) if split_at is not None else None
    for decision in decisions:
        decision_id = decision["decision_id"]
        items = ledger.query("SELECT * FROM decision_items WHERE decision_id = ? ORDER BY item_ref, question_id",
                             (decision_id,))
        outcomes = ledger.query("SELECT * FROM outcomes WHERE decision_id = ? ORDER BY observed_at, outcome_id",
                                (decision_id,))
        by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_ref[item["item_ref"]].append(item)
        decision_answers = {f"{r['question_id']}@v{r['question_version']}": _answer(r)
                            for r in by_ref.get("", []) if r["question_id"]}
        refs = [ref for ref in by_ref if ref] or [""]
        for ref in refs:
            rows = by_ref.get(ref, [])
            first = rows[0] if rows else {}
            answers = dict(decision_answers)
            answers.update({f"{r['question_id']}@v{r['question_version']}": _answer(r) for r in rows if r["question_id"]})
            joined = [o for o in outcomes if o["item_ref"] == ref]
            row: dict[str, Any] = {
                "export_version": EXPORT_VERSION,
                "decision_id": decision_id,
                "ts": decision["ts"],
                "surface": decision["surface"],
                "mode": decision["mode"],
                "policy_version": decision["policy_version"],
                "question_set_id": decision["question_set_id"],
                "question_sha256": decision["question_sha256"],
                "model_served": decision["model_served"],
                "transport_outcome": decision["transport_outcome"],
                "fallback_reason": decision["fallback_reason"],
                "item_ref": ref,
                "item_kind": first.get("item_kind"),
                "rank_legacy": first.get("rank_legacy"),
                "rank_final": first.get("rank_final"),
                "exposed": max((r["exposed"] or 0 for r in rows), default=0),
                "delivered": max((r["delivered"] or 0 for r in rows), default=0),
                "answers": answers,
                "action_taken": _loads(decision["action_taken"]),
                "jev_action": _loads(decision["jev_action"]),
                "legacy_action": _loads(decision["legacy_action"]),
                "exploration_arm": decision["exploration_arm"],
                "chosen_propensity": decision["chosen_propensity"],
                "action_propensities": _loads(decision["action_propensities_json"]),
                "thresholds": _loads(decision["thresholds_json"]),
                "baseline_features": _loads(decision["baseline_features_json"]),
                "outcomes": [{"kind": o["kind"], "value": o["value"], "label_source": o["label_source"],
                              "observed_at": o["observed_at"], "lag_s": o["lag_s"], "was_exposed": o["was_exposed"],
                              "reward_version": o["reward_version"]} for o in joined],
                "label_sources": sorted({o["label_source"] for o in joined if o["label_source"]}),
                "decision_outcomes": sorted({o["kind"] for o in outcomes if o["item_ref"] == ""}) if ref else [],
                "split": None if split is None else ("train" if decision["ts"] < split else "test"),
            }
            if include_state:
                row["state_redacted"] = decision["state_redacted"]
            yield row


def _answer(item: dict[str, Any]) -> dict[str, Any]:
    return {"answer": item["answer"], "probabilities": _loads(item["probabilities_json"]),
            "confidence": item["confidence"]}


def export_jsonl(ledger: DecisionLedger, path: str | Path, **kwargs: Any) -> int:
    """Write :func:`iter_training_rows` to ``path``; return the number of rows."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in iter_training_rows(ledger, **kwargs):
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


__all__ = ["EXPORT_VERSION", "export_jsonl", "iter_training_rows"]
