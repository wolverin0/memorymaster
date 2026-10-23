from __future__ import annotations

import os
from pathlib import Path

import pytest

from memorymaster.capture import (
    CaptureRejected,
    InlineTextAdapter,
    LocalFileAdapter,
    ReferenceUrlAdapter,
    capture_batch,
    capture_input,
)


@pytest.fixture
def capture_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_ROOTS", f"fixture={tmp_path}")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_TRUST_MODE", "local-trusted")
    return tmp_path


def test_inline_text_is_redacted_and_hashed() -> None:
    envelope = InlineTextAdapter("note token=super-secret-value").capture()
    assert envelope.text != "note token=super-secret-value"
    assert envelope.warning_codes == ("sensitive_content_redacted",)
    assert len(envelope.content_hash) == 64


@pytest.mark.parametrize("extension", [None, ".md", ".html"])
def test_mixed_generated_spans_removed_before_html_parse(capture_root, extension):
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER
    raw = f"Authored prefix.\n{GENERATED_OPENER}\nExcluded claim.\n{GENERATED_CLOSER}\nAuthored suffix."
    if extension:
        path = capture_root / ("mixed" + extension)
        path.write_bytes(raw.encode("utf-8"))
        envelope = LocalFileAdapter(path).capture()
    else:
        envelope = InlineTextAdapter(raw).capture()
    assert "Excluded claim" not in envelope.text
    assert [span.text.strip() for span in envelope.retained_spans] == ["Authored prefix.", "Authored suffix."]
    assert all(raw[span.start:span.end].strip() == span.text.strip() for span in envelope.retained_spans)


def test_generated_only_inline_does_not_create_capture():
    from memorymaster.core.generated_artifacts import GENERATED_OPENER
    with pytest.raises(CaptureRejected) as exc:
        InlineTextAdapter(GENERATED_OPENER + "excluded").capture()
    assert exc.value.code == "generated_artifact"


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "template"])
def test_hidden_html_context_survives_generated_span_removal(capture_root, tag):
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = (f"<{tag}>hidden before " + GENERATED_OPENER + "generated" + GENERATED_CLOSER
           + f"This script-only decision must never become evidence.</{tag}><p>Authored note.</p>")
    path = capture_root / "hidden.html"
    path.write_text(raw, encoding="utf-8")
    envelope = LocalFileAdapter(path).capture()
    assert envelope.text == "Authored note."
    assert [span.text for span in envelope.retained_spans] == ["Authored note."]
    span = envelope.retained_spans[0]
    assert raw[span.start:span.end].endswith("<p>Authored note.</p>")


@pytest.mark.parametrize("tag", ["script", "style"])
@pytest.mark.parametrize("fragment", ["&am", "<!--", "<scr", "<", "</", "</scr", "</sty", "</script", "</style"])
def test_html_raw_text_fragment_does_not_poison_authored_suffix(tag, fragment):
    from memorymaster.capture.adapters import _retained_text
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = (f'<{tag}>const fragment = "{fragment}' + GENERATED_OPENER
           + 'generated' + GENERATED_CLOSER + f'";</{tag}><p>Authored</p>')
    assert _retained_text(raw, html=True)[0] == "Authored"


@pytest.mark.parametrize("fragment", ["&am", "</scr"])
def test_local_html_capture_retains_authored_suffix_after_hidden_js_string(capture_root, fragment):
    from memorymaster.capture.adapters import _html_text
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = ('<script>const fragment = "' + fragment + GENERATED_OPENER
           + 'generated' + GENERATED_CLOSER + '";</script><p>Authored</p>')
    assert _html_text(raw.encode("utf-8")) == "Authored"
    source = capture_root / "authored.html"
    source.write_bytes(raw.encode("utf-8"))
    envelope = LocalFileAdapter(source).capture()
    assert envelope.text == "Authored"
    assert envelope.retained_spans[0].start == raw.index(GENERATED_CLOSER) + len(GENERATED_CLOSER)
    assert envelope.retained_spans[0].end == len(raw)


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "template"])
def test_excluded_generated_closing_tag_cannot_expose_hidden_authored_data(tag):
    from memorymaster.capture.adapters import _retained_text
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = (f"<{tag}>hidden before " + GENERATED_OPENER + f"</{tag}>"
           + GENERATED_CLOSER + f"script-only sentence</{tag}><p>Authored</p>")
    text, _, _, _ = _retained_text(raw, html=True)
    assert text == "Authored", f"Excluded generated structure exposed hidden data: {text!r}"


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "template"])
def test_excluded_generated_opening_tag_cannot_hide_authored_suffix(tag):
    from memorymaster.capture.adapters import _retained_text
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = ("<p>Authored before</p>" + GENERATED_OPENER + f"<{tag}>"
           + GENERATED_CLOSER + "<p>Authored after</p>")
    text, _, _, _ = _retained_text(raw, html=True)
    assert text == "Authored before\nAuthored after", f"Excluded generated structure hid authored data: {text!r}"


