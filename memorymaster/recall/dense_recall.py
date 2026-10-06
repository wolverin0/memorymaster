"""Prompt-hook client for the dense-recall service, plus its per-prompt metrics log.

Gated by ``MEMORYMASTER_RECALL_DENSE=1`` (default off). Only the prompt hook
uses it; ``recall()`` callers without hook data keep lexical retrieval.

Policy, measured 2026-10-06 on 200 real prompts with graded judgments
(artifacts/2026-10-06-embeddinggemma2-benchmark.html):

* Dense candidates replace the lexical pool. Fusing lexical rows back in added
  noise (top-5 noise 0.70 vs 0.61) for no nDCG gain (0.341 vs 0.337).
* Only candidates with cosine >= 0.68 are injected, at most 6. That keeps 92%
  of the useful claims while injecting 3.9 claims per prompt instead of 5.9.
* When nothing passes, nothing is injected: on those prompts the lexical hook
  injected 134 claims of which 6 were useful.
* When the service cannot answer, the hook falls back to lexical recall.

The metrics log holds one JSON line per prompt recall: outcome, latencies,
scores and the injected ids and size. It never stores the prompt text.

    python -m memorymaster.recall.dense_recall report --days 7
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ENABLE_ENV = "MEMORYMASTER_RECALL_DENSE"
URL_ENV = "MEMORYMASTER_RECALL_DENSE_URL"
TIMEOUT_ENV = "MEMORYMASTER_RECALL_DENSE_TIMEOUT_S"
MIN_SCORE_ENV = "MEMORYMASTER_RECALL_DENSE_MIN_SCORE"
MAX_CLAIMS_ENV = "MEMORYMASTER_RECALL_DENSE_MAX_CLAIMS"
LOG_ENV = "MEMORYMASTER_RECALL_DENSE_LOG"

DEFAULT_URL = "http://127.0.0.1:8767"
DEFAULT_TIMEOUT_S = 1.5
DEFAULT_MIN_SCORE = 0.68
DEFAULT_MAX_CLAIMS = 6
# Ask for more than we inject: scope/visibility filtering happens after the
# service answers, and the log keeps the scores just under the threshold.
FETCH_K = 12
LOG_MAX_BYTES = 20 * 1024 * 1024


class DenseUnavailable(RuntimeError):
    """The service did not give a usable answer; the caller falls back to lexical recall."""


@dataclass(frozen=True)
class DenseAnswer:
    results: tuple[tuple[int, float], ...]
    embed_ms: float
    search_ms: float
    indexed: int
    model: str


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def min_score() -> float:
    return _float_env(MIN_SCORE_ENV, DEFAULT_MIN_SCORE)


def max_claims() -> int:
    return max(1, int(_float_env(MAX_CLAIMS_ENV, DEFAULT_MAX_CLAIMS)))


def search(query: str, scopes: Sequence[str] | None, *, k: int = FETCH_K) -> DenseAnswer:
    """Candidate ids by cosine, best first. Raises :class:`DenseUnavailable` on any failure."""
    url = os.environ.get(URL_ENV, DEFAULT_URL).rstrip("/") + "/search"
    body = json.dumps({"query": query, "scopes": list(scopes) if scopes else None, "k": k}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_float_env(TIMEOUT_ENV, DEFAULT_TIMEOUT_S)) as response:
            payload = json.loads(response.read())
        results = tuple((int(item["id"]), float(item["score"])) for item in payload["results"])
        return DenseAnswer(results, float(payload.get("embed_ms") or 0.0), float(payload.get("search_ms") or 0.0),
                           int(payload.get("indexed") or 0), str(payload.get("model") or ""))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        raise DenseUnavailable(type(exc).__name__) from exc


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def log_path() -> Path:
    raw = os.environ.get(LOG_ENV)
    return Path(raw) if raw else Path.home() / ".memorymaster" / "metrics" / "recall-dense.jsonl"


def query_fingerprint(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]


def record(event: dict[str, Any]) -> None:
    """Append one metrics line; never raises (observation only)."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 — metrics must never break recall
        pass


def read_events(path: Path, *, since: datetime | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for candidate in (path.with_suffix(path.suffix + ".1"), path):
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if since is not None and datetime.fromisoformat(event["ts"]) < since:
                continue
            events.append(event)
    return events


def _pct(values: Iterable[float], q: float) -> float | None:
    data = sorted(values)
    if not data:
        return None
    return round(data[min(len(data) - 1, int(q * len(data)))], 1)


def summarize(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Outcome rates, latency, injected volume and score distribution of the logged recalls."""
    outcomes = Counter(str(e.get("outcome")) for e in events)
    dense = [e for e in events if e.get("outcome") in {"dense", "dense_empty"}]
    injected = [int(e.get("injected") or 0) for e in dense]
    chars = [int(e.get("chars") or 0) for e in dense]
    top = [float(e["top_score"]) for e in dense if e.get("top_score") is not None]
    return {
        "recalls": len(events),
        "outcomes": dict(outcomes),
        "fallback_rate": round(sum(n for o, n in outcomes.items() if o.startswith("fallback")) / len(events), 3)
        if events else None,
        "empty_rate": round(outcomes.get("dense_empty", 0) / len(dense), 3) if dense else None,
        "service_ms_p50": _pct((float(e.get("service_ms") or 0) for e in dense), 0.5),
        "service_ms_p95": _pct((float(e.get("service_ms") or 0) for e in dense), 0.95),
        "total_ms_p50": _pct((float(e.get("total_ms") or 0) for e in events), 0.5),
        "total_ms_p95": _pct((float(e.get("total_ms") or 0) for e in events), 0.95),
        "injected_mean": round(statistics.mean(injected), 2) if injected else None,
        "chars_mean": round(statistics.mean(chars)) if chars else None,
        "top_score_p50": _pct(top, 0.5),
        "filtered_out_mean": round(statistics.mean(int(e.get("filtered_out") or 0) for e in dense), 2)
        if dense else None,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Dense prompt-recall metrics")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="summarize the metrics log")
    report.add_argument("--days", type=float, default=7.0)
    report.add_argument("--log", default=None)
    args = parser.parse_args(argv)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    path = Path(args.log) if args.log else log_path()
    print(json.dumps({"log": str(path), "since": since.isoformat(timespec="seconds"),
                      **summarize(read_events(path, since=since))}, indent=2))


if __name__ == "__main__":
    main()
