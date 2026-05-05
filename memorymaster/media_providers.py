"""Real provider adapters for Atlas Inbox media processing.

These plug into the existing ``TranscriptionProvider`` / ``OcrProvider``
``Protocol``s in ``memorymaster.media_processing``. They are **opt-in** —
``MockTranscriptionProvider`` / ``MockOcrProvider`` remain the defaults.
LifeAgent (or any consumer) imports these classes and passes the instance
into ``process_transcription`` / ``process_ocr``.

Failure handling: each adapter raises ``RuntimeError`` on failure. The
existing ``_process_media`` helper in ``media_processing`` catches the
exception and records a ``media_process`` event with details
``media_process_failed`` — the source item is preserved.

Optional dependencies:
- ``OpenAIWhisperTranscriptionProvider`` uses stdlib only (urllib + manual
  multipart). No extra package required. Reads ``OPENAI_API_KEY`` and
  ``OPENAI_BASE_URL`` (default ``https://api.openai.com/v1``).
- ``TesseractOcrProvider`` requires the optional ``pytesseract`` package
  AND a system ``tesseract`` binary on PATH. Importing this module does
  NOT require either — the dependency is checked lazily inside
  ``extract()`` so the package still imports cleanly without them.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from memorymaster.media_processing import EvidenceResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenAI Whisper transcription
# ---------------------------------------------------------------------------


class OpenAIWhisperTranscriptionProvider:
    """Real transcription via OpenAI's audio transcription endpoint.

    Compatible with any OpenAI-compatible endpoint (OpenAI, Azure OpenAI,
    OpenRouter, local llama.cpp servers that implement the API). The base
    URL is read from ``OPENAI_BASE_URL`` so consumers can route through a
    proxy or alternate provider without code changes.
    """

    provider_name = "openai-whisper"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "whisper-1",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    def transcribe(self, path: str) -> EvidenceResult:
        if not self._api_key:
            raise RuntimeError(
                "OpenAIWhisperTranscriptionProvider requires OPENAI_API_KEY (or api_key= kwarg)."
            )
        media_path = Path(path)
        if not media_path.is_file():
            raise RuntimeError(f"audio file not found: {media_path}")

        mime = mimetypes.guess_type(media_path.name)[0] or "audio/mpeg"
        with media_path.open("rb") as fh:
            audio_bytes = fh.read()

        boundary = f"----memorymaster-{uuid.uuid4().hex}"
        body = self._encode_multipart(
            boundary=boundary,
            fields={"model": self._model, "response_format": "json"},
            file_field=("file", media_path.name, mime, audio_bytes),
        )
        url = f"{self._base_url}/audio/transcriptions"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(
                f"OpenAI transcription HTTP {exc.code}: {err_body[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI transcription network error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI transcription response not JSON: {exc}") from exc

        text = str(payload.get("text", "")).strip()
        if not text:
            raise RuntimeError("OpenAI transcription returned empty text.")
        return EvidenceResult(
            evidence_type="transcript",
            text=text,
            provider=self.provider_name,
            media_path=str(media_path),
            confidence=0.9,
            payload_json={"model": self._model, "raw": payload},
        )

    @staticmethod
    def _encode_multipart(
        *,
        boundary: str,
        fields: dict[str, str],
        file_field: tuple[str, str, str, bytes],
    ) -> bytes:
        """Encode multipart/form-data with one file part. stdlib-only."""
        crlf = b"\r\n"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(f"--{boundary}".encode("ascii"))
            chunks.append(f'Content-Disposition: form-data; name="{name}"'.encode("ascii"))
            chunks.append(b"")
            chunks.append(str(value).encode("utf-8"))
        field_name, filename, mime, payload = file_field
        chunks.append(f"--{boundary}".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime}".encode("ascii"))
        chunks.append(b"")
        chunks.append(payload)
        chunks.append(f"--{boundary}--".encode("ascii"))
        chunks.append(b"")
        return crlf.join(chunks)


# ---------------------------------------------------------------------------
# Tesseract OCR
# ---------------------------------------------------------------------------


class TesseractOcrProvider:
    """Local-first OCR via tesseract.

    Requires the ``pytesseract`` package AND a system ``tesseract`` binary on
    PATH. Both are checked lazily inside ``extract()``. Failures (missing
    package, binary not found, file not readable) raise ``RuntimeError`` so
    the existing ``_process_media`` records a structured failure event
    rather than crashing the caller.

    For multi-language support pass ``lang="eng+spa"`` etc. — defaults to
    English since most Atlas use cases are short receipts/messages where
    Tesseract's default works adequately.
    """

    provider_name = "tesseract"

    def __init__(self, *, lang: str = "eng", config: str | None = None) -> None:
        self._lang = lang
        self._config = config or ""

    def extract(self, path: str) -> EvidenceResult:
        try:
            import pytesseract  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised in env without dep
            raise RuntimeError(
                "TesseractOcrProvider requires the 'pytesseract' package. "
                "Install with: pip install pytesseract  (also requires a system 'tesseract' binary)."
            ) from exc
        media_path = Path(path)
        if not media_path.is_file():
            raise RuntimeError(f"image file not found: {media_path}")

        try:
            text = pytesseract.image_to_string(
                str(media_path),
                lang=self._lang,
                config=self._config,
            ).strip()
        except FileNotFoundError as exc:  # tesseract binary missing
            raise RuntimeError(
                "Tesseract binary not found. Install: "
                "https://github.com/tesseract-ocr/tesseract"
            ) from exc
        except pytesseract.TesseractError as exc:
            raise RuntimeError(f"Tesseract error: {exc}") from exc

        if not text:
            # Empty OCR output: legitimate (image had no text) but record it.
            text = ""

        return EvidenceResult(
            evidence_type="ocr",
            text=text,
            provider=self.provider_name,
            media_path=str(media_path),
            confidence=0.85 if text else 0.0,
            payload_json={"lang": self._lang},
        )


# ---------------------------------------------------------------------------
# Provider factory (used by CLI handlers)
# ---------------------------------------------------------------------------

_TRANSCRIPTION_PROVIDERS: dict[str, Any] = {}
_OCR_PROVIDERS: dict[str, Any] = {}


def get_transcription_provider(name: str) -> Any:
    """Return a transcription provider instance by name. Mock + openai supported."""
    name = name.strip().lower()
    if name in ("mock", "mock-transcription"):
        from memorymaster.media_processing import MockTranscriptionProvider
        return MockTranscriptionProvider()
    if name in ("openai", "openai-whisper", "whisper"):
        return OpenAIWhisperTranscriptionProvider()
    raise ValueError(
        f"Unknown transcription provider '{name}'. Supported: mock, openai."
    )


def get_ocr_provider(name: str) -> Any:
    """Return an OCR provider instance by name. Mock + tesseract supported."""
    name = name.strip().lower()
    if name in ("mock", "mock-ocr"):
        from memorymaster.media_processing import MockOcrProvider
        return MockOcrProvider()
    if name in ("tesseract", "tesseract-ocr", "local"):
        return TesseractOcrProvider()
    raise ValueError(
        f"Unknown OCR provider '{name}'. Supported: mock, tesseract."
    )


__all__ = [
    "OpenAIWhisperTranscriptionProvider",
    "TesseractOcrProvider",
    "get_transcription_provider",
    "get_ocr_provider",
]
