"""Synthetic decisions ledgers for the Jev read-path tests (performance and differential).

``write_synthetic_ledger`` bulk-writes a ledger shaped like a busy recall hook:
every decision asks four questions about each of ``candidates`` authorized
memories (``recall.relevant`` score plus three nouls), logs the full Plackett-Luce
distribution over the 120 orderings of the top 5 (as ``policy.explore_ranking``
does), a redacted state with every candidate's text, thresholds and baseline
features.  Fallbacks, exploration, ``used_in_turn`` outcomes, late answers and
optional ``skip:`` rows are mixed in with a seeded RNG, so a ledger is fully
reproducible.  ``mixed=True`` adds the other surfaces (revalidate, ingest with
held candidates and steward verdicts, dedup with paraphrase pairs, hints) and
operator labels, for differential tests that must exercise every read path.

The default size (4000 decisions x 80 item rows over 14 days) is the verifier's
performance ledger.  Rows are inserted in one transaction on a raw connection
after ``DecisionLedger`` created the schema, so the append-only triggers still run.
"""
from __future__ import annotations

import itertools
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.decisions.ledger import (
    DECISION_COLUMNS,
    ITEM_COLUMNS,
    OUTCOME_COLUMNS,
    DecisionLedger,
    utc_iso,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
RECALL_QUESTIONS = ("recall.relevant", "recall.usable_evidence", "recall.contradicts_request",
                    "recall.instruction_like")
RECALL_THRESHOLDS = {"recall.relevant@v1": {"include": 0.5},
                     "recall.usable_evidence@v1": {"include": 0.5},
                     "recall.contradicts_request@v1": {"label": 0.7},
                     "recall.instruction_like@v1": {"label": 0.7}}
_WORDS = ("deploy", "sqlite", "ledger", "hook", "timeout", "session", "scope", "claim", "steward", "recall",
          "cache", "index", "profile", "tenant", "budget", "breaker", "egress", "redact", "window", "latency")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(words))


def _decision_row(**values) -> tuple:
    return tuple(values.get(column) for column in DECISION_COLUMNS)


def _item_row(**values) -> tuple:
    row = {"exposed": 0, "delivered": 0, **values}
    return tuple(row.get(column) for column in ITEM_COLUMNS)


def _outcome_row(**values) -> tuple:
    return tuple(values.get(column) for column in OUTCOME_COLUMNS)


def _fallback(rng: random.Random, skip_share: float) -> str | None:
    roll = rng.random()
    if roll < skip_share:
        return "skip:no_candidates"
    roll -= skip_share
    for reason, share in (("timeout", 0.03), ("late", 0.02), ("breaker_open", 0.01), ("egress_blocked", 0.005),
                          ("http_529", 0.005)):
        if roll < share:
            return reason
        roll -= share
    return None


