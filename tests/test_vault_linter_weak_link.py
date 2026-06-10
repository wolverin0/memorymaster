"""Regression: _detect_orphans must flag single-mention subjects as weak links.

WHY: lint-vault exists to surface knowledge-base health problems. A claim whose
subject is mentioned exactly once is a weak link the steward should review. The
original code put that check behind an unreachable ``elif c["subject"] not in
all_subjects`` guard — but ``all_subjects`` contains every non-empty subject, so
the branch never fired and lint-vault reported a false 'clean'. These tests
anchor on the requirement (single-mention => weak_link, multi-mention => not),
not on the implementation, so they stay valid if the detection is rewritten.
"""
from __future__ import annotations

from memorymaster.knowledge.vault_linter import _detect_orphans


def _claim(cid: int, subject: str) -> dict:
    return {
        "id": cid,
        "human_id": f"h{cid}",
        "subject": subject,
        "predicate": "is",
        "text": f"{subject} text",
    }


def test_single_mention_subject_flagged_as_weak_link():
    claims = [_claim(1, "alpha"), _claim(2, "beta"), _claim(3, "beta")]
    weak = [o for o in _detect_orphans(claims) if o["type"] == "weak_link"]
    subjects = {o["subject"] for o in weak}
    assert subjects == {"alpha"}, weak


def test_multi_mention_subject_not_flagged():
    claims = [_claim(1, "beta"), _claim(2, "beta")]
    weak = [o for o in _detect_orphans(claims) if o["type"] == "weak_link"]
    assert weak == [], weak


def test_missing_subject_and_predicate_is_orphan_not_weak_link():
    claims = [{"id": 9, "human_id": "h9", "subject": "", "predicate": "", "text": "x"}]
    out = _detect_orphans(claims)
    assert any(o["type"] == "orphan" for o in out), out
    assert not any(o["type"] == "weak_link" for o in out), out
