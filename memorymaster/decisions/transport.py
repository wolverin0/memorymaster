"""In-process TypeSafe System One transport on the standard library (ruling R6).

One pinned endpoint and model over ``http.client`` with
``ssl.create_default_context()`` (the operating system's trust store; certificate
and hostname verification on).  If a handshake fails certificate verification and
``certifi`` is importable, that connection is retried once with a certifi
context; whichever context completed a handshake is cached for the process.
``http.client`` never reads proxy variables or ``.netrc`` and never follows
redirects (a 3xx is the ``redirect_refused`` outcome), so nothing can re-route the
bearer key, which travels only in the ``Authorization`` header.  httpx is never
imported (a cold hook paid over a second for it).

Responses are read in bounded pieces and capped at 128 KiB; every socket
operation's timeout is the time left before the deadline and the deadline is
checked between reads, so a slow-drip body cannot extend it.  Hooks call with
``max_retries=0``; batch jobs may back off on 429/529 up to three retries inside
their deadline (honouring ``Retry-After``).

Keep-alive: idle connections are pooled and each request checks one out for its
exclusive use (the engine sends every request from its own deadline thread, so a
per-thread connection would never be reused); only a connection that completed a
200 response goes back, one the server dropped while idle or that sat idle longer
than ``IDLE_EXPIRY_S`` (a NAT or firewall may drop it without a FIN) is discarded,
and any failure closes the connection so the next request reconnects.

Every failure becomes a :class:`TransportResult` outcome; exceptions never leave
this module and ``error_class`` is only the exception's type name, never its text.
"""
from __future__ import annotations

import http.client
import json
import math
import select
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.questions import AnswerSchema

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
TRANSPORT_VERSION = "stdlib-inproc/2"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 192 * 1024
MAX_BATCH_RETRIES = 3
BACKOFF_BASE_S = 0.5
READ_CHUNK_BYTES = 16 * 1024
MAX_IDLE_CONNECTIONS = 8
IDLE_EXPIRY_S = 15.0
SUM_TOLERANCE = 0.01
# Live probabilities are rounded to 2 decimals, so each option may be off by 0.005.
ROUNDING_PER_OPTION = 0.005

# Versioned price table (USD per token).  Output is free for jev-1.13.0.
PRICE_TABLE_VERSION = "typesafe-jev-1.13.0/2026-09-23"
PRICE_PER_INPUT_TOKEN_USD = 0.042e-6
PRICE_PER_OUTPUT_TOKEN_USD = 0.0

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# Outcomes where the provider may have processed (and billed) the request.
_POSSIBLY_BILLED = frozenset({"ok", "timeout", "too_large", "malformed"})