def _recall(rng: random.Random, did: str, ts: datetime, candidates: int, skip_share: float):
    """One recall decision: (decision row, item rows, outcome rows)."""
    reason = _fallback(rng, skip_share)
    refs = [f"claim:{rng.randrange(1, 60_000)}" for _ in range(candidates)]
    refs = list(dict.fromkeys(refs))
    legacy = refs[:2]
    common = dict(decision_id=did, ts=utc_iso(ts), surface="recall", policy_version="jev-policy/1",
                  question_set_id="recall.relevant@v1+recall.usable_evidence@v1", backend="typesafe",
                  code_revision="4.9.0", state_schema_version=1, legacy_action=_dumps(legacy),
                  session_key=f"s{rng.randrange(40)}", scope="project:py-apps", tenant="default",
                  randomization_id=f"{rng.getrandbits(64):016x}")
    if reason is not None and reason.startswith("skip:"):
        row = _decision_row(**common, mode="live", fallback_reason=reason, transport_outcome="not_sent",
                            attempt_count=0, engine_ms=rng.randrange(0, 3), tokens_in=0, tokens_out=0,
                            cost_usd=0.0, action_taken=_dumps(legacy), exploration_arm="fallback",
                            available_actions_json=_dumps([legacy]),
                            action_propensities_json=_dumps([{"action": legacy, "p": 1.0}]), chosen_propensity=1.0)
        return row, [], []
    answered = reason not in ("timeout", "egress_blocked", "http_529")
    relevance = {ref: round(rng.random(), 6) for ref in refs}
    usable = {ref: round(rng.random(), 6) for ref in refs}
    order = sorted(refs, key=lambda ref: -relevance[ref])
    k = max(2, min(5, sum(1 for ref in refs if usable[ref] >= 0.5)))
    jev = order[:k] if answered else None
    state = {"state": {"request": _text(rng, 14), "project": "memorymaster"},
             "subjects": {ref: {"memory": _text(rng, 22)} for ref in refs}}
    sent = reason != "egress_blocked"
    latency = rng.randrange(300, 880) if sent else None
    tokens = rng.randrange(2500, 4200) if sent else 0
    explore = reason is None and rng.random() < 0.10
    if reason is None:
        top = order[:5]
        actions = [list(p) for p in itertools.permutations(top)]
        weights = [rng.random() for _ in actions]
        total = sum(weights)
        propensities = [0.9 * (a == top) + 0.1 * w / total for a, w in zip(actions, weights)]
        chosen = rng.choice(actions) if explore else top
        taken = (chosen + order[5:])[:k] if k > 5 else chosen[:k]
        arm = "explore_order" if explore else "policy"
        extra = dict(available_actions_json=_dumps(actions),
                     action_propensities_json=_dumps([{"action": a, "p": p} for a, p in zip(actions, propensities)]),
                     chosen_propensity=propensities[actions.index(chosen)], exploration_arm=arm)
        exposed = taken
    else:
        taken = legacy
        extra = dict(available_actions_json=_dumps([legacy]),
                     action_propensities_json=_dumps([{"action": legacy, "p": 1.0}]), chosen_propensity=1.0,
                     exploration_arm="shadow" if reason == "breaker_open" else "fallback")
        exposed = legacy
    row = _decision_row(
        **common, **extra, mode="shadow" if reason == "breaker_open" else "live", fallback_reason=reason,
        question_sha256=f"{rng.getrandbits(256):064x}", primitive_summary="score:20,noul:60",
        model_requested="jev-1.13.0", model_served="jev-1.13.0" if answered else None,
        transport_version="stdlib/1", sdk_version="http.client", state_sha256=f"{rng.getrandbits(256):064x}",
        state_redacted=_dumps(state) if sent else None, egress_bytes=len(_dumps(state)) if sent else None,
        redaction_counts_json=_dumps({"home_path": rng.randrange(2)}),
        transport_outcome={"timeout": "timeout", "late": "late", "egress_blocked": "not_sent",
                           "http_529": "http_529"}.get(reason, "ok"),
        latency_ms=latency, engine_ms=(latency or 0) + rng.randrange(5, 40), attempt_count=1 if sent else 0,
        tokens_in=tokens, tokens_out=rng.randrange(40, 120) if answered else 0,
        cost_usd=tokens * 0.042e-6, jev_action=_dumps(jev) if jev is not None else None,
        action_taken=_dumps(taken), thresholds_json=_dumps(RECALL_THRESHOLDS),
        baseline_features_json=_dumps({"candidates": len(refs), "query_chars": rng.randrange(20, 400)}))
    ranks = {ref: index + 1 for index, ref in enumerate(taken)}
    exposed_set = set(exposed)
    items = []
    for index, ref in enumerate(refs):
        base = dict(decision_id=did, item_ref=ref, item_kind="claim", rank_legacy=index + 1,
                    rank_final=ranks.get(ref), exposed=int(ref in exposed_set), delivered=int(ref in exposed_set))
        if not answered:
            items.append(_item_row(**base, question_id=""))
            continue
        for question in RECALL_QUESTIONS:
            if question == "recall.relevant":
                value = relevance[ref]
                probabilities = {"1": round(1 - value, 6), "2": 0.0, "3": 0.0, "4": value}
            else:
                value = usable[ref] if question == "recall.usable_evidence" else round(rng.random() * 0.3, 6)
                probabilities = {"noul": value}
            items.append(_item_row(**base, question_id=question, question_version=1, answer=str(value),
                                   probabilities_json=_dumps(probabilities), confidence=value))
    outcomes = []
    for ref in exposed:
        if rng.random() < 0.3:
            outcomes.append(_outcome_row(decision_id=did, item_ref=ref, kind="used_in_turn", value=1.0,
                                         was_exposed=1, reward_version="used_in_turn.v1", label_source="detector",
                                         observed_at=utc_iso(ts + timedelta(minutes=rng.randrange(1, 30))),
                                         lag_s=60))
    if reason == "late":
        outcomes.append(_outcome_row(decision_id=did, item_ref="", kind="late_answer", value=float(latency or 0),
                                     reward_version="late_answer.v1", label_source="transport",
                                     observed_at=utc_iso(ts + timedelta(seconds=1))))
    return row, items, outcomes