@pytest.mark.parametrize(("prefix", "suffix"), [
    ("<scr", "ipt>hidden</script><p>After</p>"),
    ("<script>hidden</scr", "ipt><p>After</p>"),
    ("<script>hidden<", "/script><p>After</p>"),
    ("<script>hidden</", "script><p>After</p>"),
    ("<style>hidden</sty", "le><p>After</p>"),
    ("<style>hidden</style", "><p>After</p>"),
    ("<", "script>hidden</script><p>After</p>"),
    ("<p title='", "'>After</p>"),
    ("<p title='>'", ">After</p>"),
    ("<!-- pending >", " -->After"),
    ("&am", "p;After"),
    ("&#x2", "0;After"),
])
def test_html_token_crossing_generated_boundary_is_rejected(prefix, suffix):
    from memorymaster.capture.adapters import _retained_text
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = "<p>Before</p>" + prefix + GENERATED_OPENER + "generated" + GENERATED_CLOSER + suffix
    with pytest.raises(CaptureRejected) as exc:
        _retained_text(raw, html=True)
    assert exc.value.code == "ambiguous_generated_provenance"


def test_html_raw_text_closing_token_crossing_multiple_exclusions_is_rejected():
    from memorymaster.capture.adapters import _retained_text
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    gap = GENERATED_OPENER + "generated" + GENERATED_CLOSER
    raw = "<script>hidden</sc" + gap + "r" + gap + "ipt><p>After</p>"
    with pytest.raises(CaptureRejected) as exc:
        _retained_text(raw, html=True)
    assert exc.value.code == "ambiguous_generated_provenance"


def test_html_generated_mask_preserves_multiline_attribution(capture_root):
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = ("<p>Before &amp; caf\u00e9</p>\r\n" + GENERATED_OPENER
           + "\r\n</script><template>generated &amp; text\r\n" + GENERATED_CLOSER
           + "\r\n<p>After &#233;</p>")
    path = capture_root / "multiline.html"
    path.write_bytes(raw.encode("utf-8"))
    envelope = LocalFileAdapter(path).capture()
    assert envelope.text == "Before & caf\u00e9\nAfter \u00e9"
    first, second = envelope.retained_spans
    assert first.start == 0
    assert first.end == raw.index(GENERATED_OPENER)
    assert second.start == raw.index(GENERATED_CLOSER) + len(GENERATED_CLOSER)
    assert second.end == len(raw)
    assert envelope.excluded_spans == ((first.end, second.start),)
    assert envelope.warning_codes == ("generated_spans_excluded",)


def test_html_crossing_exclusions_preserves_entities_and_source_spans(capture_root):
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = ("<p>Before &amp; &#233;.</p><template>hidden " + GENERATED_OPENER
           + "generated" + GENERATED_CLOSER + " hidden</template><p>After &lt;note&gt;.</p>")
    path = capture_root / "mapped.html"
    path.write_text(raw, encoding="utf-8")
    envelope = LocalFileAdapter(path).capture()
    assert [span.text for span in envelope.retained_spans] == ["Before & é.", "After <note>."]
    first, second = envelope.retained_spans
    assert first.end <= raw.index(GENERATED_OPENER)
    assert second.start >= raw.index(GENERATED_CLOSER) + len(GENERATED_CLOSER)


def test_html_escaped_generated_markers_reject_ambiguous_evidence(capture_root):
    from html import escape

    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

    raw = "<p>Authored prefix.</p>" + escape(GENERATED_OPENER + "excluded" + GENERATED_CLOSER) + "<p>Authored suffix.</p>"
    path = capture_root / "encoded.html"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(CaptureRejected) as exc:
        LocalFileAdapter(path).capture()
    assert exc.value.code == "ambiguous_generated_provenance"


def test_generated_only_html_wrapper_does_not_create_capture(capture_root):
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER
    path = capture_root / "generated.html"
    path.write_bytes(f"<html><body>{GENERATED_OPENER}excluded{GENERATED_CLOSER}</body></html>".encode())
    with pytest.raises(CaptureRejected) as exc:
        LocalFileAdapter(path).capture()
    assert exc.value.code == "generated_artifact"


def test_local_bom_provenance_offsets_refer_to_original_text(capture_root):
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER
    raw = f"\ufeffAuthored.\r\n{GENERATED_OPENER}excluded{GENERATED_CLOSER}\r\nSuffix."
    path = capture_root / "bom.md"
    path.write_bytes(raw.encode("utf-8"))
    envelope = LocalFileAdapter(path).capture()
    assert all(raw[span.start:span.end] == span.text for span in envelope.retained_spans)


