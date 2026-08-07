"""Deterministic capture adapters for text, references, documents, images, and audio."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import mimetypes
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from memorymaster.core.security import (
    sanitize_persisted_text,
    validate_persisted_metadata,
)

INLINE_LIMIT = 2 * 1024 * 1024
DOCUMENT_LIMIT = 25 * 1024 * 1024
EXTRACTED_TEXT_LIMIT = 2 * 1024 * 1024
BATCH_LIMIT = 100

_TEXT_EXTENSIONS = frozenset({".txt", ".md"})
_HTML_EXTENSIONS = frozenset({".html", ".htm"})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"})
_AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"})
_ARCHIVE_EXTENSIONS = frozenset({".zip", ".tar", ".tgz", ".gz", ".7z", ".rar"})
_ROOT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class CaptureRejected(ValueError):
    """A fail-closed capture error with a stable actionable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class CaptureRetryable(RuntimeError):
    """A transient capture failure with a stable operator-facing code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CaptureEnvelope:
    """Normalized input; ``resolved_path`` is transient and must not be persisted."""

    source_kind: str
    content_hash: str
    content_type: str
    mime_type: str
    locator: str
    text: str | None
    source_uri: str | None = None
    evidence_type: str | None = None
    provider_kind: str | None = None
    blocked_code: str | None = None
    warning_codes: tuple[str, ...] = ()
    resolved_path: str | None = None
    producer: str | None = None
    producer_external_id_hash: str | None = None
    producer_session_hash: str | None = None
    producer_turn_id: str | None = None
    producer_metadata: tuple[tuple[str, str], ...] = ()


class CaptureAdapter(Protocol):
    def capture(self) -> CaptureEnvelope:
        """Return a normalized capture envelope or raise ``CaptureRejected``."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bounded_text(text: str) -> tuple[str, tuple[str, ...]]:
    payload = text.encode("utf-8")
    if len(payload) > EXTRACTED_TEXT_LIMIT:
        raise CaptureRejected("extracted_text_too_large", "Extracted text exceeds 2 MiB.")
    sanitized, findings = sanitize_persisted_text(text)
    warnings = ("sensitive_content_redacted",) if findings else ()
    return sanitized, warnings


def _validate_source_uri(source_uri: str | None) -> str | None:
    if source_uri is None:
        return None
    value = source_uri.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CaptureRejected("invalid_source_uri", "source_uri must be an HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise CaptureRejected("secret_bearing_url", "Credentials are not allowed in source_uri.")
    try:
        validate_persisted_metadata({"source_uri": value})
    except ValueError as exc:
        raise CaptureRejected("secret_bearing_url", "Secret-shaped URL fields are not allowed.") from exc
    return value


def _configured_roots() -> list[tuple[str, Path]]:
    raw = os.environ.get("MEMORYMASTER_CAPTURE_ROOTS", "")
    roots: list[tuple[str, Path]] = []
    for entry in raw.split(";"):
        name, separator, path = entry.strip().partition("=")
        if not separator or not _ROOT_NAME.fullmatch(name) or not path.strip():
            continue
        try:
            roots.append((name, Path(path.strip()).resolve(strict=True)))
        except OSError as exc:
            raise CaptureRejected(
                "capture_root_unavailable", f"Configured capture root '{name}' is unavailable."
            ) from exc
    return sorted(roots, key=lambda item: len(str(item[1])), reverse=True)


def _under_root(path: Path, roots: list[tuple[str, Path]]) -> tuple[str, Path] | None:
    for name, root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        locator = name if not relative.parts else f"{name}/{'/'.join(relative.parts)}"
        return locator, root
    return None


def _trusted_file(path: str | Path) -> tuple[Path, str]:
    mode = os.environ.get("MEMORYMASTER_CAPTURE_TRUST_MODE", "local-trusted").strip().lower()
    if mode not in {"private", "local-trusted"}:
        raise CaptureRejected("local_path_forbidden", "Local paths require private/local-trusted mode.")
    roots = _configured_roots()
    if not roots:
        raise CaptureRejected("capture_roots_unconfigured", "MEMORYMASTER_CAPTURE_ROOTS is required.")
    requested = Path(path)
    if requested.is_symlink():
        resolved = requested.resolve(strict=True)
    else:
        resolved = requested.resolve(strict=True)
    matched = _under_root(resolved, roots)
    if matched is None:
        raise CaptureRejected("path_outside_capture_roots", "Path resolves outside configured roots.")
    if not resolved.is_file():
        code = "directory_unsupported" if resolved.is_dir() else "path_not_file"
        raise CaptureRejected(code, "Capture accepts one regular file.")
    return resolved, matched[0]


def resolve_local_locator(locator: str) -> Path:
    """Expand a persisted root-relative locator under the current capture roots."""
    root_name, separator, relative = locator.partition("/")
    for name, root in _configured_roots():
        if name != root_name:
            continue
        resolved = (root / relative).resolve(strict=True) if separator else root
        if _under_root(resolved, [(name, root)]) is None or not resolved.is_file():
            raise CaptureRejected("path_outside_capture_roots", "Locator escaped its capture root.")
        return resolved
    raise CaptureRejected("capture_root_unavailable", "Locator root is not configured.")


def _read_document(path: Path) -> bytes:
    size = path.stat().st_size
    if size > DOCUMENT_LIMIT:
        raise CaptureRejected("document_too_large", "Local document exceeds 25 MiB.")
    return path.read_bytes()


def _decode_utf8(payload: bytes) -> str:
    if b"\x00" in payload:
        raise CaptureRejected("binary_masquerading", "Text document contains NUL bytes.")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CaptureRejected("invalid_utf8", "Text and Markdown must be UTF-8.") from exc


def _html_text(payload: bytes) -> str:
    parser = _VisibleTextParser()
    parser.feed(_decode_utf8(payload))
    return "\n".join(parser.parts)


def _pdf_text(payload: bytes) -> str:
    if not payload.startswith(b"%PDF-"):
        raise CaptureRejected("binary_masquerading", "PDF signature is missing.")
    if importlib.util.find_spec("pypdf") is None:
        raise CaptureRejected("parser_unavailable_pdf", "Install memorymaster[capture] for PDF.")
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(payload))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise CaptureRejected("malformed_pdf", "PDF parsing failed.") from exc