def _other(rng: random.Random, did: str, ts: datetime, surface: str, skip_share: float):
    """Revalidate / ingest / dedup / hints decisions with lifecycle outcomes and operator labels."""
    reason = _fallback(rng, skip_share)
    ref = f"claim:{rng.randrange(1, 9000)}" if surface != "dedup" else f"pair:{rng.randrange(900)}-{rng.randrange(900)}"
    questions = {"revalidate": ("lifecycle.still_valid", "lifecycle.durable", "lifecycle.useful_future"),
                 "ingest": ("ingest.evidence", "ingest.usefulness", "ingest.novelty"),
                 "dedup": ("memory.same_fact", "memory.contradicts", "memory.supersedes", "memory.supersedes_alt"),
                 "hints": ("hints.decision", "hints.constraint")}[surface]
    actions = {"revalidate": ("keep_stale", "confirm"), "ingest": ("admit", "hold"),
               "dedup": ("none", "propose"), "hints": (["decision"], [])}[surface]
    legacy = actions[0]
    answered = reason is None or reason in ("late", "breaker_open")
    jev = rng.choice(actions) if answered else None
    skip = reason is not None and reason.startswith("skip:")
    taken = jev if reason is None else legacy
    thresholds = {f"{q}@v1": {"accept": 0.8, "low": 0.2} for q in questions}
    row = _decision_row(
        decision_id=did, ts=utc_iso(ts), surface=surface, mode="shadow" if reason == "breaker_open" else "live",
        fallback_reason=reason, transport_outcome="not_sent" if skip else ("ok" if answered else "timeout"),
        latency_ms=None if skip else rng.randrange(200, 7000), engine_ms=rng.randrange(1, 7100),
        attempt_count=0 if skip else 1, tokens_in=0 if skip else 900, tokens_out=0 if skip else 12,
        cost_usd=0.0 if skip else 900 * 0.042e-6, legacy_action=_dumps(legacy),
        jev_action=_dumps(jev) if jev is not None else None, action_taken=_dumps(taken),
        exploration_arm=("explore_alternative" if reason is None and rng.random() < 0.05 else
                         "policy" if reason is None else "shadow" if reason == "breaker_open" else "fallback"),
        available_actions_json=_dumps(list(actions)), chosen_propensity=1.0,
        action_propensities_json=_dumps([{"action": taken, "p": 1.0}]), thresholds_json=_dumps(thresholds),
        state_redacted=_dumps({"state": {"claim": {"text": _text(rng, 12)}}, "subjects": {ref: _text(rng, 6)}}),
        session_key=f"s{rng.randrange(40)}")
    items, outcomes = [], []
    if skip:
        return row, items, outcomes
    for question in questions:
        if not answered:
            items.append(_item_row(decision_id=did, item_ref=ref, item_kind="pair" if surface == "dedup" else "claim",
                                   question_id=""))
            break
        value = round(rng.random(), 4)
        items.append(_item_row(decision_id=did, item_ref=ref, item_kind="pair" if surface == "dedup" else "claim",
                               question_id=question, question_version=1, answer=str(value),
                               probabilities_json=_dumps({"noul": value}), exposed=1, delivered=1))
    if rng.random() < 0.02 and answered:  # a choice-type answer: counted as a choice, never as a probability
        items.append(_item_row(decision_id=did, item_ref="", question_id="route.query_type", question_version=1,
                               answer="factual", probabilities_json=_dumps({"factual": 0.8, "unknown": 0.2})))
    kinds = {"revalidate": ("stale", "superseded", "used_in_turn"), "ingest": ("steward_confirmed", "archived"),
             "dedup": ("proposal_approved", "proposal_rejected", "superseded"), "hints": ("used_in_turn",)}[surface]
    if rng.random() < 0.5:
        outcomes.append(_outcome_row(decision_id=did, item_ref=ref, kind=rng.choice(kinds), value=1.0, was_exposed=1,
                                     label_source=rng.choice(("steward", "operator", "automation", "jev",
                                                              "unattributed_override", "detector")),
                                     observed_at=utc_iso(ts + timedelta(hours=rng.randrange(1, 200)))))
    if answered and rng.random() < 0.1:
        truth = rng.randrange(2)
        outcomes.append(_outcome_row(
            decision_id=did, item_ref=ref, kind="operator_review", value=float(truth), label_source="operator",
            observed_at=utc_iso(ts + timedelta(hours=2)),
            details_json=_dumps({"question_id": questions[0], "question_version": 1, "truth": truth,
                                 "verdict": "correct" if truth else "incorrect"})))
    return row, items, outcomes


def write_synthetic_ledger(path: str | Path, *, decisions: int = 4000, candidates: int = 20, days: float = 14,
                           end: datetime = NOW, seed: int = 0, skip_share: float = 0.0,
                           mixed: bool = False) -> Path:
    """Write a reproducible ledger at ``path`` (which must not exist) and return it."""
    path = Path(path)
    ledger = DecisionLedger(path)
    assert ledger.set_watermark("synthetic", str(seed)), "schema creation failed"
    rng = random.Random(seed)
    span = timedelta(days=days)
    surfaces = ("revalidate", "ingest", "dedup", "hints")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        insert_d = f"INSERT INTO decisions ({', '.join(DECISION_COLUMNS)}) VALUES ({', '.join('?' * len(DECISION_COLUMNS))})"
        insert_i = f"INSERT INTO decision_items ({', '.join(ITEM_COLUMNS)}) VALUES ({', '.join('?' * len(ITEM_COLUMNS))})"
        insert_o = (f"INSERT OR IGNORE INTO outcomes ({', '.join(OUTCOME_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(OUTCOME_COLUMNS))})")
        for index in range(decisions):
            did = f"{rng.getrandbits(128):032x}"
            ts = end - span + span * rng.random()
            if mixed and index % 3 == 2:
                row, items, outcomes = _other(rng, did, ts, surfaces[(index // 3) % len(surfaces)], skip_share)
            else:
                row, items, outcomes = _recall(rng, did, ts, candidates, skip_share)
            conn.execute(insert_d, row)
            conn.executemany(insert_i, items)
            conn.executemany(insert_o, outcomes)
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return path


__all__ = ["NOW", "RECALL_QUESTIONS", "write_synthetic_ledger"]
