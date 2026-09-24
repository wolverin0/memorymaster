"""Off-policy evaluation of a candidate Jev threshold / top-k policy against the logging policy.

Every logged decision on ``--surface`` with a logging propensity is a unit:
``mu`` = the logging policy's probability of the action actually taken, ``pi`` = 1
when the deterministic candidate policy would take that same action (else 0).
Weights are clipped: ``w = min(pi / mu, --clip)``.  Ranking exploration logs a
distribution over whole orderings of the top 5 while the action is only the first
k refs (adaptive k), so ``mu`` sums every logged ordering sharing that prefix; the
logged ``chosen_propensity`` (one whole ordering) is used only when no distribution
was logged.

Support: the logging policy explores only the order of the top refs, never k, the
tail or (on fallback/shadow rows) anything but the action taken.  Each decision is
``supported`` when the logging probability of the candidate's action is > 0,
``unsupported`` when it is 0 and ``unverified`` when no distribution was logged.
With any unsupported decision IPS and SNIPS are not identified (reported as null
with a warning); DR is kept with a caveat, since it values those decisions by the
reward model alone.

Fallback rows: a decision whose ``fallback_reason`` is set (``timeout``, ``late``,
``choose_error``, ``choose_invalid``, ``ledger_unavailable``, ``http_*``, ...) took the
legacy action because the request failed, and the live engine running the candidate
would have failed there too, so the candidate's target on it is the legacy action
(even when late answers were logged).  ``breaker_open`` and shadow rows keep the
candidate's own target: the answers were on time, only the mode withheld them.
``skip:`` rows (the surface did not ask Jev) are not units at all.

Candidate policies (answers come from ``decision_items``; a decision whose needed
answers are missing falls back to its legacy action, as the live engine would):

* ``threshold``: gate = the minimum answer over ``--question`` (one or more);
  action = ``--above`` when gate >= ``--threshold`` else ``--below`` (compared with
  the JSON-decoded ``action_taken``).
* ``topk``: order refs by the first ``--question`` (descending; legacy rank breaks
  ties); k = number of refs whose ``--include-question`` answer >= ``--threshold``,
  clamped to [``--k-min``, ``--k-max``]; action = the first k refs.

Reward: ``threshold`` -> 1 when the decision has a ``--reward-kind`` outcome;
``topk`` -> number of refs in the action with that outcome.  ``jev`` and
``unattributed_override`` label sources never count.  Estimators: IPS, SNIPS and
doubly robust (binned reward model, 2-fold cross-fitted).  Output always states n,
the effective sample size, the clipping rule and a percentile bootstrap interval.
The ledger is opened read-only.

    python scripts/jev_ope.py --surface ingest --policy threshold --question ingest.evidence \\
        --question ingest.usefulness --threshold 0.6 --above '"admit"' --below '"hold"'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.decisions.metrics import UNTRUSTED_LABEL_SOURCES, is_skip  # noqa: E402 — after sys.path bootstrap

UNTRUSTED_SOURCES = UNTRUSTED_LABEL_SOURCES
# Fallback reasons after which the request's answers were still usable: the candidate keeps its own target.
ANSWERED_FALLBACKS = frozenset({"breaker_open", "shadow"})
DEFAULT_REWARD = {"recall": "used_in_turn", "session": "used_in_turn", "ingest": "steward_confirmed",
                  "revalidate": "used_in_turn"}
POLICIES = ("threshold", "topk")
LOW_ESS = 30
BINS = 10


def open_ledger(path: str | Path | None = None):
    from memorymaster.surfaces.jev_review import read_ledger

    return read_ledger(path)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _key(action: Any) -> str:
    return json.dumps(action, sort_keys=True, ensure_ascii=False, default=str)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bin(value: float | None) -> int | None:
    return None if value is None else min(max(int(value * BINS), 0), BINS - 1)


def _fold(decision_id: str) -> int:
    return hashlib.sha256(decision_id.encode("utf-8")).digest()[0] % 2


def _is_ranking(actions: list[Any]) -> bool:
    """Every logged action is an ordering of the same refs (``policy.explore_ranking``)."""
    return all(isinstance(a, list) for a in actions) and len({_key(sorted(map(str, a))) for a in actions}) == 1


def _distribution(propensities_json: Any) -> list[tuple[Any, float]] | None:
    """The logged ``[{"action", "p"}]`` distribution, or ``None`` when absent or malformed."""
    logged = _loads(propensities_json)
    if not isinstance(logged, list) or not logged:
        return None
    entries = []
    for entry in logged:
        p = _number(entry.get("p")) if isinstance(entry, dict) and "action" in entry else None
        if p is None or p < 0.0:
            return None
        entries.append((entry["action"], p))
    return entries


def action_probability(action: Any, logged_action: Any, propensities_json: Any) -> float | None:
    """Probability that the logging policy would take ``action`` on a decision that logged ``logged_action``.

    An action logged verbatim (binary exploration, fallback/shadow, or a ranking with
    k = 5) has its own probability; any other non-ranking action has 0.  Ranking
    exploration (``policy.explore_ranking``) permutes only the explored top refs and
    k is deterministic given Jev's answers, so a top-k action has probability 0
    unless it has the logged action's length (and, beyond the explored top, its
    fixed tail); otherwise it is the sum over the logged orderings that start with
    it.  ``None`` when no usable distribution was logged: support cannot be checked.
    """
    entries = _distribution(propensities_json)
    if entries is None:
        return None
    actions = [logged for logged, _ in entries]
    ranking = isinstance(action, list) and _is_ranking(actions)
    if ranking and isinstance(logged_action, list) and len(action) != len(logged_action):
        return 0.0  # k was not explored: a whole logged ordering is not a candidate with another k
    key = _key(action)
    exact = [p for logged, p in entries if _key(logged) == key]
    if exact:
        return sum(exact)
    if not ranking:
        return 0.0
    top = len(actions[0])
    if len(action) > top and not (isinstance(logged_action, list) and action[top:] == logged_action[top:]):
        return 0.0  # the tail beyond the explored top was not explored either
    return sum(p for logged, p in entries if logged[:len(action)] == action[:len(logged)])


def falls_back(fallback_reason: Any) -> bool:
    """The live engine would take the legacy action on this row whatever the candidate policy."""
    return bool(fallback_reason) and fallback_reason not in ANSWERED_FALLBACKS and not is_skip(fallback_reason)


def logging_propensity(action_taken: Any, propensities_json: Any, chosen_propensity: Any) -> float | None:
    """Probability that the logging policy took ``action_taken`` (see :func:`action_probability`).

    Without a usable logged distribution, or when it does not contain the action,
    the logged ``chosen_propensity`` is used.
    """
    probability = action_probability(action_taken, action_taken, propensities_json)
    return probability if probability else _number(chosen_propensity)


def _load_units(ledger: Any, surface: str, reward_kind: str, now: datetime,
                since: datetime | None) -> tuple[list[dict[str, Any]], int, int]:
    from memorymaster.decisions.ledger import utc_iso

    clause, params = "d.surface = ? AND d.ts <= ?", [surface, utc_iso(now)]
    if since is not None:
        clause += " AND d.ts >= ?"
        params.append(utc_iso(since))
    decisions = ledger.query(
        "SELECT d.decision_id, d.ts, d.legacy_action, d.action_taken, d.action_propensities_json, "
        "d.chosen_propensity, d.fallback_reason FROM decisions d "
        f"WHERE {clause} ORDER BY d.ts, d.decision_id", params)
    items = ledger.query(
        "SELECT i.decision_id, i.item_ref, i.question_id, i.answer, i.exposed, i.rank_legacy "
        f"FROM decision_items i JOIN decisions d USING (decision_id) WHERE {clause}", params)
    outcomes = ledger.query(
        "SELECT o.decision_id, o.item_ref, o.label_source FROM outcomes o JOIN decisions d USING (decision_id) "
        f"WHERE {clause} AND o.kind = ?", [*params, reward_kind])
    answers: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    exposed: dict[str, set[str]] = defaultdict(set)
    for row in items:
        if row["question_id"]:
            value = _number(row["answer"])
            if value is not None:
                answers[row["decision_id"]][(row["item_ref"], row["question_id"])] = value
        if row["rank_legacy"] is not None:
            ranks[row["decision_id"]][row["item_ref"]] = int(row["rank_legacy"])
        if row["exposed"]:
            exposed[row["decision_id"]].add(row["item_ref"])
    rewarded: dict[str, set[str]] = defaultdict(set)
    for row in outcomes:
        if row["label_source"] not in UNTRUSTED_SOURCES:
            rewarded[row["decision_id"]].add(row["item_ref"])
    units, skipped, skip_rows = [], 0, 0
    for row in decisions:
        if is_skip(row["fallback_reason"]):
            skip_rows += 1
            continue
        logged = _loads(row["action_taken"])
        mu = logging_propensity(logged, row["action_propensities_json"], row["chosen_propensity"])
        if mu is None or mu <= 0.0 or mu > 1.0 + 1e-9:
            skipped += 1
            continue
        did = row["decision_id"]
        units.append({"decision_id": did, "mu": min(mu, 1.0), "logged": logged,
                      "propensities": row["action_propensities_json"],
                      "legacy": _loads(row["legacy_action"]), "answers": answers.get(did, {}),
                      "ranks": ranks.get(did, {}), "exposed": exposed.get(did, set()),
                      "rewarded": rewarded.get(did, set()), "falls_back": falls_back(row["fallback_reason"])})
    return units, skipped, skip_rows


def _threshold_policy(questions: Sequence[str], threshold: float, above: Any, below: Any
                      ) -> Callable[[dict[str, Any]], tuple[Any, float | None]]:
    def target(unit: dict[str, Any]) -> tuple[Any, float | None]:
        values = []
        for question in questions:
            answered = [v for (_, q), v in unit["answers"].items() if q == question]
            if not answered:
                return unit["legacy"], None
            values.append(min(answered))
        gate = min(values)
        return (above if gate >= threshold else below), gate

    return target


def _topk_policy(question: str, include_question: str | None, threshold: float, k_min: int, k_max: int
                 ) -> Callable[[dict[str, Any]], tuple[Any, None]]:
    def target(unit: dict[str, Any]) -> tuple[Any, None]:
        scores = {ref: v for (ref, q), v in unit["answers"].items() if q == question}
        if not scores:
            return unit["legacy"], None
        order = sorted(scores, key=lambda ref: (-scores[ref], unit["ranks"].get(ref, 10**9), ref))
        if include_question:
            k = sum(1 for ref in order if unit["answers"].get((ref, include_question), -1.0) >= threshold)
        else:
            k = k_max
        return order[:min(max(k, k_min), k_max, len(order))], None

    return target


def _mean(values: Sequence[float], default: float) -> float:
    return sum(values) / len(values) if values else default


def _reward_model(units: list[dict[str, Any]], policy: str, question: str):
    """2-fold cross-fitted binned reward model: ``q(unit, action)`` uses only the other fold."""
    if policy == "threshold":
        tables = []
        for fold in (0, 1):
            train = [u for u in units if u["fold"] != fold]
            by_bin, by_action = defaultdict(list), defaultdict(list)
            for u in train:
                by_bin[(_key(u["logged"]), _bin(u["feature"]))].append(u["r"])
                by_action[_key(u["logged"])].append(u["r"])
            overall = _mean([u["r"] for u in train], 0.0)
            tables.append(({k: _mean(v, overall) for k, v in by_bin.items()},
                           {k: _mean(v, overall) for k, v in by_action.items()}, overall))

        def q_threshold(unit: dict[str, Any], action: Any) -> float:
            by_bin, by_action, overall = tables[unit["fold"]]
            key = _key(action)
            return by_bin.get((key, _bin(unit["feature"])), by_action.get(key, overall))

        return q_threshold

    item_tables = []
    for fold in (0, 1):
        by_bin, every = defaultdict(list), []
        for u in units:
            if u["fold"] == fold or not isinstance(u["logged"], list):
                continue
            for ref in u["logged"]:
                used = float(ref in u["rewarded"])
                every.append(used)
                by_bin[_bin(u["answers"].get((ref, question)))].append(used)
        overall = _mean(every, 0.0)
        item_tables.append(({k: _mean(v, overall) for k, v in by_bin.items()}, overall))

    def q_topk(unit: dict[str, Any], action: Any) -> float:
        by_bin, overall = item_tables[unit["fold"]]
        refs = action if isinstance(action, list) else []
        return sum(by_bin.get(_bin(unit["answers"].get((ref, question))), overall) for ref in refs)

    return q_topk


def _estimates(rows: Sequence[tuple[float, float, float, float]]) -> dict[str, float | None]:
    n = len(rows)
    if not n:
        return {"ips": None, "snips": None, "dr": None}
    total_w = sum(w for w, _, _, _ in rows)
    return {
        "ips": sum(w * r for w, r, _, _ in rows) / n,
        "snips": (sum(w * r for w, r, _, _ in rows) / total_w) if total_w > 0 else None,
        "dr": sum(q_target + w * (r - q_logged) for w, r, q_target, q_logged in rows) / n,
    }


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def evaluate(ledger: Any, *, surface: str, policy: str, questions: Sequence[str], threshold: float,
             above: Any = None, below: Any = None, include_question: str | None = None, k_min: int = 2,
             k_max: int = 5, reward_kind: str | None = None, clip: float = 10.0, bootstrap: int = 1000,
             seed: int = 0, now: datetime | None = None, since: datetime | None = None) -> dict[str, Any]:
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    if not questions:
        raise ValueError("at least one --question is required")
    if policy == "threshold" and (above is None or below is None):
        raise ValueError("the threshold policy needs --above and --below actions")
    if clip <= 0 or bootstrap < 1 or not 1 <= k_min <= k_max:
        raise ValueError("clip must be > 0, bootstrap >= 1 and 1 <= k_min <= k_max")
    kind = reward_kind or DEFAULT_REWARD.get(surface)
    if not kind:
        raise ValueError(f"no default reward for surface {surface!r}: pass --reward-kind")
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    units, skipped, skip_rows = _load_units(ledger, surface, kind, end, since)
    target = (_threshold_policy(questions, threshold, above, below) if policy == "threshold"
              else _topk_policy(questions[0], include_question, threshold, k_min, k_max))
    clipped = 0
    support = {"supported": 0, "unsupported": 0, "unverified": 0}
    for unit in units:
        action, feature = (unit["legacy"], None) if unit["falls_back"] else target(unit)
        unit["target"], unit["feature"], unit["fold"] = action, feature, _fold(unit["decision_id"])
        if policy == "threshold":
            unit["r"] = 1.0 if unit["rewarded"] else 0.0
        else:
            shown = unit["logged"] if isinstance(unit["logged"], list) else sorted(unit["exposed"])
            unit["r"] = float(sum(1 for ref in shown if ref in unit["rewarded"]))
        taken = _key(action) == _key(unit["logged"])
        target_mu = action_probability(action, unit["logged"], unit["propensities"])
        support["supported" if taken or (target_mu or 0.0) > 0.0
                else "unverified" if target_mu is None else "unsupported"] += 1
        raw = (1.0 if taken else 0.0) / unit["mu"]
        clipped += raw > clip
        unit["w"] = min(raw, clip)
    q = _reward_model(units, policy, questions[0])
    rows = [(u["w"], u["r"], q(u, u["target"]), q(u, u["logged"])) for u in units]
    point = _estimates(rows)
    total_w, total_w2 = sum(r[0] for r in rows), sum(r[0] ** 2 for r in rows)
    ess = (total_w ** 2 / total_w2) if total_w2 > 0 else 0.0
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {name: [] for name in point}
    if rows:
        for _ in range(bootstrap):
            replicate = _estimates([rows[rng.randrange(len(rows))] for _ in rows])
            for name, value in replicate.items():
                if value is not None:
                    samples[name].append(value)
    unsupported = support["unsupported"]
    estimates = {name: {"value": point[name],
                        "ci95": ([_percentile(samples[name], 0.025), _percentile(samples[name], 0.975)]
                                 if samples[name] else None),
                        "identified": not unsupported}
                 for name in point}
    support["coverage"] = (support["supported"] / len(units)) if units else None
    warnings = []
    if unsupported:
        # w = 0 on a decision where the candidate's action had no chance of being logged:
        # IPS/SNIPS are biased toward the logged actions and their intervals are not valid.
        for name in ("ips", "snips"):
            estimates[name] = {"value": None, "ci95": None, "identified": False}
        estimates["dr"]["caveat"] = (f"{unsupported} unsupported decisions are valued by the reward model alone: "
                                     "valid only if that model is right")
        warnings.append(f"{unsupported} of {len(units)} decisions give the candidate's action zero logging "
                        "probability (the logging policy explores only the order of the top refs, never k, the "
                        "tail or an action its fallback/shadow rows did not take): IPS and SNIPS are not "
                        "identified; DR relies on the reward model there")
    if support["unverified"]:
        warnings.append(f"support of the candidate's action could not be verified for {support['unverified']} "
                        "decisions without a logged action distribution")
    if ess < LOW_ESS:
        warnings.append(f"effective sample size {ess:.1f} < {LOW_ESS}: estimates are unreliable")
    if skipped:
        warnings.append(f"{skipped} decisions without a usable logging propensity were skipped")
    return {
        "surface": surface,
        "policy": {"kind": policy, "questions": list(questions), "threshold": threshold,
                   "include_question": include_question, "k_min": k_min, "k_max": k_max,
                   "above": above, "below": below},
        "reward": {"kind": kind, "per": "decision" if policy == "threshold" else "item in the action",
                   "excluded_label_sources": sorted(UNTRUSTED_SOURCES)},
        "n": len(units),
        "n_matched": sum(1 for u in units if u["w"] > 0),
        "support": support,
        "skipped_no_propensity": skipped,
        "excluded_skip_rows": skip_rows,
        "fallback_rows": sum(1 for u in units if u["falls_back"]),
        "ess": ess,
        "logging_value": (sum(u["r"] for u in units) / len(units)) if units else None,
        "clipping": {"rule": f"w = min(pi/mu, {clip})", "max_weight": clip, "clipped": clipped},
        "estimates": estimates,
        "bootstrap": {"replicates": bootstrap, "seed": seed, "method": "percentile", "level": 0.95},
        "reward_model": "binned by gate/score decile, 2-fold cross-fitted",
        "warnings": warnings,
    }


def format_text(result: dict[str, Any]) -> str:
    def value(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.4f}"

    policy, support = result["policy"], result["support"]
    lines = [
        f"Jev OPE surface={result['surface']} policy={policy['kind']} questions={','.join(policy['questions'])} "
        f"threshold={policy['threshold']} reward={result['reward']['kind']}",
        f"n={result['n']} decisions (matched {result['n_matched']}, skipped without propensity "
        f"{result['skipped_no_propensity']})",
        f"support: {support['supported']} of {result['n']} decisions give the candidate's action a non-zero "
        f"logging probability (unsupported {support['unsupported']}, unverified {support['unverified']})",
        f"fallback rows valued at the legacy action: {result['fallback_rows']}; "
        f"skip rows excluded: {result['excluded_skip_rows']}",
        f"effective sample size: {result['ess']:.1f}",
        f"clipping: {result['clipping']['rule']}; {result['clipping']['clipped']} of {result['n']} weights clipped",
        f"logging policy value: {value(result['logging_value'])}",
    ]
    for name in ("ips", "snips", "dr"):
        estimate = result["estimates"][name]
        if estimate["value"] is None and not estimate["identified"]:
            lines.append(f"{name.upper()}: not identified ({support['unsupported']} unsupported decisions)")
            continue
        interval = estimate["ci95"]
        shown = "n/a" if interval is None else f"[{interval[0]:.4f}, {interval[1]:.4f}]"
        caveat = f"  ({estimate['caveat']})" if estimate.get("caveat") else ""
        lines.append(f"{name.upper()}: {value(estimate['value'])}  bootstrap 95% interval {shown}{caveat}")
    boot = result["bootstrap"]
    lines.append(f"bootstrap: {boot['replicates']} {boot['method']} replicates, seed {boot['seed']}")
    lines.extend(f"warning: {warning}" for warning in result["warnings"])
    return "\n".join(lines)


def _action(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def _time(value: str) -> datetime:
    raw = value.strip()
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="decisions ledger (default: MEMORYMASTER_DECISIONS_DB)")
    parser.add_argument("--surface", required=True)
    parser.add_argument("--policy", choices=POLICIES, required=True)
    parser.add_argument("--question", action="append", required=True, dest="questions",
                        help="gate question(s) for threshold; ranking question for topk")
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--above", type=_action, default=None, help="threshold: action when gate >= threshold")
    parser.add_argument("--below", type=_action, default=None, help="threshold: action otherwise")
    parser.add_argument("--include-question", default=None, help="topk: include refs whose answer >= threshold")
    parser.add_argument("--k-min", type=int, default=2)
    parser.add_argument("--k-max", type=int, default=5)
    parser.add_argument("--reward-kind", default=None, help="outcome kind used as reward")
    parser.add_argument("--clip", type=float, default=10.0, help="maximum importance weight")
    parser.add_argument("--bootstrap", type=int, default=1000, help="bootstrap replicates")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--since", type=_time, default=None)
    parser.add_argument("--now", type=_time, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = evaluate(open_ledger(args.db), surface=args.surface, policy=args.policy, questions=args.questions,
                          threshold=args.threshold, above=args.above, below=args.below,
                          include_question=args.include_question, k_min=args.k_min, k_max=args.k_max,
                          reward_kind=args.reward_kind, clip=args.clip, bootstrap=args.bootstrap, seed=args.seed,
                          now=args.now, since=args.since)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True, default=str) if args.json else format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
