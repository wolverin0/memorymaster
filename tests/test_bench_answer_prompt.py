"""Guards the LongMemEval answerer prompt against over-refusal regressions.

WHY: The bench's `answer_question` prompt drives the QA-accuracy metric. An
earlier wording ("If the context does not contain the answer, say I don't
know.") biased the answerer toward refusing whenever the fact was not stated
verbatim, depressing QA accuracy on questions whose answer WAS present but
phrased differently. The contract for this prompt is now: extract the most
specific relevant fact from the retrieved context, and only abstain when the
answer is genuinely absent. These tests anchor on that requirement (intent),
not on the exact prose, so they stay meaningful if the wording is polished
further — what must hold is that the prompt instructs specificity and scopes
the abstention to genuine absence rather than mere non-verbatim phrasing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "bench_longmemeval",
    Path(__file__).resolve().parent / "bench_longmemeval.py",
)
assert _SPEC and _SPEC.loader
bench = importlib.util.module_from_spec(_SPEC)
# Register before exec so the module's frozen @dataclass decorators can resolve
# their own __module__ via sys.modules during class creation.
sys.modules[_SPEC.name] = bench
_SPEC.loader.exec_module(bench)


class _PromptCapturingJudge:
    """Captures the prompt instead of calling any provider."""

    def __init__(self) -> None:
        self.prompt: str | None = None

    def complete(self, prompt, *, max_tokens, temperature=0.0):
        self.prompt = prompt
        return bench.LLMResponse(text="captured", model="test", provider="test")


def _build_prompt() -> str:
    judge = _PromptCapturingJudge()
    bench.answer_question("What city did the user move to?", ["ctx-a", "ctx-b"], judge)
    assert judge.prompt is not None
    return judge.prompt


def test_prompt_requests_specific_fact() -> None:
    # Intent: the answerer must be told to surface the most specific relevant
    # fact, not just "answer the question" generically.
    prompt = _build_prompt().lower()
    assert "most specific" in prompt
    assert "relevant fact" in prompt


def test_abstention_is_scoped_to_genuine_absence() -> None:
    # Intent: abstention ("I don't know") must be gated on the answer being
    # genuinely absent — NOT the blanket "if the context does not contain the
    # answer" wording that triggered over-refusal on non-verbatim matches.
    prompt = _build_prompt().lower()
    assert "genuinely absent" in prompt
    assert "if the context does not contain the answer" not in prompt


def test_prompt_still_includes_question_and_contexts() -> None:
    # Guard the structural contract so the softening did not drop inputs.
    judge = _PromptCapturingJudge()
    bench.answer_question("Q?", ["alpha-ctx", "beta-ctx"], judge)
    assert judge.prompt is not None
    assert "Q?" in judge.prompt
    assert "alpha-ctx" in judge.prompt
    assert "beta-ctx" in judge.prompt
