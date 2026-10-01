"""T-0739: injected memories carry a citable id, and the usage detector can see it.

Week one of live Jev showed 2 "used" out of 2,811 recalled items: the recall block
had no claim ids, so the detector could only match 8 verbatim words. These tests
pin the format (id on every bullet + a cite instruction), the strong detector
paths (human_id, #<claim id>, 8-gram) and the separately labelled weak path.
"""
from __future__ import annotations

from types import SimpleNamespace

from memorymaster.decisions import outcomes
from memorymaster.decisions.metrics import _exposure_use
from memorymaster.recall import context_hook


def _claim(cid: int, human_id: str | None, text: str):
    return SimpleNamespace(id=cid, human_id=human_id, text=text, wiki_article=None)


# --- AC1: every injected claim carries its short id, and the block asks to cite it ----------


def test_every_recall_bullet_carries_its_short_id():
    rows = [{"claim": _claim(1, "mm-8077", "Core tools opened the cwd db.")},
            {"claim": _claim(2, "mm-240e", "Proactor loop kills the listener.")}]
    lines, rendered = context_hook._render_recall_lines(rows, budget=500)
    bullets = [line for line in lines if line.startswith("- ")]
    assert bullets == ["- [mm-8077] Core tools opened the cwd db.", "- [mm-240e] Proactor loop kills the listener."]
    assert len(rendered) == 2


def test_recall_block_instructs_the_agent_to_cite_ids():
    lines, _ = context_hook._render_recall_lines([{"claim": _claim(1, "mm-8077", "x")}], budget=500)
    text = context_hook._recall_text(lines)
    assert text.startswith("# Memory Context\n")
    assert "cite its [mm-id]" in text


def test_labels_stay_after_the_id_and_claims_without_id_render_plainly():
    assert context_hook._recall_chunk(_claim(1, "mm-8077", "fact"), ("[stale]",)) == "- [mm-8077] [stale] fact"
    assert context_hook._recall_chunk(_claim(2, None, "fact")) == "- fact"


def test_empty_recall_renders_nothing():
    lines, rendered = context_hook._render_recall_lines([], budget=500)
    assert context_hook._recall_text(lines) == "" and rendered == []