def test_url_only_is_visibly_awaiting_evidence() -> None:
    envelope = ReferenceUrlAdapter("https://example.com/article").capture()
    assert envelope.text is None
    assert envelope.blocked_code == "awaiting_evidence"


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/item",
        "https://user:password@example.com/item",
        "https://example.com/?api_key=secret-value",
    ],
)
def test_reference_url_fails_closed_for_unsafe_values(url: str) -> None:
    with pytest.raises(CaptureRejected):
        ReferenceUrlAdapter(url).capture()


def test_utf8_bom_markdown_round_trip(capture_root: Path) -> None:
    document = capture_root / "note.md"
    document.write_bytes(b"\xef\xbb\xbf# Heading\nBody")
    envelope = LocalFileAdapter(document).capture()
    assert envelope.locator == "fixture/note.md"
    assert envelope.text == "# Heading\nBody"
    assert str(capture_root) not in envelope.locator


def test_html_strips_script_and_style(capture_root: Path) -> None:
    document = capture_root / "page.html"
    document.write_text(
        "<html><style>hidden</style><body>Hello<script>secret()</script>World</body></html>",
        encoding="utf-8",
    )
    assert LocalFileAdapter(document).capture().text == "Hello\nWorld"


@pytest.mark.parametrize("extension", [".txt", ".md", ".html"])
def test_binary_masquerading_fails_closed(capture_root: Path, extension: str) -> None:
    document = capture_root / f"fake{extension}"
    document.write_bytes(b"prefix\x00binary")
    with pytest.raises(CaptureRejected, match="NUL"):
        LocalFileAdapter(document).capture()


def test_remote_mode_rejects_local_paths(capture_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = capture_root / "note.txt"
    document.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_TRUST_MODE", "team")
    with pytest.raises(CaptureRejected) as caught:
        LocalFileAdapter(document).capture()
    assert caught.value.code == "local_path_forbidden"


def test_unconfigured_roots_reject_local_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = tmp_path / "note.txt"
    document.write_text("hello", encoding="utf-8")
    monkeypatch.delenv("MEMORYMASTER_CAPTURE_ROOTS", raising=False)
    with pytest.raises(CaptureRejected) as caught:
        LocalFileAdapter(document).capture()
    assert caught.value.code == "capture_roots_unconfigured"


def test_symlink_escape_is_rejected(capture_root: Path, tmp_path_factory, monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("Windows symlink creation requires an operator-enabled privilege.")
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("outside", encoding="utf-8")
    link = capture_root / "link.txt"
    link.symlink_to(outside)
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_ROOTS", f"fixture={capture_root}")
    with pytest.raises(CaptureRejected) as caught:
        LocalFileAdapter(link).capture()
    assert caught.value.code == "path_outside_capture_roots"


def test_directory_and_archive_are_unsupported(capture_root: Path) -> None:
    with pytest.raises(CaptureRejected) as caught:
        LocalFileAdapter(capture_root).capture()
    assert caught.value.code == "directory_unsupported"
    archive = capture_root / "items.zip"
    archive.write_bytes(b"PK\x03\x04")
    with pytest.raises(CaptureRejected) as caught:
        LocalFileAdapter(archive).capture()
    assert caught.value.code == "archive_unsupported"


def test_image_signature_and_provider_route(capture_root: Path) -> None:
    image = capture_root / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    envelope = LocalFileAdapter(image).capture()
    assert envelope.mime_type == "image/png"
    assert envelope.provider_kind == "ocr"
    assert envelope.text is None


def test_fake_image_extension_is_rejected(capture_root: Path) -> None:
    image = capture_root / "photo.png"
    image.write_bytes(b"not-a-png")
    with pytest.raises(CaptureRejected) as caught:
        LocalFileAdapter(image).capture()
    assert caught.value.code == "binary_masquerading"


def test_capture_input_accepts_content_plus_provenance() -> None:
    envelope = capture_input(text="article body", source_uri="https://example.com/article")
    assert envelope.text == "article body"
    assert envelope.source_uri == "https://example.com/article"


def test_capture_input_rejects_ambiguous_sources(capture_root: Path) -> None:
    file_path = capture_root / "note.txt"
    file_path.write_text("text", encoding="utf-8")
    with pytest.raises(CaptureRejected):
        capture_input(text="text", path=file_path)
    with pytest.raises(CaptureRejected):
        capture_input(text="text", path=file_path, source_uri="https://example.com")


def test_batch_limit() -> None:
    with pytest.raises(CaptureRejected) as caught:
        capture_batch([InlineTextAdapter("x")] * 101)
    assert caught.value.code == "batch_too_large"
