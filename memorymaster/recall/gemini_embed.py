"""Hardened Gemini Embedding API client (stdlib only, no google-genai).

Everything that can leave the process goes through ``_egress``: a secrets-only
redaction (operator decision 2026-11-01: private IPs, emails, phones and paths MAY be
sent; credentials may not). Text that still holds a credential after redaction is
blocked and produces no request. The HTTP function ``_post`` only accepts the
``_Prepared`` objects ``_egress`` builds.

Failure policy: every failure raises ``EmbedUnavailable(reason)`` (an
``EncoderUnavailable``), never fake vectors. Reasons: api, breaker, capped, rate,
blocked, auth, ledger. The key travels only in the ``x-goog-api-key`` header and never
appears in a URL, log line or exception text.

Transport: one persistent keep-alive connection per embedder (``_post``). Documents go
through ``batchEmbedContents`` in chunks of at most ``GEMINI_MAX_BATCH`` (the single
batch constant, defined in ``recall.embeddings``); queries use the single
``embedContent`` call. Every item asks for ``outputDimensionality`` = ``dims`` (768).

Contract note: the request/response shapes follow the v1beta REST surface
(``embedContent`` -> ``embedding.values``; ``batchEmbedContents`` with ``model`` and
``outputDimensionality`` per item -> ``embeddings[].values``). Verified live on
2026-11-02: batchEmbedContents answers 200 with 768-dim vectors; a keep-alive single call
measured p50 539 ms / p95 747 ms.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import random
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memorymaster.core.llm_provider import parse_retry_after
from memorymaster.core.provider_health import record_provider_failure
from memorymaster.core.usage_ledger import (
    EMBED_DOC_OPERATION,
    EMBED_QUERY_OPERATION,
    UsageLedger,
    UsageQuotaExceeded,
    default_ledger_path,
    embed_token_limit,
)
from memorymaster.decisions.egress import prepare_egress
from memorymaster.recall.embeddings import GEMINI_MAX_BATCH, EncoderUnavailable, normalize

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-embedding-2"
DEFAULT_DIMS = 768
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
BASE_URL_ENV = "MEMORYMASTER_EMBED_API_BASE_URL"  # tests only; non-loopback must be https
KEY_ENV = "MEMORYMASTER_EMBED_API_KEY"
TIER_ENV = "MEMORYMASTER_EMBED_API_TIER"
# Bumped whenever a prefix below changes, so cache tags (model:dim:version) invalidate.
TEMPLATE_VERSION = "t1"
QUERY_TEMPLATE = "task: search result | query: {content}"
DOC_TEMPLATE = "title: none | text: {content}"

QUERY_MAX_CHARS = 4_000
DOC_MAX_CHARS = 8_000
QUERY_TIMEOUT_S = 1.1
QUERY_RETRY_WINDOW_S = 0.3
DOC_TIMEOUT_S = 20.0
DOC_ATTEMPTS = 4
DOC_BACKOFF_S = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 60.0
BREAKER_FAILURES = 3
BREAKER_OPEN_S = 60.0
QUERY_RATE_PER_MIN = 100
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_STALE_CONNECTION = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)


class EmbedUnavailable(EncoderUnavailable):
    """The embedding API cannot answer now. ``reason`` is one of REASONS."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"embedding unavailable: {reason}" + (f" ({detail})" if detail else ""))


def chunk(items: Sequence[Any], size: int = GEMINI_MAX_BATCH) -> list[Sequence[Any]]:
    """Split into pieces of at most ``min(size, GEMINI_MAX_BATCH)`` (>= 1) items."""
    size = max(1, min(int(size), GEMINI_MAX_BATCH))
    return [items[i : i + size] for i in range(0, len(items), size)]


def estimate_tokens(text: str) -> int:
    # Conservative (real ratio is ~3.5-4 chars/token): over-counting is the safe side.
    return max(1, math.ceil(len(text) / 3))


@dataclass(frozen=True, slots=True)
class _Prepared:
    """Text that passed ``_egress``; the only thing ``_post`` payloads are built from."""

    text: str


