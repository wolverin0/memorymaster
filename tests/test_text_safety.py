"""UTF-8 boundaries preserve Unicode scalars and replace surrogate code points."""
import pytest


@pytest.mark.parametrize(("text", "expected"), [
    ("", ""),
    ("ASCII\n\r\t\x00", "ASCII\n\r\t\x00"),
    ("\u00f1 \u4e2d\u6587 \U0001f680 e\u0301 \ufffd", "\u00f1 \u4e2d\u6587 \U0001f680 e\u0301 \ufffd"),
    ("\ud800", "\ufffd"),
    ("\udc8f", "\ufffd"),
    ("a\udfff\ud800z", "a\ufffd\ufffdz"),
])
def test_sanitize_text_preserves_every_non_surrogate(text, expected):
    from memorymaster.core.text_safety import sanitize_text

    clean = sanitize_text(text)
    assert clean == expected
    assert sanitize_text(clean) == clean
    clean.encode("utf-8")


def test_fit_brief_cuts_at_a_whole_line_inside_the_byte_budget_and_says_so():
    from memorymaster.core.text_safety import MAX_BRIEF_BYTES, fit_brief

    short = "# Project\n- one\n- two"
    assert fit_brief(short) == short  # inside the budget: untouched
    lines = [f"- fact number {i:03d} \u00e9\u00e9" for i in range(200)]
    out = fit_brief("\n".join(lines))
    assert len(out.encode("utf-8")) <= MAX_BRIEF_BYTES
    assert out.endswith("\n[... truncated]")
    kept = out.splitlines()[:-1]
    assert kept and kept == lines[: len(kept)]  # whole lines only, in order
    one_long = fit_brief("x" * 5000, limit=100)  # a single oversized line keeps the byte-safe cut
    assert one_long == "x" * 100
    assert len(fit_brief("\u00e9" * 500, limit=101).encode("utf-8")) <= 101