class MalformedResponse(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedAnswer:
    primitive: str
    value: float | str
    probabilities: dict[str, float]
    confidence: float | None = None
    label: str | None = None


@dataclass(frozen=True)
class ValidatedResponse:
    answers: dict[str, ParsedAnswer]
    model_served: str
    tokens_in: int
    tokens_out: int
    usage_reported: bool


@dataclass(frozen=True)
class TransportResult:
    status: int | None
    body: Mapping[str, Any] | None
    latency_ms: int
    attempt_count: int
    outcome: str
    model_served: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    answers: Mapping[str, ParsedAnswer] | None = None
    error_class: str | None = None
    tokens_estimated: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    return tokens_in * PRICE_PER_INPUT_TOKEN_USD + tokens_out * PRICE_PER_OUTPUT_TOKEN_USD


def estimate_tokens(payload_bytes: int) -> int:
    return max(1, math.ceil(payload_bytes / 4))


def transport_version() -> str:
    """Ledger ``sdk_version``: the HTTP stack is the interpreter's own ``http.client``."""
    return f"stdlib-http.client/py{sys.version_info[0]}.{sys.version_info[1]}"


def _import_ssl() -> Any:
    try:
        import ssl
    except ImportError:  # an interpreter built without OpenSSL cannot reach the https endpoint
        return None
    return ssl


def _system_tls_context(ssl: Any) -> Any:
    """The OS trust store, verification and hostname checks on (``create_default_context``)."""
    return ssl.create_default_context()


def _certifi_tls_context(ssl: Any) -> Any:
    """Mozilla's bundle, only when certifi is importable (``None`` otherwise)."""
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


_tls_lock = threading.Lock()
_tls_context: Any = None  # the context that completed a handshake in this process


def _cached_tls_context() -> Any:
    with _tls_lock:
        return _tls_context


def _remember_tls_context(context: Any) -> None:
    global _tls_context
    with _tls_lock:
        if _tls_context is None:
            _tls_context = context


def _reset_tls_context() -> None:
    """Tests only: forget the cached context."""
    global _tls_context
    with _tls_lock:
        _tls_context = None


def _close_quietly(conn: Any) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _idle_connection_usable(conn: Any) -> bool:
    """False when the server closed (or wrote to) an idle keep-alive connection."""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


class _Deadline(TimeoutError):
    """The deadline passed between socket operations."""


def _validate_endpoint(endpoint: str) -> str:
    if endpoint == ENDPOINT:
        return endpoint
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme in {"http", "https"} and parts.hostname in _LOOPBACK and not parts.username:
        return endpoint  # local test servers only
    raise ValueError("endpoint is pinned to the TypeSafe System One API")


def _unit(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def sum_tolerance(option_count: int) -> float:
    """How far a rounded distribution may sum from 1: ``max(0.01, 0.005 * options)``."""
    return max(SUM_TOLERANCE, ROUNDING_PER_OPTION * max(int(option_count), 0))


def _distribution(raw: object, keys: tuple[str, ...], what: str) -> dict[str, float]:
    if not isinstance(raw, Mapping) or set(raw) != set(keys):
        raise MalformedResponse(f"{what}_options")
    probabilities: dict[str, float] = {}
    for key in keys:
        value = _unit(raw[key])
        if value is None:
            raise MalformedResponse(f"{what}_probability")
        probabilities[key] = value
    if abs(sum(probabilities.values()) - 1.0) > sum_tolerance(len(keys)):
        raise MalformedResponse(f"{what}_sum")
    return probabilities


def _optional_confidence(answer: Mapping[str, Any]) -> float | None:
    if "confidence" not in answer or answer["confidence"] is None:
        return None
    value = _unit(answer["confidence"])
    if value is None:
        raise MalformedResponse("confidence")
    return value


def _parse_answer(answer: object, schema: AnswerSchema) -> ParsedAnswer:
    if not isinstance(answer, Mapping) or answer.get("type") != schema.primitive:
        raise MalformedResponse("answer_type")
    if schema.primitive == "noul":
        value = _unit(answer.get("noul"))
        if value is None:
            raise MalformedResponse("noul_value")
        return ParsedAnswer("noul", value, {"noul": value})
    if schema.primitive == "choice":
        choice = answer.get("choice")
        probabilities = _distribution(answer.get("probabilities"), schema.options, "choice")
        if not isinstance(choice, str) or choice not in probabilities:
            raise MalformedResponse("choice_value")
        return ParsedAnswer("choice", choice, probabilities, _optional_confidence(answer), choice)
    if schema.primitive == "score":
        # Live jev-1.13 keys probabilities (and its ``legend``) by the 0-based index of
        # each criterion as sent; index i is the spec's i-th level.
        indexes = tuple(str(index) for index in range(len(schema.levels)))
        by_index = _distribution(answer.get("probabilities"), indexes, "score")
        legend = answer.get("legend")
        if isinstance(legend, Mapping) and set(legend) != set(indexes):
            raise MalformedResponse("score_legend")
        if "score" in answer and answer["score"] is not None:
            raw_score = answer["score"]
            if type(raw_score) not in (int, float) or not math.isfinite(float(raw_score)):
                raise MalformedResponse("score_value")
        probabilities = {str(level): by_index[index] for index, level in zip(indexes, schema.levels)}
        low, high = min(schema.levels), max(schema.levels)
        span = (high - low) or 1
        expected = sum(probabilities[str(level)] * (level - low) / span for level in schema.levels)
        label = max(probabilities, key=lambda key: probabilities[key])
        return ParsedAnswer("score", min(max(expected, 0.0), 1.0), probabilities, _optional_confidence(answer), label)
    raise MalformedResponse("primitive")


def validate_response(body: object, expected: Mapping[str, AnswerSchema]) -> ValidatedResponse:
    """Validate a decoded System One body against the questions that were asked."""
    if not isinstance(body, Mapping):
        raise MalformedResponse("not_object")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise MalformedResponse("model")
    answers = body.get("answers")
    if not isinstance(answers, Mapping):
        raise MalformedResponse("answers")
    missing = set(expected) - set(answers)
    if missing:
        raise MalformedResponse("unanswered")
    if set(answers) - set(expected):
        raise MalformedResponse("unexpected_answer")
    parsed = {wire_id: _parse_answer(answers[wire_id], schema) for wire_id, schema in expected.items()}
    usage = body.get("usage", {})
    if usage is None:
        usage = {}
    if not isinstance(usage, Mapping):
        raise MalformedResponse("usage")
    counts: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if value is None:
            counts[name] = 0
        elif type(value) is not int or value < 0:
            raise MalformedResponse("usage")
        else:
            counts[name] = value
    return ValidatedResponse(parsed, model, counts["input_tokens"], counts["output_tokens"],
                             usage_reported="input_tokens" in usage)


def _status_outcome(status: int) -> str:
    if status in (401, 403, 422, 429, 529):
        return f"http_{status}"
    if 300 <= status < 400:
        return "redirect_refused"
    if status >= 500:
        return "http_5xx"
    return "http_4xx"


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("retry-after")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and value >= 0 else None


class HttpTransport:
    """Pinned-endpoint System One client; construct once per process or hook run."""

    def __init__(
        self,
        api_key: ApiKey,
        *,
        endpoint: str = ENDPOINT,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(api_key, ApiKey):
            raise TypeError("api_key must be an ApiKey")
        self._key = api_key
        self.endpoint = _validate_endpoint(endpoint)
        parts = urllib.parse.urlsplit(self.endpoint)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port or (443 if self._https else 80)
        self._target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self._sleep = sleep
        self._clock = clock
        self._idle: list[tuple[Any, float]] = []  # (connection, clock when it went idle)
        self._idle_lock = threading.Lock()

    def close(self) -> None:
        with self._idle_lock:
            idle, self._idle = self._idle, []
        for conn, _ in idle:
            _close_quietly(conn)

    # --------------------------------------------------------- connections ---
    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _Deadline("deadline")
        return remaining

    def _connect(self, context: Any, deadline: float) -> Any:
        if context is None:
            conn: Any = http.client.HTTPConnection(self._host, self._port, timeout=self._remaining(deadline))
        else:
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self._remaining(deadline),
                                               context=context)
        try:
            conn.connect()  # TCP and, for https, the verified TLS handshake
        except BaseException:
            _close_quietly(conn)
            raise
        return conn

    def _open(self, ssl: Any, deadline: float) -> Any:
        if not self._https:
            return self._connect(None, deadline)
        cached = _cached_tls_context()
        if cached is not None:
            return self._connect(cached, deadline)
        system = _system_tls_context(ssl)
        try:
            conn = self._connect(system, deadline)
        except ssl.SSLCertVerificationError:
            fallback = _certifi_tls_context(ssl)
            if fallback is None:
                raise
            conn = self._connect(fallback, deadline)  # once; a second failure is the outcome
            _remember_tls_context(fallback)
            return conn
        _remember_tls_context(system)
        return conn

    def _checkout(self, ssl: Any, deadline: float) -> Any:
        while True:
            with self._idle_lock:
                conn, idle_since = self._idle.pop() if self._idle else (None, 0.0)
            if conn is None:
                return self._open(ssl, deadline)
            if self._clock() - idle_since <= IDLE_EXPIRY_S and _idle_connection_usable(conn):
                return conn
            _close_quietly(conn)

    def _checkin(self, conn: Any, response: Any) -> None:
        if response.will_close or getattr(conn, "sock", None) is None:
            _close_quietly(conn)
            return
        with self._idle_lock:
            if len(self._idle) < MAX_IDLE_CONNECTIONS:
                self._idle.append((conn, self._clock()))
                return
        _close_quietly(conn)

    def _set_timeout(self, conn: Any, deadline: float) -> None:
        sock = getattr(conn, "sock", None)
        if sock is None:
            raise ConnectionError("connection closed")
        sock.settimeout(self._remaining(deadline))

    def _attempt(self, ssl: Any, body: bytes, deadline: float) -> tuple[int | None, bytes | None, str, str | None, Any]:
        if deadline - self._clock() <= 0:
            return None, None, "timeout", "deadline", None
        headers = {
            **self._key.bearer_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"memorymaster-decisions/{TRANSPORT_VERSION}",
        }
        conn: Any = None
        status: int | None = None
        response_headers: Any = None
        try:
            conn = self._checkout(ssl, deadline)
            self._set_timeout(conn, deadline)
            conn.request("POST", self._target, body=body, headers=headers)
            self._set_timeout(conn, deadline)
            response = conn.getresponse()
            status, response_headers = response.status, response.headers
            if status != 200:
                return status, None, _status_outcome(status), None, response_headers
            declared = response.getheader("content-length")
            if declared is not None and declared.strip().isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                return status, None, "too_large", None, response_headers
            buffer = bytearray()
            while True:
                self._set_timeout(conn, deadline)
                chunk = response.read1(READ_CHUNK_BYTES)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_RESPONSE_BYTES:
                    return status, None, "too_large", None, response_headers
            if response.length:  # read1 ends quietly when the peer closes before Content-Length
                raise http.client.IncompleteRead(b"", response.length)
            self._checkin(conn, response)
            conn = None
            return status, bytes(buffer), "ok", None, response_headers
        except _Deadline:
            return status, None, "timeout", "deadline", response_headers
        except TimeoutError as exc:  # socket.timeout is TimeoutError
            return status, None, "timeout", type(exc).__name__, response_headers
        except Exception as exc:  # never propagate: text could echo request details
            return status, None, "network_error", type(exc).__name__, response_headers
        finally:
            _close_quietly(conn)  # anything but a completed 200 never goes back to the pool

    def send(
        self,
        payload: Mapping[str, Any],
        *,
        expected: Mapping[str, AnswerSchema],
        timeout_s: float,
        max_retries: int = 0,
    ) -> TransportResult:
        started = self._clock()
        ssl = _import_ssl() if self._https else None
        if self._https and ssl is None:
            return TransportResult(None, None, 0, 0, "transport_unavailable", error_class="ImportError")
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return TransportResult(None, None, 0, 0, "request_invalid", error_class=type(exc).__name__)
        if len(body) > MAX_REQUEST_BYTES:
            return TransportResult(None, None, 0, 0, "request_too_large")
        timeout = timeout_s if math.isfinite(timeout_s) and timeout_s > 0 else 0.0
        deadline = started + timeout
        retries = max(0, min(int(max_retries), MAX_BATCH_RETRIES))
        attempts = 0
        while True:
            attempts += 1
            status, raw, outcome, error_class, headers = self._attempt(ssl, body, deadline)
            if outcome not in {"http_429", "http_529"} or attempts > retries:
                break
            delay = BACKOFF_BASE_S * (2 ** (attempts - 1))
            hinted = _retry_after(headers)
            if hinted is not None:
                delay = max(delay, hinted)
            if self._clock() + delay >= deadline:
                break
            self._sleep(delay)
        latency_ms = int(round((self._clock() - started) * 1000))
        estimated = estimate_tokens(len(body)) if outcome in _POSSIBLY_BILLED else 0

        def result(**kwargs: Any) -> TransportResult:
            base: dict[str, Any] = dict(status=status, body=None, latency_ms=latency_ms, attempt_count=attempts,
                                        outcome=outcome, error_class=error_class, tokens_in=estimated,
                                        cost_usd=cost_usd(estimated, 0), tokens_estimated=bool(estimated))
            base.update(kwargs)
            return TransportResult(**base)

        if outcome != "ok" or raw is None:
            return result()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return result(outcome="malformed", error_class=f"malformed:json:{type(exc).__name__}")
        model_served = decoded.get("model") if isinstance(decoded, Mapping) else None
        model_served = model_served if isinstance(model_served, str) and model_served.strip() else None
        try:
            validated = validate_response(decoded, expected)
        except MalformedResponse as exc:
            return result(outcome="malformed", error_class=f"malformed:{exc.reason}", model_served=model_served)
        tokens_in = validated.tokens_in if validated.usage_reported else estimated
        return TransportResult(
            status=status,
            body=decoded,
            latency_ms=latency_ms,
            attempt_count=attempts,
            outcome="ok",
            model_served=validated.model_served,
            tokens_in=tokens_in,
            tokens_out=validated.tokens_out,
            cost_usd=cost_usd(tokens_in, validated.tokens_out),
            answers=validated.answers,
            tokens_estimated=not validated.usage_reported,
        )


#: Pre-R6 name, kept so existing callers and test doubles keep working.
HttpxTransport = HttpTransport
_STDLIB_TRANSPORT = HttpTransport


def transport_class() -> type:
    """The class the engine builds: ``HttpTransport``, or whichever name a caller replaced.

    Tests and embedders replace ``HttpxTransport`` (the pre-R6 name) or
    ``HttpTransport``; a replacement of either is honoured so no double is bypassed.
    """
    module = sys.modules[__name__]
    current = getattr(module, "HttpTransport")
    if current is _STDLIB_TRANSPORT:
        return getattr(module, "HttpxTransport")
    return current


__all__ = [
    "ENDPOINT",
    "HttpTransport",
    "HttpxTransport",
    "MAX_RESPONSE_BYTES",
    "MODEL",
    "MalformedResponse",
    "PRICE_TABLE_VERSION",
    "ParsedAnswer",
    "TRANSPORT_VERSION",
    "TransportResult",
    "cost_usd",
    "transport_class",
    "transport_version",
    "validate_response",
]
