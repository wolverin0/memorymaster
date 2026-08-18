"""The same question must score the same whichever language it is asked in.

``_lexical_score`` divides the query/claim token overlap by the number of query
tokens, so every filler word the ranker fails to discard dilutes the score of
the words that carry the meaning. The ranker's own stopword list was 34 entries
of English only, while ``recall_tokenizer`` — used by the recall hook on the
very same prompts — already carried 250 covering both languages.

The measured effect before this fix, against a claim about backups:

    "what happens with the backups"   lex = 0.300
    "que pasa con los backups"        lex = 0.135

Same question, same claim, less than half the score, purely because "que",
"con" and "los" were counted as meaningful terms. The operator writes prompts
in Spanish and the recall hook turns those prompts into queries, so this
penalised the everyday path rather than an edge case.
"""
from __future__ import annotations

import pytest

from memorymaster.recall.retrieval import _lexical_score, _tokens


class _Claim:
    """Minimal stand-in — _lexical_score only reads these four fields."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.normalized_text = None
        self.subject = None
        self.object_value = None


CLAIM = _Claim("MemoryMaster backups are stored on the E drive and trimmed weekly.")

# (english, spanish) phrasings of the same question.
PAIRS = [
    ("where are the backups", "donde estan los backups"),
    ("what happens with the backups", "que pasa con los backups"),
    ("how do I check the backups", "como reviso los backups"),
]


@pytest.mark.parametrize("english,spanish", PAIRS)
def test_spanish_phrasing_scores_like_the_english_one(english, spanish):
    en = _lexical_score(english, CLAIM)
    es = _lexical_score(spanish, CLAIM)
    assert es == pytest.approx(en, abs=0.05), (
        f"{spanish!r} scored {es:.3f} against {english!r} at {en:.3f} — the "
        f"ranker is counting Spanish filler words as meaningful terms"
    )


@pytest.mark.parametrize("english,spanish", PAIRS)
def test_filler_words_are_discarded_in_both_languages(english, spanish):
    """The token sets must be the same size, not merely score similarly."""
    assert len(_tokens(spanish)) == len(_tokens(english)), (
        f"{sorted(_tokens(spanish))} vs {sorted(_tokens(english))}"
    )


def test_the_meaningful_term_survives():
    """Discarding filler must not discard the word that carries the query."""
    assert "backups" in _tokens("que pasa con los backups")
    assert "backups" in _tokens("what happens with the backups")


def test_a_bare_term_still_scores_highest():
    """A wordy question costs the same in either language, never more.

    The cost of asking in sentences rather than keywords is legitimate — the
    extra words really are extra terms to match. What was not legitimate was
    paying that cost twice for asking in Spanish.
    """
    bare = _lexical_score("backups", CLAIM)
    spanish = _lexical_score("que pasa con los backups", CLAIM)
    english = _lexical_score("what happens with the backups", CLAIM)
    assert bare >= spanish
    assert spanish == pytest.approx(english, abs=0.01)
