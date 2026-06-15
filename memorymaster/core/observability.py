from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
import time

CounterKey = tuple[str, tuple[tuple[str, str], ...]]

_HISTOGRAM_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_COUNTERS: defaultdict[CounterKey, int] = defaultdict(int)
_HISTOGRAMS: dict[str, list[float]] = defaultdict(list)
_LOCK = Lock()


def _normalize_label_value(value: str | None, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text or default


def _labels(**labels: str | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, _normalize_label_value(value)) for key, value in labels.items()))


def _counter_key(name: str, **labels: str | None) -> CounterKey:
    return name, _labels(**labels)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    formatted = ",".join(f'{key}="{_escape_label(value)}"' for key, value in labels)
    return f"{{{formatted}}}"


def _counter_line(name: str, labels: tuple[tuple[str, str], ...], value: int | float) -> str:
    if isinstance(value, float) and not value.is_integer():
        rendered = f"{value:.6f}".rstrip("0").rstrip(".")
    else:
        rendered = str(int(value))
    return f"{name}{_format_labels(labels)} {rendered}"


def bump_counter(name: str, amount: int = 1, **labels: str | None) -> None:
    if amount < 0:
        raise ValueError("Counter increments must be non-negative.")
    with _LOCK:
        _COUNTERS[_counter_key(name, **labels)] += amount


def bump_claim_ingested(source_agent: str | None) -> None:
    bump_counter("claims_ingested_total", source_agent=source_agent)


def bump_claim_filtered(reason: str | None) -> None:
    bump_counter("claims_filtered_total", reason=_filter_reason(reason))


def bump_claim_filtered_findings(findings: list[str] | tuple[str, ...]) -> None:
    for finding in findings:
        bump_claim_filtered(finding)


def bump_compactor_run(status: str | None) -> None:
    bump_counter("compactor_run_total", status=status)


def bump_decay_run(status: str | None) -> None:
    bump_counter("decay_run_total", status=status)


def observe_steward_cycle_duration(seconds: float) -> None:
    if seconds < 0:
        raise ValueError("Duration samples must be non-negative.")
    with _LOCK:
        _HISTOGRAMS["steward_cycle_duration_seconds"].append(float(seconds))


@contextmanager
def steward_cycle_timer() -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        observe_steward_cycle_duration(time.monotonic() - started)


def metric_value(name: str, **labels: str | None) -> int | float:
    with _LOCK:
        if name == "steward_cycle_duration_seconds_count":
            return len(_HISTOGRAMS["steward_cycle_duration_seconds"])
        if name == "steward_cycle_duration_seconds_sum":
            return sum(_HISTOGRAMS["steward_cycle_duration_seconds"])
        return _COUNTERS.get(_counter_key(name, **labels), 0)


def reset_metrics() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _HISTOGRAMS.clear()


def _filter_reason(finding: str | None) -> str:
    text = _normalize_label_value(finding)
    if text == "jwt_token":
        return "jwt"
    if "ip" in text:
        return "ip"
    if text in {"openai_key", "anthropic_key", "google_api_key", "aws_access_key", "aws_sts_key", "stripe_key"}:
        return "api_key"
    if "api_key" in text or text.endswith("_key"):
        return "api_key"
    if "password" in text or "credential" in text:
        return "password"
    if "token" in text:
        return "token"
    return text


def _counter_family_lines(name: str, help_text: str) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
    with _LOCK:
        rows = [
            (labels, count)
            for (counter_name, labels), count in _COUNTERS.items()
            if counter_name == name
        ]
    for labels, count in sorted(rows):
        lines.append(_counter_line(name, labels, count))
    return lines


def _steward_histogram_lines() -> list[str]:
    name = "steward_cycle_duration_seconds"
    with _LOCK:
        samples = list(_HISTOGRAMS[name])

    lines = [
        f"# HELP {name} Steward cycle wall-clock duration in seconds.",
        f"# TYPE {name} histogram",
    ]
    for bucket in _HISTOGRAM_BUCKETS:
        count = sum(1 for sample in samples if sample <= bucket)
        lines.append(_counter_line(f"{name}_bucket", (("le", str(bucket)),), count))
    lines.append(_counter_line(f"{name}_bucket", (("le", "+Inf"),), len(samples)))
    lines.append(_counter_line(f"{name}_count", (), len(samples)))
    lines.append(_counter_line(f"{name}_sum", (), sum(samples)))
    return lines


def metrics_text() -> str:
    lines: list[str] = []
    lines.extend(
        _counter_family_lines(
            "claims_ingested_total",
            "Claims successfully ingested grouped by source agent.",
        )
    )
    lines.extend(
        _counter_family_lines(
            "claims_filtered_total",
            "Claims rejected or redacted by the sensitivity filter grouped by reason.",
        )
    )
    lines.extend(_steward_histogram_lines())
    lines.extend(
        _counter_family_lines(
            "compactor_run_total",
            "Compactor runs grouped by status.",
        )
    )
    lines.extend(
        _counter_family_lines(
            "decay_run_total",
            "Decay runs grouped by status.",
        )
    )
    return "\n".join(lines) + "\n"