def _docx_text(payload: bytes) -> str:
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise CaptureRejected("binary_masquerading", "DOCX document part is missing.")
    except BadZipFile as exc:
        raise CaptureRejected("binary_masquerading", "DOCX ZIP signature is invalid.") from exc
    if importlib.util.find_spec("docx") is None:
        raise CaptureRejected("parser_unavailable_docx", "Install memorymaster[capture] for DOCX.")
    from docx import Document

    try:
        document = Document(io.BytesIO(payload))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception as exc:
        raise CaptureRejected("malformed_docx", "DOCX parsing failed.") from exc


def _image_mime(extension: str, payload: bytes) -> str | None:
    signatures = {
        ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
        ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
        ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
        ".gif": (b"GIF8", "image/gif"),
        ".webp": (b"RIFF", "image/webp"),
        ".tif": (b"II*\x00", "image/tiff"),
        ".tiff": (b"MM\x00*", "image/tiff"),
    }
    signature, mime = signatures.get(extension, (b"", ""))
    if signature and payload.startswith(signature):
        if extension == ".webp" and payload[8:12] != b"WEBP":
            return None
        return mime
    return None


def _audio_mime(extension: str, payload: bytes) -> str | None:
    if extension == ".mp3" and (payload.startswith(b"ID3") or payload[:2] in {b"\xff\xfb", b"\xff\xf3"}):
        return "audio/mpeg"
    if extension == ".wav" and payload.startswith(b"RIFF") and payload[8:12] == b"WAVE":
        return "audio/wav"
    if extension in {".m4a"} and payload[4:8] == b"ftyp":
        return "audio/mp4"
    if extension in {".ogg"} and payload.startswith(b"OggS"):
        return "audio/ogg"
    if extension == ".flac" and payload.startswith(b"fLaC"):
        return "audio/flac"
    if extension == ".webm" and payload.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    return None


@dataclass(frozen=True, slots=True)
class InlineTextAdapter:
    text: str
    source_uri: str | None = None

    def capture(self) -> CaptureEnvelope:
        raw = self.text.encode("utf-8")
        if len(raw) > INLINE_LIMIT:
            raise CaptureRejected("inline_text_too_large", "Inline text exceeds 2 MiB.")
        text, warnings = _bounded_text(self.text)
        uri = _validate_source_uri(self.source_uri)
        return CaptureEnvelope(
            source_kind="inline",
            content_hash=_digest(raw),
            content_type="text",
            mime_type="text/plain",
            locator=uri or f"inline:{_digest(raw)[:16]}",
            text=text,
            source_uri=uri,
            evidence_type="text",
            warning_codes=warnings,
        )


