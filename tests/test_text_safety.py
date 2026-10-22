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