def _egress(content: str, template: str, max_chars: int) -> _Prepared | None:
    """The single choke point: secrets-only redaction, truncation, then the template.

    Returns None when the text is blocked (a credential survived) or empty.
    """
    result = prepare_egress(content, max_chars=max_chars, secrets_only=True)
    if result.blocked or not result.text.strip():
        return None
    return _Prepared(template.format(content=result.text))


def _check_base_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "http" and loopback or parsed.scheme == "https":
        if parsed.hostname:
            return parsed
    raise EmbedUnavailable("api", "base url must be https (or loopback http)")


class GeminiEmbedder:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        dims: int = DEFAULT_DIMS,
        base_url: str | None = None,
        ledger_path: str | Path | None = None,
        batch_size: int = GEMINI_MAX_BATCH,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        query_rate_per_min: int = QUERY_RATE_PER_MIN,
    ) -> None:
        # No fallback to GEMINI_API_KEY or the key rotator file: this traffic uses one
        # dedicated key whose billing tier the operator attests to.
        self._key = (api_key if api_key is not None else os.environ.get(KEY_ENV, "")).strip()
        if not self._key:
            raise EmbedUnavailable("auth", "no embedding api key configured")
        if os.environ.get(TIER_ENV, "").strip().lower() != "paid":
            raise EmbedUnavailable("auth", "paid tier attestation missing")
        self.model = model
        self.dims = int(dims)
        self._url = _check_base_url(base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        self._ledger_path = Path(ledger_path) if ledger_path else default_ledger_path()
        self._ledger: UsageLedger | None = None
        self._batch = max(1, min(int(batch_size), GEMINI_MAX_BATCH))
        self._clock = clock
        self._sleep = sleep
        self._rate_limit = int(query_rate_per_min)
        self._rate_calls: deque[float] = deque()
        self._lock = threading.Lock()  # guards breaker/rate state and the connection
        self._conn: http.client.HTTPConnection | None = None
        self._failures = 0
        self._open_until = 0.0
        self.auth_failed = False
        self.last_usage_tokens: int | None = None  # real count from the API, when present

    def __repr__(self) -> str:  # the default repr is safe, but pin it: never include the key
        return f"GeminiEmbedder(model={self.model!r}, dims={self.dims})"

    @property
    def model_tag(self) -> str:
        return f"{self.model}:{self.dims}:{TEMPLATE_VERSION}"

    @property
    def breaker_open(self) -> bool:
        return self._clock() < self._open_until

    # ---- public API -------------------------------------------------------------

    def embed_query(self, text: str) -> list[float]:
        self._gate()
        prepared = _egress(text, QUERY_TEMPLATE, QUERY_MAX_CHARS)
        if prepared is None:
            raise EmbedUnavailable("blocked", "query withheld by egress filter")
        self._take_rate_slot()
        reservation = self._reserve(EMBED_QUERY_OPERATION, estimate_tokens(prepared.text))
        try:
            data = self._call(
                f"/v1beta/models/{self.model}:embedContent",
                self._item(prepared),
                deadline=self._clock() + QUERY_TIMEOUT_S,
                is_query=True,
            )
            vector = self._vector((data or {}).get("embedding"))
        except EmbedUnavailable:
            self._finish(reservation, "failed")
            raise
        self._finish(reservation, "ok")
        return vector

    def embed_documents(self, texts: Sequence[str]) -> list[list[float] | None]:
        """Embed documents, aligned with ``texts``. An item is None when it was withheld
        by the egress filter or rejected by the API (400 on its own); any other failure
        raises ``EmbedUnavailable`` (the caller abandons the round)."""
        self._gate()  # an open breaker must cost microseconds, not a redaction pass per text
        out: list[list[float] | None] = [None] * len(texts)
        prepared: list[tuple[int, _Prepared]] = []
        for index, text in enumerate(texts):
            item = _egress(text, DOC_TEMPLATE, DOC_MAX_CHARS)
            if item is not None:
                prepared.append((index, item))
        for piece in chunk(prepared, self._batch):
            self._gate()
            reservation = self._reserve(
                EMBED_DOC_OPERATION, sum(estimate_tokens(p.text) for _, p in piece)
            )
            try:
                vectors = self._embed_batch(list(piece))
            except EmbedUnavailable:
                self._finish(reservation, "failed")
                raise
            self._finish(reservation, "ok")
            for (index, _), vector in zip(piece, vectors):
                out[index] = vector
        return out

    # ---- guards -------------------------------------------------------------------

    def _gate(self) -> None:
        if self.auth_failed:
            raise EmbedUnavailable("auth", "api key rejected earlier")
        if self.breaker_open:
            raise EmbedUnavailable("breaker", "circuit open")

    def _take_rate_slot(self) -> None:
        with self._lock:
            now = self._clock()
            while self._rate_calls and now - self._rate_calls[0] >= 60.0:
                self._rate_calls.popleft()
            if len(self._rate_calls) >= self._rate_limit:
                raise EmbedUnavailable("rate", "local query rate limit")
            self._rate_calls.append(now)

    def _reserve(self, operation: str, units: int) -> Any:
        """Fail closed: no ledger, no cap headroom, or a zero budget means no request."""
        limit = embed_token_limit(operation)
        if limit <= 0:  # the ledger treats 0 as unlimited; here it is a zero budget
            raise EmbedUnavailable("capped", "daily budget is zero")
        try:
            if self._ledger is None:
                self._ledger = UsageLedger(self._ledger_path)
            return self._ledger.reserve(
                operation=operation,
                provider="gemini",
                actor="embedder",
                units=units,
                global_limit=limit,
            )
        except UsageQuotaExceeded:
            raise EmbedUnavailable("capped", "daily token cap reached") from None
        except Exception as exc:  # noqa: BLE001 - any ledger problem must block the send
            raise EmbedUnavailable("ledger", type(exc).__name__) from None

    def _finish(self, reservation: Any, outcome: str) -> None:
        try:
            if self._ledger is not None:
                self._ledger.finish(reservation, outcome=outcome)
        except Exception:  # noqa: BLE001 - bookkeeping must not mask the result
            logger.warning("gemini_embed: could not finish ledger reservation")

    def _note_success(self) -> None:
        self._failures = 0

    def _note_failure(self, reason: str, detail: str) -> None:
        self._failures += 1
        if self._failures >= BREAKER_FAILURES:
            self._open_until = self._clock() + BREAKER_OPEN_S
        record_provider_failure("gemini_embed", reason, detail=detail)
        logger.warning("gemini_embed: request failed reason=%s detail=%s", reason, detail)

    # ---- request building and parsing ---------------------------------------------

    def _item(self, prepared: _Prepared) -> dict[str, Any]:
        if not isinstance(prepared, _Prepared):  # the choke point is not optional
            raise TypeError("only egress-prepared text may be sent")
        return {
            "model": f"models/{self.model}",
            "content": {"parts": [{"text": prepared.text}]},
            "outputDimensionality": self.dims,
        }

    def _vector(self, node: Any) -> list[float]:
        values = node.get("values") if isinstance(node, dict) else None
        if not isinstance(values, list) or len(values) < self.dims:
            raise EmbedUnavailable("api", "malformed embedding response")
        try:
            vec = [float(v) for v in values[: self.dims]]  # longer than asked: truncate
        except (TypeError, ValueError):
            raise EmbedUnavailable("api", "malformed embedding response") from None
        if not any(vec):
            raise EmbedUnavailable("api", "zero vector in response")
        return normalize(vec)

    def _record_usage(self, data: dict[str, Any] | None) -> None:
        meta = (data or {}).get("usageMetadata")
        if isinstance(meta, dict):
            for key in ("totalTokenCount", "promptTokenCount"):
                if isinstance(meta.get(key), int):
                    self.last_usage_tokens = meta[key]
                    return

    def _embed_batch(self, piece: list[tuple[int, _Prepared]]) -> list[list[float] | None]:
        """One chunk; a 400 bisects so a single poison text cannot block the rest."""
        if not 0 < len(piece) <= GEMINI_MAX_BATCH:
            raise ValueError("batch size out of range")
        payload = {"requests": [self._item(p) for _, p in piece]}
        status, data = self._call_status(
            f"/v1beta/models/{self.model}:batchEmbedContents", payload, is_query=False
        )
        if status == 400:
            if len(piece) == 1:
                logger.warning("gemini_embed: one document rejected by the api (400)")
                return [None]
            mid = len(piece) // 2
            return self._embed_batch(piece[:mid]) + self._embed_batch(piece[mid:])
        embeddings = (data or {}).get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(piece):
            raise EmbedUnavailable("api", "embedding count mismatch")
        self._record_usage(data)
        return [self._vector(e) for e in embeddings]

    # ---- transport ------------------------------------------------------------------

    def _call(self, path: str, item: dict[str, Any], *, deadline: float, is_query: bool) -> Any:
        status, data = self._call_status(path, item, is_query=is_query, deadline=deadline)
        if status == 400:  # a rejected query is not an outage: no breaker count
            raise EmbedUnavailable("api", "query rejected (400)")
        self._record_usage(data)
        return data

    def _call_status(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        is_query: bool,
        deadline: float | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        """Send with the retry policy. Returns (200|400, parsed body); raises otherwise."""
        attempts = 2 if is_query else DOC_ATTEMPTS
        started = self._clock()
        reason, detail = "api", "no attempt"
        for attempt in range(attempts):
            self._gate()
            timeout = DOC_TIMEOUT_S
            if deadline is not None:
                timeout = deadline - self._clock()
                if timeout <= 0:
                    reason, detail = "api", "query deadline"
                    break
            try:
                status, data, retry_after = self._post(path, payload, timeout)
            except TimeoutError:
                reason, detail = "api", "timeout"
            except (OSError, http.client.HTTPException) as exc:
                reason, detail = "api", type(exc).__name__
            else:
                if status == 200 and data is not None:
                    self._note_success()
                    return 200, data
                if status == 200:  # unparseable body: treated like a transient 5xx
                    status = 500
                if status == 400:
                    return 400, None
                if status in (401, 403):
                    self.auth_failed = True
                    record_provider_failure("gemini_embed", f"http_{status}", detail=f"HTTP {status}")
                    raise EmbedUnavailable("auth", f"http {status}")
                reason = "rate" if status == 429 else "api"
                detail = f"http {status}" if data is not None or status != 500 else "malformed body"
                if status not in _RETRYABLE:
                    break
                if is_query:
                    # Never sleep on a 429; one quick retry only for a fast 5xx.
                    if status == 429 or self._clock() - started > QUERY_RETRY_WINDOW_S:
                        break
                    continue
                if attempt + 1 < attempts:
                    self._sleep(self._backoff(attempt, retry_after))
                continue
            if is_query and self._clock() - started > QUERY_RETRY_WINDOW_S:
                break
            if not is_query and attempt + 1 < attempts:
                self._sleep(self._backoff(attempt, None))
        self._note_failure(reason, detail)
        raise EmbedUnavailable(reason, detail)

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), MAX_RETRY_AFTER_S)
        base = DOC_BACKOFF_S[min(attempt, len(DOC_BACKOFF_S) - 1)]
        return base + random.uniform(0.0, base / 4)  # noqa: S311 - jitter, not security

    def _post(
        self, path: str, payload: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any] | None, float | None]:
        """The only function that talks to the network (keep-alive connection reused)."""
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._key}
        with self._lock:
            for fresh in (False, True):
                reused = self._conn is not None and not fresh
                if reused and self._conn.sock is not None:
                    self._conn.sock.settimeout(timeout)
                elif not reused:
                    self._close()
                    self._open(timeout)
                try:
                    self._conn.request("POST", self._url.path.rstrip("/") + path, body, headers)
                    resp = self._conn.getresponse()
                    raw = resp.read(8 * 1024 * 1024)
                    retry_after = parse_retry_after(
                        raw.decode("utf-8", errors="replace") if resp.status == 429 else "",
                        {"Retry-After": resp.getheader("Retry-After")},
                    )
                    if resp.will_close:
                        self._close()
                    break
                except _STALE_CONNECTION:
                    self._close()
                    if not reused:  # an idle keep-alive died: one retry on a fresh socket
                        raise
                except BaseException:
                    self._close()
                    raise
        data: dict[str, Any] | None = None
        if resp.status == 200:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
            data = parsed if isinstance(parsed, dict) else None
        return resp.status, data, retry_after

    def _open(self, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._url.scheme == "https" else http.client.HTTPConnection
        self._conn = cls(self._url.hostname, self._url.port, timeout=timeout)
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close()