@dataclass(frozen=True, slots=True)
class ReferenceUrlAdapter:
    source_uri: str

    def capture(self) -> CaptureEnvelope:
        uri = _validate_source_uri(self.source_uri)
        assert uri is not None
        return CaptureEnvelope(
            source_kind="reference",
            content_hash=_digest(uri.encode("utf-8")),
            content_type="reference",
            mime_type="text/uri-list",
            locator=uri,
            text=None,
            source_uri=uri,
            blocked_code="awaiting_evidence",
        )


@dataclass(frozen=True, slots=True)
class LocalFileAdapter:
    path: str | Path
    source_uri: str | None = None

    def _media_envelope(
        self, resolved: Path, locator: str, payload: bytes, extension: str
    ) -> CaptureEnvelope:
        mime = _image_mime(extension, payload)
        provider = "ocr"
        evidence_type = "ocr"
        if mime is None:
            mime = _audio_mime(extension, payload)
            provider = "transcription"
            evidence_type = "transcript"
        if mime is None:
            raise CaptureRejected("binary_masquerading", "File signature does not match extension.")
        return self._envelope(
            resolved, locator, payload, mime, None, evidence_type, provider
        )

    def _envelope(
        self,
        resolved: Path,
        locator: str,
        payload: bytes,
        mime: str,
        text: str | None,
        evidence_type: str | None,
        provider: str | None = None,
    ) -> CaptureEnvelope:
        bounded, warnings = _bounded_text(text) if text is not None else (None, ())
        return CaptureEnvelope(
            source_kind="file",
            content_hash=_digest(payload),
            content_type=mime.split("/", 1)[0],
            mime_type=mime,
            locator=locator,
            text=bounded,
            source_uri=_validate_source_uri(self.source_uri),
            evidence_type=evidence_type,
            provider_kind=provider,
            warning_codes=warnings,
            resolved_path=str(resolved),
        )

    def capture(self) -> CaptureEnvelope:
        resolved, locator = _trusted_file(self.path)
        extension = resolved.suffix.lower()
        if extension in _ARCHIVE_EXTENSIONS:
            raise CaptureRejected("archive_unsupported", "Archives are unsupported in capture v1.")
        payload = _read_document(resolved)
        if extension in _TEXT_EXTENSIONS:
            return self._envelope(resolved, locator, payload, "text/plain", _decode_utf8(payload), "text")
        if extension in _HTML_EXTENSIONS:
            return self._envelope(resolved, locator, payload, "text/html", _html_text(payload), "text")
        if extension == ".pdf":
            return self._envelope(resolved, locator, payload, "application/pdf", _pdf_text(payload), "text")
        if extension == ".docx":
            return self._envelope(
                resolved,
                locator,
                payload,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                _docx_text(payload),
                "text",
            )
        if extension in _IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS:
            return self._media_envelope(resolved, locator, payload, extension)
        guessed = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        raise CaptureRejected("unsupported_content_type", f"Unsupported capture type: {guessed}.")


def capture_input(
    *,
    text: str | None = None,
    path: str | Path | None = None,
    source_uri: str | None = None,
) -> CaptureEnvelope:
    """Normalize exactly one text, file, or URL-only capture."""
    if text is not None and path is not None:
        raise CaptureRejected("invalid_capture_input", "Text and file are mutually exclusive.")
    if text is None and path is None and source_uri is None:
        raise CaptureRejected("invalid_capture_input", "Provide text, one file, or one URL.")
    if text is not None:
        return InlineTextAdapter(text, source_uri).capture()
    if path is not None:
        return LocalFileAdapter(path, source_uri).capture()
    assert source_uri is not None
    return ReferenceUrlAdapter(source_uri).capture()


def capture_batch(adapters: list[CaptureAdapter]) -> list[CaptureEnvelope]:
    if len(adapters) > BATCH_LIMIT:
        raise CaptureRejected("batch_too_large", "Capture batches are limited to 100 items.")
    return [adapter.capture() for adapter in adapters]
