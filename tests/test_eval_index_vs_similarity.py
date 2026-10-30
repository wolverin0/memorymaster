"""T-1433 AC3 evaluator: cost accounting, blind judge parsing and the shared-label metrics."""

from __future__ import annotations

import pytest

import scripts.eval_index_vs_similarity as ev


def _cand(cid: str, body: str) -> dict:
    return {"cid": cid, "id": int(cid.split("-")[1], 16), "body": body}


def test_index_titles_are_72_collapsed_chars_and_reads_cost_full_bodies():
    long_body = "first   line\n" + "x" * 200
    cands = [_cand("mm-a1", long_body), _cand("mm-b2", "short")]
    index = ev.index_text(cands)
    assert index.splitlines()[0] == ev.INDEX_HEADER
    assert index.splitlines()[1] == "- [mm-a1] " + ("first line " + "x" * 200)[:72]
    assert ev.index_text([]) == ""
    # B pays for every body it asks for, in full, and nothing for ids outside the index.
    assert ev.read_text(cands, ["mm-b2", "mm-zz"]) == "[mm-b2]\nshort"
    assert ev.read_text(cands, []) == ""


def test_reader_choices_are_restricted_to_the_index_and_capped():
    cands = [_cand(f"mm-{i:x}", "b") for i in range(1, 6)]
    raw = '```json\n{"read": ["mm-1", "mm-1", "mm-99", "mm-2", "mm-3", "mm-4"]}\n```'
    assert ev.parse_reads(raw, cands) == ["mm-1", "mm-2", "mm-3"]


def test_judges_see_a_seeded_shuffle_and_must_label_every_candidate():
    pool = [_cand(f"mm-{i:x}", f"body {i}") for i in range(1, 9)]
    order = ev.judge_order("uuid-1", pool)
    assert order == ev.judge_order("uuid-1", pool)  # reproducible
    assert sorted(c["cid"] for c in order) == sorted(c["cid"] for c in pool)
    prompt = ev.judge_prompt("hola", order)
    assert "mm-" not in prompt.split("CANDIDATES:")[1]  # blind: no ids, no rank, no strategy
    raw = "[" + ",".join(f'{{"c": "C{n}", "label": "{"R" if n == 1 else "I"}"}}' for n in range(1, 9)) + "]"
    labels = ev.parse_labels(raw, order)
    assert labels[order[0]["cid"]] == "R" and list(labels.values()).count("I") == 7
    with pytest.raises(ValueError):
        ev.parse_labels('[{"c": "C1", "label": "R"}]', order)


def test_consensus_keeps_only_agreed_labels():
    gem = {"a": "R", "b": "I", "c": "R", "d": "U"}
    cla = {"a": "R", "b": "I", "c": "I", "d": "R"}
    assert ev.consensus([gem, cla]) == {"a": "R", "b": "I", "c": "D", "d": "U"}


def test_kappa_matches_hand_computation():
    assert ev.kappa([(True, True), (False, False)]) == 1.0
    # observed 0.5, both raters 50% positive -> expected 0.5 -> kappa 0
    assert ev.kappa([(True, True), (True, False), (False, True), (False, False)]) == 0.0
    assert ev.kappa([]) is None


def test_strategy_metrics_score_both_strategies_against_the_same_labels():
    pool = [{"cid": c} for c in ("r1", "i1", "r2", "i2")]
    event = {
        "pool": pool, "a_ids": ["r1", "i1"], "a_chars": 1000,
        "index_cids": ["r1", "i1", "r2", "i2"], "index_chars": 300,
        "read_ids": ["r2"], "read_chars": 200,
        "labels": {"k": {"r1": "R", "i1": "I", "r2": "R", "i2": "I"}},
    }
    empty_rel = {**event, "a_ids": ["i1", "i2"], "read_ids": [], "read_chars": 0,
                 "labels": {"k": {"r1": "I", "i1": "I", "r2": "I", "i2": "I"}}}
    m = ev.strategy_metrics([event, empty_rel], "k")
    assert m["a_chars_per_prompt"] == 1000
    assert m["b_chars_per_prompt"] == (300 + 200 + 300 + 0) / 2
    assert m["a_irrelevant_rate"] == 0.75  # 3 of A's 4 bodies
    assert m["b_read_irrelevant_rate"] == 0.0  # B read one body, relevant
    assert m["a_relevant_recall"] == 0.5 and m["b_relevant_recall"] == 0.5  # each delivered 1 of 2
    assert m["events_with_rel"] == 1 and m["a_prompt_hit_rate"] == 1.0 and m["b_prompt_hit_rate"] == 1.0
