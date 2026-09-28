"""A recalled ID is useful only if its evidence survives the delivered text."""
from types import SimpleNamespace
import io
import sys

import pytest

from memorymaster.recall import context_hook


def row(cid, text):
    return {"claim": SimpleNamespace(id=cid, text=text, wiki_article=None)}


def render(rows, budget):
    lines, included = context_hook._render_recall_lines(rows, budget)
    return context_hook._recall_text(lines), [item["claim"].id for item in included]


def test_relevant_tail_is_not_silently_removed():
    text = "Background context. " * 20 + "Required setting: journal_mode=WAL."
    output, ids = render([row(1, text)], 200)
    assert ids == [1]
    assert text in output


def test_unicode_evidence_survives_rendering():
    text = "La contraseña no se registra. Código de estado: 完成."
    output, ids = render([row(1, text)], 100)
    assert ids == [1]
    assert text in output


def test_oversized_claim_does_not_hide_a_later_fitting_claim():
    output, ids = render([row(1, "long evidence " * 30), row(2, "Use WAL.")], 12)
    assert ids == [2]
    assert "Use WAL." in output


@pytest.mark.parametrize("budget", [0, 1, 5, 12, 20, 40, 100])
def test_budget_covers_header_separators_and_flags(budget):
    rows = [row(i, "answer " * i) for i in range(1, 6)]
    lines, included = context_hook._render_recall_lines(
        rows, budget, {i: ("[CHECK]",) for i in range(1, 6)}
    )
    output = context_hook._recall_text(lines)
    assert (len(output) + 3) // 4 <= budget
    for item in included:
        assert item["claim"].text in output


def test_jev_capacity_includes_framing_and_flags(monkeypatch):
    from memorymaster.recall import jev_surfaces as jev

    rows = [row(i, "evidence " * 4) for i in range(1, 5)]
    budget = 35
    observed = {}

    def choose(_query, candidates, **kwargs):
        observed.update(kwargs)
        ids = [candidate.claim_id for candidate in candidates][:kwargs["k_cap"]]
        return SimpleNamespace(claim_ids=ids, labels={cid: jev.WORST_CASE_LABELS for cid in ids})

    monkeypatch.setattr(jev, "surface_active", lambda _surface: True)
    monkeypatch.setattr(jev, "decide_recall", choose)
    lines, included = context_hook._render_recall_lines(rows, budget)
    final_lines, final_rows = context_hook._jev_recall_rendering(
        "evidence", rows, lines, included, budget, {}, {}
    )
    assert len(final_rows) == observed["k_cap"]
    assert (len(context_hook._recall_text(final_lines)) + 3) // 4 <= budget


def test_jev_oversized_candidate_does_not_zero_out_fitting_pool(monkeypatch):
    from memorymaster.recall import jev_surfaces as jev

    rows = [row(1, "evidence " * 100), row(2, "Use WAL.")]
    budget = 35
    observed = {}

    def choose(_query, candidates, **kwargs):
        observed["pool"] = [candidate.claim_id for candidate in candidates]
        observed.update(kwargs)
        return SimpleNamespace(claim_ids=[2], labels={2: jev.WORST_CASE_LABELS})

    monkeypatch.setattr(jev, "surface_active", lambda _surface: True)
    monkeypatch.setattr(jev, "decide_recall", choose)
    lines, included = context_hook._render_recall_lines(rows, budget)
    final_lines, final_rows = context_hook._jev_recall_rendering(
        "evidence", rows, lines, included, budget, {}, {}
    )
    assert observed["pool"] == [2]
    assert observed["k_cap"] == 1
    assert [item["claim"].id for item in final_rows] == [2]
    assert (len(context_hook._recall_text(final_lines)) + 3) // 4 <= budget


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
def test_cli_handles_unicode_at_the_output_boundary(monkeypatch, encoding):
    from memorymaster.surfaces.cli_handlers_curation import _handle_recall

    text = "Español: 完成"
    monkeypatch.setattr(context_hook, "recall", lambda *args, **kwargs: text)
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding)
    args = SimpleNamespace(query="state", budget=100, output_format="text")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stream)
        assert _handle_recall(args, None, None, ":memory:") == 0
        stream.flush()
    result = buffer.getvalue().decode(encoding)
    assert "Español" in result
    assert ("完成" if encoding == "utf-8" else "\\u5b8c\\u6210") in result