def test_the_budget_counts_the_id_and_the_instruction():
    claim = _claim(1, "mm-8077", "y" * 40)
    header = len("\n".join(context_hook.RECALL_HEADER))
    bullet = len(context_hook._recall_chunk(claim)) + 1
    fits = -(-(header + bullet) // 4)  # ceil to the 4-char token estimate
    assert context_hook._render_recall_lines([{"claim": claim}], budget=fits)[1]
    assert not context_hook._render_recall_lines([{"claim": claim}], budget=fits - 1)[1]


# --- AC2: strong detector paths and a separately labelled weak path -------------------------


TEXT = "The shared MemoryMaster server runs uvicorn on a SelectorEventLoop because the proactor loop drops the listener"


def test_detector_sees_a_cited_human_id():
    assert outcomes.detect_usage("mm-8077", TEXT, "Per [mm-8077] I moved the server.") == "human_id"


def test_detector_sees_the_session_start_claim_number():
    assert outcomes.detect_usage(None, TEXT, "see #148702 for why", claim_id=148702) == "claim_id"
    assert outcomes.detect_usage(None, TEXT, "see #1487021 and 148702", claim_id=148702) is None


def test_detector_keeps_the_verbatim_8gram_path():
    haystack = "note: the shared memorymaster server runs uvicorn on a selectoreventloop now"
    assert outcomes.detect_usage(None, TEXT, haystack) == "ngram"


def test_weak_path_needs_a_4gram_and_an_entity():
    paraphrase = "switched uvicorn on a SelectorEventLoop so the port survives resets"
    assert outcomes.detect_usage(None, TEXT, paraphrase) is None  # not strong
    assert outcomes.detect_weak_usage(TEXT, paraphrase) is True
    assert outcomes.detect_weak_usage(TEXT, "runs uvicorn on a server somewhere") is False  # 4-gram, no entity
    assert outcomes.detect_weak_usage(TEXT, "SelectorEventLoop mentioned alone") is False  # entity, no 4-gram


class _Ledger:
    def __init__(self, exposures):
        self.exposures, self.rows, self.marks = exposures, [], {}

    def exists(self):
        return True

    def get_watermark(self, name):
        return self.marks.get(name)

    def set_watermark(self, name, value):
        self.marks[name] = value

    def query(self, sql, params):
        return self.exposures

    def record_outcomes_checked(self, rows):
        self.rows.extend(rows)
        return len(rows)


def test_joiner_labels_strong_and_weak_outcomes_separately():
    ledger = _Ledger([
        {"decision_id": "d1", "ts": "2026-09-30T10:00:00+00:00", "item_ref": "claim:1"},
        {"decision_id": "d1", "ts": "2026-09-30T10:00:00+00:00", "item_ref": "claim:2"},
        {"decision_id": "d1", "ts": "2026-09-30T10:00:00+00:00", "item_ref": "claim:3"},
    ])
    claims = {"claim:1": ("mm-aaaa", "unrelated fact about backups"),
              "claim:2": (None, TEXT),
              "claim:3": ("mm-cccc", "nothing in common here at all")}
    turn = {"turn_id": "t1", "assistant_text": "Per [mm-aaaa]; also switched uvicorn on a SelectorEventLoop today."}
    inserted = outcomes.record_turn_usage(ledger, "s1", turn, observed_at="2026-09-30T10:05:00+00:00",
                                          lookup=claims.get)
    by_ref = {row.item_ref: row for row in ledger.rows}
    assert inserted == 2 and set(by_ref) == {"claim:1", "claim:2"}
    assert (by_ref["claim:1"].kind, by_ref["claim:1"].label_source) == ("used_in_turn", "detector")
    assert (by_ref["claim:2"].kind, by_ref["claim:2"].label_source) == ("used_in_turn_weak", "detector_weak")


def test_metrics_report_weak_use_beside_the_strong_rate():
    exposed = {"jev": {("d1", "claim:1"), ("d1", "claim:2"), ("d1", "claim:3"), ("d1", "claim:4")}}
    outcome_set = {("d1", "claim:1", "used_in_turn"), ("d1", "claim:2", "used_in_turn_weak"),
                   ("d1", "claim:1", "used_in_turn_weak")}
    arm = _exposure_use(exposed, outcome_set)["jev"]
    assert (arm["used"], arm["used_weak"]) == (1, 1)  # a strongly used item is not double counted as weak
    assert arm["rate"] == 0.25 and arm["rate_weak"] == 0.25


# --- review findings (independent review of T-0739) -------------------------------------------


def test_human_id_does_not_credit_the_parent_of_a_derived_or_suffixed_id():  # F3
    assert outcomes.detect_usage("mm-a3f8", TEXT, "Per [mm-a3f8.1] the fix holds.") is None
    assert outcomes.detect_usage("mm-a3f8", TEXT, "Per [mm-a3f8~2] the fix holds.") is None
    assert outcomes.detect_usage("mm-a3f8", TEXT, "That came from mm-a3f8.") == "human_id"


def test_claim_number_only_counts_as_a_free_standing_citation():  # F4
    for noise in ("color: #148;", "PR #148 merged", "issue #148 is open", "see [a](#148)", "x &#148; y", "x#148"):
        assert outcomes.detect_usage(None, TEXT, noise, claim_id=148) is None, noise
    for cite in ("see #148 for why", "(#148) says so", "#148 [bug/confirmed] applies"):
        assert outcomes.detect_usage(None, TEXT, cite, claim_id=148) == "claim_id", cite


def test_entity_tokens_keep_identifiers_and_drop_prose():  # F5
    found = outcomes.entity_tokens(
        "C:/x/y.py mcp_server.py v2.3.1 SelectorEventLoop HTTP T-0739 sha256 well-known and/or 2026 hello")
    assert found == {"c:/x/y.py", "mcp_server.py", "v2.3.1", "selectoreventloop", "http", "t-0739", "sha256"}


def test_prose_overlap_is_not_weak_usage():  # F5
    claim = "Retries are a well-known source of lock contention on the worker pool"
    # Shares a real 4-gram ("contention on the worker") and only the hyphenated prose word "well-known".
    turn = "No lock contention on the worker pool today; a well-known pattern in 2026."
    assert outcomes.detect_weak_usage(claim, turn) is False


def test_entity_extraction_is_linear_on_long_runs():  # F2 (minor): the old regex was quadratic
    import time

    started = time.perf_counter()
    outcomes.entity_tokens("acgt" * 25_000)  # 100 KB without separators
    assert time.perf_counter() - started < 2.0


def test_joiner_computes_turn_side_weak_features_once(monkeypatch):  # F2
    calls = {"entities": 0}
    real = outcomes.entity_tokens

    def counting(text):
        if len(text) > 10_000:  # the turn, not a claim
            calls["entities"] += 1
        return real(text)

    monkeypatch.setattr(outcomes, "entity_tokens", counting)
    exposures = [{"decision_id": f"d{i}", "ts": "2026-09-30T10:00:00+00:00", "item_ref": f"claim:{i % 20}"}
                 for i in range(40)]  # 20 claims, each exposed twice
    ledger = _Ledger(exposures)
    lookup = {f"claim:{i}": (f"mm-{i:04x}", f"unrelated note number {i} about kopia retention") for i in range(20)}
    turn = {"turn_id": "t", "assistant_text": "lorem ipsum dolor sit amet " * 4000}
    outcomes.record_turn_usage(ledger, "s", turn, observed_at="2026-09-30T10:05:00+00:00", lookup=lookup.get)
    assert calls["entities"] == 1
