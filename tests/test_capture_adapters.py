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
